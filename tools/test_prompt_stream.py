#!/usr/bin/env python3
"""Execute the SHIPPED prompt_stream in its four start-up cases.

Stanley defect 3 is "a restarted agent will not have any response unless
someone messages it". agent-10 showed the resumed-session case; testing it
turned up a second, in the SAME function: mail that was already in
`pending_delivery` when the stream started made the old `elif not
pending_delivery` skip the kick, while the queue is drained only by a
"__kick__" marker -- so the stream yielded nothing and those items waited.
Both are now one rule: THE STREAM YIELDS EXACTLY ONCE AT START-UP, ALWAYS.

Run:  python3 tools/test_prompt_stream.py [path/to/agent-daemon.py]
Exit: 0 all four cases yield a first turn, 1 one did not (a mute life).

This extracts the real function body with AST, so it measures the shipped
logic rather than a copy of it.

Extracted from agent/agent-daemon.py by AST (the real body, not a copy), so a
change to the shipped logic changes what this measures. The point: prove that
a RESUMED life emits a turn, because a resumed session that emits nothing is
Stanley's defect 3 ('an agent newly restarted will not have any response').
"""
import ast
import asyncio
import sys

DAEMON = sys.argv[1] if len(sys.argv) > 1 else "agent/agent-daemon.py"
src = open(DAEMON, encoding="utf-8").read()
tree = ast.parse(src)
fn = next(n for n in ast.walk(tree)
          if isinstance(n, ast.AsyncFunctionDef) and n.name == "prompt_stream")

import textwrap
_body = ast.unparse(ast.Module(body=fn.body, type_ignores=[]))
harness = ("async def _ps(resumed, pending_delivery, inbox_next, "
           "opening_message, drain_pending_delivery, wake_prompt=None):\n"
           + textwrap.indent(_body, "    "))
ns = {}
exec(harness, {"__builtins__": __builtins__}, ns)


class Q:
    def __init__(self, items):
        self.items = list(items)

    async def get(self):
        return self.items.pop(0) if self.items else None


async def first_yields(resumed, pending, items, n=1):
    # drain_pending_delivery must mirror the REAL contract: return the queued
    # text and CLEAR it. A stub that always returns "MAIL" makes every case
    # look identical -- which is how this harness first fooled me.
    box = {"v": "MAIL" if pending else None}

    def drain():
        v = box["v"]
        box["v"] = None
        return v

    out = []
    gen = ns["_ps"](resumed, pending, Q(items),
                    lambda w: "OPENING", drain, "WAKE")
    async for item in gen:
        out.append(item["message"]["content"])
        if len(out) >= n:
            break
    return out


CASES = [
    # resumed, pending, inbox items, label, expect_a_turn
    (False, [], [], "fresh life, no mail", True),
    (True, [], [], "RESUMED, no mail (the defect)", True),
    (True, ["queued"], [], "resumed, mail already queued", True),
    (True, [], ["late"], "resumed, mail arrives at a boundary", True),
]

ok = True
for resumed, pending, items, label, expect in CASES:
    got = asyncio.run(first_yields(resumed, pending, items))
    passed = bool(got) == expect
    ok = ok and passed
    preview = (got[0][:44].replace("\n", " ") if got else "<NOTHING>")
    print("  %-38s %s  first=%r"
          % (label, "PASS" if passed else "FAIL", preview))

print("\n%s" % ("PROMPT-STREAM PASS" if ok else "PROMPT-STREAM FAIL"))
sys.exit(0 if ok else 1)
