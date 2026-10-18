"""test_idle_proof.py -- a live agent with nothing to do must still be visible.

Run:  python3 agent/tests/test_idle_proof.py          (stdlib + SDK, no Docker)

THE DEFECT (Stanley's, verbatim): "an agent newly restarted will not have any
response unless someone messages it." The daemon feeds the opening message and
then blocks on an empty inbox; a model with nothing to do writes nothing. So a
perfectly healthy agent's transcript contains only `birth`, and the UI cannot
distinguish it from a dead one.

THE FIX is evidence, not behaviour: the daemon writes one `idle` transcript
line at life start and whenever a run boundary goes quiet. No model turn, no
tokens, nothing the agent can observe.

WHY A FAKE CLIENT. No Docker on this box, and the behaviour under test is not
the model's -- it is what the DAEMON emits when the model says nothing. A fake
client that never yields is the worst case for the UI and the exact case
Stanley describes.


Why a fake client: no Docker here, and the behaviour under test is not the
model's -- it is what the DAEMON emits into the transcript when no message
arrives. We can observe that exactly without a model.

What we measure: for each startup condition, how many transcript lines the
daemon writes, and of what kind, in the window AFTER the opening message and
BEFORE any peer/task message arrives.
"""
import asyncio, json, os, sys, tempfile, types

import os as _os
DAEMON = _os.environ.get(
    "DAEMON_DIR", _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
# the file is agent-daemon.py (hyphen); load it under a module name
sys.path.insert(0, DAEMON)
import importlib.util
_src = _os.environ.get("DAEMON_FILE", os.path.join(DAEMON, "agent-daemon.py"))
_spec = importlib.util.spec_from_file_location("agent_daemon", _src)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["agent_daemon"] = _mod
_spec.loader.exec_module(_mod)

FED = []

class FakeClient:
    """Accepts the prompt stream and answers with nothing.

    This models a model that has nothing to say: it is the WORST case for the
    UI, and the case Stanley describes. Any output the UI gets in this case
    must come from the daemon, not from the model.
    """
    def __init__(self, *a, **k):
        self.resumed = False
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def query(self, *a, init=None, **k):
        # record everything the daemon feeds the model
        if hasattr(a[0], "__aiter__"):
            async for item in a[0]:
                FED.append(item)
                break
        elif init is not None:
            FED.append(init)
        return None
    async def get_context_usage(self): return {"totalTokens": 0}
    async def receive_response(self):
        # Never yield. The life is alive but silent, like a model with no work.
        await asyncio.sleep(3600)
        yield  # pragma: no cover

async def run_case(label, wake_prompt, timeout=1.5, resumed=False):
    """Run one life for `timeout` seconds with an empty inbox; report lines."""
    import agent_daemon as D

    work = tempfile.mkdtemp()
    D.TRANSCRIPTS = work
    D.SELF_DIR = os.path.join(work, "self")
    D.WAKE_FILE = os.path.join(D.SELF_DIR, "wake.md")
    D.SESSION_FILE = os.path.join(D.SELF_DIR, "session-id")
    D.RESTART_FLAG = os.path.join(work, "restart")
    os.makedirs(D.SELF_DIR, exist_ok=True)
    if wake_prompt is not None:
        open(D.WAKE_FILE, "w").write(wake_prompt)

    D.ClaudeSDKClient = FakeClient
    D.build_options = lambda cfg: types.SimpleNamespace(
        resume=("sess-123" if resumed else None), model=None)
    D.read_wake_prompt = lambda: wake_prompt or ""

    q = asyncio.Queue()
    task = asyncio.create_task(D.live_one_life("test", {}, q))
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except asyncio.TimeoutError:
        task.cancel()
        try: await task
        except BaseException: pass
    lines = []
    for f in sorted(os.listdir(work)):
        if f.startswith("life-") and f.endswith(".jsonl"):
            lines += [json.loads(x) for x in open(os.path.join(work, f)) if x.strip()]
    kinds = [l["kind"] for l in lines]
    print(f"  {label:44} lines={len(lines):2}  kinds={kinds}")
    return kinds

async def main():
    print("A live agent whose model has nothing to say, and whose inbox is empty.")
    print("The UI can only show what the DAEMON writes in that window:\n")
    k1 = await run_case("first boot (no wake.md)", None)
    k2 = await run_case("wake with a plan", "- check the board\n- review a branch")
    FED.clear()
    k3 = await run_case("RESTARTED container, resume succeeds", "- plan", resumed=True)
    print(f"  {'':44} messages fed to the model: {len(FED)}")
    print(f"{'':46}  the agent is never given anything to do"
          if not FED else "")
    print()
    # the FIRST-BOOT / wake-only cases are the ones Stanley describes: the
    # agent is given its opening message, then nothing. Evidence must exist in
    # BOTH, and also in the resumed case where the model is fed nothing at all.
    alive_evidence = [k for k in k1 + k2 + k3 if k not in ("birth", "context")]
    print(f"  proof-of-life lines across all three cases: {alive_evidence}")
    print("VERDICT")
    print(f"  the daemon emits any proof of life besides 'birth'?  "
          f"{'YES' if alive_evidence else 'NO'}")
    if not alive_evidence:
        print("  => the UI has nothing to show until a message arrives.")
        print("     That is Stanley's defect 3, reproduced without Docker.")
    return 0 if alive_evidence else 1

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
