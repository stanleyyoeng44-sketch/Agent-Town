#!/usr/bin/env python3
"""undefined_names.py — a name used but never bound, found without running the code.

WHY THIS EXISTS
    Master did not boot for two hours. Commit 718d278 deleted
    `WAKE_NOTICE_TOKENS = int(...)` while leaving four uses of it, and nothing
    in this repo could see it:

      - C6 (ast.parse) is a SYNTAX check. `{WAKE_NOTICE_TOKENS}` in an
        f-string parses perfectly. A NameError is not a SyntaxError.
      - C7 imports the module. Import SUCCEEDS: the failing use is inside
        main_loop(), and importing a module never calls its functions.
      - The daemon itself died at the first print() in main_loop(), before the
        life loop, so it wrote no transcript, no birth event and no error file.
        A container built from that tree restart-loops with the traceback on
        stderr only.

    That is a defect class, not an incident: deleting a definition while
    leaving its uses is invisible to every check the repo had. pyflakes finds
    it instantly but is not installed in the image and is not a repo
    dependency. This module is the part of pyflakes that matters here, using
    nothing but the stdlib, so C12 can run anywhere the repo runs.

WHAT IT IS NOT
    It is not a type checker and not a full pyflakes. It answers exactly one
    question: is there a name LOADED at some point that is not bound anywhere
    that load can see? When it cannot prove a load is safe it stays SILENT --
    a false positive here would fail an honest tree, and a check that bites
    healthy code is worse than no check (that lesson cost this cohort a day;
    see the C5 threshold withdrawn on measurement).

    Deliberate silences, each one a place where "undefined" would be a lie:
      * `from x import *`            -> any module-level name may exist
      * globals()/locals()/eval/exec -> the module writes its own namespace
      * try/except NameError         -> the code means to probe for the name
      * a name bound anywhere in an enclosing function, even later
        (Python resolves at CALL time, so `def f(): return X` above
        `X = 1` at module level is legal)
      * comprehension / lambda / class-body scoping handled explicitly

Usage:
    python3 tools/undefined_names.py            # every tracked .py
    python3 tools/undefined_names.py FILE ...   # named files
Exit 0 clean, 1 findings.
"""
import ast
import builtins
import os
import subprocess
import sys

BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__",
                                 "__package__", "__spec__", "__loader__",
                                 "__builtins__", "__annotations__", "__dict__",
                                 "__class__", "__debug__"}
# Names a function may always load without a binding we can see.
ESCAPE_CALLS = {"globals", "locals", "eval", "exec", "vars"}


