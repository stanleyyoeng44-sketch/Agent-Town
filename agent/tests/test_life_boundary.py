"""test_life_boundary.py -- one normal response must not end a life.

THE DEFECT. agent-daemon.py drives the session with

    async for message in client.receive_response():

and the SDK's own docstring says that iterator "terminates immediately after
yielding a ResultMessage". A normal, completed model response IS a
ResultMessage. So the loop exits after every ordinary answer:

    response completes -> loop exits -> live_one_life returns (no Death)
    -> main_loop: 'life ... ended without sleep; restarting in 10s'
    -> next life starts; SESSION_FILE was not deleted (the Death handler
       never ran) -> read_resumable_session() resumes -> the model is told
       "the container restarted".

Measured on the running container three times in a row: PID 1 start 13:08:07
unchanged across lives 1-3; every life ended with result=success, zero calls
to the restart tool, zero death events; each next birth 10s later with
wake_file=False.

WHAT THIS TEST ASSERTS. With a fake client that emits one completed response
and then FINISHES (the client's stream ends), the daemon must produce exactly
ONE life. With a fake client that emits two completed responses before the
stream ends, it must still produce ONE life -- because a completed response is
not an ending.

WHY A FAKE CLIENT. No Docker here; the behaviour under test is the daemon's
life boundary, not the model's. A fake client is the honest instrument: it
lets us say exactly how many ResultMessages occurred and what the daemon did.

Run:  python3 agent/tests/test_life_boundary.py
Exit: 0 the boundary holds, 1 the daemon started more lives than the stream
      justified.
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
    """Import the SHIPPED daemon with every module-level path redirected.

    CURRENT_LIFE_FILE and the other derived paths are bound at import time, so
    redirecting TRANSCRIPTS alone does not move them -- that mistake made a
    test write into a LIVE agent's transcripts earlier today. Every path is
    asserted to be inside `work` before the test proceeds.
    """
    spec = importlib.util.spec_from_file_location("daemon_lb", SRC)
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
    for name in ("SELF_DIR", "TRANSCRIPTS", "INBOX", "REPLIES"):
        assert str(getattr(mod, name)).startswith(work), (
            "redirect leak: %s=%r" % (name, getattr(mod, name)))
    if hasattr(mod, "CURRENT_LIFE_FILE"):
        assert str(mod.CURRENT_LIFE_FILE).startswith(work), "CURRENT leak"
    for d in (mod.SELF_DIR, mod.TRANSCRIPTS, mod.INBOX, mod.REPLIES,
              mod.MY_INBOX):
        os.makedirs(d, exist_ok=True)
    return mod


class FakeMessages:
    """Results the fake client yields, in order."""

    def __init__(self, results):
        self.results = results

    def __call__(self, *a, **k):
        return self


async def run_one_shot(mod, n_results):
    """Drive live_one_life with a client whose stream offers n_results and
    does not close until all of them have been consumed.

    THE FAKE MUST MODEL THE SDK CONTRACT, not a convenient version of it.
    The real receive_response() "terminates immediately after yielding a
    ResultMessage", so a fake that simply yields two results would let the
    daemon stop at the first and the test would pass vacuously -- which is
    exactly what my first version did. This fake instead records how many
    results the daemon CONSUMED before returning, which is the observable
    that distinguishes 'the life ended because the stream ended' from 'the
    life ended because a response completed'.
    """
    import claude_agent_sdk as sdk

    consumed = []

    class FakeClient:
        """A client that implements BOTH SDK iterators with their REAL
        contracts, so this test measures what the daemon actually asks for.

        - receive_response(): terminates immediately after the first
          ResultMessage (the SDK's documented behaviour).
        - receive_messages():  yields every message; it does not stop on a
          ResultMessage.

        Whichever the daemon calls determines how many completed responses
        it sees. That is the property under test -- not the model's behaviour,
        and not a convenient fake's.
        """

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

        def _results(self):
            for i in range(n_results):
                yield sdk.ResultMessage(
                    subtype="success", duration_ms=1, duration_api_ms=1,
                    is_error=False, num_turns=1, session_id="s",
                    total_cost_usd=0.0, usage={})

        def receive_response(self):
            """REAL CONTRACT: stop at the first ResultMessage."""
            async def gen():
                for r in self._results():
                    consumed.append("response")
                    yield r
                    return          # <- the SDK's documented stop
            return gen()

        def receive_messages(self):
            """REAL CONTRACT: keep yielding; do not stop on a ResultMessage."""
            async def gen():
                for r in self._results():
                    consumed.append("messages")
                    yield r
            return gen()

        async def get_context_usage(self):
            return {"totalTokens": 0}

    mod.ClaudeSDKClient = FakeClient
    mod.build_options = lambda cfg, *a: types.SimpleNamespace(
        resume=None, model=None)
    mod.deliver_pending_hook = _noop_hook
    if hasattr(mod, "log_idle_proof"):
        mod.log_idle_proof = lambda *a, **k: None

    q = asyncio.Queue()
    task = asyncio.create_task(mod.live_one_life("lb", {}, q))
    try:
        await asyncio.wait_for(task, timeout=10)
    except asyncio.TimeoutError:
        task.cancel()
        raise
    return task.result(), len(consumed)


async def _noop_hook(*a, **k):
    return {}


def lives_in(work):
    d = os.path.join(work, "transcripts")
    return sorted(f for f in os.listdir(d) if f.startswith("life-"))


def main():
    checks = []

    def check(name, cond, detail=""):
        checks.append((name, bool(cond), detail))
        print("  %-5s %s%s" % ("PASS" if cond else "FAIL", name,
                               "" if cond else "  (%s)" % detail))

    # Case A: one completed response, then the stream would end.
    work = tempfile.mkdtemp()
    mod = load_daemon(work)
    slept, seen = asyncio.run(run_one_shot(mod, 1))
    check("one completed response runs one life", len(lives_in(work)) == 1,
          lives_in(work))
    check("a completed response is not reported as a sleep", slept is False,
          "slept=%r" % (slept,))
    check("the daemon consumed the single response", seen == 1, seen)

    # Case B: THE DEFECT. Two completed responses are offered. A completed
    # response is not an ending, so the daemon must consume BOTH before it
    # returns. Today it returns after the first, which is why every normal
    # answer ends the life and the next life announces a 'restart'.
    work2 = tempfile.mkdtemp()
    mod2 = load_daemon(work2)
    _slept2, seen2 = asyncio.run(run_one_shot(mod2, 2))
    check("a life does not end on the first completed response",
          seen2 == 2, "daemon stopped after %d of 2 responses" % seen2)

    ok = all(c[1] for c in checks)
    print("\n%s" % ("LIFE-BOUNDARY PASS" if ok else "LIFE-BOUNDARY FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
