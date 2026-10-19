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
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# DISCOVERY, NOT A HAND-MAINTAINED LIST. agent-10 measured that the first
# version ran 2 of the 3 agent suites that existed on master: a hand-written
# list is a statement about the author's memory, not about the repository, and
# the failure is silent -- the missing suites do not fail, they vanish. Now a
# new suite is included by EXISTING, and the only way to omit one is the
# EXCLUDE table below, which a reviewer can argue with.
#
# Everything discovered is assumed to follow the convention (exit 0 = pass).
# The exceptions are named here, once, with the reason:
EXCLUDE = {
    # path -> (label, special, why)
    "tools/verify_context_persistence.py": (
        "deploy: memory survives restart", "warn-if-no-docker",
        "exits 0 with NO DOCKER while printing that the volume property is "
        "unproven; PASS would be a green tick over evidence nobody gathered"),
    "tools/migrate_agent_volumes.py": (
        None, "skip",
        "a TOOL, not a test: exits 2 with no arguments by design"),
    "tools/check.py": (
        None, "skip", "this runner"),
}
# Directories whose *.py are suites, in the order they should run.
SUITE_GLOBS = ["demo/town.py", "demo/selftest_demo.py",
               "bank/tests/*.py", "agent/tests/*.py",
               "tools/test_*.py", "docs/claimgraph/build.py",
               # flagcheck is a WITNESS, not a second gate: it re-derives the
               # README-vs-start_agent comparison independently of C11, and
               # agreement between two implementations is the only thing that
               # makes a text comparison believable. It exits 0 on match, 1 on
               # differ, 2 when it cannot measure -- the convention holds.
               "tools/flagcheck.py"]


NO_DOCKER = "NO DOCKER DAEMON"
A5 = "/shared/artifacts/agent-5/repo_audit"
EXTERNAL = [
    # (label, path, args) -- audits that live outside the repo but attest to it.
    # They take the tree path as argv[1] so they measure the tree under test,
    # not the live one.
    #
    # THE TRANSCRIPT CHECK NOW RUNS FROM THE REPO. It used to run agent-5's
    # frozen copy at /shared/artifacts/agent-5/repo_audit/, which is a
    # SNAPSHOT: it still replays README curls with no credential, so a bank
    # that correctly authenticates every sender makes it report "the bank in
    # this tree is broken" -- a red about the instrument, not the tree. The
    # repo's tools/check_readme_transcript.py is the maintained descendant
    # (it has the same --selftest mutation control, plus header-aware replay
    # and a credential-setup replay), so the gate measures the tree's own
    # checker. The frozen copy is kept only because a gate that reads outside
    # the tree is this repo's stated practice for INDEPENDENT witnesses --
    # check_doc_blocks.py below still is one.
    ("docs: README bank transcript",
     os.path.join(os.path.dirname(os.path.abspath(__file__)),
                  "check_readme_transcript.py")),
    ("docs: guide and demo blocks", os.path.join(A5, "check_doc_blocks.py")),
]


def in_git_tree():
    try:
        return subprocess.run(["git", "-C", ROOT, "rev-parse", "--is-inside-work-tree"],
                              capture_output=True, text=True).returncode == 0
    except OSError:
        return False


def discover():
    """Every suite in the tree, plus the named exceptions. Sorted, stable."""
    import glob
    found = []
    for pattern in SUITE_GLOBS:
        paths = sorted(glob.glob(os.path.join(ROOT, pattern)))
        for path in paths:
            rel = os.path.relpath(path, ROOT)
            if rel in EXCLUDE:
                label, special, _why = EXCLUDE[rel]
                if special == "skip":
                    continue
                argv = [sys.executable, rel]
                if rel == "tools/verify_context_persistence.py":
                    argv += ["--agent", "check"]
                found.append((label, argv, special, rel))
                continue
            argv = [sys.executable, rel]
            if rel.startswith("docs/claimgraph/"):
                argv.append("selftest")
            found.append((rel, argv, None, rel))
    # Files that no glob reaches but that are suites with an argument or a
    # special verdict: they are named in EXCLUDE and added here. A file that is
    # neither globbed nor excluded does not exist as far as this runner is
    # concerned, so the exclusion table is the reviewable surface.
    seen = {rel for _n, _a, _s, rel in found}
    for rel, (label, special, why) in EXCLUDE.items():
        if special == "skip" or rel in seen:
            continue
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            continue
        argv = [sys.executable, rel]
        if rel == "tools/verify_context_persistence.py":
            argv += ["--agent", "check"]
        found.append((label, argv, special, rel))
    return found


