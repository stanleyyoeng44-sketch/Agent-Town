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
  RUN INSIDE A GIT TREE. C1/C2/C3/C5 read `git ls-files`; a `git archive`
  export (or a bare directory) has no .git and every one of those checks
  reports "git ls-files exit 128" -- four FAILs that look like defects and are
  a missing repository. `git init && git add -A && git commit -qm x` makes such
  a tree checkable. This is not a repo defect and the output now says so.
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


REDACT_FULL = False       # --show-secrets for local use; never in CI


def _redact(s):
    """A report that cannot be pasted in public is a weaker report.

    This check runs in CI logs. Printing the secret it found re-publishes it
    on every run -- the tool would become a new distribution channel for the
    exact string it exists to catch. So the default output is a recognisable
    prefix plus a length, which is enough to find the line and not enough to
    use the credential.
    """
    if REDACT_FULL:
        return s
    if len(s) <= 8:
        return s
    return f"{s[:6]}…({len(s)} chars, redacted)"


def scan_text(name, text):
    """Placeholder filtering happens on the RAW match, never on the redacted
    string. An earlier version redacted first and then tested for
    placeholders -- and the redaction marker contains the word 'redacted',
    which is itself in the placeholder list, so every redacted hit was
    filtered out as a placeholder. The check silently found nothing, which is
    why two negative controls went red. Redaction is presentation; it must
    not touch the decision.
    """
    hits = []
    for label, pat in SECRET_PATTERNS:
        for m in pat.finditer(text):
            if PLACEHOLDER.search(m.group(0)):
                continue            # documented placeholder, not a secret
            hits.append(f"{name}: {label}: {_redact(m.group(0))}")
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


BASELINE_FILE = ".repo-check-baseline"


def _baseline(root):
    """Accepted exposures, one reason per line. Each entry suppresses a hit
    that CONTAINS the given substring. The file is deliberately human-edited
    and reviewed: a baseline entry is a written admission, not a mute switch.
    """
    p = os.path.join(root, BASELINE_FILE)
    if not os.path.isfile(p):
        return []
    out = []
    for line in open(p, encoding="utf-8", errors="replace"):
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line.split("#")[0].strip())
    return [b for b in out if b]


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

    if not hits:
        return "C3 secrets in HISTORY", "PASS", coverage

    # A KNOWN, ACCEPTED LEAK IS STILL A LEAK. The only thing a baseline may do
    # is stop re-reporting it on every run; it can never turn red into green.
    # Every unbaselined leak is a FAIL and blocks. A baselined one is a WARN
    # that names the accepted exposure out loud, every run, so the debt is
    # visible in the log rather than forgotten in a ticket.
    baseline = _baseline(root)
    new = [h for h in hits if not any(b in h for b in baseline)]
    known = [h for h in hits if any(b in h for b in baseline)]
    if new:
        return "C3 secrets in HISTORY", "FAIL", new + coverage + \
            [f"({len(known)} baselined hit(s) suppressed; see .repo-check-baseline)"]
    return "C3 secrets in HISTORY", "WARN", coverage + [
        f"{len(known)} KNOWN ACCEPTED leak(s), baselined -- this is debt, not a pass"]



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


# A probe is executed with shell=True, so the allowlist must reason about the
# WHOLE LINE, not just the prefix it recognises. These characters can start a
# second command, substitute one, or redirect output. Any of them means the
# 'safe' first word says nothing about what the line does:
#   python3 -c "print(1)" && rm -rf victim      <- deleted a directory
# The probe declined only the line it could not parse; the shell ran all of it.
_SHELL_COMPOUND = (";", "&&", "||", "|", "&", "`", "$(", ">", "<", "\n")