def _bound_in(node, out):
    """Add every name THIS SCOPE binds to `out`.

    The single most important property: it does NOT descend into a nested
    FunctionDef / Lambda / ClassDef / comprehension body. A name bound inside
    a sibling function is not visible outside it, and a comprehension target
    does not leak. Both were false NEGATIVES in the first version of this
    module -- the checker reported nothing on code it should have caught,
    which is the failure mode that looks like success.
    """
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        out.add(node.name)
        for d in node.decorator_list:          # evaluated in the OUTER scope
            _bound_in(d, out)
        for a in list(node.args.defaults) + [x for x in node.args.kw_defaults if x]:
            _bound_in(a, out)
        return                                  # body is its own scope
    if isinstance(node, ast.Lambda):
        for a in list(node.args.defaults) + [x for x in node.args.kw_defaults if x]:
            _bound_in(a, out)
        return
    if isinstance(node, ast.ClassDef):
        out.add(node.name)
        for d in node.decorator_list:
            _bound_in(d, out)
        for b in node.bases:
            _bound_in(b, out)
        return                                  # class body is its own scope
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp,
                         ast.GeneratorExp)):
        # The comprehension TARGETS stay inside (they do not leak), but a
        # WALRUS target binds in the CONTAINING scope in CPython, so it DOES
        # leak to here. Missing this was a false positive: `if any((n := x) >
        # 1 for x in data): print(n)` reported `n` undefined on the print.
        # `_comp_targets` covers the walrus INSIDE the comprehension; this
        # covers the same binding in the scope that contains it.
        for n in ast.walk(node):
            if isinstance(n, ast.NamedExpr) and isinstance(n.target, ast.Name):
                out.add(n.target.id)
        return                                  # targets stay inside
    if isinstance(node, ast.match_case):
        # A `case` pattern binds captures in the enclosing function scope
        # (match does not create a scope). Walk the pattern's capture nodes.
        _bound_in(node.pattern, out)
        return
    if isinstance(node, ast.MatchAs):
        # `x as name`, and the bare capture `case name:` (pattern is None).
        if node.name:
            out.add(node.name)
        if node.pattern is not None:
            _bound_in(node.pattern, out)
        return
    if isinstance(node, ast.MatchStar):
        if node.name:
            out.add(node.name)          # `case [*rest]`
        return
    if isinstance(node, ast.MatchMapping):
        # `case {**rest}` -- `rest` captures the remaining items; the VALUES
        # are themselves patterns (`case {"a": v}` captures v). Missing the
        # values was the residual false positive after the first match fix.
        if node.rest:
            out.add(node.rest)
        for pat in node.patterns:
            _bound_in(pat, out)
        return
    if isinstance(node, ast.MatchClass):
        # `case Point(x=a, y=b)` -- keyword-pattern values are sub-patterns.
        for pat in list(node.patterns) + list(node.kwd_patterns):
            _bound_in(pat, out)
        return
    if isinstance(node, ast.MatchSequence):
        for pat in node.patterns:
            _bound_in(pat, out)
        return
    if isinstance(node, ast.MatchOr):
        for pat in node.patterns:
            _bound_in(pat, out)
        return
    if isinstance(node, ast.Name):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            out.add(node.id)
        return
    if isinstance(node, ast.arg):
        out.add(node.arg)
        return
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        for a in node.names:
            if a.name == "*":
                out.add("*")                    # marks "star import here"
            elif isinstance(node, ast.Import):
                out.add(a.asname or a.name.split(".")[0])
            else:
                out.add(a.asname or a.name)
        return
    if isinstance(node, ast.ExceptHandler):
        # Do NOT return here: the handler BODY runs in the enclosing scope,
        # so a name assigned inside `except ...:` is bound outside it.
        # Returning early made `try: detail = x / except: detail = y` look
        # like a use of an undefined `detail` -- a real false positive on
        # agent/mcp-stdio.py, found by running the check on the repo.
        if node.name:
            out.add(node.name)
        for c in node.body:
            _bound_in(c, out)
        return
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        out.update(node.names)
        return
    for c in ast.iter_child_nodes(node):
        _bound_in(c, out)


def _comp_targets(node):
    """Names a comprehension binds for its OWN element expression.

    Two sources, and missing the second cost a false positive on
    claude_agent_sdk/types.py, which pyflakes accepts:
      1. the `for` targets;
      2. walrus targets (`if (tool := f(x)) is not None` then `tool` in the
         elt). Measured: `dict.fromkeys(tool for entry in x if (tool := f(entry)))`
         was reported undefined until this was added. A `:=` inside a
         comprehension binds in the CONTAINING scope in CPython, so the
         enclosing scope already picked it up via _bound_in; the elt needs it
         in `inner` because the elt is walked with `visible | inner`.
    """
    out = set()
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp,
                         ast.GeneratorExp)):
        for g in node.generators:
            for x in ast.walk(g.target):
                if isinstance(x, ast.Name):
                    out.add(x.id)
        for n in ast.walk(node):
            if isinstance(n, ast.NamedExpr) and isinstance(n.target, ast.Name):
                out.add(n.target.id)
    return out


