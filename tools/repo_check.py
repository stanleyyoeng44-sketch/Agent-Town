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
    C14 check registry     every c<N>_ check defined here is in CHECKS, so a
                           merge that drops one list entry cannot turn a check
                           off silently (nothing else reads the registry)

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
import json
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
# A leading VAR=value is environment, not the command; _strip_env_prefix
# looks through it. It used to be a single regex here whose \S* stopped at
# the first space INSIDE a $( ) substitution -- see _strip_env_prefix.


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

    ALLOWED, and nothing else:
      * `bash -n` / `sh -n`        -- parse only, by definition side-effect free
      * `test ...` / `[ ... ]`     -- pure predicate, once compounds are out
    Everything else is declined and reported as an honoured SKIP, which is
    VISIBLE: silence is what this change removed.

    WHY `python3 -c` IS NO LONGER PROBED AT ALL. Two rounds of review found
    two different ways past a substring list, and the second one is decisive:

      1. `python3 -c "print(1)" && rm -rf victim` -- the allowlist judged the
         first command and shell=True ran the rest. (agent-2; the line deleted
         a directory during a probe.)
      2. `python3 -c "__builtins__.__dict__['op'+'en'](path,'w')..."` --
         string concatenation spells 'open' without the substring 'open'.
         (agent-10; the file was written on both this branch and its parent.)

    A substring list answers "does this text look dangerous", and the question
    that matters is "what will this program do". Those are different questions
    and the first will keep losing to the next spelling. Rather than blocklist
    a third time, the probe gives up on this shape: a SKIP-marked python line
    becomes an honoured, reported SKIP. The cost is that a doc hiding a
    WORKING python one-liner behind SKIP is no longer caught -- accepted, and
    named here, because the alternative is a checker that executes code it
    cannot reason about.

    This is policy, not a remaining probe bug: C5 already executes UNMARKED
    blocks whose first word is runnable, so any tracked document is executable
    by design. The probe only owes an answer for lines a doc marked NOT to run.
    """
    c = cmd.strip()
    if any(tok in c for tok in _SHELL_COMPOUND):
        return False
    if c.startswith(("bash -n ", "sh -n ")):
        return True
    if c.startswith(("test ", "[ ")):
        # `-a`/`-o` are test's own operators, and `test -f a -a rm -rf x` is a
        # syntactically odd expression whose safety depends on the shell's
        # argument handling, so a line mentioning a command word is declined.
        return not any(w in c.split() for w in ("rm", "mv", "cp", "dd", "sh",
                                                "bash", "python", "curl",
                                                "wget", "chmod", "chown"))
    return False


def _strip_env_prefix(line):
    """Strip leading VAR=value assignments from a command line.

    MEASURED DEFECT THIS REPLACES (agent-2, reproduced by agent-4): the old
    form was ENV_PREFIX = r"^[A-Za-z_][A-Za-z0-9_]*=\\S*\\s+", and \\S* stops
    at the first whitespace INSIDE a command substitution. On the line
    docs/CLOUD-DEPLOYMENT.md actually ships:

        docker0=$(ip -o addr show docker0 | awk '{print $4}' | cut -d/ -f1)

    it consumed 'docker0=$(ip' and returned '-o addr show docker0 | ...'.
    That value is what C5 CLASSIFIES and what it SHOWS as the skip reason, so
    the checker reported a fragment of a command as the command.

    IT WAS REPORTED AS COSMETIC AND IT WAS NOT. MEASURED on ede3310, same
    parser, a block that is ONE shell assignment:

        X=$(env python3 -c "open('/tmp/.../SIDE_EFFECT','w').write('ran')")

    parsed as first='python3 -c "open(...)...")', matched RUNNABLE, and THE
    WHOLE LINE WAS EXECUTED -- the file appeared and the run said PASS. So a
    garbled parse is not a garbled label: it decides WHETHER A DOCUMENT'S CODE
    RUNS. The arm ("C5 does not EXECUTE a block that is only an assignment")
    asserts the positive artifact, and the old parser fails it.

    Rules, in order:
      * VAR=$( ... )  consume to the MATCHING ')' (quotes respected), then
        strip the whitespace after it
      * VAR=value     consume the value to the first unquoted whitespace
      * if nothing is left, the line was ONLY assignments -- return the line
        unchanged rather than "", so it is still visible as a skipped block
    """
    s = line
    while True:
        m = re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", s)
        if not m:
            return s
        rest = s[m.end():]
        if rest.startswith("$("):
            depth, i = 0, 0
            quote = None
            while i < len(rest):
                c = rest[i]
                if quote:
                    if c == quote:
                        quote = None
                elif c in ("'", '"'):
                    quote = c
                elif c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                i += 1
            tail = rest[i:]
        else:
            mm = re.match(r"\S*", rest)
            tail = rest[mm.end():]
            if not tail:
                return s          # bare NAME=value: still the whole line
        tail = tail.lstrip()
        if not tail:
            return s              # assignment(s) only
        s = tail


def _first_cmd(body):
    for line in body.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            s = _strip_env_prefix(s)
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

    WHAT IT DOES NOT MEASURE, stated because the summary line used to imply
    otherwise. It counts blocks, not COMMANDS: a 12-line block with one
    runnable first line and eleven unrunnable ones is "executed". It does not
    run docker, so a docker doc can only ever be UNVERIFIED here. And it
    classifies by the FIRST WORD, so a compound line is judged by what it
    starts with -- the 'cd <dir> &&' form is the one exception, and it is
    there so the WHERE of a command is not mistaken for the WHAT.
    """
    findings, ran, skipped, warnings = [], 0, [], []
    # PER-FILE COVERAGE, because the summary line was not one.
    # MEASURED on master 633d62e: "executed 5 block(s), skipped 40" while
    # docs/CLOUD-DEPLOYMENT.md -- 11 fenced blocks, the file README.md calls a
    # verified migration transcript -- appeared ZERO times in either bucket's
    # visible output (the skip list is capped at 20 and nothing asked for it by
    # file). A total cannot tell a reader that one document is entirely
    # unexecuted; that fact only exists per file, so it is reported per file.
    per_file = {}          # f -> [blocks, executed, {reason: count}]
    for f in _tracked(root):
        if not f.endswith(DOC_SUFFIXES):
            continue
        counts = per_file.setdefault(f, [0, 0, {}])

        def did_not_run(reason):
            """Record why THIS file's block did not execute, then keep going."""
            counts[2][reason] = counts[2].get(reason, 0) + 1

        text = open(os.path.join(root, f), encoding="utf-8", errors="replace").read()
        for block in SH_BLOCK.finditer(text):
            counts[0] += 1
            body = block.group(1).strip()
            first = _first_cmd(body)
            if not first:
                did_not_run("no command line")
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
                        did_not_run("SKIP-marked, probe declined")
                        continue
                if probe == 0:
                    warnings.append(
                        f"{f}: `{inner[:60]}` marked SKIP but runs fine here "
                        f"-- the doc is opting out of a check it could pass")
                else:
                    reason = ("exits %s without its environment" % probe
                              if probe is not None else "not runnable here")
                    skipped.append(f"{f}: {inner[:60]} (marked SKIP; {reason})")
                    did_not_run("SKIP-marked: " + reason)
                continue
            if first.startswith(ENV_LIMITED):
                skipped.append(f"{f}: {first[:60]} (no docker/make on this box)")
                did_not_run("needs docker/make (absent here)")
                continue
            if not first.startswith(RUNNABLE):
                skipped.append(f"{f}: {first[:60]} (not a command this check runs)")
                did_not_run("first word is not a command this check runs")
                continue
            ran += 1
            counts[1] += 1
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
    # A DOCUMENT NOTHING RUNS IS NAMED. The single total could not express
    # this, and that is how it hid a whole file: MEASURED on master 633d62e,
    # "executed 5 block(s), skipped 40" over a tree where
    # docs/CLOUD-DEPLOYMENT.md -- 11 fenced blocks, the file README.md calls a
    # verified migration transcript -- did not appear in either bucket's
    # VISIBLE output (the skip list is capped at 20). A total is a fact about
    # the checker; "this document has never run" is a fact about the document,
    # and it only exists per file.
    # Reasons are reported, not just the count, because they are different
    # facts: a block needing docker may run on a machine that has it, while
    # "first word is not a command this check runs" is a limit of the checker.
    uncovered = []
    for f in sorted(per_file):
        blocks, executed, reasons = per_file[f]
        if blocks and not executed:
            detail = "; ".join(f"{n} x {r}" for r, n in sorted(reasons.items(),
                                                               key=lambda kv: -kv[1]))
            uncovered.append(f"{f}: 0 of {blocks} block(s) executed "
                             f"-- UNVERIFIED here ({detail})")
    note.extend(uncovered)
    # skips are always visible; they are never silently absorbed
    note.extend("skipped " + s for s in skipped[:20])
    if uncovered:
        # NOT a PASS, and NOT silent. WARN rather than FAIL for the same reason
        # the existing no-runnable-block case is WARN: the blocks may
        # legitimately need docker, and WARN never claims the docs are correct.
        # Verified: on a tree where every block executes, this arm does not
        # fire (the C5 selftest arms assert both sides).
        warnings = warnings + [
            "a tracked doc with 0 executed blocks is UNVERIFIED, not verified "
            "-- named per file above"]
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
        # THE CHILD MUST BE GIVEN AN ABSOLUTE PATH. The first version passed
        # `os.path.basename(f)` and ran the child with cwd=root, so it looked
        # for <root>/agent-daemon.py -- which does not exist for any target
        # below the top level. On a box with claude_agent_sdk installed the
        # interpreter import-failed anyway and the basename never mattered;
        # on a bare runner it produced `FileNotFoundError: <repo>/agent-daemon.py`
        # and C7 failed for a reason that has nothing to do with the module.
        # A check that reports its own bug as an object defect is the exact
        # pattern this file warns about elsewhere.
        code = (
            "import importlib.util,sys\n"
            f"spec=importlib.util.spec_from_file_location('m', {os.path.abspath(path)!r})\n"
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
            # Name the FIRST line that says what went wrong, not the last:
            # a traceback's last line is the symptom, and its first
            # "XError:" is the cause.
            for line in tail:
                if line.startswith(("ModuleNotFoundError", "ImportError",
                                    "FileNotFoundError", "SyntaxError",
                                    "AttributeError", "NameError")):
                    return line[:200]
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


# Numbers the README states about the code, and how to recompute each one.
# A count in prose is a courier, not a check: every one of these drifted at
# least once tonight (26 checks when there were 28; 12 modules when there
# were 17; "five ways" when there were six; 101 claims when the graph held
# 102). Each entry is (regex with one group, callable returning the truth).
# The regex describes the SENTENCE, not just the number, so a sentence that
# survives with a swapped value is still caught.
README_COUNT_CLAIMS = [
    (r"—\s*(\d+)\s+checks",
     lambda r: _demo_pass_lines(r)),
    (r"`python3 demo/town\.py`,\s*(\d+)\s+checks",
     lambda r: _demo_pass_lines(r)),
    (r"mutates the bank\s+(\w+)\s+ways",
     lambda r: _selftest_control_lines(r)),
    (r"`git ls-files '\*\.py'`\s*\((\d+)\s+files\)",
     lambda r: len([x for x in sh_ls(r) if x.strip()])),
    # NO CLAIMGRAPH NUMBER LIVES HERE, and it is C13 there, not an omission.
    # agent-8's finding was real: mutating the README's claimgraph numbers
    # left repo_check green. An earlier version of this comment said the
    # counts had "MOVED OUT of the prose into a table generated by build.py"
    # and that regexes here would be "a control matching nothing". BOTH HALVES
    # OF THAT WERE FALSE IN THE TREE IT SHIPPED IN, and it is worth keeping
    # the correction: only docs/claimgraph/README.md has a generated table;
    # the TOP-LEVEL README still states the four numbers in a sentence
    # ("holds 165 claims and 111 typed edges, 102 of them supersessions, plus
    # 23 retractions"), so a regex here would have matched 4/4 -- measured by
    # restored by a rebase AFTER the comment was written and no gate could
    # see the pair.
    # THE ARM THIS CLAIM RESTS ON IS A FORCED-WRONG ONE, not a historical
    # sha. Measured by the author of this comment on ede3310 with four
    # single-group regexes restored: correct numbers -> C8 PASS "recomputed
    # 8"; all four forced to truth+1 -> C8 FAIL naming all four. A
    # restore-the-regexes arm alone proves nothing here, because it is GREEN
    # on any tree where C13 has already corrected the sentence -- a
    # predicate pinned to an artifact that moves, which is how the sentence
    # became false in the first place. The historical instance (d791564:
    # 113/68/60/21 against 165/111/102/23) is agent-8's measurement and is
    # cited as an instance, not as the proof.
    # The fix is C13 (c13_claimgraph_prose_numbers): it recomputes those four
    # values from graph.json, FAILs on a wrong one, and FAILs when the
    # sentence is DELETED rather than silently shrinking. Putting a second
    # copy of the same assertions in this table would be the duplicate; one
    # gate that owns the sentence is the stronger mechanism.
]
_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
          "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def _demo_pass_lines(root):
    out = subprocess.run([sys.executable, "-I", "demo/town.py"], cwd=root,
                         text=True, capture_output=True)
    return len([l for l in out.stdout.splitlines() if l.startswith("  PASS")])


def _selftest_control_lines(root):
    out = subprocess.run([sys.executable, "-I", "demo/selftest_demo.py"],
                         cwd=root, text=True, capture_output=True)
    import re as _re
    return len([l for l in out.stdout.splitlines()
                if _re.match(r"^(guard|single|grant|env-var|unknown)", l)])


def sh_ls(root):
    out = subprocess.run(["git", "-C", root, "ls-files", "*.py"],
                         text=True, capture_output=True)
    return out.stdout.splitlines()


TEMP_ROOT = __import__("tempfile").gettempdir()
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # the repo this file lives in


def c8_readme_numbers(root):
    """The README's numbers about the code, recomputed instead of trusted.

    WHY THIS IS A CHECK AND NOT ADVICE: the Verification table is the row
    that makes the rest of the README credible, and three of its numbers were
    stale at once while every suite was green. Each was a value a human had
    copied from an earlier run. Nothing was lying; nothing was maintained.

    A claim whose SENTENCE is gone is a FAIL, not a skip. Otherwise the
    cheapest way to get green is to delete the claim, and the table quietly
    shrinks to nothing -- a check that reports fewer claims every time it is
    appeased. WARN is for a number that cannot be measured on this box.
    """
    path = os.path.join(root, "README.md")
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return "C8 readme numbers", "FAIL", ["README.md is absent"]
    findings, measured, absent = [], {}, []
    for rx, truth in README_COUNT_CLAIMS:
        import re as _re
        m = _re.search(rx, text)
        if not m:
            absent.append(rx)
            continue
        stated_raw = m.group(1)
        stated = _WORDS.get(stated_raw.lower())
        if stated is None:
            try:
                stated = int(stated_raw)
            except ValueError:
                findings.append(f"unparseable number {stated_raw!r} for {rx}")
                continue
        actual = truth(root)
        measured[rx] = actual
        if stated != actual:
            findings.append(f"README says {stated} for /{rx}/, measured {actual}")
    if absent:
        findings.append(f"{len(absent)} README claim(s) are no longer worded "
                        f"so they can be checked: {absent}")
    if findings:
        return "C8 readme numbers", "FAIL", findings
    return "C8 readme numbers", "PASS", \
        [f"recomputed {len(measured)} README numbers"]


# (text in README.md, its lie, label). Lives beside the check so the two
# cannot drift apart.
# Each entry is a REGEX with exactly one group -- the number to lie about --
# so the control reads the claim actually in the README and lies about THAT.
# Pinning literal text means the control breaks the moment the number
# legitimately changes, which is a control pinning a value it is not testing.
# Measured: '(20 files)' broke the moment the tree grew to 22 modules, and
# before that it was VACUOUS -- the control's farm held 2 .py files, so the
# count was already wrong before the mutation and C8 was red for the fixture.
def _c8_controls():
    """Build the control pairs FROM the README, so they do not go stale.

    Hardcoded literals were the first version and they rotted within the hour:
    the module count moved 17 -> 20 -> 22 -> 24 -> 25 and each time the
    control stopped being buildable, which the selftest reports as a FAILURE
    ("claim is still checkable") rather than a skip. Deriving them is the same
    rule as the check itself -- a number typed beside the thing it describes
    is a courier. (agent-8's form, from c8-claimgraph.)
    """
    import re as _re
    text = open(os.path.join(HERE, "..", "README.md"), encoding="utf-8").read()

    def find(rx):
        m = _re.search(rx, text)
        return (m.group(0), m.group(1)) if m else (None, None)

    pairs = []
    for rx, label, bump in (
            (r"—\s*(\d+)\s+checks", "demo caption", 1),
            (r"`python3 demo/town\.py`,\s*(\d+)\s+checks",
             "verification table", 1),
            (r"mutates the bank\s+(\w+)\s+ways", "selftest mutations", None),
            (r"`git ls-files '\*\.py'`\s*\((\d+)\s+files\)",
             "tracked modules", 5)):
        whole, val = find(rx)
        if whole is None:
            continue
        if bump is None:      # a word, not a number
            lie = "five" if val != "five" else "six"
        else:
            lie = str(int(val) + bump)
        pairs.append((whole, whole.replace(val, lie, 1), label))
    return pairs


C8_CONTROLS = _c8_controls()

def c9_readme_transcript(root):
    """The README quotes bank output. Replay it against the bank in the tree.

    C5 EXECUTES the fenced blocks, and this is not that. The transcript's
    command lines are prefixed with '$', so C5 treats the block as prose and
    never runs it -- which is why the quoted refusal drifted from
    {"error": "insufficient funds"} to {"error": "unknown sender"} while every
    suite stayed green. The text looks like output, so a reader trusts it more
    than a claim; nothing was maintaining it.

    Called C9, NOT C8: C8 is "readme numbers" (readme-truth's recomputation of
    the counts), and two different checks sharing a number is how a report line
    stops identifying a check. Renumbered during the merge that brought both.

    Runs as a SUBPROCESS so a wedged bank cannot hang the checker, and a
    non-zero exit is the finding. A missing replayer is a FAIL, not a skip:
    otherwise deleting the script turns the check off.
    """
    script = os.path.join(root, "tools/check_readme_transcript.py")
    if not os.path.exists(script):
        return "C9 readme transcript", "FAIL", [
            "tools/check_readme_transcript.py is absent, so the README's "
            "quoted bank transcript is unverified"]
    r = subprocess.run([sys.executable, script], cwd=root, text=True,
                       capture_output=True, timeout=180)
    if r.returncode != 0:
        out = (r.stdout + r.stderr).strip().splitlines()
        return "C9 readme transcript", "FAIL", out[:12] or ["replayer failed"]
    return "C9 readme transcript", "PASS", [r.stdout.strip().splitlines()[-1]]


def c10_readme_links_the_artifact(root):
    """The README must still POINT AT the correction graph.

    MASTER'S BLIND SPOT, measured 2026-10-18: the graph numbers moved out of
    the prose into a table generated by build.py, and build.py's M3 correctly
    fails when that table is stale. But nothing fails when the README stops
    mentioning the graph at all. I deleted the whole paragraph from a clone of
    master and ran tools/check.py:

        repo_check  PASS (8 pass, 1 warn, 0 fail)
        graph M3    PASS
        CHECK: WARN -- exit 0

    So the mechanism that replaced the numbers protects the numbers and not
    the LINK. The correction graph is the thing in this repo that is hardest
    to copy; a reader who never learns it exists is the failure this check
    exists to prevent, and it was reachable in one deletion.

    This does not restate a count -- counts belong to the generated table. It
    asserts that docs/claimgraph/index.html is referenced from README.md, and
    that the file it references is in the tree. A link to a file that does not
    ship is worse than no link.
    """
    path = os.path.join(root, "README.md")
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return "C10 readme links the graph", "FAIL", ["README.md is absent"]
    target = "docs/claimgraph/index.html"
    if target not in text:
        return "C10 readme links the graph", "FAIL", [
            f"README.md does not reference {target} -- the correction graph "
            f"is in the tree and nothing tells a reader it exists"]
    if not os.path.exists(os.path.join(root, target)):
        return "C10 readme links the graph", "FAIL", [
            f"README.md links {target} but that file is not in the tree"]
    return "C10 readme links the graph", "PASS", [f"README.md links {target}"]


def c11_readme_docker_flags(root):
    """The README's transcribed `docker run` must match what control.py builds.

    THE DEFECT THIS PINS (agent-10 and agent-7, independently, 2026-10-18):
    README step 4 says "Under the hood it runs, from ui/control.py:start_agent"
    and then shows a block missing the three durable volumes and
    CLAUDE_CONFIG_DIR. An operator who copies it gets an agent whose memory is
    wiped on every stop/start -- the feature the README opens with -- and
    nothing caught it, because C5 skips `docker run` on a box with no daemon,
    which is every box this was written on.

    A transcription of a command that another file BUILDS is a courier. This
    compares the block's flags to the flag list in ui/control.py by parsing
    the source, so the block cannot drift from the code without failing here.
    The two are allowed to differ only in the VALUES that are per-agent
    placeholders (<model>, <prompts>, agent-1) -- the FLAG SETS must match.
    """
    readme = os.path.join(root, "README.md")
    ctl = os.path.join(root, "ui", "control.py")
    try:
        text = open(readme, encoding="utf-8").read()
    except OSError:
        return "C11 readme docker flags", "FAIL", ["README.md is absent"]
    try:
        src = open(ctl, encoding="utf-8").read()
    except OSError:
        return "C11 readme docker flags", "FAIL", ["ui/control.py is absent"]

    # 1. What does the README claim? The FIRST bash block containing '
# docker
    #    run' -- the block that advertises itself as the real invocation.
    # Only fenced blocks, and only one that STARTS a docker run line -- the
    # architecture diagram also mentions "docker run" in passing, and picking
    # that chunk made this check compare the wrong text (a false FAIL on a
    # correct README, which is how a check gets deleted).
    # The block this check is about is the one that ADVERTISES ITSELF as the
    # agent invocation -- it cites start_agent. The README has other docker
    # run blocks (the bank, the UI); comparing those against start_agent's
    # flags produced a false FAIL on a correct README, which is how a check
    # gets deleted rather than fixed.
    # The citation sits in the PROSE BEFORE the fence ("Under the hood it
    # runs, from ui/control.py:start_agent:"), not inside it -- so a block is
    # the agent invocation when it starts with `docker run` and the text
    # immediately before it names start_agent. Matching inside the block only
    # found nothing, and "found nothing" must not read as "fine".
    parts = text.split("```")
    block = None
    for i in range(1, len(parts), 2):
        body = parts[i]
        if not re.search(r"^\s*docker run\b", body, re.M):
            continue
        if "start_agent" in parts[i - 1]:
            block = body
            break
    if block is None:
        # No abridged agent block is itself a way to stop this check seeing
        # anything -- report it, do not pass.
        return "C11 readme docker flags", "FAIL", [
            "README has no agent `docker run` block citing start_agent"]

    # 2. What flags does the code actually pass? Every literal that follows a
    #    '-v' / '-e' / '--label' / '--name' / '--restart' / '--label' token in
    #    start_agent's args list.
    default_src = src.split("def start_agent", 1)
    if len(default_src) < 2:
        return "C11 readme docker flags", "FAIL", [
            "ui/control.py has no start_agent -- the check cannot locate the "
            "command it is meant to compare"]
    body = default_src[1].split("\ndef ", 1)[0]
    code_flags = set(re.findall(r'"(--[a-z-]+|-[a-z])"', body))

    readme_flags = set(re.findall(r"(?<!\w)(--[a-z-]+|-[a-z])(?=\s|$)", block))

    # --name/--restart/--label/-e/-v are the docker-level flags both should
    # carry. A flag the CODE passes and the README omits is the defect.
    missing = sorted(f for f in code_flags if f in (
        "-v", "-e", "--label", "--name", "--restart") and f not in readme_flags)
    if missing:
        return "C11 readme docker flags", "FAIL", [
            f"README's docker run omits flag(s) that start_agent passes: "
            f"{missing}. An operator copying this block gets an agent that "
            f"loses its memory on restart."]

    # 3. The volume MOUNTS are the thing that was missing, so compare the
    #    mount TARGETS, not just the -v flag. A block with one -v and a code
    #    path with three is still the defect.
    def _target(mount):
        """Container path of a `host:container[:ro]` mount.

        rstrip(":ro") is a CHARACTER-CLASS strip, not a suffix strip: it turns
        "ro" into "" and silently erases every read-only mount from both
        sides. That made the system-prompt and transfer-token mounts
        invisible to this check (found 2026-10-18 by adding a fourth volume
        to start_agent and watching C11 stay green). Use removesuffix.
        """
        cont = mount.split(":")[-1] if mount.count(":") < 2 else mount.split(":")[1]
        if mount.endswith(":ro"):
            cont = mount[:-3].split(":")[-1]
        return cont

    code_mounts = set(re.findall(r'"([^"]*?:/[^"]*)"', body))
    code_targets = {_target(m) for m in code_mounts} - {""}
    readme_targets = {_target(m) for m in re.findall(r"-v\s+(\S+)", block)} - {""}
    # The transfer token is mounted ONLY for agent-0 (control.py:190) and the
    # README block is agent-1's, so it is excluded BY NAME, with the reason
    # here, rather than silently absorbed by a subtraction.
    conditional = {"/app/agent0-transfer-token"}
    code_targets -= conditional

    # ONE comparison covers all three failures this check exists for. Listing
    # the three durable paths separately is how the first version missed a
    # FOURTH volume: it checked the three names it knew and had no limb for
    # "a mount the code makes that the block does not have" (it computed that
    # set and then discarded it with `del lost`). A transcription is wrong if
    # it omits ANY flag, not only the three that were once forgotten.
    #
    # `test -v` and the transfer token are the only deliberate absences; each
    # is named, and a new one has to be argued for here.
    lost = sorted(code_targets - readme_targets)
    if lost:
        return "C11 readme docker flags", "FAIL", [
            f"start_agent mounts {lost} but the README's docker run does not. "
            f"The block is presented as what the code runs; a mount it omits "
            f"is a mount an operator will not have."]
    # THE ENV VARS, AS A SET -- not one hard-coded name. The first version
    # checked only CLAUDE_CONFIG_DIR, which was the env var that had been
    # forgotten; adding ANY OTHER -e to start_agent left C11 green while
    # agent-7's flagcheck (which compares env NAMES) went red. Two
    # implementations of one fact disagreeing is the signal, and the answer
    # is to compare the set here too.
    #
    # AGENT_NAME/AGENT_MODEL are per-agent values the code sets from `name`
    # and `model`; the README block is agent-1's, so they are named as
    # conditional rather than silently excluded.
    conditional_env = {"AGENT_NAME", "AGENT_MODEL"}
    code_env = {v for v in re.findall(r'"([A-Z][A-Z0-9_]{2,})=', body)} - conditional_env
    readme_env = set(re.findall(r"-e\s+(?:[A-Z_]+=)?([A-Z][A-Z0-9_]{2,})=", block))
    readme_env |= {v for v in re.findall(r"-e\s+([A-Z][A-Z0-9_]{2,})=", block)}
    missing_env = sorted(code_env - readme_env)
    if missing_env:
        return "C11 readme docker flags", "FAIL", [
            f"start_agent sets {missing_env} but the README's docker run does "
            f"not. An env var an operator does not set is a behaviour they do "
            f"not get (CLAUDE_CONFIG_DIR is the one that already bit us)."]
    return "C11 readme docker flags", "PASS", [
        f"README block carries all {len(code_targets)} mounts and "
        f"{len(code_env)} env vars the code sets"]


CLAIMGRAPH_PROSE_CLAIMS = [
    # (where, regex with 4 groups, label, truth(stats, graph) -> 4 values)
    # Each entry pins ONE sentence that retypes a statistic. The sentence is
    # checked in BOTH files because the same defect had two copies: the
    # claimgraph README restates the STATS table below it, and the TOP-LEVEL
    # README restates the graph while naming C8 as the check that owns it --
    # C8 does not read claimgraph numbers at all, so that sentence named a
    # gate that did not exist. A number typed beside the thing it describes
    # is a courier; this is the alarm for both couriers.
    (
        "docs/claimgraph/README.md",
        r"\*\*(\d+) superseded and (\d+) retracted against (\d+)\s*\n?live",
        "claimgraph README prose (superseded/retracted/live)",
        lambda s, g: (s["superseded"], s["retracted"], s["live"]),
        "superseded/retracted/live",
    ),
    (
        "README.md",
        r"the committed graph holds (\d+) claims and (\d+) typed\s+edges, "
        r"(\d+) of them supersessions, plus (\d+) retractions",
        "top-level README (claims/edges/supersessions/retractions)",
        lambda s, g: (s["claims"], len(g["edges"]), s["supersede_edges"],
                      s["retracted"]),
        "claims/edges/supersessions/retractions",
    ),
]


def c13_claimgraph_prose_numbers(root):
    """Statistic numbers retyped in PROSE, recomputed from graph.json.

    THE DEFECT THIS PINS (agent-6, 2026-10-18, found by reading, not by a
    failing check): docs/claimgraph/README.md's STATS table is generated by
    build.py and its staleness fails the build. The paragraph two below it
    retypes three of those numbers in a sentence -- "36 superseded and 20
    retracted against 46 live" -- and nothing checked them. On master 067428e
    the table said 81 / 23 / 60, so the sentence two paragraphs down from a
    generated table disagreed with it by two generations of store growth.

    The TOP-LEVEL README has the same defect on the same source, and it is
    not a rounding error: at 7b84456 it says the graph "holds 113 claims and
    68 typed edges, 60 of them supersessions, plus 21 retractions" while
    graph.json has 165 / 111 / 102 / 23 -- wrong by 52 / 43 / 42 / 2. The
    sentence was EXACTLY TRUE when it was written (commit 583bfec: graph.json
    held 113/68/60/21), which is why reading it finds nothing: it drifted.
    It also claims "The counts are checked against the committed graph.json
    by tools/repo_check.py (C8)" -- and C8 does not read claimgraph numbers.
    So the sentence named a gate that did not exist, which is worse than an
    unchecked number: a reader is told the number is maintained.

    WHY BOTH SENTENCES AND NOT A SCANNER: a narrative is allowed numbers
    ("10x", "a fraction", a quoted historical figure -- the same file quotes
    "101 claims" as history and means it). Scanning every integer produces
    false positives and forces a whitelist, and a whitelist is how a scanner
    gets silenced. Two named sentences with a maintainer beat a scanner.

    A DELETED SENTENCE IS A FAIL, NOT A SKIP. Otherwise the cheapest way to
    green is to delete the claim, and the check shrinks to nothing -- C8's
    rule, and this check exists because C8's regex table has a comment
    saying claimgraph numbers deliberately do NOT live there. They live in
    prose now, and the comment was written when they had moved into a
    generated table. They had not all moved.
    """
    gj = os.path.join(root, "docs/claimgraph/graph.json")
    try:
        graph = json.load(open(gj, encoding="utf-8"))
        stats = graph["stats"]
    except (OSError, ValueError, KeyError) as e:
        return "C13 claimgraph prose numbers", "FAIL", [
            f"docs/claimgraph/graph.json is unreadable or has no stats: {e!r}"]

    findings, checked = [], []
    for rel, pat, label, truth, names in CLAIMGRAPH_PROSE_CLAIMS:
        try:
            text = open(os.path.join(root, rel), encoding="utf-8").read()
        except OSError:
            findings.append(f"{rel} is absent, so its {label} is unverified")
            continue
        # The claimgraph README's generated block is build.py's, and
        # build.py's own M3 already fails when it is stale. Check the prose
        # OUTSIDE it, so this is not a second opinion on the same bytes.
        if rel.startswith("docs/claimgraph/"):
            m = re.search(r"<!-- STATS:START -->.*?<!-- STATS:END -->",
                          text, re.S)
            if m:
                text = text[:m.start()] + text[m.end():]
        hit = re.search(pat, text)
        if not hit:
            findings.append(
                f"{rel}: the sentence stating {names} is gone or reworded "
                f"beyond recognition. Deleting a stale number is not a fix "
                f"(the reader still needs it); this is FAIL, not SKIP")
            continue
        got = tuple(int(g) for g in hit.groups())
        want = tuple(truth(stats, graph))
        if got != want:
            findings.append(
                f"{rel}: {names} says {got}, graph.json has {want}")
        else:
            checked.append(f"{rel}: {names} {got}")
    if findings:
        return "C13 claimgraph prose numbers", "FAIL", findings
    return "C13 claimgraph prose numbers", "PASS", checked


def c12_undefined_names(root):
    """A name LOADED but never bound -- the defect class that stopped the daemon.

    WHY THIS CHECK EXISTS, stated as the incident, because the incident is the
    specification:
      master fcb2458 could not boot. Commit 718d278 inserted
      CURRENT_LIFE_FILE directly above `WAKE_NOTICE_TOKENS = int(...)` and, in
      the same hunk, deleted the assignment while leaving four uses of it.

      C6 (ast.parse) passed: `f"{WAKE_NOTICE_TOKENS}"` is valid syntax. A
      NameError is not a SyntaxError.
      C7 (import) passed: the uses are inside main_loop() and watch_context(),
      and importing a module does not call its functions.
      The daemon died at the first print() in main_loop(), BEFORE the life
      loop -- so it wrote no transcript, no birth event, no error file. A
      container built from that tree restart-loops with the traceback on
      stderr only. It survived two hours and about a dozen human-visible boot
      events because it is invisible to every check this repo had.

    The check is a stdlib-only subset of pyflakes (tools/undefined_names.py)
    rather than a pyflakes dependency: the image does not ship it, and a check
    that needs a package the image lacks is a check that does not run.

    FALSE POSITIVES ARE THE FAILURE MODE THAT MATTERS. A check that bites
    correct code gets disabled, and then it catches nothing. So the module
    stays SILENT wherever it cannot prove a load is unsafe: star imports,
    globals()/locals()/eval/exec, a caught NameError. Its own suites are run
    here as the control, in both directions:
      - tools/tests/undefined_names_cases.py  (10 snippets that MUST be caught)
      - tools/tests/undefined_names_legal.py  (legal Python that MUST NOT be)
    A check with a negative control but no positive one is this cohort's most
    repeated mistake; this one asserts both.
    """
    tool = os.path.join(root, "tools", "undefined_names.py")
    if not os.path.isfile(tool):
        return "C12 undefined names", "FAIL", [
            "tools/undefined_names.py is missing; C12 cannot run. This is a "
            "FAIL and not a skip: C6 and C7 both pass on a daemon that cannot "
            "boot, and this is the only check that sees that class."]
    findings = []
    # 1. the repo itself
    r = subprocess.run([sys.executable, "-I", tool], cwd=root,
                       capture_output=True, text=True)
    for line in (r.stdout or "").splitlines():
        if line.startswith("UNDEFINED "):
            findings.append(line[len("UNDEFINED "):])
    # 2. both controls, run every time. A tool that silently stopped detecting
    #    would otherwise report PASS on every tree forever.
    cases = os.path.join(root, "tools", "tests", "undefined_names_cases.py")
    legal = os.path.join(root, "tools", "tests", "undefined_names_legal.py")
    if not os.path.isfile(cases) or not os.path.isfile(legal):
        findings.append("C12's own control suites are missing "
                        "(tools/tests/undefined_names_{cases,legal}.py); the "
                        "check is unverified without them")
        return "C12 undefined names", "FAIL", findings
    rc = subprocess.run([sys.executable, "-I", cases], cwd=root,
                        capture_output=True, text=True)
    if rc.returncode != 0:
        findings.append("DETECTION CONTROL FAILED: a snippet that uses an "
                        "undefined name was not reported -- "
                        + (rc.stdout or rc.stderr).strip().splitlines()[-1])
    rl = subprocess.run([sys.executable, "-I", tool, legal], cwd=root,
                        capture_output=True, text=True)
    if rl.returncode != 0:
        findings.append("FALSE-POSITIVE CONTROL FAILED: legal Python was "
                        "reported -- " + (rl.stdout or "").strip()[-300:])
    if findings:
        return "C12 undefined names", "FAIL", findings
    return "C12 undefined names", "PASS", [
        "no unbound name in any tracked .py (10/10 detection cases, "
        "legal-code control clean)"]
def _registry_orphans(source):
    """Parse a repo_check.py SOURCE and return (defined, registered, orphans).

    A check is a module-level function named c<N>_<words>. It runs only
    because its name is also an element of the CHECKS list. Those are two
    separate facts and nothing used to compare them.

    Module-level only, deliberately: the file contains nested helpers named
    c11_on/c12_on/c13_on, and ast.walk would report them. A check that bites
    correct code gets deleted, and then it catches nothing.
    """
    tree = ast.parse(source)
    defined, registered = [], []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and re.fullmatch(
                r"c[0-9]+_[a-z0-9_]+", node.name):
            defined.append(node.name)
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "CHECKS" for t in node.targets):
            registered = [e.id for e in node.value.elts if isinstance(e, ast.Name)]
    return (sorted(defined), sorted(registered),
            sorted(set(defined) - set(registered)),
            sorted(set(registered) - set(defined)))