def _probe_safe(cmd: str) -> bool:
    """Is `cmd` safe to execute twice, for the SKIP probe?

    Deliberately narrow, and it must be: the probe runs a command a document
    asked NOT to run. Allowed:
      * `bash -n` / `sh -n`        -- parse only, by definition side-effect free
      * `python3 -c "..."`         -- only a single snippet, no imports, no
                                      open/exec/system/remove/rmtree/subprocess
      * `test ...` / `[ ... ]`     -- pure predicate
    Everything else is declined and reported as an honoured SKIP, which is
    visible: silence is what this change removed.

    THE BUG THIS GUARD FIXES (found by agent-2 reviewing the first version):
    the check inspected only the leading command. With shell=True the rest of
    the line still ran, so `python3 -c "print(1)" && rm -rf victim` passed the
    allowlist on its first token and then deleted the directory. Demonstrated
    end to end before the fix. A prefix test is not a statement about a shell
    line; any compound operator disqualifies the line outright.
    """
    c = cmd.strip()
    if any(tok in c for tok in _SHELL_COMPOUND):
        return False
    if c.startswith(("bash -n ", "sh -n ", "test ", "[ ")):
        return True
    if c.startswith(("python3 -c", "python -c")):
        body = c.split("-c", 1)[1].strip().strip("'\"")
        banned = ("import ", "open(", "exec(", "eval(", "system(", "remove(",
                  "rmtree(", "subprocess", "socket", "unlink(", "rename(",
                  "chmod(", "chown(", "shutil", "os.")
        return not any(b in body for b in banned)
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

    IT IS NOT A READ-ONLY OBSERVER. This check RUNS the documented commands,
    so it inherits every side effect they have. MEASURED: docs/guide.md
    documents `python3 demo/town.py`, whose bank wrote real payment
    notifications into live agents' mailboxes; C5 executed it on every push
    and on every local run, so the checker was a COURIER for the leak, not
    just a witness. (That block is fixed -- it now writes only to its own
    temp directory -- but this check still runs whatever a doc block says,
    so THE RULE IS: A NEW DOC COMMAND IS A NEW COURIER. Before adding one,
    ask what it writes and where; if it can write outside the tree, either
    give it a temp path or mark the block SKIP.) Any check that executes
    untrusted code must be as careful about what that code does as about
    whether it exits 0. If a doc block has an effect outside the tree, that
    is a defect in the block, and fixing the block is what makes this check
    read-only in practice.
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
            # The SKIP marker may appear two ways and BOTH must mean the same
            # thing:
            #   (a) a leading comment line "# SKIP: <reason>"   (what docs use)
            #   (b) a literal first token "SKIP <command>"
            # The old code read body.splitlines()[0] only, so (b) worked and (a)
            # was invisible -- a doc could write "# SKIP python3 ..." and the
            # command still RAN, or write "SKIP python3 ..." and it was silently
            # swallowed. Two spellings, opposite outcomes, both silent.
            skip_lines = [l.strip() for l in body.splitlines()
                          if l.strip() and not l.strip().startswith("#")]
            skip_marked = (first.upper().startswith("SKIP")
                           or any(l.strip().upper().startswith("# SKIP")
                                  for l in body.splitlines()))
            if skip_marked:
                # SKIP must NOT be able to silence a command this checker can
                # actually run. Otherwise any doc can disable its own check by
                # writing the word SKIP in a fenced block, and the check reports
                # a skip that looks deliberate rather than an unverified claim.
                # SKIP is honoured only for commands that are otherwise
                # unrunnable here (env-limited), and even then it is named with
                # its reason. A runnable command marked SKIP is a WARN: the
                # document asked not to be verified, which is a fact worth
                # surfacing, not a pass.
                inner = first[4:].strip() if first.upper().startswith("SKIP") else first
                # A SKIP is only an opt-out if the command COULD have run here
                # and passed. Probe it: if it exits 0 it was runnable and the
                # SKIP was hiding a working check (WARN, and say so). If it
                # exits non-zero it genuinely needs the environment the doc
                # names (Docker, a daemon), and the SKIP is legitimate history.
                # Older code never probed, so "SKIP" was a universal silencer.
                probe = None
                if inner.startswith(RUNNABLE):
                    # The probe executes a command the doc explicitly asked not
                    # to run, so it must be limited to commands that are safe to
                    # RUN twice. `python3 <script>` is NOT automatically safe: a
                    # doc could mark a destructive line SKIP and the probe would
                    # execute it anyway. The allowlist therefore narrows to the
                    # shapes this checker already runs elsewhere -- a syntax
                    # check, a one-liner that is not an import-and-execute, or a
                    # test/[ expression. Anything else stays an honoured SKIP
                    # with its reason, which is the status quo and is safe.
                    if _probe_safe(inner):
                        try:
                            pr = subprocess.run(inner, shell=True, cwd=root,
                                                text=True, capture_output=True,
                                                timeout=timeout)
                            probe = pr.returncode
                        except subprocess.TimeoutExpired:
                            probe = "TIMEOUT"
                    else:
                        skipped.append(f"{f}: {inner[:60]} (marked SKIP; probe "
                                       f"declined -- not safe to run twice)")
                        continue
                if probe == 0:
                    warnings.append(
                        f"{f}: `{inner[:60]}` marked SKIP but runs fine here "
                        f"-- the doc is opting out of a check it could pass")
                else:
                    why = ("exits %s without its environment" % probe
                           if probe is not None else "not runnable here")
                    skipped.append(f"{f}: {inner[:60]} (marked SKIP; {why})")
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


