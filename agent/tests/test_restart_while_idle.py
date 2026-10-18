"""Does an agent that answers and THEN calls restart actually die?

Option B makes the life end on the session, not the response. That means the
receive loop is blocked while the model is quiet, so the restart flag must be
noticed some other way. This test drives the real live_one_life with a client
that:
  1. emits one completed response (the model answers), then
  2. blocks forever (the model is quiet), while
  3. the restart flag appears on disk.

It must return (life ends) rather than hang. A hang is the defect: the agent
called restart and nothing happened.
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
    spec = importlib.util.spec_from_file_location("daemon_rr", SRC)
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


async def scenario(flag_delay=0.6):
    import claude_agent_sdk as sdk
    work = tempfile.mkdtemp()
    mod = load_daemon(work)

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
                yield sdk.ResultMessage(
                    subtype="success", duration_ms=1, duration_api_ms=1,
                    is_error=False, num_turns=1, session_id="s",
                    total_cost_usd=0.0, usage={})
                # model is now quiet: never yield again
                await asyncio.sleep(3600)
            return gen()

        # not used when the daemon calls receive_messages, but defined so the
        # test works against either shape
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

    async def raise_flag():
        await asyncio.sleep(flag_delay)
        with open(mod.RESTART_FLAG, "w") as f:
            f.write("1")

    q = asyncio.Queue()
    task = asyncio.create_task(mod.live_one_life("rr", {}, q))
    fl = asyncio.create_task(raise_flag())
    try:
        slept = await asyncio.wait_for(task, timeout=6)
        return slept, True
    except asyncio.TimeoutError:
        task.cancel()
        return None, False
    finally:
        fl.cancel()


def main():
    slept, returned = asyncio.run(scenario())
    if not returned:
        print("  FAIL  restart after a quiet response: THE LIFE HUNG")
        print("\nRESTART-WHILE-IDLE FAIL")
        return 1
    ok = slept is True
    print("  %-5s restart after a quiet response ends the life (slept=%r)"
          % ("PASS" if ok else "FAIL", slept))
    print("\n%s" % ("RESTART-WHILE-IDLE PASS" if ok
                    else "RESTART-WHILE-IDLE FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
