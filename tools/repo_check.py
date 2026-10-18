#!/usr/bin/env python3
"""repo_check.py — the repo checks itself.

WHY THIS EXISTS
    .gitignore stops `git add .` from staging junk. It does NOT stop
    `git add -f`, it does NOT un-track a file that is already in the index,
    and a file already in HISTORY stays fetchable forever even after it is
    deleted at HEAD. A .gitignore is a wish; this is a check.

    This repo was, at the time of writing, shipping 9 committed .pyc files,
    a tracked SQLite database of the operator's private chat, a tracked
    process log, and a LIVE CONTROL TOKEN in `ui/control.log` -- the token
    that gates the entire control UI's /api surface. Every one of those was
    invisible to `git status` because git was doing exactly what it was told.

CHECKS
    C1 tracked junk        __pycache__, *.pyc, *.log, *.pid, sqlite, jsonl
    C2 secrets in HEAD     token/key-shaped strings in tracked file content
    C3 secrets in HISTORY  same, over every blob reachable from any ref
    C4 .gitignore present  and covers the classes above
    C5 docs commands run   every ```sh block in README runs, or is marked
    C6 sources import      every .py in the tree parses (stdlib ast only)

Each check returns (name, ok, [findings]). `--selftest` runs a NEGATIVE
CONTROL per check: it builds a deliberately bad tree and asserts the check
FAILS on it. A check that has never failed is not a check -- that is the
whole rule this cohort spent the day learning, and the repo is no exception.
"""
import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile

# --- patterns -------------------------------------------------------------
JUNK_NAME = re.compile(r"""
    (^|/)(__pycache__)(/|$)
  | \.py[cod]$
  | \.log$
  | \.pid$
  | \.(sqlite3?|db)$
  | \.jsonl$
""", re.X)