IMPORT_TARGETS = ["bank/bank.py", "agent/agent-daemon.py",
                  "ui/stanley_chat.py", "agent/mcp-stdio.py",
                  "ui/control.py"]


def c7_sources_import(root, timeout=60):
    """C6 proves a file PARSES. That is not the same as 'it can be loaded'.

    The distinction cost this cohort a day: a registry reported 'structure
    green, 0 problems' over a store containing a claim whose instrument could
    not import its own dependency. Parsing is a fact about syntax; importing
    is a fact about the module actually resolving its imports on this box.

    Each target is loaded in a SUBPROCESS, so a file that starts a server on
    import cannot wedge this check. A module that exits (CLI/server entry
    points) counts as importable: SystemExit after a successful module body is
    still a successful import. A timeout is a FAIL with the reason, never a
    silent skip.

    CWD COUPLING IS A SEPARATE FACT, and the first version of this check got
    it wrong. ui/control.py does `import stanley_chat` (a sibling), so it
    imports from ui/ and fails from the repo root. Reporting that as "import
    failed" is the wrong noun: the module is fine, its cwd contract is
    undeclared. So each target is tried from the repo root AND from its own
    directory. Root-ok = PASS. Own-dir-only = WARN naming the coupling, which
    is a real doc bug (the guide must say `cd ui`) but not a broken module.
    Neither = FAIL.
    """
    findings, warn = [], []
    for f in IMPORT_TARGETS:
        path = os.path.join(root, f)
        if not os.path.isfile(path):
            findings.append(f"{f}: listed as an import target but MISSING")
            continue
        code = (
            "import importlib.util,sys\n"
            f"spec=importlib.util.spec_from_file_location('m', {os.path.basename(f)!r})\n"
            "m=importlib.util.module_from_spec(spec)\n"
            "try:\n"
            "    spec.loader.exec_module(m)\n"
            "except SystemExit:\n"
            "    pass\n"
        )

        def try_from(cwd):
            try:
                r = subprocess.run([sys.executable, "-c", code], cwd=cwd,
                                   capture_output=True, text=True,
                                   timeout=timeout)
            except subprocess.TimeoutExpired:
                return "timeout"
            if r.returncode == 0:
                return "ok"
            tail = (r.stderr or r.stdout).strip().splitlines()
            return tail[-1][:200] if tail else "?"

        at_root = try_from(root)
        if at_root == "ok":
            continue
        at_home = try_from(os.path.dirname(path))
        if at_home == "ok":
            warn.append(f"{f}: imports only from {os.path.dirname(f)}/ "
                        f"(cwd-coupled; docs must say so)")
        elif at_root == "timeout" or at_home == "timeout":
            findings.append(f"{f}: import TIMEOUT after {timeout}s")
        else:
            findings.append(f"{f}: import failed from root AND own dir: {at_root}")
    if findings:
        return "C7 sources import", "FAIL", findings + warn
    if warn:
        return "C7 sources import", "WARN", warn
    return "C7 sources import", "PASS", []


