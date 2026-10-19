#!/usr/bin/env python3
"""A life must always emit a first turn; and this test must be able to notice
when it does not.

Stanley defect 3: 'an agent newly restarted will not have any response unless
someone messages it.' Two separate silences live in prompt_stream():

  * a RESUMED session yielded nothing (agent-10), and
  * mail already queued in `pending_delivery` when the stream started made the
    old `elif not pending_delivery` skip the kick, while the queue is drained
    only by a "__kick__" marker (found by writing this test).

This file has two halves, and the second is what makes the first trustworthy:

  A. CASES   -- run the SHIPPED prompt_stream, extracted by AST from the file
               under test, in four start-up states. Every one must yield a
               first turn.
  B. CONTROL -- mutate the SHIPPED body back to the pre-fix shape, run the
               same cases, and REQUIRE the prequeued case to fail. If the
               mutation does not make it fail, the test is blind and exits 1.

Extracting by AST matters: a test written against a copy of the function
drifts from the file it claims to test. That is the defect this cohort keeps
shipping, so this instrument avoids it by construction.

Run:  python3 tools/test_prompt_stream.py [path/to/agent-daemon.py]
Exit: 0 shipped passes AND the control fires; 1 either half failed.
"""
import ast
import asyncio
import os
import sys
import tempfile
import textwrap

DAEMON = sys.argv[1] if len(sys.argv) > 1 else "agent/agent-daemon.py"

CASES = [
    # resumed, pending, inbox items, label, must_yield_a_turn
    (False, [], [], "fresh life, no mail", True),
    (True, [], [], "resumed, no mail", True),
    (True, ["queued"], [], "resumed, mail already queued", True),
    (True, [], ["late"], "resumed, mail arrives at a boundary", True),
]

# Cases where the FIRST TURN MUST CONTAIN the wake-up prompt itself.
# The shipped test asserted only bool(first) -- that a turn exists. Stanley's
# commit made "the resume path sends the wake-up prompt" its headline claim,
# and two mutants that DELETE the prompt from the kick passed the old suite:
#   wake_prompt body -> "(omitted)"        : PASS
#   wake_prompt body removed entirely      : PASS
# A turn that opens the session but carries no plan is not the behaviour the
# commit claims, and only a content assertion can tell the two apart.
MUST_CARRY_WAKE = {
    # The activation path THIS commit added. The fresh-life case is not in
    # this set because the harness stubs opening_message() with a constant,
    # so a missing prompt there would be the stub's doing, not the code's.
    "resumed, no mail",
}


def extract(path):
    """The shipped prompt_stream body, as an unparsed source string."""
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.AsyncFunctionDef)
               and n.name == "prompt_stream"), None)
    if fn is None:
        raise SystemExit("FAIL: no prompt_stream in %s" % path)
    return ast.unparse(ast.Module(body=fn.body, type_ignores=[]))


def compile_stream(body):
    harness = ("async def _ps(resumed, pending_delivery, inbox_next, "
               "opening_message, drain_pending_delivery, wake_prompt=None):\n"
               + textwrap.indent(body, "    "))
    ns = {}
    exec(harness, {"__builtins__": __builtins__}, ns)
    return ns["_ps"]


class Q:
    def __init__(self, items):
        self.items = list(items)

    async def get(self):
        return self.items.pop(0) if self.items else None


def run_case(fn, resumed, pending, items):
    """First yielded turn, or None. The drain stub mirrors the real contract:
    return the queued text and CLEAR it. (A stub that always returns text made
    all four cases look identical the first time this was written.)"""
    box = {"v": "MAIL" if pending else None}

    def drain():
        v = box["v"]
        box["v"] = None
        return v

    async def go():
        out = []
        async for item in fn(resumed, pending, Q(items),
                             lambda w: "OPENING", drain, "WAKE"):
            out.append(item["message"]["content"])
            break
        return out

    got = asyncio.run(go())
    return got[0] if got else None