SECRET_PATTERNS = [
    # the exact shape that was committed here: "token: <43 urlsafe chars>"
    ("control/bank token line", re.compile(r"(?i)\btoken\s*[:=]\s*['\"]?[A-Za-z0-9_-]{24,}")),
    ("openai-style key", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
]

# placeholders that are NOT secrets; a doc may legitimately show these
PLACEHOLDER = re.compile(
    r"(?i)your[-_ ]?(api[-_ ]?)?(key|token)|xxx+|\.\.\.|<[^>]*>|\$\{?[A-Z_]+\}?|example|placeholder|redacted")

DOC_SUFFIXES = (".md", ".rst")
CODE_SUFFIXES = (".py",)


def _tracked(root):
    out = subprocess.run(["git", "-C", root, "ls-files"],
                         capture_output=True, text=True, check=True)
    return [l for l in out.stdout.splitlines() if l.strip()]


def _show(root, spec):
    out = subprocess.run(["git", "-C", root, "show", spec],
                         capture_output=True, text=True, errors="replace")
    return out.stdout


# --- checks ---------------------------------------------------------------
def c1_tracked_junk(root):
    bad = [f for f in _tracked(root) if JUNK_NAME.search(f)]
    return "C1 tracked junk", "FAIL" if bad else "PASS", bad


def scan_text(name, text):
    hits = []
    for label, pat in SECRET_PATTERNS:
        for m in pat.finditer(text):
            frag = m.group(0)
            if PLACEHOLDER.search(frag):
                continue            # documented placeholder, not a secret
            hits.append(f"{name}: {label}: {frag[:60]}")
    return hits


def c2_secrets_head(root):
    hits = []
    for f in _tracked(root):
        if f.endswith((".png", ".jpg", ".ico", ".gif", ".woff", ".woff2")):
            continue
        try:
            text = open(os.path.join(root, f), encoding="utf-8",
                        errors="replace").read()
        except OSError:
            continue
        hits.extend(scan_text(f, text))
    return "C2 secrets in HEAD", "FAIL" if hits else "PASS", hits


def c3_secrets_history(root):
    """Scan every blob reachable from any ref, including deleted files."""
    revs = subprocess.run(["git", "-C", root, "rev-list", "--all"],
                          capture_output=True, text=True).stdout.split()
    seen, hits = set(), []
    for rev in revs:
        listing = subprocess.run(
            ["git", "-C", root, "ls-tree", "-r", "-z", "--long", rev],
            capture_output=True, text=True, errors="replace").stdout
        for rec in listing.split("\0"):
            if not rec.strip():
                continue
            meta, _, path = rec.partition("\t")
            parts = meta.split()
            if len(parts) < 4:
                continue
            sha, size = parts[2], parts[3]
            if sha in seen:
                continue
            seen.add(sha)
            try:
                if int(size) > 2_000_000:
                    continue
            except ValueError:
                continue
            blob = _show(root, sha)
            hits.extend(f"{rev[:8]}:{path}: {h}" for h in scan_text(path, blob))
    # "zero secrets found" and "zero blobs scanned" must not print the same
    # green. If the walk inspected nothing, this check learned nothing.
    coverage = [f"scanned {len(seen)} distinct blob(s) across {len(revs)} commit(s)"]
    if len(seen) < 2:
        return "C3 secrets in HISTORY", "WARN", \
            coverage + ["history walk found nothing to inspect -- NOT verified"]
    return "C3 secrets in HISTORY", "FAIL" if hits else "PASS", hits + coverage


def c4_gitignore(root):
    p = os.path.join(root, ".gitignore")
    if not os.path.isfile(p):
        return "C4 .gitignore covers junk", "FAIL", [".gitignore missing"]
    txt = open(p, encoding="utf-8", errors="replace").read()
    need = ["__pycache__", "*.py[cod]", "*.log", "*.pid", "*.sqlite3",
            "control-token"]
    miss = [n for n in need if n not in txt]
    return "C4 .gitignore covers junk", "FAIL" if miss else "PASS", [f"missing rule: {m}" for m in miss]


SH_BLOCK = re.compile(r"```(?:sh|bash|console)\n(.*?)```", re.S)

# Commands this check can HONESTLY execute on this box. Anything else is
# reported as skipped WITH ITS REASON -- never silently dropped.
RUNNABLE = ("python3 ", "python ", "bash -n ", "sh -n ", "test ", "[ ")
ENV_LIMITED = ("docker ", "docker-compose ", "make ")   # no docker daemon here
# a leading VAR=value is environment, not the command; look through it
ENV_PREFIX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*\s+")


def _looks_like_server(body, root="."):
    """A block that starts a long-running listener, not a one-shot command.

    The block itself usually does NOT contain 'serve_forever' -- it runs a
    script that does. So look at the script it names as well as the block.
    """
    if re.search(r"\b(uvicorn|flask run|gunicorn)\b", body):
        return True
    targets = re.findall(r"python3?\s+([\w./-]+\.py)", body)
    for t in targets:
        try:
            src = open(os.path.join(root, t), encoding="utf-8",
                       errors="replace").read()
        except OSError:
            continue
        if "serve_forever" in src or "ThreadingHTTPServer(" in src:
            return True
    return False


def _first_cmd(body):
    for line in body.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            while ENV_PREFIX.match(s):
                s = ENV_PREFIX.sub("", s, count=1)
            return s
    return ""


def c5_doc_commands(root, timeout=60):
    """Execute the runnable code blocks in tracked docs.

    THE VACUOUS PASS, AND WHY THIS FUNCTION REFUSES IT. An earlier version of
    this check passed on a repo whose README had 16 fenced blocks, because
    none of them started with a command it recognised: 'zero blocks executed'
    was reported as PASS. That is a check that has never seen a document and
    therefore cannot fail one. It is the same defect as the join-default bug
    and as b0i's aliveness-at-expiry: the verdict is about the checker's view,
    not about the object.

    Now: executed and skipped are both counted. A block whose first command is
    env-limited (docker/make, neither available here) is SKIPPED AND NAMED.
    Three outcomes, because there are three facts:
      PASS  at least one block ran and none failed
      FAIL  a block ran and failed
      WARN  nothing could run here -- the docs are UNVERIFIED on this box.
            WARN is not PASS: it never claims the docs are correct. It is also
            not FAIL: the blocks may legitimately need docker. It is printed
            on every run so the gap cannot quietly become a green tick.
    """
    findings, ran, skipped, warnings = [], 0, [], []
    for f in _tracked(root):
        if not f.endswith(DOC_SUFFIXES):
            continue
        text = open(os.path.join(root, f), encoding="utf-8", errors="replace").read()
        for block in SH_BLOCK.finditer(text):
            body = block.group(1).strip()
            first = _first_cmd(body)
            if not first:
                continue
            if "SKIP" in body.splitlines()[0]:
                skipped.append(f"{f}: {first[:60]} (marked SKIP)")
                continue
            if first.startswith(ENV_LIMITED):
                skipped.append(f"{f}: {first[:60]} (no docker/make on this box)")
                continue
            if not first.startswith(RUNNABLE):
                skipped.append(f"{f}: {first[:60]} (not a command this check runs)")
                continue
            ran += 1
            try:
                r = subprocess.run(body, shell=True, cwd=root, text=True,
                                   capture_output=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                findings.append(f"{f}: `{first[:60]}` TIMEOUT after {timeout}s")
                continue
            if r.returncode != 0:
                err = (r.stderr or r.stdout).strip()
                # A long-running SERVER that binds a busy port is not a broken
                # doc command -- it is a command that collides with the box it
                # is run on. Report it as WARN with the reason, and say what to
                # do, rather than calling the document wrong.
                if "Address already in use" in err and _looks_like_server(body, root):
                    warnings.append(
                        f"{f}: `{first[:60]}` needs a free port; this box already "
                        f"runs it (use PORT=<free> to verify)")
                    continue
                findings.append(f"{f}: `{first[:60]}` exit {r.returncode}: "
                                f"{err[:200]}")

    note = [f"executed {ran} block(s), skipped {len(skipped)}"]
    # skips are always visible; they are never silently absorbed
    note.extend("skipped " + s for s in skipped[:20])
    if findings:
        return "C5 doc commands run", "FAIL", findings + warnings + note
    if warnings:
        return "C5 doc commands run", "WARN", warnings + note
    if ran == 0:
        # NOT a pass (it cannot see a document) and NOT a failure (the docs may
        # legitimately need docker). A named coverage gap, reported every run.
        return "C5 doc commands run", "WARN", \
            ["no doc block was runnable on this box -- docs are UNVERIFIED here"] + note
    return "C5 doc commands run", "PASS", warnings + note


def c6_sources_parse(root):
    findings = []
    for f in _tracked(root):
        if f.endswith(CODE_SUFFIXES):
            try:
                ast.parse(open(os.path.join(root, f), encoding="utf-8",
                               errors="replace").read(), filename=f)
            except SyntaxError as e:
                findings.append(f"{f}: {e}")
    return "C6 sources parse", "FAIL" if findings else "PASS", findings


CHECKS = [c1_tracked_junk, c2_secrets_head, c3_secrets_history,
          c4_gitignore, c5_doc_commands, c6_sources_parse]


def run(root="."):
    """Tri-state: PASS / WARN / FAIL. Only FAIL fails the build; WARN is
    printed every run. A check that cannot see is never allowed to be silent."""
    results = []
    for fn in CHECKS:
        try:
            name, status, findings = fn(root)
        except Exception as e:                     # a crash is a FAILED check
            name, status, findings = fn.__name__, "FAIL", [f"check crashed: {e!r}"]
        results.append((name, status, findings))
    return all(s == "PASS" for _, s, _ in results), results


# --- negative controls ----------------------------------------------------
def _git(root, *args):
    subprocess.run(["git", "-C", root, *args], capture_output=True, check=True,
                   text=True)


def selftest():
    """One deliberately-bad tree per check; each check MUST fail on it."""
    print("negative controls — each check must FAIL on a lying input\n")
    outcomes = []

    def farm():
        d = tempfile.mkdtemp(prefix="rc_")
        _git(d, "init", "-q")
        _git(d, "config", "user.email", "t@t")
        _git(d, "config", "user.name", "t")
        return d

    # C1: a tracked .pyc
    d = farm()
    os.makedirs(os.path.join(d, "__pycache__"))
    open(os.path.join(d, "__pycache__", "m.cpython-312.pyc"), "wb").write(b"\x00")
    open(os.path.join(d, ".gitignore"), "w").write(
        "__pycache__\n*.py[cod]\n*.log\n*.pid\n*.sqlite3\ncontrol-token\n")
    _git(d, "add", "-f", ".")
    _git(d, "commit", "-qm", "junk")
    _, r = run(d)
    outcomes.append(("C1 catches tracked .pyc",
                     dict((n, o) for n, o, _ in r)["C1 tracked junk"] == "FAIL"))
    shutil.rmtree(d)

    # C2: a secret in a tracked file at HEAD
    d = farm()
    open(os.path.join(d, "a.txt"), "w").write("token: REDACTED-CONTROL-TOKEN-ROTATE-ME\n")
    open(os.path.join(d, ".gitignore"), "w").write(
        "__pycache__\n*.py[cod]\n*.log\n*.pid\n*.sqlite3\ncontrol-token\n")
    _git(d, "add", "-f", ".")
    _git(d, "commit", "-qm", "secret")
    _, r = run(d)
    outcomes.append(("C2 catches a secret at HEAD",
                     dict((n, o) for n, o, _ in r)["C2 secrets in HEAD"] == "FAIL"))
    shutil.rmtree(d)

    # C3: a secret that exists ONLY in history (deleted at HEAD)
    d = farm()
    open(os.path.join(d, ".gitignore"), "w").write(
        "__pycache__\n*.py[cod]\n*.log\n*.pid\n*.sqlite3\ncontrol-token\n*.log\n")
    open(os.path.join(d, "control.log"), "w").write(
        "[control] token: REDACTED-CONTROL-TOKEN-ROTATE-ME\n")
    # NOTE: .gitignore is committed too, so the tree has >=2 blobs. Without
    # that, C3's own coverage guard (a walk of <2 blobs is WARN, not PASS)
    # fires and this control would be asserting against a WARN, which would
    # hide the very thing it is testing. A control fixture has to satisfy the
    # check's preconditions or it tests the fixture.
    _git(d, "add", "-f", ".gitignore", "control.log")
    _git(d, "commit", "-qm", "leak")
    _git(d, "rm", "-q", "--cached", "control.log")
    _git(d, "commit", "-qm", "remove")
    _, r = run(d)
    res = dict((n, o) for n, o, _ in r)
    outcomes.append(("C3 catches a secret that exists only in HISTORY", res["C3 secrets in HISTORY"] == "FAIL"))
    outcomes.append(("C3 does NOT fire on junk alone", res["C1 tracked junk"] == "PASS"))
    shutil.rmtree(d)

    # C3 coverage: a walk that inspected almost nothing must NOT read PASS.
    d = farm()
    open(os.path.join(d, "only.py"), "w").write("x = 1\n")
    _git(d, "add", "-f", ".")
    _git(d, "commit", "-qm", "one blob")
    _, r = run(d)
    status = dict((n, o) for n, o, _ in r)["C3 secrets in HISTORY"]
    outcomes.append(("C3 refuses to PASS when it inspected almost nothing",
                     status == "WARN"))
    shutil.rmtree(d)

    # C4: no .gitignore at all
    d = farm()
    open(os.path.join(d, "x.py"), "w").write("print(1)\n")
    _git(d, "add", "-f", ".")
    _git(d, "commit", "-qm", "no ignore")
    _, r = run(d)
    outcomes.append(("C4 catches a missing .gitignore",
                     dict((n, o) for n, o, _ in r)["C4 .gitignore covers junk"] == "FAIL"))
    shutil.rmtree(d)

    # C5: a README command that does not run
    d = farm()
    open(os.path.join(d, ".gitignore"), "w").write(
        "__pycache__\n*.py[cod]\n*.log\n*.pid\n*.sqlite3\ncontrol-token\n")
    open(os.path.join(d, "README.md"), "w").write(
        "# x\n\n```sh\npython3 -c \"import definitely_not_a_module_xyz\"\n```\n")
    _git(d, "add", "-f", ".")
    _git(d, "commit", "-qm", "bad doc")
    _, r = run(d)
    outcomes.append(("C5 catches a doc command that fails",
                     dict((n, o) for n, o, _ in r)["C5 doc commands run"] == "FAIL"))
    shutil.rmtree(d)

    # C6: a .py that does not parse
    d = farm()
    open(os.path.join(d, ".gitignore"), "w").write(
        "__pycache__\n*.py[cod]\n*.log\n*.pid\n*.sqlite3\ncontrol-token\n")
    open(os.path.join(d, "broken.py"), "w").write("def f(:\n")
    _git(d, "add", "-f", ".")
    _git(d, "commit", "-qm", "bad syntax")
    _, r = run(d)
    outcomes.append(("C6 catches a SyntaxError",
                     dict((n, o) for n, o, _ in r)["C6 sources parse"] == "FAIL"))
    shutil.rmtree(d)

    bad = 0
    for label, passed in outcomes:
        print(f"  {'PASS' if passed else 'FAIL'}  {label}")
        bad += (not passed)
    print(f"\nselftest: {len(outcomes) - bad}/{len(outcomes)} controls passed")
    return bad == 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    root = next((a for a in sys.argv[1:] if not a.startswith("-")), ".")
    ok, results = run(root)
    for name, status, findings in results:
        print(f"[{ {'PASS': 'ok  ', 'WARN': 'WARN', 'FAIL': 'FAIL'}[status] }] {name}")
        for f in findings[:12]:
            print(f"        {f}")
        if len(findings) > 12:
            print(f"        ... and {len(findings) - 12} more")
    fails = sum(1 for _, s, _ in results if s == "FAIL")
    warns = sum(1 for _, s, _ in results if s == "WARN")
    print(f"\nREPO CHECK: {'PASS' if ok else 'FAIL'} "
          f"({len(results) - fails - warns} pass, {warns} warn, {fails} fail)")
    sys.exit(0 if ok else 1)