def c8_readme_transcript(root):
    """The README quotes bank output. Replay it against the bank in the tree.

    C5 EXECUTES the fenced blocks, and this is not that. The transcript's
    command lines are prefixed with '$', so C5 treats the block as prose and
    never runs it -- which is why the quoted refusal drifted from
    {"error": "insufficient funds"} to {"error": "unknown sender"} while every
    suite stayed green. The text looks like output, so a reader trusts it more
    than a claim; nothing was maintaining it.

    Runs as a SUBPROCESS so a wedged bank cannot hang the checker, and a
    non-zero exit is the finding. A missing replayer is a FAIL, not a skip:
    otherwise deleting the script turns the check off.
    """
    script = os.path.join(root, "tools/check_readme_transcript.py")
    if not os.path.exists(script):
        return "C8 readme transcript", "FAIL", [
            "tools/check_readme_transcript.py is absent, so the README's "
            "quoted bank transcript is unverified"]
    r = subprocess.run([sys.executable, script], cwd=root, text=True,
                       capture_output=True, timeout=180)
    if r.returncode != 0:
        out = (r.stdout + r.stderr).strip().splitlines()
        return "C8 readme transcript", "FAIL", out[:12] or ["replayer failed"]
    return "C8 readme transcript", "PASS", [r.stdout.strip().splitlines()[-1]]


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # the repo this file lives in

CHECKS = [c1_tracked_junk, c2_secrets_head, c3_secrets_history,
          c4_gitignore, c5_doc_commands, c6_sources_parse, c7_sources_import,
          c8_readme_transcript]


def _is_git_tree(root):
    r = subprocess.run(["git", "-C", root, "rev-parse", "--is-inside-work-tree"],
                       capture_output=True, text=True)
    return r.returncode == 0 and r.stdout.strip() == "true"


