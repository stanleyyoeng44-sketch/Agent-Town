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
               "tools/flagcheck.py",
               # repo_check was NOT in this list, so the gate that is quoted
               # as "one command runs everything" did not run the repo's
               # self-check -- and agent-10 found a RED row (README said 29
               # tracked .py, the tree had 31) that this gate reported as a
               # clean 17/1/1. A gate with a hole in it is worse than no gate,
               # because its green is quoted. The hole was invisible because
               # adding one test file changes a NUMBER in the README, and
               # nothing in SUITE_GLOBS counted.
               "tools/repo_check.py"]


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
    # THE CHECKER'S OWN CONTROL. The line above runs it with no arguments,
    # which executes the replay only; its --selftest mutation arms never ran
    # in the gate. agent-10 found three of those arms DEAD on 4041edf --
    # printing "could not build the lie" and testing nothing -- while this gate
    # reported the file PASS. A gate that runs a check but not its control is
    # the same defect as a check that cannot red, one door down.
    ("docs: README transcript CONTROL",
     os.path.join(os.path.dirname(os.path.abspath(__file__)),
                  "check_readme_transcript.py"), ["--selftest"]),
    ("docs: guide and demo blocks", os.path.join(A5, "check_doc_blocks.py")),
]


def in_git_tree():
    try:
        return subprocess.run(["git", "-C", ROOT, "rev-parse", "--is-inside-work-tree"],
                              capture_output=True, text=True).returncode == 0
    except OSError:
        return False


MANIFEST = os.path.join(HERE, "suites.txt")