def run_cases(fn, label):
    ok = True
    print(label)
    for resumed, pending, items, name, must in CASES:
        first = run_case(fn, resumed, pending, items)
        passed = bool(first) == must
        if passed and name in MUST_CARRY_WAKE:
            passed = "WAKE" in (first or "")
            if not passed:
                print("  %-5s %-36s first=%r  (does NOT carry the wake "
                      "prompt)" % ("FAIL", name, (first or "")[:40]))
                ok = False
                continue
        print("  %-5s %-36s first=%r"
              % ("PASS" if passed else "FAIL", name,
                 (first or "<NOTHING>")[:40].replace("\n", " ")))
    return ok


def strip_wake_from_kick(body):
    """Remove f-string interpolation of wake_prompt from the RESUME kick.

    This is the mutant that the old assertions could not see: the turn is
    still yielded (bool(first) is True), but it no longer carries the plan.
    Built from the parsed AST so it cannot miss on a whitespace change, and
    it REFUSES to return a no-op mutation -- a control that did not mutate is
    not a control.
    """
    def visit(src):
        tree = ast.parse(src)
        hit = [False]
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                keep = []
                for v in node.values:
                    if isinstance(v, ast.FormattedValue) and \
                            isinstance(v.value, ast.Name) and \
                            v.value.id == "wake_prompt":
                        hit[0] = True
                        keep.append(ast.Constant(value="[PROMPT-REMOVED]"))
                    else:
                        keep.append(v)
                node.values = keep
        return ast.unparse(tree), hit[0]

    src = "async def _x():\n" + textwrap.indent(body, "    ")
    out, changed = visit(src)
    if not changed:
        raise SystemExit("FAIL: could not find wake_prompt in the kick; the "
                         "control would be a no-op")
    # strip the synthetic wrapper back off
    tree = ast.parse(out)
    return "\n".join(ast.unparse(st) for st in tree.body[0].body)


def pre_fix_body(body):
    """Rebuild the PRE-FIX start-up shape from the SHIPPED body, structurally.

    Pre-fix, prompt_stream was:

        if not resumed:            yield opening
        elif not pending_delivery: yield kick
        while True:                ...   # drains only on '__kick__'

    The defect: nothing drained `pending_delivery` before the receive loop, so
    mail queued at start-up produced no first turn. This reconstructs exactly
    that by replacing the leading statements of the real function with a call
    to a helper that has the old shape -- so the mutation is a semantic one,
    not a text edit that can miss.

    Why not text surgery: my first attempt set `prequeued = None`, which still
    fell through to the resume kick and left the case passing. The control
    reported itself blind, which is precisely what a control is for. A second
    attempt at line-based removal produced invalid indentation. Building the
    pre-fix shape from the parsed statements is the version that cannot drift.
    """
    tree = ast.parse("async def _x():\n" + textwrap.indent(body, "    "))
    fn = tree.body[0]
    stmts = fn.body
    # Find the `while True:` receive loop; everything before it is the
    # start-up block we are replacing.
    loop_idx = next((i for i, st in enumerate(stmts)
                     if isinstance(st, ast.While)), None)
    if loop_idx is None:
        raise SystemExit("FAIL: no receive loop found in prompt_stream")
    resume_branch = stmts[loop_idx - 1]   # the `else:` kick (pre-fix `elif`)
    if not isinstance(resume_branch, ast.If):
        raise SystemExit("FAIL: unexpected start-up shape; update the control")
    opening = ("{'type': 'user', 'message': {'role': 'user', 'content': "
               "opening_message(wake_prompt)}}")
    kick = ("{'type': 'user', 'message': {'role': 'user', 'content': "
            "'RESUME-NOTE'}}")
    pre = (
        "if not resumed:\n"
        "    yield %s\n"
        "elif not pending_delivery:\n"
        "    yield %s\n" % (opening, kick)
    )
    tail = "\n".join(ast.unparse(st) for st in stmts[loop_idx:])
    mutated = pre + tail + "\n"
    if "drain_pending_delivery()" not in mutated:
        raise SystemExit("FAIL: control lost the in-loop drain")
    if mutated.strip() == body.strip():
        raise SystemExit("FAIL: control mutation was a no-op")
    return mutated


