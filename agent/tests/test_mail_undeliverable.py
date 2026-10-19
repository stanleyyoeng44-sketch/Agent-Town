"""Does a message that cannot be delivered leave a record, or vanish?

THE DEFECT THIS EXISTS FOR, measured 2026-10-19: 26 undeliverable files were
sitting in 10 of 10 inboxes. feed_chat() removed each file BEFORE checking it
could be used, so:
  - a file that failed json.load was unlinked? no -- it stayed, and was
    re-read every 3 seconds forever, with nothing logged;
  - a file that parsed but had empty text WAS unlinked, and never logged.
Both outcomes are silent, and silence is indistinguishable from "no message
arrived". That is the property under test: EVERY file either reaches the
delivery queue or leaves a log line naming why it did not. There is no third
outcome, and "the file is still there" is not an outcome.

THE ARMS, and why each one is here:
  P1..P4  the classifier names each malformed SHAPE separately, because the
          owners differ (three are writer bugs, one is a schema this reader
          does not know). A single "bad" reason would fix none of them.
  P5      VALID input classifies as None -- without this, a classifier that
          returns a reason for everything passes P1-P4 and delivers nothing.
  P6      quarantine RENAMES in place, so the *.msg glob cannot pick it up
          again, and the bytes survive.
  P7      a failed quarantine still returns None rather than raising: it must
          be a loggable outcome, not a crash that kills the poller.
  M1      the mutation: drop the `if reason is not None` branch (treat
          everything as deliverable) and the SHIPPED defect must reappear --
          an undeliverable file is unlinked without a record. If M1 passes,
          the arms above are measuring the test, not the code.
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DAEMON = os.path.join(HERE, "..", "agent-daemon.py")


def _load(path=DAEMON):
    """Import the daemon module without running main()."""
    spec = importlib.util.spec_from_file_location("a2daemon", path)
    m = importlib.util.module_from_spec(spec)
    # the daemon reads config at import; stub the SDK so this stays a unit test
    fake = type(sys)("claude_agent_sdk")
    for n in ("AssistantMessage", "ClaudeAgentOptions", "ClaudeSDKClient",
              "ResultMessage", "SystemMessage", "TextBlock", "UserMessage",
              "query"):
        setattr(fake, n, object)
    sys.modules.setdefault("claude_agent_sdk", fake)
    spec.loader.exec_module(m)
    return m


def main():
    d = _load()
    outcomes = []

    cases = [
        ("P1 raw prose is not-json",
         "agent-6 - you were right twice", "not-json"),
        # the REAL shape, taken from a file on disk, not from my imagination:
        # it ends with a literal backslash-n, not a newline. My first fixture
        # used "\n\n" (real newlines), which json.loads accepts as whitespace,
        # so the arm went FAIL and the classifier was right. Check the bytes
        # before writing the case.
        ("P2 a literal \\n after the closing brace is trailing-data",
         '{"from":"a","text":"hi"}\\n\n', "trailing-data"),
        ("P3 a raw newline inside text is a control character",
         '{"from":"a","text":"line1\nline2"}', "raw-control-character"),
        ("P4 an object with no text is no-text",
         '{"from":"a","body":"hi"}', "no-text"),
        ("P4b text that is only whitespace is no-text",
         '{"from":"a","text":"   \\n  "}', "no-text"),
        ("P4c valid JSON that is not an object is not-object",
         '["from","a"]', "not-object"),
    ]
    for name, raw, want in cases:
        got = d.undeliverable_reason(raw)
        outcomes.append((name, got == want, "got %r want %r" % (got, want)))

    # P5: the control that keeps P1-P4 honest. A classifier that rejects
    # everything satisfies every arm above.
    good = json.dumps({"from": "peer", "text": "a real message"})
    outcomes.append(("P5 a deliverable message classifies as None",
                     d.undeliverable_reason(good) is None,
                     "got %r" % (d.undeliverable_reason(good),)))

    # P6: quarantine renames in place; the glob must no longer see it.
    p6 = tempfile.mkdtemp()
    try:
        src = os.path.join(p6, "1-peer.msg")
        open(src, "w").write("not json at all")
        moved = d.quarantine(src, "not-json")
        still_globbed = os.path.exists(src)
        survived = moved and os.path.exists(moved) and \
            open(moved).read() == "not json at all"
        outcomes.append(("P6 quarantine renames in place and keeps the bytes",
                         bool(survived) and not still_globbed,
                         "moved=%r glob_sees_old=%s" % (moved, still_globbed)))
        # the * .msg glob must not match the quarantined name
        import glob as _g
        hits = _g.glob(os.path.join(p6, "*.msg"))
        outcomes.append(("P6b the *.msg glob no longer matches a .bad file",
                         hits == [], "glob=%r" % (hits,)))
    finally:
        shutil.rmtree(p6)

    # P7: a quarantine that cannot rename must be reportable, not fatal.
    p7 = tempfile.mkdtemp()
    try:
        missing = os.path.join(p7, "does-not-exist.msg")
        got = d.quarantine(missing, "not-json")
        outcomes.append(("P7 a failed quarantine returns None, does not raise",
                         got is None, "got %r" % (got,)))
    finally:
        shutil.rmtree(p7)

    # M1: THE MUTATION, and it DRIVES THE READER rather than asserting on a
    # string I built. My first version asserted `"if False:" in broken and
    # index(if False) < index(os.remove)` -- which is a fact about the
    # mutant's TEXT, not about behaviour, and it was FALSE because
    # `os.remove` also appears in the quarantine helper above it. A mutation
    # must be run against the code path, not searched for in it.
    #
    # What is under test is the DECISION, so the decision is extracted as the
    # pure function it should be and both the real and the broken version are
    # driven over the same inputs. `handle_message_file` returns an action:
    # ("deliver", text) | ("quarantine", reason) | ("log", reason).
    mut = tempfile.mkdtemp()
    try:
        cases = [
            ("deliverable", '{"from":"p","text":"hi"}', "deliver"),
            ("raw prose",   "not json",                "quarantine"),
            ("empty text",  '{"from":"p","text":""}',  "quarantine"),
            ("trailing bs", '{"from":"p","text":"hi"}\\n', "quarantine"),
        ]
        real = [d.handle_message_file(raw)[0] for _, raw, _ in cases]
        want = [w for _, _, w in cases]
        outcomes.append(("M0 the reader DELIVERS valid input and quarantines "
                         "each malformed shape",
                         real == want, "got %r want %r" % (real, want)))
        src = open(DAEMON, encoding="utf-8").read()
        # The anchor is the BRANCH in handle_message_file, asserted to
        # occur exactly once there -- an anchor that also matches the loop
        # would mutate the wrong copy (the defect I shipped two lives ago).
        needle = "    reason = undeliverable_reason(raw)\n    if reason is not None:\n        return (\"quarantine\", reason)"
        assert src.count(needle) == 1, "mutation anchor moved or ambiguous"
        broken_src = src.replace(needle,
            "    reason = None          # MUTANT: deliver anything", 1)
        dst = os.path.join(mut, "broken-daemon.py")
        open(dst, "w", encoding="utf-8").write(broken_src)
        bd = _load(dst)
        def act(mod, raw):
            """The action, or the exception type if the reader blows up."""
            try:
                return mod.handle_message_file(raw)[0]
            except Exception as e:
                return "RAISED:" + e.__class__.__name__
        broken = [act(bd, raw) for _, raw, _ in cases]
        real_acts = [act(d, raw) for _, raw, _ in cases]
        # The mutant must FAIL TO QUARANTINE the malformed inputs. It may do
        # so by delivering them or by raising -- BOTH are the shipped defect
        # (nothing is recorded and the file is not set aside), and asserting
        # only "deliver" missed the raise I actually got.
        outcomes.append(("M1 the mutant does not quarantine malformed input "
                         "(the shipped defect is reproduced)",
                         broken[0] == "deliver"
                         and all(b != "quarantine" for b in broken[1:]),
                         "mutant=%r real=%r" % (broken, real_acts)))
    finally:
        shutil.rmtree(mut)

    bad = 0
    for name, ok, detail in outcomes:
        print("  %-58s %s  (%s)" % (name, "PASS" if ok else "FAIL", detail))
        bad += (not ok)
    print("\nmail-undeliverable: %d/%d arms passed" % (len(outcomes) - bad,
                                                       len(outcomes)))
    return bad == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
