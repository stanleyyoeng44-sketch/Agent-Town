"""selftest_demo.py — three tests that the demo is evidence, not theatre.

1. NEGATIVE CONTROL FIRES: remove the overdraft guard and the demo must fail.
2. FORWARD COMPATIBILITY: apply the bank fix recommended in review #1 (env-var
   ledger path) and the demo must STILL pass, via the unmodified file. Without
   this, a good change to bank.py would silently break the demo.
3. UNKNOWN LEDGER SHAPE: if bank.py neither hardcodes /data nor reads an env
   var, the demo must exit 2 with a reason, not run against the wrong ledger.

MUTATION TEST: delete the overdraft guard in a copy of bank.py and assert the
demo then FAILS. If the demo still passes with the guard removed, its
"overdraft refused" check is not testing anything.
"""
import os, subprocess, sys, tempfile, shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO = os.path.join(ROOT, "demo", "town.py")

def run_demo_with_bank(transform):
    """Run the demo in a temp tree whose bank.py is `transform(source)`."""
    tmp = tempfile.mkdtemp(prefix="demo-selftest-")
    try:
        os.makedirs(os.path.join(tmp, "bank"))
        os.makedirs(os.path.join(tmp, "demo"))
        src = open(os.path.join(ROOT, "bank", "bank.py")).read()
        open(os.path.join(tmp, "bank", "bank.py"), "w").write(transform(src))
        shutil.copy(DEMO, os.path.join(tmp, "demo", "town.py"))
        r = subprocess.run([sys.executable, os.path.join(tmp, "demo", "town.py")],
                           capture_output=True, text=True, timeout=90)
        return r.returncode, r.stdout, r.stderr
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_demo_with_mutated_bank(guard_on: bool):
    """Copy the repo, optionally remove the overdraft guard, run the demo."""
    tmp = tempfile.mkdtemp(prefix="demo-selftest-")
    try:
        # minimal tree: bank/ + demo/
        os.makedirs(os.path.join(tmp, "bank"))
        os.makedirs(os.path.join(tmp, "demo"))
        src = open(os.path.join(ROOT, "bank", "bank.py")).read()
        if not guard_on:
            # SEMANTIC mutation, not textual (agent-2's point): make the guard
            # condition always false by rewriting the COMPARISON, wherever it
            # is. A text-replace of the exact three lines breaks the moment
            # anyone reformats the guard, and then this selftest silently
            # stops testing anything.
            needle = "if s_bal < amount:"
            assert needle in src, "overdraft comparison not found -- selftest stale"
            src = src.replace(needle, "if False:  # GUARD DISABLED BY SELFTEST")
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
def env_fix(src):
    return src.replace('DATA = "/data/accounts.json"',
                       'DATA = os.environ.get("BANK_DATA", "/data/accounts.json")')

def unknown_shape(src):
    """A ledger path the demo cannot redirect.

    Post-merge, bank.py reads the env var, so the FIRST branch of the demo's
    resolver wins and this mutation is a no-op -- the test was silently
    passing a working bank. It must remove BOTH supported shapes: the env
    read AND the literal. Written after agent-10's bank fix landed on
    repo-hygiene and this exact test went green for the wrong reason.
    """
    out = src.replace('DATA = "/data/accounts.json"',
                      'DATA = "/srv/x/ledger.json"')
    out = out.replace('os.environ.get("DATA", "/data/accounts.json")',
                      '"/srv/x/ledger.json"')
    out = out.replace("os.environ.get('DATA', '/data/accounts.json')",
                      "'/srv/x/ledger.json'")
    return out

def single_value_guard(src):
    """agent-2's attack: refuse overdrafts ONLY at the amount the demo tests.

    Found by agent-2 in peer review #2. My first negative control tested one
    amount, so a bank that special-cased that amount PASSED. This mutation is
    kept as a permanent regression: if the demo ever goes back to a
    single-value check, this line catches it.
    """
    return src.replace("if s_bal < amount:",
                       "if s_bal < amount and amount == 10000:")

rc5, out5, err5 = run_demo_with_bank(single_value_guard)
print(f"single-value guard (agent-2): rc={rc5}  (expect 1)")
print("  " + ([l for l in out5.splitlines() if "FAIL" in l][:1] or ["(none)"])[0])

rc3, out3, err3 = run_demo_with_bank(env_fix)
print(f"env-var fix applied: rc={rc3}  (expect 0)")
print("  " + ([l for l in out3.splitlines() if "DEMO" in l][-1]
               if "DEMO" in out3 else "(no verdict line)"))
rc4, out4, err4 = run_demo_with_bank(unknown_shape)
print(f"unknown ledger shape: rc={rc4}  (expect 2)")
print("  " + ([l for l in out4.splitlines() if "FAIL" in l][:1] or ["(none)"])[0])

ok = (rc == 0 and rc2 == 1 and rc3 == 0 and rc4 == 2 and rc5 == 1)
print("\nSELFTEST", "PASS" if ok else "FAIL", end="")
print(" - control fires, single-value guard caught, bank fix does not break "
      "the demo, unknown shape refuses" if ok else "")
sys.exit(0 if ok else 1)