def registry_status(source):
    """Return (status, findings) for one source string. Shared by C14 and the
    C14 control, so the control exercises the same code path."""
    defined, registered, orphans, phantoms = _registry_orphans(source)
    findings = []
    if orphans:
        findings.append(
            "DEFINED BUT NEVER REGISTERED: " + ", ".join(orphans) +
            " -- the function exists, is documented, and would FAIL if it "
            "ran, but it is not in CHECKS, so nothing ever calls it. A green "
            "run cannot tell this apart from the check passing.")
    if phantoms:
        findings.append(
            "REGISTERED BUT NOT DEFINED: " + ", ".join(phantoms) +
            " -- CHECKS names a function that does not exist here.")
    return ("FAIL" if findings else "PASS"), findings + [
        f"{len(registered)} checks registered: " + ", ".join(registered)]


def c14_check_registry(root):
    """A check that is defined but never registered is a check that can be
    erased silently -- and this repo had no guard against it.

    MEASURED (agent-4, 2026-10-18): inject a real check function that would
    return FAIL, do not add it to CHECKS, and the whole repo reports
    PASS (10 pass, 1 warn, 0 fail); `--selftest` reports 33/33. The function
    is dead code wearing the name of a check.

    SCOPE, and it is narrower than it looks: this compares NAMES. A check
    whose body is gutted to `return ("x", "PASS", [])` is still registered and
    still named, and C14 passes. That is the boundary of a registry check, not
    a defect in this one -- catching a gutted body needs a different
    instrument (inject a defect per check and require red). Stated here so
    "registry PASS" is not read as more than it is. (agent-2's review.)

    This is the same failure the checker already names elsewhere: "a missing
    replayer is a FAIL, not a skip: otherwise deleting the script turns the
    check off" (C9). A merge that drops one line from CHECKS turns a check off
    just as completely, and emits no conflict marker when the added line sits
    on a line the other side also edited.

    `root` is unused on purpose: the file it audits is the one running, not one
    looked up under the tree, so the control can hand a mutated source string
    to registry_status without writing it into a git tree.
    """
    return ("C14 check registry",) + registry_status(
        open(os.path.join(HERE, "repo_check.py"), encoding="utf-8").read())