def check_source(src, filename="<src>"):
    """Return a list of (lineno, name) that are loaded but never bound."""
    try:
        tree = ast.parse(src, filename=filename)
    except SyntaxError:
        return []            # C6 owns syntax; stay quiet here.

    module_bound = set()
    _bound_in(tree, module_bound)
    if "*" in module_bound:
        return []            # a star import can define anything
    # Is the module willing to touch its own namespace?
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id in ESCAPE_CALLS):
            return []
    # Does it catch NameError anywhere? (a deliberate probe)
    for n in ast.walk(tree):
        if isinstance(n, ast.ExceptHandler) and n.type is not None:
            t = n.type
            names = [t] if isinstance(t, ast.Name) else (
                t.elts if isinstance(t, ast.Tuple) else [])
            if any(isinstance(x, ast.Name) and x.id == "NameError"
                   for x in names):
                return []

    findings = []

    def scope_names(body, node=None):
        """Everything bound in this scope: its own statements plus, when this
        is a function or lambda, its parameters.

        Parameters were MISSING in the first version, which reported `self`,
        `life_id` and every other argument as undefined -- 518 findings on a
        healthy tree. A checker that bites correct code is worse than none.
        """
        out_names = set()
        if node is not None:
            a = node.args
            for x in (a.posonlyargs + a.args + a.kwonlyargs):
                out_names.add(x.arg)
            if a.vararg:
                out_names.add(a.vararg.arg)
            if a.kwarg:
                out_names.add(a.kwarg.arg)
        for st in body:
            _bound_in(st, out_names)
        return out_names

    def visit_scope(body, env, node=None):
        visible = env | scope_names(body, node)
        for st in body:
            walk(st, visible)

    def walk(node, visible):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # decorators and defaults are evaluated where the def appears
            for d in node.decorator_list:
                walk(d, visible)
            for a in list(node.args.defaults) + [x for x in node.args.kw_defaults if x]:
                walk(a, visible)
            visit_scope(node.body, visible, node)
            return
        if isinstance(node, ast.ClassDef):
            for d in node.decorator_list:
                walk(d, visible)
            for b in node.bases:
                walk(b, visible)
            # A class body is NOT a closure: it does not see enclosing
            # function locals, only the module. Passing `visible` is a
            # deliberate, conservative over-approximation (it can only hide
            # a finding, never invent one).
            visit_scope(node.body, visible)
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp,
                             ast.GeneratorExp)):
            inner = set(_comp_targets(node))
            gens = node.generators
            if gens:
                walk(gens[0].iter, visible)
            for i, g in enumerate(gens):
                if i:
                    walk(g.iter, visible | inner)
                for cond in g.ifs:
                    walk(cond, visible | inner)
            if isinstance(node, ast.DictComp):
                walk(node.key, visible | inner)
                walk(node.value, visible | inner)
            else:
                walk(node.elt, visible | inner)
            return
        if isinstance(node, ast.Lambda):
            for a in list(node.args.defaults) + [x for x in node.args.kw_defaults if x]:
                walk(a, visible)
            inner = scope_names([], node)
            visit_scope([ast.Expr(node.body)], visible | inner)
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in visible and node.id not in BUILTINS:
                findings.append((node.lineno, node.id))
            return
        for c in ast.iter_child_nodes(node):
            walk(c, visible)

    visit_scope(tree.body, set())
    seen, out = set(), []
    for ln, name in sorted(findings):
        if (ln, name) not in seen:
            seen.add((ln, name))
            out.append((ln, name))
    return out


def tracked_py(root="."):
    r = subprocess.run(["git", "-C", root, "ls-files", "*.py"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return []
    return [f for f in r.stdout.splitlines() if f.strip()]


def main(argv):
    root = "."
    files = argv[1:]
    if not files:
        files = tracked_py(root)
    bad = 0
    for f in files:
        p = f if os.path.isabs(f) else os.path.join(root, f)
        if not os.path.isfile(p):
            continue
        with open(p, encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        for ln, name in check_source(src, f):
            bad += 1
            print(f"UNDEFINED {f}:{ln}: {name}")
    print("undefined-names: %s (%d finding%s)"
          % ("FAIL" if bad else "PASS", bad, "" if bad == 1 else "s"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
