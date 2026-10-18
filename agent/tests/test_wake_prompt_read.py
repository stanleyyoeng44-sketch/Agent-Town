#!/usr/bin/env python3
"""test_wake_prompt_read.py -- the daemon must READ the agent's wake.md, not a stub.

Run:  python3 agent/tests/test_wake_prompt_read.py

THE GAP THIS CLOSES, measured by agent-6 on 2026-10-18 and reproduced here.
`read_wake_prompt()` is what makes "a restarted agent responds without being
messaged" true: on resume the daemon puts the note the last life wrote into
the kick. It has TWO call sites in the whole repo and neither is a test that
exercises it for real:

    agent/agent-daemon.py:558   wake_prompt = read_wake_prompt()   (the real one)
    agent/tests/test_idle_proof.py:112   D.read_wake_prompt = lambda: ...  (a STUB)

So the only test that touches it replaces it. And tools/test_prompt_stream.py
cannot see the difference by construction: its harness SUPPLIES the prompt
(compile_stream binds wake_prompt=None, run_case passes the literal "WAKE"),
so it proves prompt_stream TRANSMITS a prompt it is handed -- not that the
daemon HANDS IT the real one.

Measured consequence, one line, nothing else touched:
    agent/agent-daemon.py:558   wake_prompt = read_wake_prompt()  ->  wake_prompt = ""
    python3 tools/test_prompt_stream.py   ->   PROMPT-STREAM PASS, exit 0
A real agent would wake to "Your wake-up prompt to yourself:" and nothing
after it: silent, plausible, green.

WHAT THIS TEST ASSERTS, and each arm can fail on its own:
  A  a wake.md with content is returned VERBATIM (stripped)
  B  an EMPTY wake.md falls back to the first-boot text, NOT to ""
  C  a MISSING wake.md falls back to the first-boot text
  D  the returned value is the FILE's content, not the fallback -- proved by
     a second file whose content differs from the first and from the fallback
  E  the daemon's call site actually assigns the result (AST): line 558's
     statement is `wake_prompt = read_wake_prompt()`, so a mutation that
     replaces the CALL with a constant is visible here even though
     prompt_stream's suite stays green.
  F  CONTROL: mutate the source so read_wake_prompt returns "" always, and
     require THIS test to fail. Done on a COPY of the module; the tree is not
     modified. A test that cannot fail is a belief.

WHY A TEST AND NOT A MOCK: mocks are how the gap got here. This calls the
real function from the real file.

WHAT THIS CANNOT SEE: the file's path is patched to a temp dir, so it does not
prove the daemon's WAKE_FILE points at /workspace/self/wake.md -- that is a
constant, asserted separately in arm G by reading the source text. It also
does not run a life; prompt_stream's own suite covers transmission.
"""
import ast
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DAEMON_DIR = os.path.dirname(HERE)                    # agent/
DAEMON_FILE = os.path.join(DAEMON_DIR, "agent-daemon.py")

FIRST_BOOT = "This is your first boot."


