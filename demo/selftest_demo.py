"""selftest_demo.py — mutation test for demo/town.py's negative control.

MUTATION TEST: delete the overdraft guard in a copy of bank.py and assert the
demo then FAILS. If the demo still passes with the guard removed, its
"overdraft refused" check is not testing anything.
"""
import os, subprocess, sys, tempfile, shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO = os.path.join(ROOT, "demo", "town.py")

def run_demo_with_mutated_bank(guard_on: bool):
    """Copy the repo, optionally remove the overdraft guard, run the demo."""
    tmp = tempfile.mkdtemp(prefix="demo-selftest-")
    try:
        # minimal tree: bank/ + demo/
        os.makedirs(os.path.join(tmp, "bank"))
        os.makedirs(os.path.join(tmp, "demo"))
        src = open(os.path.join(ROOT, "bank", "bank.py")).read()
        if not guard_on:
            needle = '            if s_bal < amount:\n                return self._send(400, {"error": "insufficient funds",\n                                        "money": s_bal})\n'
            assert needle in src, "guard text not found -- selftest is stale"
            src = src.replace(needle, "            # GUARD REMOVED BY SELFTEST\n")
        open(os.path.join(tmp, "bank", "bank.py"), "w").write(src)
        shutil.copy(DEMO, os.path.join(tmp, "demo", "town.py"))
        r = subprocess.run([sys.executable, os.path.join(tmp, "demo", "town.py")],
                           capture_output=True, text=True, timeout=90)
        return r.returncode, r.stdout
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

rc, out = run_demo_with_mutated_bank(True)
print(f"guard ON : rc={rc}  (expect 0)")
print("  " + [l for l in out.splitlines() if "DEMO" in l][-1])
rc2, out2 = run_demo_with_mutated_bank(False)
print(f"guard OFF: rc={rc2}  (expect 1)")
print("  " + [l for l in out2.splitlines() if "DEMO" in l][-1])
ok = (rc == 0 and rc2 == 1)
print("\nSELFTEST", "PASS" if ok else "FAIL",
      "- the negative control fires only when the guard is present" if ok else "")
sys.exit(0 if ok else 1)
