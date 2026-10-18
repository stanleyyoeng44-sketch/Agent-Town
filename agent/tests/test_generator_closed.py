"""The session generator must be CLOSED by cleanup on every exit, including
an exception raised in the message-handling body.

THE INVARIANT: whatever way a life ends, the async generator returned by
client.receive_messages() has been closed before live_one_life returns.
Stated as an invariant it stays true when someone adds an exit; stated as a
list of exits it goes stale.

THE DEFECT, measured on master 1d67f79 (no aclose() in cleanup):
    life ends by an exception in the message body
    -> nxt was already None, so cancelling tasks touches nothing
    -> the generator stays OPEN, still open after 0.3s, still open after
       gc.collect()

Why cancelling the pending task is not enough: it closes the generator only as
a side effect, and only when a task is actually pending. The in-body exception
path has no pending task, so nothing closes it. aclose() in cleanup covers
every exit.

HOW THIS TEST AVOIDS THE VACUOUS PASS THAT FOOLED AN EARLIER VERSION: the
exception is raised from INSIDE the message-handling body of the real
live_one_life, on the second ResultMessage, and the test asserts the generator
state AFTER the life has returned and after a short settle -- not by trusting
a flag that refcounting might flip.

Run:  python3 agent/tests/test_generator_closed.py
Exit: 0 closed on every exit, 1 an exit left it open.
"""
import asyncio
import importlib.util
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_DIR = os.path.dirname(HERE)
SRC = os.path.join(AGENT_DIR, "agent-daemon.py")


def load_daemon(work):
    spec = importlib.util.spec_from_file_location("daemon_gc", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.WORKSPACE = work
    mod.SELF_DIR = os.path.join(work, "self")
    mod.WAKE_FILE = os.path.join(mod.SELF_DIR, "wake.md")
    mod.DIARY_FILE = os.path.join(mod.SELF_DIR, "diary.md")
    mod.SESSION_FILE = os.path.join(mod.SELF_DIR, "session-id")
    mod.TRANSCRIPTS = os.path.join(work, "transcripts")
    if hasattr(mod, "CURRENT_LIFE_FILE"):
        mod.CURRENT_LIFE_FILE = os.path.join(mod.TRANSCRIPTS, "CURRENT")
    mod.INBOX = os.path.join(work, "inbox")
    mod.REPLIES = os.path.join(work, "replies")
    mod.RESTART_FLAG = os.path.join(work, "restart-requested")
    mod.MY_INBOX = os.path.join(work, "mailbox", "incoming")
    for d in (mod.SELF_DIR, mod.TRANSCRIPTS, mod.INBOX, mod.REPLIES,
              mod.MY_INBOX):
        os.makedirs(d, exist_ok=True)
    return mod


async def _noop_hook(*a, **k):
    return {}


async def run(exit_kind):
    import claude_agent_sdk as sdk
    work = tempfile.mkdtemp()
    mod = load_daemon(work)
    state = {"closed": False}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def query(self, stream):
            async def drain():
                try:
                    async for _ in stream:
                        break
                except Exception:
                    pass
            self._t = asyncio.create_task(drain())
            return None

        def receive_messages(self):
            async def gen():
                try:
                    for _ in range(6):
                        yield sdk.ResultMessage(
                            subtype="success", duration_ms=1,
                            duration_api_ms=1, is_error=False, num_turns=1,
                            session_id="s", total_cost_usd=0.0, usage={})
                        await asyncio.sleep(0.1)
                finally:
                    state["closed"] = True
            return gen()

        def receive_response(self):
            return self.receive_messages()

        async def get_context_usage(self):
            return {"totalTokens": 0}

    mod.ClaudeSDKClient = FakeClient
    mod.build_options = lambda cfg, *a: types.SimpleNamespace(
        resume=None, model=None)
    mod.deliver_pending_hook = _noop_hook
    if hasattr(mod, "log_idle_proof"):
        mod.log_idle_proof = lambda *a, **k: None

    if exit_kind == "raise-in-body":
        real_log = mod.LifeLog.log
        seen = {"n": 0}

        def boom(self, kind, **kw):
            if kind == "result":
                seen["n"] += 1
                if seen["n"] == 2:
                    raise RuntimeError("exception inside the message body")
            return real_log(self, kind, **kw)

        mod.LifeLog.log = boom

    q = asyncio.Queue()
    task = asyncio.create_task(mod.live_one_life("gc", {}, q))
    await asyncio.sleep(0.45)
    if exit_kind == "cancel":
        task.cancel()
    try:
        await asyncio.wait_for(task, timeout=5)
    except (asyncio.CancelledError, Exception):
        pass
    # Settle: a generator closed only by later garbage collection must not
    # count as cleanup. Give refcounting every chance and still require True.
    await asyncio.sleep(0.2)
    return state["closed"]


def main():
    ok = True
    for kind in ("cancel", "raise-in-body"):
        closed = asyncio.run(run(kind))
        good = bool(closed)
        ok = ok and good
        print("  exit=%-14s %-5s generator closed by cleanup=%s"
              % (kind, "PASS" if good else "FAIL", closed))
        if not good:
            print("        still open after the life returned and settled")
    print("\n%s" % ("GENERATOR-CLOSED PASS" if ok else "GENERATOR-CLOSED FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
