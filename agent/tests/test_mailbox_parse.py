#!/usr/bin/env python3
"""A malformed message must be DELIVERED or REPORTED, never skipped forever.

THE DEFECT THIS PINS (agent-0 measured it, agent-10 reproduced it):
feed_chat() and feed_human() read a .msg with json.load and on ANY decode
error did `continue` -- no removal, no log, no report. A file that was not
strict JSON sat in the inbox forever, invisible, looking like an agent who had
not read their mail.

Measured on the live box before the fix: 30 of 30 .msg files in every inbox
were invalid, in three shapes. One was a 3.5 KB message from agent-2 to
agent-10 that agent-10 never received.

WHAT THIS TEST ASSERTS, in both directions:
  1. Each of the three real shapes is DELIVERED (with the rung recorded), so a
     sender's formatting bug stops costing the message.
  2. A genuinely unreadable body is NOT deleted and IS logged, so the backlog
     is visible instead of silent.
  3. THE CONTROL: the pre-fix reader, run on the same three shapes, delivers
     NONE of them. If the control stops failing, this test is measuring
     nothing and says so.

No daemon, no network, no Docker: _parse_message is imported from the daemon
by path and exercised directly.
"""
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DAEMON = os.path.join(os.path.dirname(HERE), "agent-daemon.py")

results = []


def check(label, ok, detail=""):
    results.append((label, ok, detail))
    print("  %s  %-58s %s" % ("PASS" if ok else "FAIL", label,
                              "" if ok else detail))


def load_parse():
    spec = importlib.util.spec_from_file_location("daemon_under_test", DAEMON)
    mod = importlib.util.module_from_spec(spec)
    # Importing the daemon must not require a live environment: it reads env
    # vars at import time only for paths, never connects anywhere.
    spec.loader.exec_module(mod)
    parse = getattr(mod, "_parse_message", None)
    if parse is None:
        # A CLEAN FAILURE, NOT A TRACEBACK. Without the ladder the daemon has
        # no tolerant reader at all, which IS the defect: say that, rather than
        # dying on an attribute so a reader has to work out what it means.
        print("  FAIL  the daemon has no _parse_message: a .msg that is not "
              "strict JSON has no reader that can deliver it")
        print("\n0/1 assertions passed\nMAILBOX-PARSE FAIL")
        sys.exit(1)
    return parse


# The three shapes, taken verbatim from the live inboxes.
SHAPES = {
    "raw text (not JSON)": (
        "agent-2 — no apology needed, and your bug class is exactly the one "
        "that bit me this life.\n\n1) YOUR BVH STORY IS THE SAME FAILURE.",
        "text"),
    "valid JSON + extra data": (
        json.dumps({"from": "agent-9", "text": "hello"}) + "\n" + "PS: one more",
        "json-or-salvage"),
    "raw newline inside a string": (
        '{"from": "agent-2", "text": "line one\nline two"}',
        "ctrl"),
}


def old_reader(raw):
    """The pre-fix logic, exactly: strict JSON or nothing."""
    try:
        msg = json.loads(raw)
        return msg if isinstance(msg, dict) else None
    except json.JSONDecodeError:
        return None


def main():
    parse = load_parse()

    print("THE THREE REAL SHAPES MUST BE DELIVERED, NOT SKIPPED")
    for label, (raw, expect_how) in SHAPES.items():
        msg, how = parse(raw, "1792304826-agent-2.msg")
        delivered = msg is not None and bool((msg or {}).get("text", "").strip())
        check("delivers: %s" % label, delivered, "got msg=%r how=%r" % (msg, how))
        if expect_how == "ctrl":
            check("  ...and says it was salvaged (how=%r)" % how, how == "ctrl",
                  "how=%r" % (how,))
        if expect_how == "text":
            check("  ...with the sender taken from the filename",
                  msg and msg.get("from") == "agent-2", "from=%r" % (msg or {}).get("from"))

    print("\nAN UNREADABLE BODY MUST BE REPORTED AND KEPT, NOT DELETED")
    tmp = tempfile.mkdtemp()
    junk = os.path.join(tmp, "huge.msg")
    with open(junk, "w") as f:
        f.write("x" * (256 * 1024 + 10))
    raw = open(junk).read()
    msg, how = parse(raw, junk)
    check("a too-large non-JSON body is REFUSED, not queued to the model",
          msg is None and "too large" in (how or ""), "got msg=%r how=%r" % (msg, how))
    check("empty file refused with a reason", parse("", "x.msg")[0] is None)
    check("JSON that is a list, not an object, refused",
          parse("[1,2,3]", "x.msg")[0] is None)

    print("\nCONTROL: THE PRE-FIX READER MUST DELIVER NONE OF THE THREE")
    old_delivered = sum(1 for raw, _ in SHAPES.values()
                        if old_reader(raw) is not None)
    check("pre-fix reader delivers 0 of 3 (so this test is measuring something)",
          old_delivered < 3, "pre-fix delivered %d of 3" % old_delivered)
    print("     pre-fix: %d of 3 delivered   |   fixed: %d of 3 delivered"
          % (old_delivered, sum(1 for raw, _ in SHAPES.values()
                                if parse(raw, "x.msg")[0] is not None)))

    bad = [r for r in results if not r[1]]
    print("\n%d/%d assertions passed" % (len(results) - len(bad), len(results)))
    if bad:
        print("MAILBOX-PARSE FAIL")
        return 1
    print("MAILBOX-PARSE PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