def load_daemon():
    spec = importlib.util.spec_from_file_location("agent_daemon_wake", DAEMON_FILE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agent_daemon_wake"] = mod
    spec.loader.exec_module(mod)
    return mod


def with_wake_file(mod, path):
    """Point the module's WAKE_FILE at `path` for one arm."""
    mod.WAKE_FILE = path


def main():
    checks = []

    def check(name, cond, detail=""):
        checks.append((name, bool(cond), detail))

    try:
        mod = load_daemon()
    except Exception as e:
        print("CANNOT MEASURE: importing the daemon failed: %r" % (e,))
        return 2

    tmp = tempfile.mkdtemp()
    original = mod.WAKE_FILE

    # A: content is returned verbatim, stripped.
    p1 = os.path.join(tmp, "wake-a.md")
    body = "Read the diary first.\nThen run the two checks.\n"
    open(p1, "w").write(body)
    with_wake_file(mod, p1)
    got = mod.read_wake_prompt()
    check("A wake.md content is returned verbatim", got == body.strip(),
          "got %r" % got[:60])

    # B: EMPTY file falls back, does not return "".
    p2 = os.path.join(tmp, "wake-b.md")
    open(p2, "w").write("   \n\n")
    with_wake_file(mod, p2)
    got = mod.read_wake_prompt()
    check("B empty wake.md falls back (not empty string)",
          got.startswith(FIRST_BOOT) and got.strip() != "", "got %r" % got[:60])

    # C: missing file falls back.
    with_wake_file(mod, os.path.join(tmp, "does-not-exist.md"))
    got = mod.read_wake_prompt()
    check("C missing wake.md falls back", got.startswith(FIRST_BOOT),
          "got %r" % got[:60])

    # D: the return tracks the FILE, not a constant. Two different bodies.
    p3 = os.path.join(tmp, "wake-c.md")
    open(p3, "w").write("SECOND BODY")
    with_wake_file(mod, p3)
    d1 = mod.read_wake_prompt()
    open(p3, "w").write("THIRD BODY")
    d2 = mod.read_wake_prompt()
    check("D the value follows the file's content",
          d1 == "SECOND BODY" and d2 == "THIRD BODY",
          "d1=%r d2=%r" % (d1[:20], d2[:20]))

    with_wake_file(mod, original)

    # E: the daemon's call site ASSIGNS the call (AST, so a constant
    # substitution is visible even when prompt_stream's suite stays green).
    src = open(DAEMON_FILE, encoding="utf-8").read()
    tree = ast.parse(src)
    assigns_call = False
    call_line = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "read_wake_prompt"):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "wake_prompt":
                    assigns_call = True
                    call_line = node.lineno
    check("E `wake_prompt = read_wake_prompt()` exists in the daemon",
          assigns_call, "found at line %s" % call_line)

    # G: WAKE_FILE is under /workspace/self, so the agent's own note is read.
    check("G WAKE_FILE points at /workspace/self/wake.md",
          "WAKE_FILE = os.path.join(SELF_DIR, \"wake.md\")" in src
          and 'SELF_DIR = os.path.join(WORKSPACE, "self")' in src,
          "")

    # F: CONTROL -- a module whose read_wake_prompt returns "" must FAIL arm A.
    mutant_src = src.replace(
        "    if os.path.exists(WAKE_FILE):\n"
        "        with open(WAKE_FILE) as f:\n"
        "            content = f.read().strip()\n"
        "        if content:\n"
        "            return content\n",
        "    return \"\"\n", 1)
    control_ok = False
    control_detail = "anchor not found -- INCONCLUSIVE"
    if mutant_src != src:
        mp = os.path.join(tmp, "agent-daemon-mutant.py")
        open(mp, "w").write(mutant_src)
        try:
            mspec = importlib.util.spec_from_file_location("daemon_mut", mp)
            mmod = importlib.util.module_from_spec(mspec)
            sys.modules["daemon_mut"] = mmod
            mspec.loader.exec_module(mmod)
            mmod.WAKE_FILE = p1
            control_ok = (mmod.read_wake_prompt() != body.strip())
            control_detail = "mutant returned %r" % mmod.read_wake_prompt()[:30]
        except Exception as e:
            control_detail = "mutant failed to load: %r" % (e,)
    check("F CONTROL: a stubbed read_wake_prompt FAILS arm A",
          control_ok, control_detail)

    ok = True
    for name, cond, detail in checks:
        print("  %s  %s%s" % ("PASS" if cond else "FAIL", name,
                              ("  (" + detail + ")") if detail and not cond else ""))
        ok &= cond

    # Arm F is inverted: its PASS means the control FIRED. If it could not be
    # built, that is an ERROR, not a pass.
    if not control_ok and "INCONCLUSIVE" in control_detail:
        print("  CONTROL ERROR: the mutation anchor is gone; update this test")
        ok = False

    print("\nWAKE-PROMPT-READ %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