# ---------------------------------------------------------------------------
# D. THE DELIVERY ARM -- does the daemon HAND the stream the REAL wake prompt?
#
# WHY THIS EXISTS (agent-6, measured): the A/B/C sections above prove
# prompt_stream TRANSMITS whatever wake_prompt it is given -- the harness
# binds the literal "WAKE" and checks the string comes out. They cannot prove
# the daemon HANDS IT THE REAL ONE. One line, nothing else touched:
#
#     agent/agent-daemon.py:558   wake_prompt = read_wake_prompt()
#                          ->     wake_prompt = ""
#     tools/test_prompt_stream.py -> A 4/4 PASS, exit 0
#
# Every agent would wake to "Your wake-up prompt to yourself:" followed by
# nothing. That is Stanley's headline claim (the agent wakes holding the note
# its last life wrote) delivered as an empty string, with the suite green.
#
# The chain has two links and this section measures both:
#   D1  read_wake_prompt RETURNS the note, for all three file states
#   D2  the variable prompt_stream interpolates is ASSIGNED FROM that reader
# Neither link alone is enough: D2 without D1 pins a call to a broken reader;
# D1 without D2 pins a working reader nobody calls.
# ---------------------------------------------------------------------------

def extract_function(path, name):
    """Pull one top-level function out of the shipped file, unparsed."""
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == name), None)
    if fn is None:
        raise SystemExit("FAIL: no %s in %s" % (name, path))
    return ast.unparse(ast.Module(body=[fn], type_ignores=[])), tree


# The shipped read_wake_prompt is no longer self-contained: it delegates to
# find_wake_file(), which walks wake_candidates() over ARTIFACT_FALLBACK_DIRS.
# Extracting just the one function put it in a namespace that had none of
# them, and the arm died with NameError -- a harness that carried the
# function but not its dependencies. These are the names it needs, taken from
# the shipped file by the same AST extraction so they cannot drift from it.
WAKE_HELPERS = ("wake_candidates", "find_wake_file", "read_wake_prompt")


def wake_reader_ns(wake_path):
    """A namespace with the SHIPPED reader plus everything it calls."""
    ns = {"os": os, "str": str, "list": list, "tuple": tuple,
          "WAKE_FILE": wake_path, "ARTIFACT_FALLBACK_DIRS": ()}
    for name in WAKE_HELPERS:
        body, _ = extract_function(DAEMON, name)
        exec(compile(body, "<%s>" % name, "exec"), ns)
    return ns


def d1_reader_returns_the_note():
    """Run the SHIPPED read_wake_prompt against real files on disk."""
    work = tempfile.mkdtemp()
    wake = os.path.join(work, "wake.md")
    ns = wake_reader_ns(wake)
    read = ns["read_wake_prompt"]

    ok = True
    BODY = "# wake\nDO THESE THREE THINGS."
    FIRST_BODY = "# wake\nFIRST BODY"
    SECOND_BODY = "# wake\nSECOND BODY"

    open(wake, "w").write(BODY)
    got = read()
    hit = (got.strip() == BODY.strip())
    ok = ok and hit
    print("  %-5s a real wake.md comes back verbatim          got=%r"
          % ("PASS" if hit else "FAIL", got[:36]))

    os.remove(wake)
    got = read()
    hit = "first boot" in got.lower()
    ok = ok and hit
    print("  %-5s no wake.md gives the FIRST-BOOT text        got=%r"
          % ("PASS" if hit else "FAIL", got[:36]))

    open(wake, "w").write("   \n\n")
    got = read()
    hit = "first boot" in got.lower()
    ok = ok and hit
    print("  %-5s a whitespace-only wake.md is NOT a plan     got=%r"
          % ("PASS" if hit else "FAIL", got[:36]))

    # D1d: the value must FOLLOW THE FILE across two reads.
    # MEASURED GAP, found by agent-7 reviewing this pair: the three arms above
    # all read the file at most once per state, so a reader that CACHED its
    # first result passes all of them. Verified rather than reasoned about: a
    # reader carrying a first-read memo leaves the three arms green and
    # returns FIRST BODY on the second read. Two different bodies, so a
    # constant cannot pass either.
    #
    # SCOPE, corrected by agent-1's review: this arm covers IN-FUNCTION
    # caching (a memo/global declared inside read_wake_prompt). It does NOT
    # cover a MODULE-LEVEL cache -- D1 extracts the function with ast and
    # execs it in a fresh namespace, so a module-level name is a NameError
    # there, and the mutant dies with a traceback instead of a clean arm
    # failure. That is not a hole in the assertion (the shipped function has
    # no module-level cache to lose) but it is a limit of the harness, and my
    # first commit message described the C1 mutant as if the new arm caught
    # it. It does not: C1 fails the two FALLBACK arms, because a cache poisons
    # every file state, not because the re-read arm fired. Stated here so the
    # next reader does not over-read this arm.
    open(wake, "w").write("# wake\nFIRST BODY")
    first = read()
    open(wake, "w").write("# wake\nSECOND BODY")
    second = read()
    hit = (first.strip() == FIRST_BODY and second.strip() == SECOND_BODY)
    ok = ok and hit
    print("  %-5s the value FOLLOWS the file across two reads got=%r -> %r"
          % ("PASS" if hit else "FAIL", first[:12], second[:12]))
    return ok


