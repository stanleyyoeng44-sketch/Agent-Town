"""A PASS row must not be summarised by a failure word.

THE DEFECT (agent-5 measured it, I reproduced it on master 309b707): three
CORRECT suites exit 0 while printing a line that contains a failure word,
because the word is the NAME of the case or of the pre-fix behaviour under
test:

    demo/selftest_demo.py          "FAIL: bank.py has neither a DATA env ..."
    bank/tests/test_transfer_amounts.py   "... before=FAIL(mint visible)"
    tools/test_prompt_stream.py    "FAIL  resumed, mail already queued"

run_suite() returned PASS for all three -- the VERDICT was always right --
but it chose the detail line by looking for a failure word LAST, without
consulting the exit status it had already computed. So the runner printed
three PASS rows that read as failures. The mirror of agent-10's original bug:
a reassurance in a FAIL row became a failure word in a PASS row.

WHY THIS IS A SEPARATE FILE AND NOT ANOTHER SELFTEST ROW: a row inside
check.py's --selftest cannot fail on a runner that does not have the fix --
it does not have the row. I tried that first and the unfixed runner reported
PASS, which is precisely the vacuous control this cohort has been finding all
day. This test drives run_suite() on the REAL suites, so it exists and fails
independently of what check.py's own selftest knows about.

BOTH DIRECTIONS, because a rule that merely suppresses failure words would
also undo agent-10's fix (the doc audit's reassurance AFTER a real verdict):
  A  three passing suites      -> detail contains no failure word
  B  a failing suite whose last line is a reassurance -> detail keeps FAIL

Run:  python3 tools/test_runner_pass_detail.py
Exit: 0 both directions behave; 1 either does not.
"""
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import check  # noqa: E402  (the runner itself; this tests its real code path)

BAD = re.compile(r"FAIL|STALE|ERROR|DIFFER|TIMEOUT", re.I)

# The three suites agent-5 named. Kept as paths, not literals of the printed
# text: the suites may reword their output, and this test is about the
# runner's selection rule, not about a sentence.
PASSING = ["demo/selftest_demo.py",
           "bank/tests/test_transfer_amounts.py",
           "tools/test_prompt_stream.py"]


def main():
    ok = True
    lines = []

    for rel in PASSING:
        path = os.path.join(os.path.dirname(HERE), rel)
        if not os.path.exists(path):
            lines.append("  SKIP  %s is not in this tree -- NOT EVALUATED"
                         % rel)
            continue
        state, detail = check.run_suite(rel, [sys.executable, rel], None, False)
        hit = BAD.search(detail)
        # Both halves matter: the verdict must still be PASS (we are not
        # fixing this by making the suite look broken) and the detail must
        # not read as a failure.
        case_ok = (state == "PASS" and hit is None)
        ok = ok and case_ok
        lines.append("  %-5s A %-38s state=%s detail=%r"
                     % ("PASS" if case_ok else "FAIL", rel, state, detail[:70]))

    # B: agent-10's fix must survive. A failing suite whose LAST line is a
    # reassurance must still report the failing line as the diagnosis.
    work = tempfile.mkdtemp(prefix="runner-pass-detail-")
    body = ("print('SUITE FAILED: two plus two')\n"
            "print('Every suite in the repo can still exit 0: none reads prose')\n"
            "import sys; sys.exit(1)\n")
    probe = os.path.join(work, "fail_then_reassure.py")
    with open(probe, "w", encoding="utf-8") as f:
        f.write(body)
    state, detail = check.run_suite("t", [sys.executable, probe], None, False)
    b_ok = (state == "FAIL" and BAD.search(detail) is not None
            and "two plus two" in detail)
    ok = ok and b_ok
    lines.append("  %-5s B FAIL row keeps its diagnosis       state=%s detail=%r"
                 % ("PASS" if b_ok else "FAIL", state, detail[:70]))

    print("\n".join(lines))
    print("\nRUNNER-PASS-DETAIL %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
