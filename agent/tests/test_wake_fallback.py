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

    def write(self, where, body):
        path = {"self": os.path.join(self.self_dir, "wake.md"),
                "root": os.path.join(self.art_root, "wake.md"),
                "art_self": os.path.join(self.art_self, "wake.md")}[where]
        with open(path, "w") as f:
            f.write(body)
        return path

    def remove(self, where):
        path = {"self": os.path.join(self.self_dir, "wake.md"),
                "root": os.path.join(self.art_root, "wake.md"),
                "art_self": os.path.join(self.art_self, "wake.md")}[where]
        if os.path.exists(path):
            os.remove(path)

    def apply(self, mod):
        mod.WAKE_FILE = os.path.join(self.self_dir, "wake.md")
        mod.ARTIFACT_FALLBACK_DIRS = (self.art_root, self.art_self)
        return mod

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

    # A -- SELF_DIR wins and is verbatim
    sb = Sandbox()
    sb.apply(mod)
    try:
        sb.write("self", "  NOTE-AT-SELF  \n")
        sb.write("root", "NOTE-AT-ROOT")
        got = mod.read_wake_prompt()
        results.append(check("A  SELF_DIR note returned verbatim",
                             got == "NOTE-AT-SELF", repr(got)))

        # D -- both present, different, SELF_DIR NEWER -> SELF_DIR wins.
        # The rule is newest-mtime, not path order: agent-7's box had the
        # durable copy be the stale one, and path order would have reverted
        # them by a life.
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