def load_manifest():
    """The suites this gate EXPECTS, from tools/suites.txt. Comments and blank
    lines ignored. Returns None only when the file is ABSENT.

    ABSENT IS A FAILURE, NOT A FALLBACK (agent-8's review, measured). The first
    version made it a WARN to "let an older checkout still run" -- but an older
    checkout has no suites.txt AND no code that reads one; the reader only
    exists at or after the commit that also adds the file. So the fallback
    serves exactly one case: someone ran `git rm tools/suites.txt`. That is the
    same act as deleting a suite, one level up, and it must be as loud. This
    is not a migration path; a human who wants to run without the manifest
    passes --no-manifest and says so out loud.
    """
    try:
        with open(MANIFEST, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    return [ln.strip() for ln in lines
            if ln.strip() and not ln.strip().startswith("#")]


def manifest_diff(suites, expected):
    """(vanished, unregistered) -- both directions, because a swap keeps a
    COUNT still. See tools/suites.txt for the measurement that forced this."""
    found = {rel for _n, _a, _sp, rel in suites}
    want = set(expected)
    return sorted(want - found), sorted(found - want)


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
    # A WORD THAT APPEARS IN BOTH VOCABULARIES CANNOT VOTE. agent-10 found
    # RESULT in both lists: a FAIL line whose detail happened to contain
    # "a transfer RESULT of 900" was classified GOOD, so the gate printed
    #     FAIL  docs: README transcript CONTROL   PASS  a transfer result ...
    # -- a FAIL row whose detail reads like a pass, which is exactly the row
    # that is supposed to tell a reader the control is dead. Removed from
    # both: it is a noun here, not a verdict.
    BAD_WORDS = re.compile(r"FAIL|STALE|ERROR|DIFFER|TIMEOUT", re.I)
    GOOD_WORDS = re.compile(r"PASS|HOLDS|MATCH|OK\b|GREEN", re.I)
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

    # L10-L13: THE MANIFEST COMPARISON ITSELF. The three controls that justify
    # the manifest were living in a commit message, which stops running the
    # moment someone edits manifest_diff(). agent-8's point: this file's own
    # docstring says a check that has never failed is not a check. These run
    # the REAL functions on synthetic lists, and each can red on its own.
    same = ["a/x.py", "b/y.py"]
    l10 = manifest_diff([("t", [], None, "a/x.py"), ("t", [], None, "b/y.py")],
                        same) == ([], [])
    ok = ok and l10
    print("  %-5s %-44s %s" % ("PASS" if l10 else "FAIL",
                               "L10 identical sets -> nothing reported",
                               "" if l10 else "reported a difference"))
    gone = manifest_diff([("t", [], None, "b/y.py")], same)
    l11 = gone == (["a/x.py"], [])
    ok = ok and l11
    print("  %-5s %-44s vanished=%s" % ("PASS" if l11 else "FAIL",
                                        "L11 a DELETED suite is VANISHED",
                                        gone[0]))
    # THE ONE THAT MATTERS: a swap holds the COUNT constant. A comparison by
    # count would pass here; the set comparison must not.
    swap = manifest_diff([("t", [], None, "b/y.py"), ("t", [], None, "c/z.py")],
                         same)
    l12 = swap == (["a/x.py"], ["c/z.py"])
    ok = ok and l12
    print("  %-5s %-44s vanished=%s unregistered=%s"
          % ("PASS" if l12 else "FAIL", "L12 a COUNT-PRESERVING swap still reds",
             swap[0], swap[1]))
    # L13: THE ABSENT-MANIFEST DECISION, which is where the defect lived.
    # The first version of this arm asserted only that load_manifest() returns
    # None. It does -- in the correct build AND in the broken one. agent-8
    # reverted the decision to WARN with the wording untouched and the selftest
    # stayed 13/13 PASS while the gate exited 0 on a deleted suite. An arm that
    # names a failure it cannot express is not an arm, so this one calls the
    # DECISION, not the loader, and a FAIL->WARN mutant reds it.
    l13a = manifest_state(None) == "FAIL"
    ok = ok and l13a
    print("  %-5s %-44s got %r, want 'FAIL'"
          % ("PASS" if l13a else "FAIL",
             "L13 absent manifest -> FAIL", manifest_state(None)))
    l13b = manifest_state(["a/x.py"]) is None
    ok = ok and l13b
    print("  %-5s %-44s got %r, want None"
          % ("PASS" if l13b else "FAIL",
             "L13b a PRESENT manifest -> no manifest verdict",
             manifest_state(["a/x.py"])))
    l13c = manifest_state(None, no_manifest=True) == "WARN"
    ok = ok and l13c
    print("  %-5s %-44s got %r, want 'WARN'"
          % ("PASS" if l13c else "FAIL",
             "L13c --no-manifest -> WARN, never FAIL",
             manifest_state(None, no_manifest=True)))
    # ...and the loader half: an absent file really does come back as None, so
    # the decision above is being fed the state the runner will actually see.
    saved = MANIFEST
    try:
        globals()["MANIFEST"] = os.path.join(work, "does-not-exist.txt")
        l13d = load_manifest() is None
    finally:
        globals()["MANIFEST"] = saved
    ok = ok and l13d
    print("  %-5s %-44s %s" % ("PASS" if l13d else "FAIL",
                               "L13d an absent FILE loads as None",
                               "None" if l13d else "did not return None"))

    # L4: the aggregate verdict must follow the worst suite.
    verdict = verdict_of(["PASS", "WARN", "FAIL"])
    good_case = verdict == "FAIL"
    ok = ok and good_case
    print("  %-5s %-44s got %s, want FAIL" % ("PASS" if good_case else "FAIL",
                                              "L4 one FAIL makes the verdict FAIL", verdict))
    return ok


def manifest_state(expected, no_manifest=False):
    """The VERDICT the manifest should contribute, given what was loaded.

    This exists as a function for one reason: the decision is the defect. The
    first version of the absent-manifest fix put the choice inline in main(),
    and the selftest arm for it could only assert that load_manifest() returns
    None -- which it did, in both the correct build and the broken one. MEASURED
    by agent-8: revert the inline choice to WARN, wording untouched, and the
    selftest stayed 13/13 PASS while the gate exited 0 on a deleted suite. An
    arm that cannot express the failure it is named after is not an arm.
    Extracted here, the choice is callable without a tree and a mutant flipping
    FAIL->WARN reds it.

    Returns None when there is nothing to report (the manifest is present and
    should be compared normally).
    """
    if no_manifest:
        # A human typed the flag. Deliberate, and the row says what it costs.
        return "WARN"
    if expected is None:
        # The only checkout without suites.txt is one where somebody removed
        # it: an older checkout has no reader for it either. Same act as
        # deleting a suite, one level up.
        return "FAIL"
    return None


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
    ap.add_argument("--no-manifest", action="store_true",
                    help="run WITHOUT tools/suites.txt. Deliberate and loud: "
                         "this run cannot tell a deleted suite from a passing "
                         "one, so the verdict says so.")
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
    states = []
    expected = load_manifest()
    state = manifest_state(expected, args.no_manifest)
    if state == "WARN":
        print("  %-5s %-38s --no-manifest: a deleted suite is indistinguishable "
              "from a passing one in this run" % ("WARN", "suite manifest"))
        states.append("WARN")
    elif state == "FAIL":
        # See manifest_state(): the only checkout that lacks this file is one
        # where somebody removed it.
        print("  %-5s %-38s tools/suites.txt is missing: a deleted suite is "
              "indistinguishable from a passing one (pass --no-manifest to "
              "accept that risk deliberately)" % ("FAIL", "suite manifest"))
        states.append("FAIL")
    else:
        vanished, unregistered = manifest_diff(suites, expected)
        if vanished:
            print("  %-5s %-38s expected but NOT FOUND: %s"
                  % ("FAIL", "suite manifest: vanished", ", ".join(vanished)))
            states.append("FAIL")
        else:
            print("  %-5s %-38s all %d expected suite(s) present"
                  % ("PASS", "suite manifest: no suite vanished",
                     len(expected)))
            states.append("PASS")
        if unregistered:
            # Not a failure: a new suite is a normal thing to add. But it must
            # be SEEN and registered, because "green" is a claim about an
            # agreed set, and a suite nobody registered is outside that set.
            print("  %-5s %-38s found but NOT in tools/suites.txt: %s"
                  % ("WARN", "suite manifest: unregistered",
                     ", ".join(unregistered)))
            states.append("WARN")
    print("check: %d suite(s), from %s\n" % (len(suites), ROOT))
    for name, argv, special, _rel in suites:
        state, detail = run_suite(name, argv, special, args.verbose)
        states.append(state)
        print("  %-5s %-38s %s" % (state, name, detail))
    for entry in EXTERNAL:
        label, script = entry[0], entry[1]
        extra = entry[2] if len(entry) > 2 else []
        if not os.path.exists(script):
            # agent-10's point: in a fresh clone these are ABSENT, so "one
            # command verifies the repo" would silently verify less. Say whose
            # audits they are and that a clone does not have them.
            states.append("SKIP")
            print("  %-5s %-38s agent-5's private audit, not in the repo (%s)"
                  % ("SKIP", label, script))
            continue
        state, detail = run_suite(label, [sys.executable, "-I", script, ROOT] + extra,
                                  "exit2-is-warn", args.verbose)
        states.append(state)
        print("  %-5s %-38s %s" % (state, label, detail))
    verdict = verdict_of(states)
    print()
    print("CHECK: %s (%d pass, %d warn, %d fail)%s"
          % (verdict, states.count("PASS"), states.count("WARN"),
             states.count("FAIL"),
             # agent-8's ask: a reader quoting "CHECK: WARN" never sees the
             # flag that produced it. Put the caveat ON the line people quote.
             "  [--no-manifest: deleted suites are invisible this run]"
             if args.no_manifest else ""))
    return 0 if verdict in ("PASS", "WARN") else 1


if __name__ == "__main__":
    sys.exit(main())
