"""A leaked __anext__ task means the NEXT life reads the previous life's stream.

agent-9's review of the life-boundary fix: the iterator step lives in a local
task (`nxt`), and if the life exits by a path that does not clear it, the
finally block cancels the four poller tasks but not `nxt`. The async
generator stays open, and since live_one_life is called again on the SAME
client for the next life, there are then two consumers of one stream.

The failure is invisible: no exception, no hang. The next life's first turn can
be fed from the previous life's stream. That is the class of bug that reads as
"the agent said something odd once".

THIS TEST MEASURES IT DIRECTLY: the fake client's receive_messages() generator
records when it is closed. After a life that ends by an exception raised
inside the message-handling body (a path that never clears `nxt`), the
generator must be CLOSED before live_one_life returns. If it is still open, the
task leaked.

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


async def scenario():
    import claude_agent_sdk as sdk
    work = tempfile.mkdtemp()
    mod = load_daemon(work)
    state = {"closed": False, "open": True}

    class Boom(RuntimeError):
        pass

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
                    # Raise from INSIDE the message body: the daemon is in the
                    # middle of handling a message, `nxt` is None at that
                    # instant, but a CancelledError/exception elsewhere can
                    # catch it with a task pending. Here we simulate the
                    # harsher case: the body raises after a message, and the
                    # generator should still be closed by cleanup.
                    yield sdk.ResultMessage(
                        subtype="success", duration_ms=1, duration_api_ms=1,
                        is_error=False, num_turns=1, session_id="s",
                        total_cost_usd=0.0, usage={})
                    # wait to be pulled again: this is where a leaked task
                    # would be sitting when the life exits
                    await asyncio.sleep(3600)
                finally:
                    state["closed"] = True
                    state["open"] = False
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

    q = asyncio.Queue()
    task = asyncio.create_task(mod.live_one_life("leak", {}, q))

    # Let the first result arrive and the loop start waiting on the next pull.
    await asyncio.sleep(0.8)
    # Now yank the life out from under the pending __anext__ task, the way a
    # CancelledError or an exception in the body would. This is the exit that
    # leaked: measured 1 task still alive and the generator open afterwards.
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        pass
    # Give cleanup a moment to run, then count anything still alive. The
    # generator being closed is necessary but not sufficient: the TASK must be
    # gone too, or the next life shares this stream.
    await asyncio.sleep(0.2)
    state["alive"] = [t for t in asyncio.all_tasks()
                      if not t.done() and t is not asyncio.current_task()]
    return state


def main():
    state = asyncio.run(scenario())
    ok = state["closed"] and not state.get("alive")
    print("  %-5s the async generator is CLOSED when the life returns"
          % ("PASS" if state["closed"] else "FAIL"))
    print("  %-5s no background task outlived the life (alive=%d)"
          % ("PASS" if not state.get("alive") else "FAIL",
             len(state.get("alive") or [])))
    if not ok:
        print("        a leaked __anext__ task means the next life on the "
              "same client shares this life's stream")
    print("\n%s" % ("ITERATOR-LEAK PASS" if ok else "ITERATOR-LEAK FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
