"""Each snippet MUST be reported. One shape per way a name goes missing.

Run: python3 tools/tests/undefined_names_cases.py
Exit: 0 every case was caught, 1 a case was missed (naming which).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import undefined_names as un  # noqa: E402

CASES = [
    ("deleted module constant, used in an f-string", """
def f(total):
    return f"over {LIMIT}"
""", "LIMIT"),
    ("deleted module constant, used in a comparison", """
def watch(total):
    if total >= LIMIT:
        return True
""", "LIMIT"),
    ("deleted module constant, used only at boot in main()", """
def main_loop():
    print(f"up, threshold {LIMIT}")
def other():
    return LIMIT
""", "LIMIT"),
    ("misspelled local", """
def f():
    value = 1
    return vaule + 1
""", "vaule"),
    ("function called before it was renamed", """
def real(): pass
def caller():
    return rel()
""", "rel"),
    ("name bound only inside a sibling function", """
def a():
    hidden = 1
    return hidden
def b():
    return hidden
""", "hidden"),
    ("import removed, alias still used", """
import json as j
def f(x):
    return loads(x)
""", "loads"),
    ("class attribute used as a bare name", """
class C:
    LIMIT = 1
def f():
    return LIMIT
""", "LIMIT"),
    ("comprehension target does not leak to enclosing scope", """
def f(xs):
    out = [x for x in xs]
    return x
""", "x"),
    ("deleted global used in a decorator", """
def deco(fn): return fn
@deco
@missing_deco
def g(): pass
""", "missing_deco"),
]


def main():
    failed = 0
    for label, src, want in CASES:
        found = un.check_source(src, label)
        names = [n for _, n in found]
        ok = want in names
        print("  %-6s %-52s -> %s" % ("PASS" if ok else "FAIL", label,
                                      names or "nothing"))
        if not ok:
            failed += 1
    print("\n%s (%d/%d cases caught)"
          % ("UNDEFINED-CASES PASS" if not failed else "UNDEFINED-CASES FAIL",
             len(CASES) - failed, len(CASES)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