def d2_stream_is_handed_the_reader():
    """The variable the stream interpolates must BE read_wake_prompt().

    Structural on purpose: driving live_one_life needs the SDK, and a fake
    client would prove the fake, not the daemon. What can fail here is exact:
    replacing the call with a literal (&quot;", None, a constant) leaves the
    stream interpolating something that is not the note.
    """
    _, tree = extract_function(DAEMON, "read_wake_prompt")   # parse once
    live = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.AsyncFunctionDef)
                 and n.name == "live_one_life"), None)
    if live is None:
        raise SystemExit("FAIL: no live_one_life in %s" % DAEMON)

    # 1. an assignment `wake_prompt = <call>` where the call is read_wake_prompt
    assigned_from_reader = False
    for node in ast.walk(live):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not any(isinstance(t, ast.Name) and t.id == "wake_prompt"
                       for t in targets):
                continue
            v = node.value
            if (isinstance(v, ast.Call) and isinstance(v.func, ast.Name)
                    and v.func.id == "read_wake_prompt"):
                assigned_from_reader = True

    # 2. every JoinedStr in the function that interpolates `wake_prompt`
    interpolated = []
    for node in ast.walk(live):
        if isinstance(node, ast.JoinedStr):
            for v in node.values:
                if (isinstance(v, ast.FormattedValue)
                        and isinstance(v.value, ast.Name)
                        and v.value.id == "wake_prompt"):
                    interpolated.append(node)

    ok = True
    print("  %-5s wake_prompt is ASSIGNED FROM read_wake_prompt()"
          % ("PASS" if assigned_from_reader else "FAIL"))
    ok = ok and assigned_from_reader
    hit = len(interpolated) >= 1
    print("  %-5s the stream INTERPOLATES wake_prompt (%d site(s))"
          % ("PASS" if hit else "FAIL", len(interpolated)))
    ok = ok and hit
    return ok


def main():
    body = extract(DAEMON)
    shipped_ok = run_cases(compile_stream(body), "A. SHIPPED prompt_stream")

    print("\nD. DELIVERY -- does the daemon hand the stream the REAL note?")
    d1_ok = d1_reader_returns_the_note()
    d2_ok = d2_stream_is_handed_the_reader()
    delivery_ok = d1_ok and d2_ok

    stripped = strip_wake_from_kick(body)
    stripped_ok = run_cases(compile_stream(stripped),
                            "\nC. CONTROL -- a resume kick that DROPS the "
                            "wake prompt MUST fail")
    # Inverted, like the pre-fix control: the mutant must be caught.
    strip_caught = not stripped_ok

    mutated = pre_fix_body(body)
    control_ok = run_cases(compile_stream(mutated),
                           "\nB. CONTROL -- the pre-fix shape MUST go mute "
                           "on queued mail")
    # The control's verdict is inverted: the pre-fix body must FAIL. If it
    # passes, the mutation did not restore the defect and this test is blind.
    control_fired = not control_ok

    print()
    print("  delivery chain intact (reader returns, stream uses it): %s"
          % delivery_ok)
    print("  control fired (resume kick lost the prompt): %s" % strip_caught)
    print("  control fired (pre-fix shape went mute): %s"
          % ("PASS" if control_fired else "FAIL -- this test is blind"))
    good = shipped_ok and control_fired and strip_caught and delivery_ok
    print("\n%s" % ("PROMPT-STREAM PASS" if good else "PROMPT-STREAM FAIL"))
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
