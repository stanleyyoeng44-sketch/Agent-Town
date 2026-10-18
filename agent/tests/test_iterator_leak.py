"""The cleanup must ENFORCE that no iterator task outlives the life.

THE INVARIANT, which is what this test defends: whatever exit a life takes,
the pending __anext__ task is cancelled by cleanup. Stated this way it stays
true when someone adds a fourth exit. Stated as a list of the exits that
currently leak -- which is how this defect was first described, by me and by
agent-9 in different words -- it goes stale the moment the code changes.

THE DEFECT IT WAS WRITTEN FOR: the iterator step lived in a local inside the
try, so an exit that skipped the clear left it pending while the finally
cancelled only the four pollers. Measured before the fix: one task alive and
the async generator not closed after the life returned.

WHAT IT DOES NOT COST, and the first version of this docstring got this
wrong: the client is created inside live_one_life, so the stream cannot
outlive the life and a second consumer is impossible. A leak costs a task
alive until GC holding the generator, an unretrieved-task exception at
shutdown, and a cleanup that does not enforce its invariant.

HOW IT IS MEASURED: the fake client's generator records when it is closed,
AND the test counts live tasks after the life returns. Both, because "the
generator closed" can be a coincidence of when you look -- it still passes
with the fix removed -- while the task count does not.

Run:  python3 agent/tests/test_iterator_leak.py
Exit: 0 no leak, 1 the iterator task outlived the life.
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
    spec = importlib.util.spec_from_file_location("daemon_leak", SRC)
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


async def scenario(exit_kind):
    """Run one life and end it by `exit_kind`, then report residue.

    exit_kind:
      "cancel"  -- the task driving the life is cancelled while an __anext__
                   is pending (the exit the fix was first written for);
      "raise"   -- the message-handling body raises BETWEEN messages, so no
                   task is pending and cancelling tasks cannot help. This one
                   caught a second, invisible leak: generator open, ZERO tasks
                   alive. It is the case that proves the invariant has to be
                   enforced by cleanup rather than by cancelling what happens
                   to be pending.
    """
    import claude_agent_sdk as sdk
    work = tempfile.mkdtemp()
    mod = load_daemon(work)
    state = {"closed": False, "closed_at": None}

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
                    for _ in range(4):
                        yield sdk.ResultMessage(
                            subtype="success", duration_ms=1,
                            duration_api_ms=1, is_error=False, num_turns=1,
                            session_id="s", total_cost_usd=0.0, usage={})
                        await asyncio.sleep(0.15)
                finally:
                    state["closed"] = True
                    state["closed_at"] = asyncio.get_running_loop().time()
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

    if exit_kind == "raise":
        # Raise from inside the message body on the second result, i.e. while
        # NO __anext__ task is pending.
        orig_log = mod.LifeLog.log
        seen = {"n": 0}

        def boom(self, kind, **kw):
            if kind == "result":
                seen["n"] += 1
                if seen["n"] == 2:
                    raise RuntimeError("body raised between messages")
            return orig_log(self, kind, **kw)

        mod.LifeLog.log = boom

    q = asyncio.Queue()
    task = asyncio.create_task(mod.live_one_life("leak", {}, q))
    await asyncio.sleep(0.5 if exit_kind == "cancel" else 0.9)
    if exit_kind == "cancel":
        task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    await asyncio.sleep(0.2)
    state["returned_at"] = asyncio.get_running_loop().time()
    state["alive"] = [t for t in asyncio.all_tasks()
                      if not t.done() and t is not asyncio.current_task()]
    return state


def main():
    ok = True
    for kind in ("cancel", "raise"):
        state = asyncio.run(scenario(kind))
        closed = state["closed"]
        # A generator that is only closed LATER was closed by the garbage
        # collector, not by cleanup. CPython closes async generators when they
        # are collected, which made an earlier version of this test pass with
        # the fix removed. Closure must be AT OR BEFORE the life returning.
        timely = (state.get("closed_at") is not None
                  and state.get("returned_at") is not None
                  and state["closed_at"] <= state["returned_at"] + 0.01)
        alive = state.get("alive") or []
        good = timely and not alive
        ok = ok and good
        print("  exit=%-7s %-5s closed-before-return=%-5s tasks alive=%d"
              % (kind, "PASS" if good else "FAIL", timely, len(alive)))
        if not good:
            print("        cleanup did not enforce the invariant for this exit")
    print("\n%s" % ("ITERATOR-LEAK PASS" if ok else "ITERATOR-LEAK FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
