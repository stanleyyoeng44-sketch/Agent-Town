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
import sys
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


def main():
    body = extract(DAEMON)
    shipped_ok = run_cases(compile_stream(body), "A. SHIPPED prompt_stream")

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
    print("  control fired (resume kick lost the prompt): %s" % strip_caught)
    print("  control fired (pre-fix shape went mute): %s"
          % ("PASS" if control_fired else "FAIL -- this test is blind"))
    good = shipped_ok and control_fired and strip_caught
    print("\n%s" % ("PROMPT-STREAM PASS" if good else "PROMPT-STREAM FAIL"))
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
