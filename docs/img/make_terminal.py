#!/usr/bin/env python3
"""Render REAL captured terminal output as a PNG.

Input is literal stdout captured by running the demo. Nothing is hand-typed:
the colours are driven by the lines themselves, so a FAIL in the capture is
red in the image and a check that disappears from the demo disappears from
the picture.

Emits BOTH <out>.png (what GitHub renders) and <out>.svg (the same lines as
real <text>, so the claim is checkable with grep rather than by eye).

DEPENDENCY: matplotlib, for its bundled DejaVu Sans Mono. This agent image
has NO system fonts and no cairo, so a rasteriser like cairosvg/resvg
renders every glyph invisible -- use matplotlib. matplotlib is NOT in the
base agent image (pip install --user matplotlib).

NOT BYTE-REPRODUCIBLE, and it does not need to be. The demo prints a fresh
temp path and port each run, so re-rendering produces a different PNG with
the same CHECK LINES. The reproducibility claim is about content, not bytes:
normalise /tmp/town-* and the port, and two captures from the same code are
identical. That is the property that matters -- the picture cannot show a
check the demo no longer prints.

REGENERATION RULE: the demo's check count is quoted in README.md and shown
in the image. If the demo changes, re-run it and re-run this script IN THE
SAME COMMIT, and bump the count in the two Verification rows. They are
generated from one run; changing one without the others is how a doc starts
lying.

Usage: python3 make_terminal.py <capture.txt> <out-basename> [title]
"""
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Circle

BG = "#0f1115"
BAR = "#1a1e26"
FG = "#e6e8ee"
DIM = "#8b93a7"
GREEN = "#54d18b"
RED = "#e2607a"
CYAN = "#5aa9e6"

FONT = "DejaVu Sans Mono"


def classify(line):
    s = line.strip()
    if s.startswith("PASS") or s.startswith(("DEMO PASSED", "SELFTEST PASS")):
        return GREEN
    if s.startswith("FAIL") or s.startswith(("DEMO FAILED", "SELFTEST FAIL")):
        return RED
    if len(s) > 1 and s[0].isdigit() and s[1] == ".":
        return CYAN
    if s.startswith(("ledger:", "mailbox:", "bank up")):
        return DIM
    return FG


def main():
    src, base = sys.argv[1], sys.argv[2]
    title = sys.argv[3] if len(sys.argv) > 3 else "python3 demo/town.py"
    lines = open(src, encoding="utf-8").read().rstrip("\n").splitlines()

    fs = 10.5
    # Lay out in INCHES and convert to axes fraction, so spacing does not
    # change when the line count changes (the first render used a fixed
    # fraction and the lines collided).
    line_in = 0.165
    bar_in = 0.34
    top_in = 0.16
    bot_in = 0.16
    fig_h = bar_in + top_in + bot_in + len(lines) * line_in
    line_h = line_in / fig_h
    fig = plt.figure(figsize=(11.6, fig_h), dpi=110)
    fig.patch.set_facecolor(BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.set_facecolor(BG)

    # window chrome
    ax.add_patch(FancyBboxPatch((0, 0), 1, 1, boxstyle="round,pad=0,rounding_size=0.012",
                                facecolor=BG, edgecolor="none", zorder=0))
    bar_h = bar_in / fig_h
    ax.add_patch(FancyBboxPatch((0, 1 - bar_h), 1, bar_h,
                                boxstyle="square,pad=0", facecolor=BAR,
                                edgecolor="none", zorder=1))
    for i, c in enumerate(("#e2607a", "#e0b23c", "#54d18b")):
        ax.add_patch(Circle((0.022 + i * 0.019, 1 - bar_h / 2), 0.0058,
                            facecolor=c, edgecolor="none", zorder=2))
    ax.text(0.5, 1 - bar_h / 2, title, color=DIM, fontsize=9.5,
            fontfamily=FONT, ha="center", va="center", zorder=2)

    y = 1 - bar_h - top_in / fig_h
    for line in lines:
        ax.text(0.022, y, line, color=classify(line), fontsize=fs,
                fontfamily=FONT, ha="left", va="top", zorder=2)
        y -= line_h

    fig.savefig(base + ".png", facecolor=BG, dpi=110)
    plt.close(fig)

    # Also emit an SVG with every line as real <text>. The PNG is what GitHub
    # renders; the SVG is the version a person or a diff can actually READ, so
    # the claim "the picture cannot show a check the demo no longer prints"
    # is checkable with grep instead of by eye.
    import html as _html
    W, LH, PX, PT = 980, 20, 22, 58
    H = PT + LH * len(lines) + 26
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
           f'viewBox="0 0 {W} {H}">',
           f'<rect width="{W}" height="{H}" rx="10" fill="{BG}"/>',
           f'<rect width="{W}" height="38" rx="10" fill="{BAR}"/>',
           f'<rect y="28" width="{W}" height="10" fill="{BAR}"/>']
    for i, c in enumerate(("#e2607a", "#e0b23c", "#54d18b")):
        out.append(f'<circle cx="{22 + i * 20}" cy="19" r="6" fill="{c}"/>')
    out.append(f'<text x="{W/2}" y="24" fill="{DIM}" font-family="{FONT}" '
               f'font-size="12" text-anchor="middle">'
               f'{_html.escape(title)}</text>')
    y = PT
    for line in lines:
        out.append(f'<text xml:space="preserve" x="{PX}" y="{y}" '
                   f'fill="{classify(line)}" font-family="{FONT}" '
                   f'font-size="13">{_html.escape(line)}</text>')
        y += LH
    out.append("</svg>")
    with open(base + ".svg", "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    print(f"wrote {base}.png and {base}.svg  ({len(lines)} lines)")


if __name__ == "__main__":
    main()