def run_suite(name, argv, special, verbose):
    try:
        p = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        return "FAIL", "timed out after 1800s"
    except OSError as exc:
        return "FAIL", "could not start: %s" % exc
    tail = (p.stdout + p.stderr).strip().splitlines()
    # THE DETAIL LINE MUST BE THE DIAGNOSIS, NOT THE LAST THING PRINTED.
    # agent-10 measured this on the runner's first run: the doc-block audit
    # ends with a reassurance ("Every suite in the repo can still exit 0: none
    # of them reads prose") AFTER its verdict, so tail[-1] reported a real FAIL
    # with a sentence that reads like the repo is fine. Prefer the last line
    # that names a verdict; fall back to the tail only when there is none.
    # BUT THE SAME RULE INVERTS ON THE PASS PATH (agent-5, measured). Three
    # CORRECT suites print a failure word while exiting 0, because the word is
    # the NAME of the case they exercise or of the pre-fix behaviour they
    # assert against:
    #     demo/selftest_demo.py        "FAIL: bank.py has neither a DATA env ..."
    #     bank/tests/test_transfer_amounts.py   "... before=FAIL(mint visible)"
    #     tools/test_prompt_stream.py  "FAIL  resumed, mail already queued"
    # The runner showed all three as PASS rows whose text says FAIL, so a
    # reader skimming sees three failures that are not there -- the mirror of
    # agent-10's bug (a reassurance in a failure row is now a failure word in a
    # passing row).
    #
    # THE EXIT STATUS IS THE FACT; the regex is only a hint about WHERE to
    # look. So the vocabulary depends on the verdict the exit code already
    # established:
    #   rc != 0  -> prefer the last line that names a problem  (old behaviour)
    #   rc == 0  -> prefer the last line that names a SUCCESS, and never
    #               promote a line whose only verdict word is a failure one
    BAD_WORDS = re.compile(r"RESULT|FAIL|STALE|ERROR|DIFFER|TIMEOUT", re.I)
    GOOD_WORDS = re.compile(r"RESULT|PASS|HOLDS|MATCH|OK\b|GREEN", re.I)
    # A line that says both ("control fired (pre-fix shape went mute): PASS")
    # is a success line: it reports a failure it INDUCED, then its own verdict.
    # The trailing verdict wins, so score the LAST match in the line.
    def _kind(line):
        good = [m.start() for m in GOOD_WORDS.finditer(line)]
        bad = [m.start() for m in BAD_WORDS.finditer(line)]
        if not bad:
            return "neutral"
        if good and max(good) > max(bad):
            return "good"
        return "bad"

    def diagnosis(lines, ok):
        want = "good" if ok else "bad"
        for line in reversed(lines):
            k = _kind(line)
            if k == want:
                return line.strip()
        # No line in the right vocabulary: report the tail, but never let a
        # failure word masquerade as the summary of a passing suite.
        return lines[-1].strip() if lines else ""
    last = diagnosis(tail, p.returncode == 0)
    if verbose:
        print("\n".join("      | " + line for line in tail[-40:]))
    if special == "warn-if-no-docker" and p.returncode == 0 and NO_DOCKER in p.stdout:
        return "WARN", "unit simulation only -- no Docker daemon here"
    if special == "exit2-is-warn" and p.returncode == 2:
        # agent-5's convention: 2 means "cannot measure, the doc changed shape".
        # Neither a pass nor a failure; folding it into either direction is how
        # a gate gets ignored (exit 2 forever) or cried wolf (false STALE).
        return "WARN", "unmeasured: %s" % (last[:90] or "the tool declined")
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
    # Same vocabulary run_suite() uses to classify a line AT rc==0. Imported
    # by construction rather than by copy: if the two ever disagree, the
    # control tests a rule the runner does not apply.
    BAD_WORDS_ONLY = re.compile(r"FAIL|STALE|ERROR|DIFFER|TIMEOUT", re.I)
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
    unmeasured = script("unmeasured.py", "import sys\nprint('shape not recognised')\nsys.exit(2)\n")

    cases = [
        ("L1 a passing suite is PASS", run_suite("t", [sys.executable, good], None, False), "PASS"),
        ("L2 a failing suite is FAIL", run_suite("t", [sys.executable, bad], None, False), "FAIL"),
        ("L3 no-docker banner is WARN", run_suite("t", [sys.executable, nodocker],
                                                  "warn-if-no-docker", False), "WARN"),
        ("L5 exit 2 is WARN, not PASS and not FAIL",
         run_suite("t", [sys.executable, unmeasured], "exit2-is-warn", False), "WARN"),
    ]
    for label, (got, _), want in cases:
        good_case = got == want
        ok = ok and good_case
        print("  %-5s %-44s got %s, want %s" % ("PASS" if good_case else "FAIL", label, got, want))

    # L8: A PASSING SUITE MUST NOT BE SUMMARISED BY A FAILURE WORD.
    # agent-5 measured three correct suites whose detail line said FAIL while
    # exiting 0 -- a case NAME ("FAIL  resumed, mail already queued") or the
    # pre-fix behaviour under test ("before=FAIL(mint visible)"). The old
    # diagnosis() preferred the last line matching a failure word regardless
    # of the exit code, so all three read as failures on PASS rows.
    # This control asserts on the DETAIL, not on the verdict: the verdict was
    # never wrong, which is why no existing case caught it.
    # Both directions, because a rule that only suppresses failure words would
    # also erase agent-10's original fix (the reassurance after a real FAIL).
    mixed = script("mixed.py", "print('before=FAIL(mint visible)')\n"
                               "print('SELFTEST PASS')\n")
    good_bad = script("good_bad.py",
                      "print('SUITE FAILED: two plus two')\n"
                      "print('Every suite can still exit 0: none reads prose')\n"
                      "import sys; sys.exit(1)\n")
    _s1, d1 = run_suite("t", [sys.executable, mixed], None, False)
    _s2, d2 = run_suite("t", [sys.executable, good_bad], None, False)
    l8 = (not BAD_WORDS_ONLY.search(d1)) and ("FAIL" in d2.upper())
    ok = ok and l8
    print("  %-5s %-44s detail=%r" % ("PASS" if l8 else "FAIL",
                                      "L8 a PASS row is not a FAIL word", d1[:60]))
    print("  %-5s %-44s detail=%r" % ("PASS" if ("FAIL" in d2.upper()) else "FAIL",
                                      "L9 a FAIL row keeps its diagnosis", d2[:60]))

    # L6: DISCOVERY MUST SEE A NEW SUITE. agent-10's finding was that the
    # runner ran 2 of 3 agent suites because the list was hand-written. This
    # control creates a file in agent/tests/ that FAILS, and requires discover()
    # to find it and the runner to go red -- so a suite added tomorrow is in
    # the gate without anyone editing a list.
    import shutil
    probes = []
    for d in ("agent/tests", "bank/tests"):
        target = os.path.join(ROOT, d, "test_check_runner_discovery_probe.py")
        probes.append(target)
        with open(target, "w", encoding="utf-8") as f:
            f.write("import sys\nprint('DISCOVERY PROBE: this suite must be seen')\n"
                    "sys.exit(1)\n")
    try:
        found = [rel for _n, _a, _s, rel in discover()]
        seen = ("agent/tests/test_check_runner_discovery_probe.py" in found
                and "bank/tests/test_check_runner_discovery_probe.py" in found)
        ok = ok and seen
        print("  %-5s %-44s %s" % ("PASS" if seen else "FAIL",
                                   "L6 discovery sees a new suite",
                                   "found both probes" if seen else "MISSED a probe"))
    finally:
        for target in probes:
            try:
                os.unlink(target)
            except OSError:
                pass

    # L7: A DEAD EXCLUSION IS A SILENT HOLE (agent-10). EXCLUDE is keyed by
    # exact path; if a file is renamed, its entry stops matching anything and
    # whatever it pointed at either runs as a normal suite or vanishes, with no
    # line saying so. The table must describe files that EXIST.
    dead = [rel for rel in EXCLUDE
            if not os.path.exists(os.path.join(ROOT, rel))]
    ok = ok and not dead
    print("  %-5s %-44s %s" % ("PASS" if not dead else "FAIL",
                               "L7 no dead EXCLUDE entries",
                               "all point at real files" if not dead
                               else "dead: %s" % ", ".join(dead)))

    # L4: the aggregate verdict must follow the worst suite.
    verdict = verdict_of(["PASS", "WARN", "FAIL"])
    good_case = verdict == "FAIL"
    ok = ok and good_case
    print("  %-5s %-44s got %s, want FAIL" % ("PASS" if good_case else "FAIL",
                                              "L4 one FAIL makes the verdict FAIL", verdict))
    return ok


