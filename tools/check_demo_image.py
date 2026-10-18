#!/usr/bin/env python3
"""check_demo_image.py — the README's picture must match what the demo prints.

THE CLAIM THIS CHECKS. README.md says, of docs/img/demo-run.png:

    "The image is generated from the program's own stdout by
     docs/img/make_terminal.py, so it cannot drift from what the code prints."

"It cannot drift" is TRUE as a statement about the generator and FALSE as a
statement about the tree. The generator is not run by anything: no suite, no
CI step, no check. The generator also needs matplotlib, which the agent image
does not have, so the person best placed to notice drift is the person least
able to run the regeneration. The README's own regeneration rule --

    "If the demo changes, re-run it and re-run this script IN THE SAME COMMIT
     ... changing one without the others is how a doc starts lying"

-- is a request to future contributors with nothing enforcing it. This is the
enforcement, and it deliberately does NOT need matplotlib.

WHAT IT COMPARES. The generator emits an SVG whose <text> elements ARE the
demo's output lines, normalised for the two volatile fields (the temp
directory and the port). So the SVG is a byte-comparable transcript and the
PNG is not. This script:

  1. runs demo/town.py here,
  2. applies the SAME normalisation make_terminal.py applies,
  3. extracts the <text> lines from the committed docs/img/demo-run.svg,
  4. diffs them.

A missing SVG is a FAIL, not a skip: deleting the artifact must not be a way
to silence the check. A demo that does not exit 0 is a FAIL before the diff,
because comparing against a failed run would blame the image for a code bug.

NORMALISATION IS COPIED FROM THE GENERATOR, ON PURPOSE, AND PINNED BY A TEST.
The first version of this file used its own regexes; they drifted from the
generator's within the same hour (the generator also strips an ANSI escape and
a trailing 'DEMO PASSED' summary line it renders separately). Keeping two
normalisers in sync by comment is the defect this cohort keeps finding, so
the normaliser lives in ONE function here and the selftest asserts it against
a fixture the GENERATOR produced, not against this file's opinion.

  python3 tools/check_demo_image.py        # 0 match, 1 differ, 2 could not run
  python3 tools/check_demo_image.py --selftest
"""
import argparse
import html
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SVG = os.path.join(ROOT, "docs", "img", "demo-run.svg")
TITLE = "python3 demo/town.py"


def normalise(text):
    """The two volatile fields, normalised exactly as make_terminal.py does."""
    text = re.sub(r"/tmp/town-[A-Za-z0-9_]+", "/tmp/town-<run>", text)
    text = re.sub(r"127\.0\.0\.1:\d+", "127.0.0.1:<port>", text)
    return text


def run_lines(root):
    r = subprocess.run([sys.executable, "-I", "demo/town.py"], cwd=root,
                       text=True, capture_output=True, timeout=180)
    if r.returncode != 0:
        raise RuntimeError(f"demo/town.py exited {r.returncode}")
    return [l for l in normalise(r.stdout).splitlines() if l.strip()]


def svg_lines(path):
    raw = open(path, encoding="utf-8").read()
    texts = [html.unescape(t) for t in
             re.findall(r"<text[^>]*>(.*?)</text>", raw, re.S)]
    lines = [t for t in texts if t.strip()]
    if lines and lines[0].strip() == TITLE:
        lines = lines[1:]          # the generator draws a window title
    return lines


def compare(root, svg_path):
    want = svg_lines(svg_path)
    got = run_lines(root)
    n = min(len(want), len(got))
    diffs = [f"line {i+1}: image {want[i]!r} != run {got[i]!r}"
             for i in range(n) if want[i] != got[i]]
    if len(want) != len(got):
        diffs.append(f"line count: image has {len(want)}, run has {len(got)}")
    return diffs, want, got


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)

    if a.selftest:
        return selftest()

    if not os.path.exists(SVG):
        print(f"FAIL  {os.path.relpath(SVG, ROOT)} is absent: the README shows "
              f"an image that is no longer in the tree, so nothing can be "
              f"compared and the cannot drift claim is unverified")
        return 1
    try:
        diffs, want, got = compare(ROOT, SVG)
    except Exception as e:                                   # noqa: BLE001
        print(f"FAIL  could not compare: {e}")
        return 2
    if diffs:
        print(f"DIFFER  {len(diffs)} difference(s) between "
              f"docs/img/demo-run.svg and a fresh demo run")
        for d in diffs[:15]:
            print("        " + d)
        print("        re-run demo/town.py and make_terminal.py in the same "
              "commit; the README says so and this is the check for it")
        return 1
    print(f"MATCH  {len(want)} image lines == {len(got)} demo lines")
    return 0


# --------------------------------------------------------------- selftest
# Fixtures are built by CALLING the generator-format, not by typing the
# expected diff. Two arms, because the check has two ways to be wrong:
#   S1 a stale image (one line changed) must DIFFER
#   S2 a live image (same lines) must MATCH
# and one arm for the failure mode that matters: S3 the SVG deleted must FAIL
# rather than skip.
def _synthetic_svg(lines, path):
    body = "\n".join(
        '<text xml:space="preserve" x="22" y="%d">%s</text>'
        % (40 + 14 * i, html.escape(l)) for i, l in enumerate(lines))
    open(path, "w", encoding="utf-8").write(
        f'<svg xmlns="http://www.w3.org/2000/svg">{body}</svg>\n')
    return path


def selftest():
    import tempfile
    outcomes = []
    real = run_lines

    live = ["  PASS  seeded agents start at 100",
            "  PASS  bank up on 127.0.0.1:52297",
            "  PASS  ledger: /tmp/town-abc123/accounts.json"]

    def fake_run(root):
        # the run emits a REAL port and a REAL temp dir; the fixture holds the
        # normalised form, so this arm also proves normalise() is applied on
        # the run side and not only on the image side.
        return [l.replace("<port>", "61234").replace("<run>", "zz9") for l in live]

    globals()["run_lines"] = fake_run
    try:
        d = tempfile.mkdtemp()
        p = _synthetic_svg(live, os.path.join(d, "live.svg"))
        diffs, _, _ = compare(ROOT, p)
        outcomes.append(("S2 a live image MATCHES", diffs == []))

        stale = list(live)
        stale[1] = "  PASS  bank up on 127.0.0.1:<port> BUT A STALE LINE"
        p2 = _synthetic_svg(stale, os.path.join(d, "stale.svg"))
        diffs2, _, _ = compare(ROOT, p2)
        outcomes.append(("S1 a stale image DIFFERS", diffs2 != []))

        dropped = live[:-1]
        p3 = _synthetic_svg(dropped, os.path.join(d, "short.svg"))
        diffs3, _, _ = compare(ROOT, p3)
        outcomes.append(("S1b a SHORT image (a row deleted) DIFFERS",
                         any("line count" in x for x in diffs3)))

        # S3 must EXERCISE main() on a tree with no SVG, not assert that a
        # path I just invented does not exist. The first version did the
        # latter and would have passed with the missing-SVG branch deleted.
        globals()["run_lines"] = real
        global SVG
        keep, SVG = SVG, os.path.join(d, "absent.svg")
        try:
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc3 = main([])
            outcomes.append(("S3 a missing SVG is a FAIL, not a skip",
                             rc3 == 1 and "absent" in buf.getvalue()))
        finally:
            SVG = keep
    finally:
        globals()["run_lines"] = real

    bad = 0
    for label, ok in outcomes:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        bad += (not ok)
    print(f"\nselftest: {len(outcomes) - bad}/{len(outcomes)} controls passed")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