def _mutant_tree(root, rel, old, new, mutate=True):
    """A FULL COPY of the tree with one line changed, for the connection arm.

    A copy, not a symlink farm: the first version symlinked every top-level
    entry, and the tools walk `rglob("*.py")` / `glob("*.md")`, which follows
    symlinked DIRECTORIES into cycles ("Too many levels of symbolic links")
    and misses files the tools reach by a relative path from a dir symlink.
    A tree copy of this repo is a few MB; correctness is worth it.
    """
    import shutil, tempfile
    # TEMP_ROOT is fixed to the system temp dir, NOT derived from `root`:
    # an earlier version put these directories INSIDE the repo when the check
    # was run there, and the next run then copied the copies, recursing until
    # the tools' globs hit symlink cycles. A test that writes into the tree
    # it is testing is a test that changes its own subject.
    d = tempfile.mkdtemp(prefix="mutant-", dir=TEMP_ROOT)
    for entry in os.listdir(root):
        if entry in (".git", "__pycache__") or entry.startswith("mutant-"):
            continue
        src, dst = os.path.join(root, entry), os.path.join(d, entry)
        if os.path.isdir(src):
            shutil.copytree(src, dst,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(src, dst)
    target = os.path.join(d, rel)
    try:
        text = open(target, encoding="utf-8").read()
    except OSError:
        return d, False
    if not mutate:
        return d, True
    if old not in text:
        return d, False
    open(target, "w", encoding="utf-8").write(text.replace(old, new, 1))
    return d, True


def _publisher_docs(root):
    """How many docker PUBLISH SITES the tree actually has.

    NOT read from tools/portcheck.py's PUBLISHER_DOCS: that constant is the
    thing an attacker narrows, so counting it made the assertion agree with
    the attacker (it reported 6 vs 1 and PASSED). Ground truth comes from the
    tree.

    Counted as SITES, because that is what portcheck prints: README and
    CLOUD-DEPLOYMENT carry one `-p` line each, and CLOUD-DEPLOYMENT carries
    two, so the tree has three and a tool that reads one file sees one.

    KNOWN LIMIT, measured: docs/guide.md publishes the bank INLINE on the
    docker run line ('... agent-bank -p 8790:8790 \\'), which this predicate
    does not count and portcheck does not appear to either. If that is a real
    coverage gap it should be found by reading guide.md, not by this number
    quietly including it.
    """
    import subprocess
    files = subprocess.run(["git", "-C", root, "ls-files", "-z", "*.md"],
                           capture_output=True, text=True).stdout.split("\0")
    sites = 0
    for f in filter(None, files):
        try:
            text = open(os.path.join(root, f), encoding="utf-8",
                        errors="replace").read()
        except OSError:
            continue
        sites += sum(1 for ln in text.splitlines()
                     if re.match(r"\s*-p\s+[^\s:]+:\d+(:\d+)?", ln))
    return sites


def _required_tokens(root):
    """How many secrets bank.py requires, read from ITS _token_file calls."""
    path = os.path.join(root, "bank", "bank.py")
    src = open(path, encoding="utf-8").read()
    return len(re.findall(r"^\w+_TOKEN_FILE = _token_file\(", src, re.M))


def _bank_blocks(root):
    """How many tracked .md files contain a `docker run ... agent-bank` line.

    Counted from the TREE. This is the supply side tokencheck has to read; the
    first version of this gate asserted only the token count, which is the
    DEMAND side, so narrowing the doc list stayed invisible.
    """
    import subprocess
    files = subprocess.run(["git", "-C", root, "ls-files", "-z", "*.md"],
                           capture_output=True, text=True).stdout.split("\0")
    n = 0
    for f in filter(None, files):
        try:
            text = open(os.path.join(root, f), encoding="utf-8",
                        errors="replace").read()
        except OSError:
            continue
        if re.search(r"docker run[^\n]*agent-bank", text):
            n += 1
    return n


def _run_tool(root, tool, label, mutants, reach=None, blocks=None):
    """Run tools/<tool> as a GATE: exit 0 MATCH, 1 DIFFER, 2 cannot measure.

    These two tools were built and reviewed (agent-7, portcheck+tokencheck)
    and then registered by a DIFFERENT agent (agent-0) in a separate commit.
    That separation is deliberate and it is the lesson of the night: a gate
    that must be invoked by hand enforces nothing, and "PORTCHECK: MATCH" on
    a board post reads as if it were part of the green. The tool's author is
    named here so the provenance is not guesswork.

    THREE THINGS ARE ASSERTED, and the third is the one that matters:
      1. the tool file exists (a check that cannot run must not pass)
      2. its own selftest exits 0 AND prints the shipped summary with >=4 arms
      3. THE TOOL IS CONNECTED: run it against a copy of the real tree with
         one real defect injected and REQUIRE exit 1.
    (3) exists because (2) is not enough, and that was measured rather than
    guessed: agent-8 gutted tokencheck's run() to print MATCH and return 0 and
    its selftest to `good = True`, left every arm line and the summary intact,
    deleted the transfer-token mount from README, and C16 stayed GREEN. A
    gate that reads a control's OUTPUT can only verify that the control is
    well-spoken; it cannot verify that it is connected. The mutant arm is the
    only one of the three that tests the pipeline instead of the description.
    """
    path = os.path.join(root, tool)
    if not os.path.isfile(path):
        return label, "FAIL", [f"{tool} is registered in CHECKS but missing"]
    # THE TOOL'S OWN CONTROL RUNS EVERY TIME. A tool whose selftest is only
    # run by hand is a control that rots unnoticed -- the exact failure this
    # repo keeps finding.
    st = subprocess.run([sys.executable, "-I", path, "--selftest"], cwd=root,
                        capture_output=True, text=True, timeout=240)
    st_out = (st.stdout or "") + (st.stderr or "")
    arms = [ln for ln in st_out.splitlines() if "  PASS" in ln]
    if st.returncode != 0:
        return label, "FAIL", [
            f"{tool} --selftest FAILED, so its verdict below cannot be "
            f"trusted: " + st_out.strip()[-400:]]
    if "SELFTEST: PASS" not in st_out or len(arms) < 4:
        return label, "FAIL", [
            f"{tool} --selftest exited 0 but does not look like the shipped "
            f"control: no 'SELFTEST: PASS' summary, or only {len(arms)} "
            f"arm(s) ran. A gutted selftest passes its own exit code."]
    r = subprocess.run([sys.executable, "-I", path, root], cwd=root,
                       capture_output=True, text=True, timeout=120)
    out = [ln for ln in (r.stdout or "").strip().splitlines() if ln.strip()]
    tail = out[-1] if out else "(no output)"
    # REACH IS ASSERTED, not inferred from the verdict. Measured: a portcheck
    # whose PUBLISHER_DOCS was narrowed to ONE file passes every mutant arm
    # here (they all live in CLOUD-DEPLOYMENT.md) and prints MATCH -- while
    # being blind to the same defect in docs/guide.md. A narrowed INPUT SET is
    # invisible in a verdict and visible in a count. agent-8's attack, mine to
    # fix.
    # THE SUPPLY SIDE IS ASSERTED TOO, and this is agent-8's finding: tokencheck
    # reads its DEMAND (which tokens) from bank.py, but its SUPPLY (which docs)
    # from a hand-typed BANK_DOCS tuple that no arm grows. Dropping a doc from
    # that tuple keeps every arm -- including the one that adds a token to
    # code -- and loses coverage invisibly. The tool prints how many blocks it
    # read; compare that to the tree.
    if blocks is not None:
        m = re.search(blocks[0], "\n".join(out))
        got_b = int(m.group(1)) if m else None
        want_b = blocks[1](root)
        if got_b != want_b:
            return label, "FAIL", [
                f"{tool} read {got_b} doc block(s) but this tree has "
                f"{want_b} that run the bank. A typed supply-side list is the "
                f"same defect as a narrowed input set: the verdict survives "
                f"and the coverage does not."]
    if reach is not None:
        m = re.search(reach[0], "\n".join(out))
        got = int(m.group(1)) if m else None
        want = reach[1](root)
        if got != want:
            return label, "FAIL", [
                f"{tool} checked {got} {reach[2]}(s) but this tree has "
                f"{want}. A detector that narrows its INPUT SET keeps its "
                f"verdict and loses its coverage; the count is the only arm "
                f"that sees it."]
    if r.returncode == 2:
        return label, "WARN", [f"cannot measure here: {tail}"]
    if r.returncode != 0:
        return label, "FAIL", out[-6:]
    # (3) THE CONNECTION ARMS: every mutant, not just one.
    import shutil
    notes = []
    for rel, old, new in mutants:
        d = None
        try:
            d, applied = _mutant_tree(root, rel, old, new, mutate=False)
            if not applied:
                return label, "FAIL", [
                    f"a mutant arm could not be applied: {rel} no longer "
                    f"contains {old[:60]!r}. The tree changed shape, so this "
                    f"gate cannot prove the tool is connected. Update the "
                    f"mutant in repo_check to a defect that still exists."]
            # CONTROL for this mutant: the SAME copy without the injection
            # must be exit 0. Otherwise "it went red" could be the copy
            # rather than the defect -- the confound agent-8's arm C exists
            # for. Cheap: one extra run of a tool that takes a second.
            clean = subprocess.run([sys.executable, "-I", path, d],
                                   cwd=d, capture_output=True, text=True,
                                   timeout=120)
            if clean.returncode != 0:
                return label, "FAIL", [
                    f"CONTROL CONFOUND: the UNMUTATED copy of the tree "
                    f"already fails ({tool} exit {clean.returncode}), so the "
                    f"mutant's red would prove nothing: "
                    f"{(clean.stdout or '').strip().splitlines()[-1:]}"]
            tgt = os.path.join(d, rel)
            txt = open(tgt, encoding="utf-8").read()
            if old not in txt:
                return label, "FAIL", [
                    f"a mutant arm could not be applied: {rel} no longer "
                    f"contains {old[:60]!r}. Update the mutant in repo_check "
                    f"to a defect that still exists."]
            open(tgt, "w", encoding="utf-8").write(txt.replace(old, new, 1))
            m = subprocess.run([sys.executable, "-I", path, d], cwd=d,
                               capture_output=True, text=True, timeout=120)
            if m.returncode != 1:
                return label, "FAIL", [
                    f"{tool} stayed GREEN (exit {m.returncode}) on a tree "
                    f"with a real defect injected ({rel}: the mutant rooted "
                    f"at {old[:50]!r}). Its selftest speaks correctly, so "
                    f"either "
                    f"the tool is disconnected from the docs or this mutant "
                    f"is no longer a defect. A gate that cannot fail is "
                    f"decoration."]
            notes.append(f"mutant arm: {rel} + {old[:40]!r} -> DIFFER")
        finally:
            if d:
                shutil.rmtree(d, ignore_errors=True)
    return label, "PASS", [tail] + notes


def c15_portcheck(root):
    """Every doc's port publish must serve every code-level consumer's class.

    The defect this pins, measured on master: docs/CLOUD-DEPLOYMENT.md
    published the bank on 127.0.0.1 while agent/mcp-stdio.py reaches it at
    host.docker.internal (the docker0 bridge). Every check_money/transfer got
    ECONNREFUSED and the doc's own verification curled from the ONE observer
    that cannot see it. Fixed in d791564; this makes the invariant a gate.
    """
    return _run_tool(root, "tools/portcheck.py", "C15 docs publish serve consumers",
                     [("docs/CLOUD-DEPLOYMENT.md",
                       "  -p 127.0.0.1:8790:8790 \\\n", ""),
                      # BOTH DIRECTIONS of the same publish are injected.
                      # Measured: removing the bridge publish gives DIFFER
                      # naming agent/mcp-stdio.py:47 as the cut-off caller,
                      # while removing the loopback one names
                      # ui/control.py:432. A detector wired to only one of
                      # them is as blind as a filter that names its tokens.
                      ("docs/CLOUD-DEPLOYMENT.md",
                       "  -p $docker0:8790:8790 \\\n", ""),
                      # NO PER-DOC MUTANT HERE, and the reason is portcheck's
                      # semantics, measured while trying to add one: the
                      # invariant is per PORT across all docs, so deleting
                      # README's publish of 8790 is NOT a defect while
                      # CLOUD-DEPLOYMENT still publishes it for both caller
                      # classes -- portcheck stays MATCH and a gate demanding
                      # DIFFER would be asserting a bug. Consequence: C15
                      # cannot catch "the tool ignores one doc" the way C16
                      # now can; closing that needs a per-document invariant,
                      # which is portcheck's owner's call, not a mutant I can
                      # write here.
                      ],
                     reach=(r"MATCH \((\d+) publish site", _publisher_docs,
                            "publish site"))


def c16_tokencheck(root):
    """Every secret bank.py reads must be mounted by every doc's bank block.

    Measured: deleting the admin-token -v from README's bank block left
    repo_check GREEN, and the bank then answers 503 "admin token unavailable"
    -- a missing MOUNT presenting as a bank error, which is where nobody
    looks. The requirement is read from bank.py's own _token_file() call
    sites, and the tool's arm F adds a new token in code and requires all
    three docs to go red, so it cannot be satisfied by editing its own list.
    """
    return _run_tool(root, "tools/tokencheck.py", "C16 docs mount every bank token",
                     [("README.md",
                       "  -v ~/.config/docker-ai/agent0-transfer-token:"
                       "/app/agent0-transfer-token:ro \\\n", ""),
                      # THE FOURTH TOKEN. agent-8's v4 attack filtered its
                      # findings to the three token names every shipped arm
                      # uses, so it was blind to a token nobody had named and
                      # indistinguishable from a working detector on the one
                      # mutant above. A mutant the tool's author never saw is
                      # the only mutant that cannot have been special-cased.
                      ("bank/bank.py",
                       'AGENT0_TOKEN_FILE = _token_file(',
                       'AUDIT_TOKEN_FILE = _token_file("/run/secrets/audit-'
                       'token", "/app/audit-token")\nAGENT0_TOKEN_FILE = '
                       '_token_file('),
                      # THE THIRD DOC. Both mutants above live in README and
                      # bank.py, so a tokencheck that silently ignored
                      # CLOUD-DEPLOYMENT's block passed everything.
                      ("docs/CLOUD-DEPLOYMENT.md",
                       "  -v ~/.config/docker-ai/agent0-transfer-token:"
                       "/run/secrets/agent0-transfer-token:ro \\\n", "")],
                     reach=(r"MATCH \((\d+) token", _required_tokens,
                            "token"),
                     blocks=(r"all (\d+) bank block", _bank_blocks))


CHECKS = [c1_tracked_junk, c2_secrets_head, c3_secrets_history,
          c4_gitignore, c5_doc_commands, c6_sources_parse, c7_sources_import,
          c8_readme_numbers, c9_readme_transcript,
          c10_readme_links_the_artifact, c11_readme_docker_flags,
          c13_claimgraph_prose_numbers, c12_undefined_names, c14_check_registry,
          c15_portcheck, c16_tokencheck]


def _is_git_tree(root):
    r = subprocess.run(["git", "-C", root, "rev-parse", "--is-inside-work-tree"],
                       capture_output=True, text=True)
    return r.returncode == 0 and r.stdout.strip() == "true"


def _self_guard():
    """C14 cannot detect its OWN removal from CHECKS: if it is not in the list,
    it does not run, and no other check reads the registry.

    So the guard lives OUTSIDE the registry, evaluated on every run regardless
    of CHECKS. It is one line of evidence and it is checked the same way any
    other fact is: assert it, and prove the assertion can fail (selftest).

    A check that guards everything except its own deletion has a hole exactly
    the shape of its purpose.
    """
    return c14_check_registry in CHECKS


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
    # The registry check cannot report its own absence (see _self_guard).
    # Report it here, first, so unticking C14 cannot turn it off. A crash here
    # is a FAIL, not a skipped row, for the same reason a check crash is.
    try:
        guarded = _self_guard()
    except Exception as e:
        guarded = False
        guard_detail = f"self-guard crashed: {e!r}"
    else:
        guard_detail = ("c14_check_registry is registered in CHECKS" if guarded
                        else "c14_check_registry is ABSENT from CHECKS, so the "
                             "registry check does not run and no other check "
                             "reads the registry")
    results.append(("C14 self-guard", "PASS" if guarded else "FAIL",
                    [guard_detail]))
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

    # C5a: a DOCUMENT nothing executes must be NAMED, not folded into a total.
    # MEASURED defect (agent-2): master printed "executed 5 block(s), skipped
    # 40" while docs/CLOUD-DEPLOYMENT.md -- 11 blocks -- ran zero of them and
    # was nowhere in the visible output. The control is a two-document fixture:
    # one doc executes a block, another doc is entirely unrunnable. The check
    # must name the SECOND BY FILE while staying green about the first, so this
    # arm cannot be satisfied by printing every filename unconditionally.
    d = farm()
    open(os.path.join(d, ".gitignore"), "w").write(
        "__pycache__\n*.py[cod]\n*.log\n*.pid\n*.sqlite3\ncontrol-token\n")
    open(os.path.join(d, "README.md"), "w").write(
        "# x\n\n```sh\npython3 -c \"print(1)\"\n```\n")
    os.makedirs(os.path.join(d, "docs"))
    open(os.path.join(d, "docs", "DEPLOY.md"), "w").write(
        "# x\n\nsentence one\n\n```sh\nsudo scp somewhere\n```\n\n"
        "```sh\nexport PATH=/x\n```\n")
    _git(d, "add", "-f", ".")
    _git(d, "commit", "-qm", "one covered doc, one not")
    _, r = run(d)
    c5 = dict((n, (o, d2)) for n, o, d2 in r)["C5 doc commands run"]
    blob = "\n".join(str(x) for x in c5[1])
    outcomes.append(("C5 names a document with zero executed blocks",
                     "docs/DEPLOY.md: 0 of 2 block(s) executed" in blob
                     and "README.md: 0 of" not in blob))
    shutil.rmtree(d)

    # C5a2: and a doc that DOES execute is not named, so the line is a
    # measurement and not a banner. Same fixture as C5a's first half alone.
    d = farm()
    open(os.path.join(d, ".gitignore"), "w").write(
        "__pycache__\n*.py[cod]\n*.log\n*.pid\n*.sqlite3\ncontrol-token\n")
    open(os.path.join(d, "README.md"), "w").write(
        "# x\n\n```sh\npython3 -c \"print(1)\"\n```\n")
    _git(d, "add", "-f", ".")
    _git(d, "commit", "-qm", "all covered")
    _, r = run(d)
    c5 = dict((n, (o, d2)) for n, o, d2 in r)["C5 doc commands run"]
    outcomes.append(("C5 does not name a document it did execute",
                     c5[0] == "PASS" and "0 of" not in "\n".join(map(str, c5[1]))))
    shutil.rmtree(d)

    # C5a3: _first_cmd must not eat a command substitution. The shipped line
    # in docs/CLOUD-DEPLOYMENT.md classified as "-o addr show docker0 ..."
    # because ENV_PREFIX's \S* stopped at the first space INSIDE $( ). This
    # calls the helper directly, so it fails on the regex and nothing else.
    # The SECOND assertion is the ordinary case, so a fix that simply never
    # strips an env prefix cannot pass both.
    got = _first_cmd("docker0=$(ip -o addr show docker0 | awk '{print $4}' "
                     "| cut -d/ -f1)")
    outcomes.append(("C5 _first_cmd keeps a $(...) assignment whole",
                     got.startswith("docker0=$(ip -o addr show docker0")
                     and got.endswith("cut -d/ -f1)")))
    outcomes.append(("C5 _first_cmd still strips an ordinary VAR=value",
                     _first_cmd("PYTHONPATH=/x python3 -c 'print(1)'")
                     == "python3 -c 'print(1)'"))

    # C5a4: THE CONSEQUENCE, as an artifact rather than a status word.
    # The parse bug was reported as cosmetic -- a garbled skip REASON. It was
    # not: the fragment is what C5 hands to subprocess. MEASURED on ede3310,
    # a doc block consisting of ONE shell assignment
    #     X=$(env python3 -c "open('<path>','w').write('ran')")
    # was parsed as first='python3 -c "open(...)...")', matched RUNNABLE, and
    # THE WHOLE LINE WAS EXECUTED: the marker file appeared, and the run
    # reported PASS. That is the docstring's own courier rule -- a doc command
    # is a new courier -- defeated by a parser. The evidence is the positive
    # artifact, inside the fixture's own temp directory.
    d = farm()
    leak = os.path.join(d, "SIDE_EFFECT")
    open(os.path.join(d, ".gitignore"), "w").write(
        "__pycache__\n*.py[cod]\n*.log\n*.pid\n*.sqlite3\ncontrol-token\n")
    open(os.path.join(d, "README.md"), "w").write(
        "# x\n\n```sh\nX=$(env python3 -c \"open(%r,'w').write('ran')\")\n```\n"
        % leak)
    _git(d, "add", "-f", ".")
    _git(d, "commit", "-qm", "assignment block")
    _, r = run(d)
    written = os.path.exists(leak)
    # Leave no artifact behind even when the arm FAILS, so the failure is
    # about the checker and not about the fixture.
    shutil.rmtree(d)
    outcomes.append(("C5 does not EXECUTE a block that is only an assignment",
                     not written))

    # C5b: SKIP must not silence a command that would have PASSED.
    # "SKIP" used to be a universal silencer: any doc could disable its own
    # check by writing it. Now a marked command is PROBED -- exit 0 means it
    # was runnable all along and the SKIP hid a working check (WARN).
    #
    # The probe uses `test`, NOT `python3 -c`: python lines are no longer
    # probed at all (two bypasses, see _probe_safe). The control follows the
    # policy -- if the probe ever stops covering `test`, this goes red.
    d = farm()
    open(os.path.join(d, ".gitignore"), "w").write(
        "__pycache__\n*.py[cod]\n*.log\n*.pid\n*.sqlite3\ncontrol-token\n")
    open(os.path.join(d, "README.md"), "w").write(
        "# x\n\n```sh\n# SKIP: no reason at all\ntest -d .\n```\n")
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
    # Uses `test` (still probed) rather than python3 -c (no longer probed):
    # the command genuinely fails here, so the SKIP is honoured with its exit
    # code. This is the control that stops "probe everything" from turning a
    # legitimate docker-era SKIP into a red build.
    open(os.path.join(d, "README.md"), "w").write(
        "# x\n\n```sh\n# SKIP: needs a docker daemon\n"
        "test -f /nonexistent-artifact\n```\n")
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

    # C5f: agent-10's bypass -- string concatenation spells 'open' without
    # the substring 'open'. The right response was not a third blocklist
    # entry; python lines are no longer probed at all. The control asserts the
    # FILE IS NOT WRITTEN, which is the property that matters, rather than
    # that a particular word was or was not found.
    d = farm()
    victim = os.path.join(d, "victim")
    os.makedirs(victim)
    open(os.path.join(victim, "important"), "w").write("original")
    open(os.path.join(d, ".gitignore"), "w").write(
        "__pycache__\n*.py[cod]\n*.log\n*.pid\n*.sqlite3\ncontrol-token\n")
    open(os.path.join(d, "README.md"), "w").write(
        "# x\n\n```sh\n# SKIP: needs docker\n"
        "python3 -c \"__builtins__.__dict__['op'+'en']"
        "('victim/important','w').write('pwned')\"\n```\n")
    _git(d, "add", "-f", ".")
    _git(d, "commit", "-qm", "concatenated opcode")
    _, r = run(d)
    c5 = dict((n, (o, d2)) for n, o, d2 in r)["C5 doc commands run"]
    kept = open(os.path.join(victim, "important"), encoding="utf-8").read()
    outcomes.append(("C5 does not probe a python line that spells open() "
                     "around the blocklist",
                     kept == "original"
                     and any("not probed" in str(x) or "probe declined"
                             in str(x) for x in c5[1])))
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

    # C8: the README's numbers must be RECOMPUTED, and each claim must be
    # shown to fail when it is wrong -- one control per claim, because a
    # single "C8 catches a lie" would pass while three quarters of the check
    # were dead code.
    import re as _re
    for old_text, new_text, label in C8_CONTROLS:
        d = farm()
        open(os.path.join(d, ".gitignore"), "w").write(
            "__pycache__\n*.py[cod]\n*.log\n*.pid\n*.sqlite3\ncontrol-token\n")
        for rel in ("demo/town.py", "demo/selftest_demo.py"):
            os.makedirs(os.path.join(d, os.path.dirname(rel)), exist_ok=True)
            shutil.copy(os.path.join(ROOT, rel), os.path.join(d, rel))
        # THE FARM MUST SATISFY EVERY OTHER CLAIM. The tracked-module count is
        # measured with `git ls-files '*.py'` in the FARM, so a farm holding
        # only the two demo scripts makes the README's module count false
        # BEFORE any mutation -- C8 goes red for the fixture, and the control
        # "passes" while testing nothing. Copy the same .py set the claim
        # counts, so the only false number is the one this control injects.
        if label == "tracked modules":
            for py in sh_ls(ROOT):
                dst = os.path.join(d, py)
                os.makedirs(os.path.dirname(dst) or d, exist_ok=True)
                shutil.copy(os.path.join(ROOT, py), dst)
        shutil.copy(os.path.join(ROOT, "README.md"), os.path.join(d, "README.md"))
        p = os.path.join(d, "README.md")
        t = open(p, encoding="utf-8").read()
        # _c8_controls() searched the LIVE README, so a sentence that has been
        # deleted leaves no pair at all -- reported here as a FAILURE, not a
        # silent skip, because a claim that vanishes must not turn its check
        # off.
        if old_text not in t:
            outcomes.append((f"C8 {label} claim is still checkable", False))
            shutil.rmtree(d)
            continue
        lying = new_text
        # THE MUTATION MUST BE PROVEN TO MUTATE. Deriving the lie makes a
        # no-op unlikely; it does not make it impossible. A control whose
        # replacement changes nothing leaves an HONEST tree, C8 prints PASS,
        # and the outcome line still reads "catches a stale <label> number
        # PASS" -- a dead control reporting success. That state is reachable:
        # it is what agent-10 found when a pinned literal drifted to the true
        # value and old == new. Assert the file actually changed, and name the
        # pair when it did not, so the diagnosis is in the output.
        mutated = t.replace(old_text, lying, 1)
        if mutated == t or lying == old_text:
            outcomes.append((f"C8 {label} mutation actually mutates "
                             f"(no-op: {old_text!r} -> {lying!r})", False))
            shutil.rmtree(d)
            continue
        open(p, "w", encoding="utf-8").write(mutated)
        _git(d, "add", "-f", ".")
        _git(d, "commit", "-qm", "lying readme")
        _, r8 = run(d)
        c8 = dict((n, o) for n, o, _ in r8).get("C8 readme numbers")
        outcomes.append((f"C8 catches a stale {label} number", c8 == "FAIL"))
        shutil.rmtree(d)

    # C10: deleting the paragraph that points at the correction graph must
    # FAIL. Measured before this control existed: on master, removing the
    # entire paragraph left repo_check and the graph selftest green and
    # tools/check.py exiting 0. The numbers were protected; the link was not.
    d = farm()
    open(os.path.join(d, ".gitignore"), "w").write(
        "__pycache__\n*.py[cod]\n*.log\n*.pid\n*.sqlite3\ncontrol-token\n")
    os.makedirs(os.path.join(d, "docs/claimgraph"))
    open(os.path.join(d, "docs/claimgraph/index.html"), "w").write("<html></html>")
    open(os.path.join(d, "README.md"), "w").write(
        "# x\n\nSee [the graph](docs/claimgraph/index.html).\n")
    _git(d, "add", "-f", ".")
    _git(d, "commit", "-qm", "links the graph")
    ok_has, r_has = run(d)
    # the paragraph is gone AND the file it pointed at is still shipped: the
    # exact state master was in when this check did not exist
    open(os.path.join(d, "README.md"), "w").write("# x\n\nnothing here\n")
    _git(d, "add", "-f", ".")
    _git(d, "commit", "-qm", "paragraph removed")
    _, r_gone = run(d)
    # ARM 3 (agent-10's finding): the check has TWO limbs -- "the README
    # mentions the target" and "the target ships". Only the first was
    # controlled, so a refactor dropping the os.path.exists branch would keep
    # this suite green. A control must cover every limb of the check it
    # controls, or it certifies the half it happens to exercise.
    os.remove(os.path.join(d, "docs/claimgraph/index.html"))
    open(os.path.join(d, "README.md"), "w").write(
        "# x\n\nSee [the graph](docs/claimgraph/index.html).\n")
    _git(d, "add", "-f", ".")
    _git(d, "commit", "-qm", "links a file that does not ship")
    _, r_dangling = run(d)
    outcomes.append((
        "C10 passes when the README links the graph, fails when it does not",
        dict((n, o) for n, o, _ in r_has)["C10 readme links the graph"] == "PASS"
        and dict((n, o) for n, o, _ in r_gone)["C10 readme links the graph"] == "FAIL"))
    outcomes.append((
        "C10 fails when the README links a file that is not in the tree",
        dict((n, o) for n, o, _ in r_dangling)
        ["C10 readme links the graph"] == "FAIL"))
    shutil.rmtree(d)

    # C9: a README that quotes a response the bank does not give must FAIL.
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
        outcomes.append(("C9 catches a quoted response the bank does not give",
                         False))
    else:
        open(p, "w", encoding="utf-8").write(lie)
        _git(d, "add", "-f", ".")
        _git(d, "commit", "-qm", "lying transcript")
        _, r = run(d)
        outcomes.append(("C9 catches a quoted response the bank does not give",
                         dict((n, o) for n, o, _ in r)
                         ["C9 readme transcript"] == "FAIL"))
    shutil.rmtree(d)

    # THE TOOL MUST PASS ITS OWN CHECK. My first version hardcoded the real
    # published token as the fixture string, so (a) the check that exists to
    # stop the secret spreading was itself a second copy of it, and (b) the
    # moment the file was committed C2 would flag its own source -- correctly.
    # A scanning tool is a file like any other; scan it.
    here = os.path.abspath(__file__)
    own = scan_text(os.path.basename(here),
                    open(here, encoding="utf-8", errors="replace").read())
    # C11 controls: the README's transcribed docker-run block must carry the
    # volumes and env the code passes. THREE arms, because each one is a
    # different way to lose the property, and two of them were live issues
    # while this check was being written: comparing the wrong block (the
    # bank's), and finding no block at all and reading silence as agreement.
    #
    # Without these arms, disarming c11 (returning PASS unconditionally) left
    # selftest at 26/26 GREEN -- measured at 14:41 today. A check with no
    # control is a comment.
    readme_src = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    ctl_src = open(os.path.join(ROOT, "ui", "control.py"), encoding="utf-8").read()

    def c11_on(readme_text, ctl_text):
        d = farm()
        open(os.path.join(d, ".gitignore"), "w").write(
            "__pycache__\n*.py[cod]\n*.log\n*.pid\n*.sqlite3\ncontrol-token\n")
        os.makedirs(os.path.join(d, "ui"), exist_ok=True)
        # farm has no demo scripts; C11 does not read them
        open(os.path.join(d, "README.md"), "w", encoding="utf-8").write(readme_text)
        open(os.path.join(d, "ui", "control.py"), "w",
             encoding="utf-8").write(ctl_text)
        # C8 in the farm reads the README's numbers and needs demo/, exactly
        # as the other C8 arms do; without it the run is red for a reason
        # having nothing to do with C11, which would make every C11 arm pass.
        for rel in ("demo/town.py", "demo/selftest_demo.py"):
            os.makedirs(os.path.join(d, os.path.dirname(rel)), exist_ok=True)
            shutil.copy(os.path.join(ROOT, rel), os.path.join(d, rel))
        _git(d, "add", "-f", ".")
        _git(d, "commit", "-qm", "c11 fixture")
        _ok, r = run(d)
        shutil.rmtree(d)
        return dict((n, o) for n, o, _ in r).get("C11 readme docker flags")

    # The good direction: the real files must PASS (a check that fails on the
    # real tree is worse than none -- it gets deleted).
    outcomes.append(("C11 passes on the real README and control.py",
                     c11_on(readme_src, ctl_src) == "PASS"))

    # Arm 1: a durable volume is dropped from the block.
    stripped = readme_src.replace(
        "  -v agent-workspace-agent-1-claude:/workspace/.claude \\\n", "", 1)
    outcomes.append(("C11 catches a dropped durable volume",
                     stripped != readme_src
                     and c11_on(stripped, ctl_src) == "FAIL"))

    # Arm 4: a MOUNT THE CODE MAKES THAT THE BLOCK DOES NOT. This is the limb
    # that was computed and thrown away (del lost) and the rstrip bug that hid
    # every :ro mount: adding a fourth volume to start_agent left C11 GREEN
    # until 14:47 today. The control adds it to the code side only, so the
    # only way to pass is to compare the two sets for real -- not just the
    # three known durable paths.
    four = ctl_src.replace(
        '"-v", f"{workspace_volume(name, \'claude\')}:/workspace/.claude",',
        '"-v", f"{workspace_volume(name, \'claude\')}:/workspace/.claude",\n'
        '        "-v", f"{workspace_volume(name, \'inbox\')}:/workspace/inbox",',
        1)
    outcomes.append(("C11 catches a mount the code makes and the README omits",
                     four != ctl_src and c11_on(readme_src, four) == "FAIL"))

    # Arm 5: a read-only mount must not be invisible. rstrip(":ro") is a
    # character strip and erased every `:ro` mount from BOTH sides, so the
    # system prompt and transfer token never reached the comparison at all.
    # A block that omits the system-prompt mount must FAIL.
    noprompt = readme_src.replace(
        "  -v <prompts>/agent-1/system-prompt.txt:/app/system-prompt.txt:ro \\\n",
        "", 1)
    outcomes.append(("C11 catches a dropped :ro mount (system prompt)",
                     noprompt != readme_src
                     and c11_on(noprompt, ctl_src) == "FAIL"))

    # Arm 6: a NEW env var in start_agent must be caught. The first version
    # hard-coded CLAUDE_CONFIG_DIR and nothing else, so any other -e added to
    # the code left C11 green; agent-7's independent flagcheck caught it,
    # which is what two witnesses are for. This arm fails unless the env
    # comparison is a SET.
    newev = ctl_src.replace(
        '"-e", "CLAUDE_CONFIG_DIR=/workspace/.claude",',
        '"-e", "CLAUDE_CONFIG_DIR=/workspace/.claude",\n'
        '        "-e", "AGENT_NEW_ENV=1",', 1)
    outcomes.append(("C11 catches a NEW env var the README does not set",
                     newev != ctl_src and c11_on(readme_src, newev) == "FAIL"))

    # Arm 2: the env var that makes resume work is dropped.
    noenv = readme_src.replace("  -e CLAUDE_CONFIG_DIR=/workspace/.claude \\\n",
                               "", 1)
    outcomes.append(("C11 catches a dropped CLAUDE_CONFIG_DIR",
                     noenv != readme_src
                     and c11_on(noenv, ctl_src) == "FAIL"))

    # Arm 3: no agent block at all -- silence must not read as agreement.
    parts = readme_src.split("```")
    idx = None
    for i in range(1, len(parts), 2):
        if re.search(r"^\s*docker run\b", parts[i], re.M) and "start_agent" in parts[i - 1]:
            idx = i
            break
    if idx is None:
        outcomes.append(("C11 catches a README with no agent docker-run block", False))
    else:
        abridged = "```".join(parts[:idx - 1] + parts[idx + 2:])
        outcomes.append(("C11 catches a README with no agent docker-run block",
                         c11_on(abridged, ctl_src) == "FAIL"))


    # C13 controls: the claimgraph AND top-level READMEs' PROSE numbers must be recomputed
    # from graph.json, and both failure directions must be exercised -- a
    # check that passes on the real tree but is disarmed still reads green.
    cg_readme = open(os.path.join(ROOT, "docs", "claimgraph", "README.md"),
                     encoding="utf-8").read()
    cg_graph = open(os.path.join(ROOT, "docs", "claimgraph", "graph.json"),
                    encoding="utf-8").read()

    def c13_on(cg_text, top_text, graph_text):
        """Run the whole checker on a two-README fixture; return C13's row."""
        d = farm()
        open(os.path.join(d, ".gitignore"), "w").write(
            "__pycache__\n*.py[cod]\n*.log\n*.pid\n*.sqlite3\ncontrol-token\n")
        os.makedirs(os.path.join(d, "docs", "claimgraph"), exist_ok=True)
        open(os.path.join(d, "docs", "claimgraph", "README.md"), "w",
             encoding="utf-8").write(cg_text)
        open(os.path.join(d, "README.md"), "w",
             encoding="utf-8").write(top_text)
        open(os.path.join(d, "docs", "claimgraph", "graph.json"), "w",
             encoding="utf-8").write(graph_text)
        _git(d, "add", "-f", ".")
        _git(d, "commit", "-qm", "c13 fixture")
        _ok, r = run(d)
        shutil.rmtree(d)
        return dict((n, o) for n, o, _ in r).get("C13 claimgraph prose numbers")

    top_readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()

    outcomes.append(("C13 passes on the real claimgraph AND top-level READMEs",
                     c13_on(cg_readme, top_readme, cg_graph) == "PASS"))

    # Both sentences must be FINDABLE, or every arm below is vacuous (the
    # check would be failing for 'sentence gone', not for the mutation).
    _pat_cg = CLAIMGRAPH_PROSE_CLAIMS[0][1]
    _pat_top = CLAIMGRAPH_PROSE_CLAIMS[1][1]
    _m_cg = re.search(_pat_cg, cg_readme)
    _m_top = re.search(_pat_top, top_readme)
    outcomes.append(("C13's fixture locates BOTH sentences it checks",
                     _m_cg is not None and _m_top is not None))

    def lie(text, m):
        """Replace the numbers in the matched sentence with unmistakable lies."""
        return text[:m.start()] + re.sub(r"\d+", "999", m.group(0)) + \
            text[m.end():]

    if _m_cg:
        outcomes.append(("C13 catches a stale claimgraph-README prose number",
                         c13_on(lie(cg_readme, _m_cg), top_readme,
                                cg_graph) == "FAIL"))
        outcomes.append(("C13 treats a deleted claimgraph sentence as FAIL",
                         c13_on(cg_readme[:_m_cg.start()] +
                                cg_readme[_m_cg.end():], top_readme,
                                cg_graph) == "FAIL"))

    if _m_top:
        # This arm is the whole reason C13 has two sentences: the TOP-LEVEL
        # README's numbers were wrong by 52/43/42/2 on master and NOTHING
        # looked at them, because C8's table carries no claimgraph entry.
        outcomes.append(("C13 catches a stale TOP-LEVEL README number",
                         c13_on(cg_readme, lie(top_readme, _m_top),
                                cg_graph) == "FAIL"))
        outcomes.append(("C13 treats a deleted top-level sentence as FAIL",
                         c13_on(cg_readme, top_readme[:_m_top.start()] +
                                top_readme[_m_top.end():], cg_graph) == "FAIL"))

    # The graph moves under a correct sentence -> FAIL. The store grows; this
    # is the direction that actually happens, and it is the only way the
    # top-level sentence can drift without anyone editing it.
    moved = json.loads(cg_graph)
    moved["stats"]["superseded"] += 7
    outcomes.append(("C13 catches graph.json moving under the prose",
                     c13_on(cg_readme, top_readme, json.dumps(moved)) == "FAIL"))

    # Same, but moving only the EDGE COUNT -- the top-level sentence reads
    # len(graph["edges"]), a computed value, not a stats key. If that limb
    # were wired to a constant this arm would be the one that noticed.
    moved_edges = json.loads(cg_graph)
    moved_edges["edges"] = moved_edges["edges"] + [{"type": "dummy"}]
    outcomes.append(("C13 catches the edge count moving, not just stats keys",
                     c13_on(cg_readme, top_readme, json.dumps(moved_edges))
                     == "FAIL"))

    outcomes.append(("the tool's own source contains no secret", not own))
    outcomes.append(("C2 would catch it if it did (fixtures use SYNTHETIC)",
                     any("SYNTHETIC" in l for l in
                         open(here, encoding="utf-8").read().splitlines())))

    # C12 controls: THE ACTUAL INCIDENT, reproduced. A fixture containing the
    # real shape of master fcb2458 -- a module constant deleted while its
    # uses remain -- must FAIL, and the same fixture with the line restored
    # must PASS. This is the control that matters: a mutation of the shape
    # that stopped the daemon, not a mutation I invented.
    def c12_on(body):
        d = farm()
        open(os.path.join(d, ".gitignore"), "w").write(
            "__pycache__\n*.py[cod]\n*.log\n*.pid\n*.sqlite3\ncontrol-token\n")
        os.makedirs(os.path.join(d, "tools", "tests"), exist_ok=True)
        # C12 runs the tool from the TREE, so the fixture must carry it and
        # its two control suites, or C12 would fail for the wrong reason
        # (missing tool) and the control would pass vacuously.
        for rel in ("tools/undefined_names.py",
                    "tools/tests/undefined_names_cases.py",
                    "tools/tests/undefined_names_legal.py"):
            shutil.copyfile(os.path.join(ROOT, rel), os.path.join(d, rel))
        open(os.path.join(d, "daemon.py"), "w").write(body)
        _git(d, "add", "-f", ".")
        _git(d, "commit", "-qm", "c12 fixture")
        _ok, r = run(d)
        shutil.rmtree(d)
        return dict((n, o) for n, o, _ in r).get("C12 undefined names")

    BOOT = ("import os\n"
            "LIMIT = int(os.environ.get('LIMIT', '5'))\n"
            "def main_loop():\n"
            "    print(f'[daemon] up, threshold {LIMIT}')\n")
    outcomes.append(("C12 passes on a module whose constant is defined",
                     c12_on(BOOT) == "PASS"))
    deleted = BOOT.replace("LIMIT = int(os.environ.get('LIMIT', '5'))\n", "")
    outcomes.append(("C12 catches the deleted-constant boot crash "
                     "(the fcb2458 shape)",
                     deleted != BOOT and c12_on(deleted) == "FAIL"))
    # And it must ALSO catch a definition deleted from a function body, which
    # is the more common everyday shape and a different scope branch.
    FN = ("def watch(total):\n"
          "    threshold = 5\n"
          "    return total >= threshold\n")
    fn_bad = FN.replace("    threshold = 5\n", "")
    outcomes.append(("C12 catches a deleted local in a function body",
                     fn_bad != FN and c12_on(fn_bad) == "FAIL"))
    # C14: the registry. Three arms in two directions. A one-armed control
    # here would be satisfied by a check that returns FAIL unconditionally,
    # which is the failure this check exists to prevent, one level up.
    real_src = open(here, encoding="utf-8").read()
    outcomes.append(("C14 passes on the real repo_check.py",
                     registry_status(real_src)[0] == "PASS"))

    # Arm A: define a check, do not register it. This is the exact shape of a
    # merge that keeps the function hunk and loses the CHECKS-list line.
    orphaned = real_src.replace(
        "CHECKS = [c1_tracked_junk,",
        "def c99_orphan(root):\n"
        "    return 'C99 orphan', 'FAIL', ['never called']\n\n\n"
        "CHECKS = [c1_tracked_junk,", 1)
    st, fd = registry_status(orphaned)
    outcomes.append(("C14 catches a check DEFINED but missing from CHECKS",
                     orphaned != real_src and st == "FAIL"
                     and any("c99_orphan" in f for f in fd)))

    # Arm B: the SAME name, but nested inside another function, must NOT be
    # flagged. The real file already carries c11_on/c12_on/c13_on helpers
    # named exactly like checks; a check that walked every FunctionDef would
    # report them and bite correct code. Synthetic so the arm cannot go
    # vacuous when those helpers are renamed.
    nested_src = real_src.replace(
        "CHECKS = [c1_tracked_junk,",
        "def _wrapper():\n"
        "    def c99_nested(root):\n"
        "        return 'not a check', 'PASS', []\n"
        "    return c99_nested\n\n\n"
        "CHECKS = [c1_tracked_junk,", 1)
    st_b, fd_b = registry_status(nested_src)
    outcomes.append(("C14 does NOT flag a nested check-shaped helper",
                     nested_src != real_src and st_b == "PASS"
                     and not any("c99_nested" in f for f in fd_b)))

    # Arm C: C14's own removal must still be caught. registry_status cannot
    # see this (if C14 is unregistered it never runs), so the guard is
    # asserted directly, in both the restored and removed states.
    _saved = list(CHECKS)
    try:
        CHECKS.remove(c14_check_registry)
        outcomes.append(("C14 self-guard fires when its CHECKS entry is removed",
                         _self_guard() is False))
    finally:
        CHECKS[:] = _saved
    outcomes.append(("C14 self-guard passes on the restored list",
                     _self_guard() is True))

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