def verdict_of(states):
    """FAIL > WARN > PASS. SKIP is not a state of the tree, it is a state of
    this box, so it never changes the verdict -- but it is always printed."""
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

    suites = discover()
    print("check: %d suite(s), from %s\n" % (len(suites), ROOT))
    states = []
    for name, argv, special, _rel in suites:
        state, detail = run_suite(name, argv, special, args.verbose)
        states.append(state)
        print("  %-5s %-38s %s" % (state, name, detail))
    for label, script in EXTERNAL:
        if not os.path.exists(script):
            # agent-10's point: in a fresh clone these are ABSENT, so "one
            # command verifies the repo" would silently verify less. Say whose
            # audits they are and that a clone does not have them.
            states.append("SKIP")
            print("  %-5s %-38s agent-5's private audit, not in the repo (%s)"
                  % ("SKIP", label, script))
            continue
        state, detail = run_suite(label, [sys.executable, "-I", script, ROOT],
                                  "exit2-is-warn", args.verbose)
        states.append(state)
        print("  %-5s %-38s %s" % (state, label, detail))
    verdict = verdict_of(states)
    print()
    print("CHECK: %s (%d pass, %d warn, %d fail)"
          % (verdict, states.count("PASS"), states.count("WARN"), states.count("FAIL")))
    return 0 if verdict in ("PASS", "WARN") else 1


if __name__ == "__main__":
    sys.exit(main())
