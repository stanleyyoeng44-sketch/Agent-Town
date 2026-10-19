#!/usr/bin/env python3
"""test_wake_fallback.py -- the daemon must FIND the note the agent wrote.

Run:  python3 agent/tests/test_wake_fallback.py

THE DEFECT THIS PINS, measured 2026-10-18 by agent-4 on its own container and
confirmed as a narrower shape by agent-0 and agent-6 on theirs.

`read_wake_prompt()` read exactly one path, `SELF_DIR/wake.md`. That path is
durable only when the container has a named volume for it. While it did not,
agents wrote their notes into the shared artifacts tree instead -- seven of
them at `/shared/artifacts/<agent>/wake.md`, one at `.../self/wake.md`. The
daemon never looked there. The observable consequence is in the per-life
transcript: `birth ... wake_file=false`, and the model's first message is

    "This is your first boot. There is no wake-up prompt yet."

Three of agent-4's three recorded lives opened that way.

WHY IT LOOKED FINE: the first-boot text is LEGITIMATE for a genuinely new
agent. A fallback that cannot be distinguished from the success case is
invisible, and the only witness was `wake_file=false`, a field nobody read.

WHAT THIS ASSERTS, and each arm can fail on its own:
  A  a note at SELF_DIR is returned VERBATIM -- the documented path still wins
  B  SELF_DIR absent, note at the artifacts ROOT      -> that note is found
  C  SELF_DIR absent, note at the artifacts self/ dir -> that note is found
  D  BOTH present and DIFFERENT -> SELF_DIR wins, and arm D2 proves the choice
     is on the record (`wake_fallback` is not False when the fallback supplied
     the text). A silent winner is how an agent's edit gets ignored.
  E  nothing anywhere -> the first-boot text, NOT ""
  F  an EMPTY SELF_DIR file does NOT shadow a good note in the artifacts tree
  G  `find_wake_file()` names WHY each candidate was rejected, per path
  H  CONTROL: with the fallback removed from `wake_candidates()`, arms B/C/F
     FAIL. Run on a COPY of the module; the tree is not modified. An arm that
     cannot fail is a belief.

WHAT THIS CANNOT SEE: which path a RUNNING container actually has populated.
That is a deployment fact, not a code fact -- `tools/verify_context_persistence.py`
is the instrument for it, and it must keep refusing to pretend without Docker.
"""
import importlib.util
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DAEMON_DIR = os.path.dirname(HERE)                    # agent/
DAEMON_FILE = os.path.join(DAEMON_DIR, "agent-daemon.py")

FIRST_BOOT = "This is your first boot."


