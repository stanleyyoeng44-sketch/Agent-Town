#!/usr/bin/env python3
"""Render REAL captured terminal output as a PNG (matplotlib, bundled font).

Input is literal stdout captured by running the demo. Nothing is hand-typed.
The same file also emits an SVG with the text as real <text> (so it stays
diffable); the PNG exists so the image can be inspected before it is shipped.

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
    # the two lines that carry the whole point of the artifact
    highlight = ("bank wrote its payment notice to the DEMO mailbox, not live",
                 "demo wrote nothing into the real /shared/mailbox",
                 "memory crossed the sleep boundary")
    for line in lines:
        ax.text(0.022, y, line, color=classify(line), fontsize=fs,
                fontfamily=FONT, ha="left", va="top", zorder=2)
        y -= line_h

    fig.savefig(base + ".png", facecolor=BG, dpi=110)
    plt.close(fig)
    print(f"wrote {base}.png  ({len(lines)} lines)")


if __name__ == "__main__":
    main()
