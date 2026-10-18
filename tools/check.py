#!/usr/bin/env python3
"""check.py -- one command that runs everything this repo claims to verify.

    python3 tools/check.py              # run every suite, one line each
    python3 tools/check.py --selftest   # prove this runner can report a failure
    python3 tools/check.py -v           # stream each suite's own output

WHY IT EXISTS
    Six suites live in four directories (demo/, bank/tests/, agent/tests/,
    tools/) and before this file the only thing that ran them together was a
    human reading the board. A reader who clones the repo had no single
    command that answers "is this tree fine?", and a README that lists six
    commands is a README where one of them is stale.

THE CONVENTION IT ASSUMES, measured from the suites themselves
    A suite exits 0 on success and nonzero on a real failure. Two do not fit
    and are handled explicitly rather than by a guess:

    tools/verify_context_persistence.py
        exits 0 WITH NO DOCKER while printing "NO DOCKER DAEMON: running the
        unit simulation only". Its unproven limbs are a WARN: calling that
        PASS would be a green tick over evidence nobody gathered, and calling
        it FAIL would make a good tree red on a box without a daemon.

    tools/migrate_agent_volumes.py
        is a TOOL, not a test: with no arguments it exits 2 by design. It is
        listed as SKIP so nobody "fixes" it into the runner by accident.

    Everything else -- demo/town.py, demo/selftest_demo.py, the two bank
    suites, agent/tests/test_idle_proof.py, tools/test_prompt_stream.py,
    tools/repo_check.py, docs/claimgraph/build.py selftest -- chains
    directly.

VERDICT
    FAIL if any suite exits nonzero (other than the two special cases).
    WARN if every suite passed but a suite reported it could not prove
    something here (today: the Docker limbs).
    PASS otherwise. Exit code 0 for PASS and WARN, 1 for FAIL, 2 for a
    missing prerequisite (not inside a git tree).

    WARN is not a soft FAIL. It is the honest third state this cohort keeps
    needing: "this check could not see the thing" is different from "the
    thing is broken", and collapsing them into one boolean is how a coverage
    gap becomes a green tick.
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# (name, argv, special)  -- special: None | "warn-if-no-docker"
SUITES = [
    ("demo: a two-agent town runs", [sys.executable, "demo/town.py"], None),
    ("demo: its checks can fail", [sys.executable, "demo/selftest_demo.py"], None),
    ("bank: transfers and the mint", [sys.executable, "bank/tests/test_transfer_amounts.py"], None),
    ("bank: operator token paths", [sys.executable, "bank/tests/test_token_paths.py"], None),
    ("agent: the daemon proves life", [sys.executable, "agent/tests/test_idle_proof.py"], None),
    ("agent: the prompt stream", [sys.executable, "tools/test_prompt_stream.py"], None),
    ("repo: tracked junk, secrets, docs", [sys.executable, "tools/repo_check.py", "."], None),
    ("repo: the checker can fail", [sys.executable, "tools/repo_check.py", "--selftest"], None),
    ("claims: the graph matches the store", [sys.executable, "docs/claimgraph/build.py", "selftest"], None),
    ("deploy: memory survives restart", [sys.executable, "tools/verify_context_persistence.py",
                                         "--agent", "check"], "warn-if-no-docker"),
]

NO_DOCKER = "NO DOCKER DAEMON"


def in_git_tree():
    try:
        return subprocess.run(["git", "-C", ROOT, "rev-parse", "--is-inside-work-tree"],
                              capture_output=True, text=True).returncode == 0
    except OSError:
        return False


def run_suite(name, argv, special, verbose):
    try:
        p = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        return "FAIL", "timed out after 1800s"
    except OSError as exc:
        return "FAIL", "could not start: %s" % exc
    tail = (p.stdout + p.stderr).strip().splitlines()
    last = tail[-1].strip() if tail else ""
    if verbose:
        print("\n".join("      | " + line for line in tail[-40:]))
    if special == "warn-if-no-docker" and p.returncode == 0 and NO_DOCKER in p.stdout:
        return "WARN", "unit simulation only -- no Docker daemon here"
    if p.returncode != 0:
        return "FAIL", last[:100] or ("exit %d" % p.returncode)
    return "PASS", last[:100]


def selftest():
    """A runner that has never reported a failure is not a runner.

    Two controls:
      L1 a suite that exits 0                         -> PASS
      L2 a suite that exits nonzero AND prints a failure -> FAIL
      L3 a suite that prints the no-docker banner and exits 0 -> WARN
      L4 the verdict is FAIL when any suite FAILS, even if others pass
    Each runs through run_suite() itself, so this tests the real code path.
    """
    import tempfile
    work = tempfile.mkdtemp(prefix="check-selftest-")
    ok = True

    def script(name, body):
        path = os.path.join(work, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        return path

    good = script("good.py", "print('all good')\n")
    bad = script("bad.py", "import sys\nprint('SUITE FAILED: two plus two')\nsys.exit(1)\n")
    nodocker = script("nodocker.py", "print('NO DOCKER DAEMON: running the unit simulation only.')\n")

    cases = [
        ("L1 a passing suite is PASS", run_suite("t", [sys.executable, good], None, False), "PASS"),
        ("L2 a failing suite is FAIL", run_suite("t", [sys.executable, bad], None, False), "FAIL"),
        ("L3 no-docker banner is WARN", run_suite("t", [sys.executable, nodocker],
                                                  "warn-if-no-docker", False), "WARN"),
    ]
    for label, (got, _), want in cases:
        good_case = got == want
        ok = ok and good_case
        print("  %-5s %-44s got %s, want %s" % ("PASS" if good_case else "FAIL", label, got, want))

    # L4: the aggregate verdict must follow the worst suite.
    verdict = verdict_of(["PASS", "WARN", "FAIL"])
    good_case = verdict == "FAIL"
    ok = ok and good_case
    print("  %-5s %-44s got %s, want FAIL" % ("PASS" if good_case else "FAIL",
                                              "L4 one FAIL makes the verdict FAIL", verdict))
    return ok


def verdict_of(states):
    if "FAIL" in states:
        return "FAIL"
    if "WARN" in states:
        return "WARN"
    return "PASS"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true",
                    help="prove the runner can report a failure")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        ok = selftest()
        print()
        print("RUNNER SELFTEST: %s" % ("PASS" if ok else "FAIL"))
        return 0 if ok else 1

    if not in_git_tree():
        print("CHECK: cannot run -- %s is not inside a git work tree, and two of "
              "the suites read `git ls-files`. Run this inside a clone, or "
              "`git init && git add -A && git commit -qm x` in an export." % ROOT)
        return 2

    print("check: %d suite(s), from %s\n" % (len(SUITES), ROOT))
    states = []
    for name, argv, special in SUITES:
        state, detail = run_suite(name, argv, special, args.verbose)
        states.append(state)
        print("  %-5s %-38s %s" % (state, name, detail))
    verdict = verdict_of(states)
    print()
    print("CHECK: %s (%d pass, %d warn, %d fail)"
          % (verdict, states.count("PASS"), states.count("WARN"), states.count("FAIL")))
    return 0 if verdict in ("PASS", "WARN") else 1


if __name__ == "__main__":
    sys.exit(main())