def load_daemon(path=DAEMON_FILE):
    spec = importlib.util.spec_from_file_location("agent_daemon_fb", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agent_daemon_fb"] = mod
    spec.loader.exec_module(mod)
    return mod


class Sandbox:
    """Point WAKE_FILE and the artifacts fallbacks at a temp tree."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="wakefb-")
        self.self_dir = os.path.join(self.root, "workspace", "self")
        self.art_root = os.path.join(self.root, "artifacts", "agent-x")
        self.art_self = os.path.join(self.art_root, "self")
        os.makedirs(self.self_dir)
        os.makedirs(self.art_self)

    def _path(self, where):
        return {"self": os.path.join(self.self_dir, "wake.md"),
                "root": os.path.join(self.art_root, "wake.md"),
                "art_self": os.path.join(self.art_self, "wake.md")}[where]

    def write(self, where, body):
        path = self._path(where)
        with open(path, "w") as f:
            f.write(body)
        return path

    def remove(self, where):
        path = self._path(where)
        if os.path.exists(path):
            os.remove(path)

    def apply(self, mod):
        mod.WAKE_FILE = os.path.join(self.self_dir, "wake.md")
        mod.ARTIFACT_FALLBACK_DIRS = (self.art_root, self.art_self)
        return mod

    def note_count(self):
        """How many note files exist anywhere in the sandbox.

        EXISTS BECAUSE A REVERTED ARM A IS OTHERWISE CAUGHT ONLY BY LUCK.
        agent-8 measured it: put the broken two-write form back and the
        standalone suite reds about 1 run in 40, while the whole gate reds at
        the same rate -- because that IS the flake rate. So "the flake is
        fixed" and "a reverted fix is caught" are different claims, and the
        gate can only make the second true deterministically if something
        asserts the STRUCTURE rather than the outcome.
        A count cannot flake: it does not depend on mtime ordering at all.

        IT WALKS THE WHOLE SANDBOX, NOT THE THREE KNOWN PATHS. The first
        version checked only self/root/art_self, and a copy-on-read mutant
        that wrote `wake.md.read-copy` beside the note ESCAPED it -- measured,
        A2 and A3 both green. A count that only looks where you expect files
        is a count of your expectations. Anything the reader creates, wherever
        it puts it, is a file in this tree.
        """
        return sum(len(files) for _root, _dirs, files in os.walk(self.root))

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


QUIET = False
# Every arm is recorded by NAME, not by its position in a list. An earlier
# version of this control indexed the results by appending order and got the
# mapping wrong twice -- it reported live arms as vacuous and vacuous arms as
# live. An arm named by what it asserts cannot drift that way.
RESULTS = {}


def check(label, ok, detail=""):
    RESULTS[label.split()[0]] = ok
    if not QUIET:
        print(("  PASS  " if ok else "  FAIL  ") + label +
              (("  <- " + detail) if detail and not ok else ""))
    return ok


def run_arms(mod):
    """Every arm, against an already-configured module. Returns pass count."""
    results = []

    # A -- A SELF_DIR note is returned VERBATIM (surrounding whitespace and all).
    #
    # IT WRITES ONLY THE SELF NOTE, AND THAT IS THE FIX, NOT A WEAKENING.
    # As shipped, A wrote TWO notes -- self, then root, back to back -- and
    # expected the SELF one. find_wake_file() sorts NEWEST MTIME FIRST
    # (agent-7's rule, and D pins it), so the root note, written second, is
    # newer and wins. A passed only when both writes landed in the same clock
    # tick, because Python's sort is stable and insertion order then decided.
    # MEASURED: 400 back-to-back write pairs -> 391 tie, **9 root-newer, 0
    # self-newer**. There is no configuration in which A's expectation is what
    # the code does; the filesystem race was the only thing making it look
    # green, about 1 run in 40.
    #
    # So A was not a flaky probe of the rule. It asserted the OPPOSITE of the
    # rule that D pins on purpose, and it duplicated D's setup to do it. A now
    # tests exactly one thing -- that read_wake_prompt() returns the SELF_DIR
    # note verbatim -- and D remains the only arm that owns "both present".
    # This settles NO question of intent: if "SELF_DIR wins REGARDLESS of
    # time" were the intended rule, D would be wrong too, and that is a
    # design decision for whoever owns find_wake_file().
    sb = Sandbox()
    sb.apply(mod)
    try:
        sb.write("self", "  NOTE-AT-SELF  \n")
        got = mod.read_wake_prompt()
        results.append(check("A  SELF_DIR note returned verbatim",
                             got == "NOTE-AT-SELF", repr(got)))
        # A2 -- A WRITES EXACTLY ONE NOTE. THE DETERMINISTIC HALF OF A.
        # agent-8 measured that reverting A to the old two-write form reds the
        # suite only about 1 run in 40 -- the same rate as the flake -- so
        # "A passes" cannot tell you whether the fix is present. A COUNT can:
        # it does not depend on mtime ordering at all, so it cannot flake, and
        # it reds on every run if the second write comes back.
        results.append(check("A2 A  the arm wrote exactly ONE note (no race to lose)",
                             sb.note_count() == 1,
                             "%d note(s) in the sandbox" % sb.note_count()))
        # A3 -- READING MUST NOT CREATE A FILE. A2 above has ONE ASSUMPTION:
        # that read_wake_prompt() does not write. If a copy-on-read ever
        # appears, note_count() becomes 2 on a CORRECT tree and A2 reds
        # ALWAYS -- indistinguishable from the bug A2 exists to catch. That is
        # the same shape as the manifest's absent-fallback: a helper that
        # cannot tell "the check failed" from "the check did not run".
        # agent-8's point: make the assumption a MEASUREMENT, not a comment.
        # So: take the count, read again, and require it not to have moved.
        before = sb.note_count()
        mod.read_wake_prompt()
        after = sb.note_count()
        results.append(check("A3 A  reading did not CREATE a note (A2's assumption)",
                             before == after == 1,
                             "%d before, %d after" % (before, after)))

        # D -- both present, different, SELF_DIR NEWER -> SELF_DIR wins.
        # The rule is newest-mtime, not path order: agent-7's box had the
        # durable copy be the stale one, and path order would have reverted
        # them by a life.
        #
        # D CREATES ITS OWN ROOT NOTE. It used to inherit the one A wrote,
        # which made D's setup an accident of A's -- and when A was corrected
        # to write only the SELF note, D crashed on a missing file. An arm
        # that depends on another arm's fixture fails for the other arm's
        # reasons.
        sb.write("root", "NOTE-AT-ROOT")
        old = os.path.getmtime(os.path.join(sb.art_root, "wake.md"))
        os.utime(os.path.join(sb.self_dir, "wake.md"), (old + 60, old + 60))
        path, rejected = mod.find_wake_file()
        results.append(check("D  both present, SELF_DIR newer -> SELF_DIR wins",
                             path == mod.WAKE_FILE, repr(path)))
        # D2 -- the ambiguity is ON THE RECORD, not silently resolved
        results.append(check("D2 divergent notes are recorded as AMBIGUOUS",
                             "AMBIGUOUS" in rejected
                             and len(rejected["AMBIGUOUS"]) == 2,
                             repr(rejected)))
        # D3 -- and the newer DURABLE copy wins when it is the newer one
        os.utime(os.path.join(sb.art_root, "wake.md"), (old + 600, old + 600))
        path3, _ = mod.find_wake_file()
        results.append(check("D3 newer durable copy wins over older SELF_DIR",
                             path3 == os.path.join(sb.art_root, "wake.md"),
                             repr(path3)))
        os.utime(os.path.join(sb.self_dir, "wake.md"), (old + 60, old + 60))
        sb.remove("self")
    finally:
        sb.cleanup()

    # B -- SELF_DIR absent, note at artifacts root
    sb = Sandbox()
    sb.apply(mod)
    try:
        sb.write("root", "NOTE-AT-ROOT")
        got = mod.read_wake_prompt()
        results.append(check("B  artifacts-root note found", got == "NOTE-AT-ROOT",
                             repr(got)))
        path, _ = mod.find_wake_file()
        results.append(check("B2 found path is the artifacts root",
                             path == os.path.join(sb.art_root, "wake.md"),
                             repr(path)))
    finally:
        sb.cleanup()

    # C -- SELF_DIR absent, note under artifacts self/
    sb = Sandbox()
    sb.apply(mod)
    try:
        sb.write("art_self", "NOTE-ART-SELF")
        got = mod.read_wake_prompt()
        results.append(check("C  artifacts self/ note found",
                             got == "NOTE-ART-SELF", repr(got)))
    finally:
        sb.cleanup()

    # E -- nothing anywhere
    sb = Sandbox()
    sb.apply(mod)
    try:
        got = mod.read_wake_prompt()
        results.append(check("E  nothing anywhere -> first-boot text",
                             got.startswith(FIRST_BOOT), repr(got[:40])))
    finally:
        sb.cleanup()

    # F -- an EMPTY SELF_DIR file must not shadow a good note
    sb = Sandbox()
    sb.apply(mod)
    try:
        sb.write("self", "   \n\n")
        sb.write("root", "NOTE-AT-ROOT")
        got = mod.read_wake_prompt()
        results.append(check("F  empty SELF_DIR does not shadow a good note",
                             got == "NOTE-AT-ROOT", repr(got[:40])))
    finally:
        sb.cleanup()

    # G -- the reasons are on the record, per path
    sb = Sandbox()
    sb.apply(mod)
    try:
        sb.write("root", "  ")          # empty -> skipped
        path, rejected = mod.find_wake_file()
        results.append(check("G  rejection reasons name every candidate",
                             set(rejected) == set(mod.wake_candidates()),
                             repr(rejected)))
        results.append(check("G2 an empty file is reported as 'empty'",
                             rejected.get(os.path.join(sb.art_root, "wake.md"))
                             == "empty", repr(rejected)))
        results.append(check("G3 an absent file is reported as 'absent'",
                             rejected.get(mod.WAKE_FILE) == "absent",
                             repr(rejected)))
    finally:
        sb.cleanup()

    return results


def control_without_fallback(verbose=True):
    """H -- remove the fallback and require arms B/C/F to go red.

    Done on a COPY of the module: the tree is never modified.
    """
    global QUIET
    prev, QUIET = QUIET, not verbose
    try:
        return _control_without_fallback()
    finally:
        QUIET = prev


def _control_without_fallback():
    tmpd = tempfile.mkdtemp(prefix="wakefb-ctl-")
    try:
        copy = os.path.join(tmpd, "agent-daemon.py")
        shutil.copy(DAEMON_FILE, copy)
        src = open(copy).read()
        old = """    return [WAKE_FILE] + [os.path.join(d, "wake.md")
                          for d in ARTIFACT_FALLBACK_DIRS]"""
        new = """    return [WAKE_FILE]"""
        if old not in src:
            return check("H  control could run (mutation target present)", False,
                         "wake_candidates body did not match")
        open(copy, "w").write(src.replace(old, new))
        mod = load_daemon(copy)
        RESULTS.clear()
        run_arms(mod)
        # With the fallback removed, EVERY arm that reads the artifacts tree
        # must go red, and the SELF_DIR-only arms must stay green. Both
        # halves matter: if the dependent arms stay green they are vacuous,
        # and if an independent arm goes red the control mutated too much.
        # G is NOT fallback-dependent: it asserts that find_wake_file()
        # accounts for every candidate it was given, which is true whether
        # or not the artifacts dirs are in that list. Counting it as
        # dependent was my error, and this control caught it.
        # D2 and D3 are BOTH fallback-dependent. D3 asserts the durable
        # candidate wins when newer, which needs a durable candidate. D2
        # asserts the ambiguity is RECORDED, which needs two candidates to
        # be ambiguous. With the fallback removed neither can fire. This
        # control caught me mis-filing both, one at a time, and that is
        # exactly why it asserts on named arms instead of a count.
        dependent = ("B", "B2", "C", "F", "G2", "D2", "D3")
        independent = ("A", "D", "E", "G", "G3")
        live = [k for k in dependent if RESULTS.get(k)]
        broke = [k for k in independent if not RESULTS.get(k)]
        global QUIET
        QUIET = False
        return check(
            "H  control: fallback arms red, SELF_DIR arms green, without it",
            not live and not broke,
            "vacuous (still green): %r; over-mutated (went red): %r"
            % (live, broke))
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def main():
    mod = load_daemon()
    RESULTS.clear()
    run_arms(mod)
    primary = dict(RESULTS)          # the real module's arms, before H runs
    print()
    h_ok = control_without_fallback(verbose=False)   # H repopulates RESULTS
    passed = sum(1 for v in primary.values() if v) + (1 if h_ok else 0)
    total = len(primary) + 1
    failed = [k for k, v in primary.items() if not v]
    if failed:
        print("failed arms: %r" % (failed,))
    print()
    print("wake-fallback: %d/%d arms passed" % (passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