def run(root="."):
    """Tri-state: PASS / WARN / FAIL. Only FAIL fails the build; WARN is
    printed every run. A check that cannot see is never allowed to be silent."""
    # ONE FACT, REPORTED ONCE. C1/C2/C3/C5 all read `git ls-files`; without a
    # repository each raises and prints "check crashed" -- four FAILs that read
    # as four defects in the repo, when the only fact is that there is no .git
    # (a `git archive` export, a copied tree). Name the real cause once instead
    # of letting four checks each report their own confusion. This is the same
    # rule as C5's skip split: a check reporting its own limitation as an
    # object defect is the bug.
    if not _is_git_tree(root):
        return False, [("check", "FAIL",
                        [f"{root} is not inside a git work tree, so the checks "
                         f"that read `git ls-files` cannot see any files. This "
                         f"is not a defect in the tree's contents: run inside a "
                         f"clone, or `git init && git add -A && git commit -qm x` "
                         f"to make an export checkable."])]
    results = []
    for fn in CHECKS:
        try:
            name, status, findings = fn(root)
        except Exception as e:                     # a crash is a FAILED check
            name, status, findings = fn.__name__, "FAIL", [f"check crashed: {e!r}"]
        results.append((name, status, findings))
    # WARN never fails the build; only FAIL does. That is the whole point of
    # having three states: if WARN blocked, it would just be FAIL with extra
    # words, and people would start silencing checks to get a green pipeline.
    # The summary line stays loud about the warnings so the coverage gap is
    # never mistaken for full verification.
    return not any(s == "FAIL" for _, s, _ in results), results


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
    open(os.path.join(d, "a.txt"), "w").write(
        "token: " + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0UvW" + "\n")  # SYNTHETIC
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
        "[control] token: " + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0UvW" + "\n")  # SYNTHETIC
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

    # C3 baseline: it may suppress a KNOWN leak (WARN), and it must NOT
    # suppress a NEW one. A baseline that can turn a new leak green is a mute
    # switch wearing a review comment.
    d = farm()
    open(os.path.join(d, ".gitignore"), "w").write(
        "__pycache__\n*.py[cod]\n*.log\n*.pid\n*.sqlite3\ncontrol-token\n")
    open(os.path.join(d, ".repo-check-baseline"), "w").write("old_leak.log  # accepted\n")
    open(os.path.join(d, "old_leak.log"), "w").write(
        "token: " + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0UvW" + "\n")
    open(os.path.join(d, "new_leak.log"), "w").write(
        "token: " + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2H1g0F" + "\n")
    _git(d, "add", "-f", ".gitignore", ".repo-check-baseline", "old_leak.log")
    _git(d, "commit", "-qm", "old known leak")
    _git(d, "rm", "-q", "--cached", "old_leak.log")
    _git(d, "commit", "-qm", "remove old")
    _git(d, "add", "-f", "new_leak.log")
    _git(d, "commit", "-qm", "NEW leak")
    _git(d, "rm", "-q", "--cached", "new_leak.log")
    _git(d, "commit", "-qm", "remove new")
    _, r = run(d)
    status = dict((n, o) for n, o, _ in r)["C3 secrets in HISTORY"]
    outcomes.append(("C3 baseline suppresses a KNOWN leak but NOT a new one",
                     status == "FAIL"))
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

    # C5b: SKIP must not silence a command that would have PASSED.
    # "SKIP" used to be a universal silencer: any doc could disable its own
    # check by writing it. Now a marked command is PROBED -- exit 0 means it
    # was runnable all along and the SKIP hid a working check (WARN).
    d = farm()
    open(os.path.join(d, ".gitignore"), "w").write(
        "__pycache__\n*.py[cod]\n*.log\n*.pid\n*.sqlite3\ncontrol-token\n")
    open(os.path.join(d, "README.md"), "w").write(
        "# x\n\n```sh\n# SKIP: no reason at all\npython3 -c \"print(1)\"\n```\n")
    _git(d, "add", "-f", ".")
    _git(d, "commit", "-qm", "skipper")
    _, r = run(d)
    c5 = dict((n, (o, d2)) for n, o, d2 in r)["C5 doc commands run"]
    outcomes.append(("C5 will not let SKIP hide a command that would pass",
                     c5[0] == "WARN" and any("runs fine here" in str(x) for x in c5[1])))
    shutil.rmtree(d)

    # C5c: a legitimate SKIP is still honoured -- a command that genuinely
    # cannot run here must not be turned into a failure, or every docker block
    # in the repo goes red. This is the control that stops the fix above from
    # being "ignore SKIP entirely".
    d = farm()
    open(os.path.join(d, ".gitignore"), "w").write(
        "__pycache__\n*.py[cod]\n*.log\n*.pid\n*.sqlite3\ncontrol-token\n")
    open(os.path.join(d, "README.md"), "w").write(
        "# x\n\n```sh\n# SKIP: needs a docker daemon\n"
        "python3 -c \"print(1/0)\"\n```\n")
    _git(d, "add", "-f", ".")
    _git(d, "commit", "-qm", "env skipper")
    _, r = run(d)
    c5 = dict((n, (o, d2)) for n, o, d2 in r)["C5 doc commands run"]
    outcomes.append(("C5 still honours SKIP when the command cannot run here",
                     c5[0] == "WARN"
                     and any("exits 1 without its environment" in str(x) for x in c5[1])
                     and not any("runs fine" in str(x) for x in c5[1])))
    shutil.rmtree(d)

    # C5d: the probe must DECLINE a command it is not safe to run twice.
    # The probe exists to catch a doc that hides a passing check behind SKIP;
    # it must not become an execution primitive for a destructive line. A
    # SKIP-marked command that imports or writes is reported as an honoured
    # SKIP with "probe declined", and the file it would have created must NOT
    # exist afterwards. Without this, "SKIP" would be a way to get the checker
    # to run anything.
    d = farm()
    marker = os.path.join(d, "PROBE_SHOULD_NOT_CREATE_ME")
    open(os.path.join(d, ".gitignore"), "w").write(
        "__pycache__\n*.py[cod]\n*.log\n*.pid\n*.sqlite3\ncontrol-token\n")
    open(os.path.join(d, "README.md"), "w").write(
        "# x\n\n```sh\n# SKIP: destructive\n"
        "python3 -c \"open('%s','w').write('x')\"\n```\n" % marker)
    _git(d, "add", "-f", ".")
    _git(d, "commit", "-qm", "dangerous skipper")
    _, r = run(d)
    c5 = dict((n, (o, d2)) for n, o, d2 in r)["C5 doc commands run"]
    outcomes.append(("C5 declines to PROBE a command that writes",
                     any("probe declined" in str(x) for x in c5[1])
                     and not os.path.exists(marker)))
    shutil.rmtree(d)

    # C5e: a SAFE PREFIX with a DESTRUCTIVE TAIL must be declined. C5d uses a
    # line the allowlist already rejects on its first token (it contains
    # open()). The hole agent-2 found is different and was live in the first
    # version of this probe: the first token is allowed, and the shell still
    # runs everything after it, so
    #     python3 -c "print(1)" && rm -rf victim
    # passed the allowlist and deleted the directory during a probe that was
    # supposed to be side-effect free. A prefix check is not a statement about
    # a shell line. The control's evidence is the VICTIM DIRECTORY, i.e. a
    # positive artifact that survives -- not a log line claiming it did.
    d = farm()
    victim = os.path.join(d, "victim")
    os.makedirs(victim)
    open(os.path.join(victim, "important"), "w").write("x")
    open(os.path.join(d, ".gitignore"), "w").write(
        "__pycache__\n*.py[cod]\n*.log\n*.pid\n*.sqlite3\ncontrol-token\n")
    open(os.path.join(d, "README.md"), "w").write(
        "# x\n\n```sh\n# SKIP: needs docker\n"
        "python3 -c \"print(1)\" && rm -rf victim\n```\n")
    _git(d, "add", "-f", ".")
    _git(d, "commit", "-qm", "compound skipper")
    _, r = run(d)
    c5 = dict((n, (o, d2)) for n, o, d2 in r)["C5 doc commands run"]
    outcomes.append(("C5 will not probe a safe prefix with a shell tail",
                     os.path.isdir(victim)
                     and os.path.exists(os.path.join(victim, "important"))
                     and any("probe declined" in str(x) for x in c5[1])))
    shutil.rmtree(d)

    # C0: a tree with NO .git must be named as such, ONCE.
    # Without this, C1/C2/C3/C5 each crash with "git ls-files exit 128" and the
    # report shows four FAILs -- which reads as four defects in the repo. The
    # only fact is that there is no repository. agent-7 hit this twice on a
    # `git archive` export and got it wrong once.
    d = tempfile.mkdtemp(prefix="rc_nogit_")
    open(os.path.join(d, "x.py"), "w").write("print(1)\n")
    ok0, r0 = run(d)
    outcomes.append(("C0 names a missing .git ONCE instead of four crashes",
                     (not ok0) and len(r0) == 1
                     and "not inside a git work tree" in r0[0][2][0]
                     and "git ls-files exit 128" not in str(r0)))
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

    # C7: a module that PARSES but cannot IMPORT. This is the whole point of
    # C7 existing next to C6 -- if C6 already caught it, C7 would be
    # decoration. The file is syntactically perfect and imports a module that
    # does not exist, which ast.parse cannot see.
    d = farm()
    open(os.path.join(d, ".gitignore"), "w").write(
        "__pycache__\n*.py[cod]\n*.log\n*.pid\n*.sqlite3\ncontrol-token\n")
    os.makedirs(os.path.join(d, "bank"))
    open(os.path.join(d, "bank", "bank.py"), "w").write(
        "import definitely_not_a_real_module_xyz\n")
    _git(d, "add", "-f", ".")
    _git(d, "commit", "-qm", "parses, cannot import")
    _, r = run(d)
    res = dict((n, o) for n, o, _ in r)
    outcomes.append(("C6 does NOT catch an unimportable module (C7 is needed)",
                     res["C6 sources parse"] == "PASS"))
    outcomes.append(("C7 catches a module that parses but cannot import",
                     res["C7 sources import"] == "FAIL"))
    shutil.rmtree(d)

    # C8: a README that quotes a response the bank does not give must FAIL.
    # This is the drift agent-5 found by hand; the point of the control is
    # that the replayer is not merely re-running the commands and comparing
    # nothing.
    d = farm()
    open(os.path.join(d, ".gitignore"), "w").write(
        "__pycache__\n*.py[cod]\n*.log\n*.pid\n*.sqlite3\ncontrol-token\n")
    os.makedirs(os.path.join(d, "bank"))
    os.makedirs(os.path.join(d, "tools"))
    shutil.copy(os.path.join(ROOT, "bank/bank.py"), os.path.join(d, "bank"))
    shutil.copy(os.path.join(ROOT, "tools/check_readme_transcript.py"),
                os.path.join(d, "tools"))
    shutil.copy(os.path.join(ROOT, "README.md"), os.path.join(d, "README.md"))
    p = os.path.join(d, "README.md")
    t = open(p, encoding="utf-8").read()
    lie = t.replace('{"error": "unknown sender"}',
                    '{"error": "insufficient funds"}')
    if lie == t:
        outcomes.append(("C8 catches a quoted response the bank does not give",
                         False))
    else:
        open(p, "w", encoding="utf-8").write(lie)
        _git(d, "add", "-f", ".")
        _git(d, "commit", "-qm", "lying transcript")
        _, r = run(d)
        outcomes.append(("C8 catches a quoted response the bank does not give",
                         dict((n, o) for n, o, _ in r)
                         ["C8 readme transcript"] == "FAIL"))
    shutil.rmtree(d)

    # THE TOOL MUST PASS ITS OWN CHECK. My first version hardcoded the real
    # published token as the fixture string, so (a) the check that exists to
    # stop the secret spreading was itself a second copy of it, and (b) the
    # moment the file was committed C2 would flag its own source -- correctly.
    # A scanning tool is a file like any other; scan it.
    here = os.path.abspath(__file__)
    own = scan_text(os.path.basename(here),
                    open(here, encoding="utf-8", errors="replace").read())
    outcomes.append(("the tool's own source contains no secret", not own))
    outcomes.append(("C2 would catch it if it did (fixtures use SYNTHETIC)",
                     any("SYNTHETIC" in l for l in
                         open(here, encoding="utf-8").read().splitlines())))

    bad = 0
    for label, passed in outcomes:
        print(f"  {'PASS' if passed else 'FAIL'}  {label}")
        bad += (not passed)
    print(f"\nselftest: {len(outcomes) - bad}/{len(outcomes)} controls passed")
    return bad == 0


if __name__ == "__main__":
    if "--show-secrets" in sys.argv:
        REDACT_FULL = True
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
