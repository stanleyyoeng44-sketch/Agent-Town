#!/usr/bin/env python3
"""Extract the PAGE HTML string and its <script> block from ui/control.py.

Why: the console is a Python module that emits one big PAGE triple-quoted
string. That makes the JavaScript untestable by ordinary means -- you cannot
import it, and you cannot run the server without Docker. This pulls the two
pieces out so node can parse and exercise them, and so a render function can
be unit-tested with no browser and no docker.

ASCII-only in the PAGE: the source uses an em dash inside a JS template
literal. Printing it is fine; the extraction must not depend on encoding.
"""
import ast, os, sys

def extract(path):
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    page = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "PAGE":
                    page = node.value.value
    if page is None:
        raise SystemExit("no PAGE string found")
    start = page.index("<script>") + len("<script>")
    end = page.rindex("</script>")
    return page, page[start:end]

if __name__ == "__main__":
    path = sys.argv[1]
    page, js = extract(path)
    outdir = sys.argv[2] if len(sys.argv) > 2 else "."
    os.makedirs(outdir, exist_ok=True)
    open(os.path.join(outdir, "page.html"), "w", encoding="utf-8").write(page)
    open(os.path.join(outdir, "console.js"), "w", encoding="utf-8").write(js)
    print(f"PAGE bytes={len(page)}  js bytes={len(js)}  js lines={js.count(chr(10))}")
