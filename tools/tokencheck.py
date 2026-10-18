#!/usr/bin/env python3
"""tokencheck.py -- every token file the bank READS must be mounted by every doc
that starts the bank.

    python3 tokencheck.py [repo-root]     # exit 0 all mounted / 1 a doc omits one / 2 cannot measure

WHY THIS EXISTS, measured. C11 (tools/repo_check.py:879) excludes
`/app/agent0-transfer-token` BY NAME because it is a CONDITIONAL mount in
start_agent -- only agent-0 gets it. That is correct for the block C11
compares (the agent's block). It is NOT correct for the BANK's block, where
the mount is unconditional, and the exclusion is keyed on the container path,
not on which block is being read.

MEASURED consequence, on master 5a934d3: delete the bank's transfer-token
`-v` line from README.md and

    tools/repo_check.py .   ->  REPO CHECK: PASS (11 pass, 1 warn, 0 fail)

while bank.py:260-266 answers 503 "delegate token unavailable" to every
`from: agent-0` transfer, because AGENT0_TOKEN_FILE resolves to a path that
was never mounted. A missing MOUNT presents as a BANK error, which is the
last place an operator looks. agent-0 found this while reviewing C11; the
class is theirs and the check is ordinary.

THE INVARIANT, read from CODE, not transcribed:
    for every token the bank opens, every doc that starts the bank mounts it
The required token paths come from bank.py's `_token_file(...)` call sites --
the candidate list IS the contract, because the bank tries each path in
order. Docs are the fenced `docker run ... agent-bank` blocks. A doc that
starts the bank without mounting a token the bank reads is a DIFFER.

WHY BOTH CANDIDATE PATHS ARE ACCEPTED FOR ONE TOKEN. `_token_file` takes
candidates: if ANY of them exists the bank uses it. So mounting either
`/run/secrets/bank-admin-token` or `/app/bank-admin-token` satisfies the
admin token. The check requires, per token, that the doc mounts AT LEAST ONE
candidate -- not all of them. Requiring all would red the documented bundle
for a correct simplification.

WHAT THIS CANNOT SEE, so a PASS is not read as more than it is:
    It does not run the bank, so it cannot tell you a mounted file is EMPTY,
    unreadable, or holds the wrong secret. It reads the mount TARGET only
    (the container path); a correct target with the wrong HOST source still
    passes -- the same blind spot I left in portcheck, stated here rather
    than discovered later. It matches docs by the `agent-bank` image token in
    the same `docker run`; a doc that starts the bank some other way (compose,
    systemd, a wrapper script) is invisible to it.

Exit: 0 every required token mounted / 1 a doc omits one / 2 cannot measure.
"""
import os
import re
import sys

BANK_SOURCE = "bank/bank.py"
# The docs that are supposed to start the bank. NAMED, so deleting one is a
# visible edit rather than a silent narrowing (the M3b lesson).
BANK_DOCS = ("README.md", "docs/CLOUD-DEPLOYMENT.md", "docs/guide.md")

TOKEN_CALL_RE = re.compile(r"_token_file\(([^)]*)\)")
STRING_RE = re.compile(r'"([^"]+)"')


def required_tokens(root):
    """[ (token_name, [candidate_paths]) ] read from bank.py's call sites."""
    path = os.path.join(root, BANK_SOURCE)
    try:
        src = open(path, encoding="utf-8").read()
    except OSError:
        return None
    out = []
    for m in TOKEN_CALL_RE.finditer(src):
        args = m.group(1)
        candidates = STRING_RE.findall(args)
        if not candidates:
            continue
        # Name the token by its FIRST candidate's basename: both candidates
        # are alternative locations for the same secret.
        name = os.path.basename(candidates[0])
        # The call sites assign to a module constant whose name says which
        # secret it is; take the nearest preceding assignment for the label.
        prefix = src[:m.start()]
        am = None
        for am2 in re.finditer(r"^([A-Z0-9_]+)\s*=\s*$", prefix, re.M):
            am = am2
        label = am.group(1) if am else name
        out.append((label, candidates))
    return out


def doc_bank_blocks(root):
    """{doc: [block_text]} -- fenced blocks that start the bank image."""
    out = {}
    for rel in BANK_DOCS:
        path = os.path.join(root, rel)
        try:
            text = open(path, encoding="utf-8").read()
        except OSError:
            continue
        blocks = []
        for m in re.finditer(r"```[a-z]*\n(.*?)```", text, re.S):
            body = m.group(1)
            if re.search(r"^\s*docker run\b", body, re.M) and \
                    re.search(r"\bagent-bank\b\s*$", body, re.M):
                blocks.append(body)
        out[rel] = blocks
    return out


def mount_targets(block):
    """Container paths mounted by -v in this block."""
    targets = set()
    for m in re.finditer(r"-v\s+([^\s\\]+)", block):
        spec = m.group(1)
        cont = spec[:-3] if spec.endswith(":ro") else spec
        parts = cont.split(":")
        targets.add(parts[1] if len(parts) >= 2 else parts[0])
    return targets


def evaluate(root):
    tokens = required_tokens(root)
    if tokens is None:
        return None, "%s is missing or unreadable" % BANK_SOURCE
    if not tokens:
        return None, "no _token_file(...) call sites found in %s" % BANK_SOURCE
    docs = doc_bank_blocks(root)
    if not docs or not any(docs.values()):
        return None, "no fenced `docker run ... agent-bank` block in %s" % (
            BANK_DOCS,)
    findings = []
    checked = 0
    for doc, blocks in sorted(docs.items()):
        if not blocks:
            findings.append("%s starts no bank container -- if that doc is "
                            "supposed to, this is a DIFFER, not a skip" % doc)
            continue
        for bi, block in enumerate(blocks, 1):
            targets = mount_targets(block)
            checked += 1
            for label, candidates in tokens:
                if not (set(candidates) & targets):
                    findings.append(
                        "%s bank block #%d mounts none of %s for %s -- the bank "
                        "answers 503 for it (mounts seen: %s)"
                        % (doc, bi, candidates, label, sorted(targets)))
    return (len(tokens), checked, findings), None


def run(root="."):
    result, err = evaluate(root)
    if err:
        print("TOKENCHECK: CANNOT MEASURE --", err)
        return 2
    ntokens, checked, findings = result
    if findings:
        print("TOKENCHECK: DIFFER (%d token(s) x %d bank block(s))"
              % (ntokens, checked))
        for f in findings:
            print("  -", f)
        return 1
    print("TOKENCHECK: MATCH (%d token(s) mounted by all %d bank block(s))"
          % (ntokens, checked))
    return 0


def selftest():
    """Both directions. Each arm names the outcome it requires."""
    import tempfile

    def fixture(readme_block, guide_block=None, drop_token_from_code=None):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "bank"))
        os.makedirs(os.path.join(d, "docs"))
        code = ('ADMIN_TOKEN_FILE = _token_file("/run/secrets/bank-admin-token",\n'
                '                              "/app/bank-admin-token")\n'
                'AGENT0_TOKEN_FILE = _token_file("/run/secrets/agent0-transfer-token",\n'
                '                               "/app/agent0-transfer-token")\n')
        if drop_token_from_code:
            code = code.replace(drop_token_from_code, "")
        open(os.path.join(d, "bank", "bank.py"), "w").write(code)
        if guide_block is None:
            guide_block = readme_block
        open(os.path.join(d, "README.md"), "w").write(readme_block)
        open(os.path.join(d, "docs", "guide.md"), "w").write(guide_block)
        open(os.path.join(d, "docs", "CLOUD-DEPLOYMENT.md"), "w").write(
            readme_block)
        return d

    def block(tokens):
        body = "```bash\ndocker run -d --name agent-bank \\\n"
        body += "  -p 8790:8790 \\\n  -v bank-data:/data \\\n"
        for t in tokens:
            body += "  -v %s \\\n" % t
        body += "  agent-bank\n```\n"
        return body

    both = block(["~/.config/docker-ai/bank-admin-token:/app/bank-admin-token:ro",
                  "~/.config/docker-ai/agent0-transfer-token:/app/agent0-transfer-token:ro"])
    admin_only = block(["~/.config/docker-ai/bank-admin-token:/app/bank-admin-token:ro"])
    secrets_form = block(["/x/bank-admin-token:/run/secrets/bank-admin-token:ro",
                          "/x/agent0-transfer-token:/run/secrets/agent0-transfer-token:ro"])

    arms = [
        ("A both /app tokens mounted -> MATCH", both, both, 0),
        ("B transfer token missing -> DIFFER", admin_only, both, 1),
        ("C /run/secrets form accepted -> MATCH", secrets_form, both, 0),
        ("D guide omits it too, README ok -> DIFFER", both, admin_only, 1),
    ]
    ok = True
    for name, readme_block, guide_block, want in arms:
        d = fixture(readme_block, guide_block)
        got = run(d)
        good = (got == want)
        ok &= good
        print("  %-50s %s (exit %d, want %d)"
              % (name, "PASS" if good else "FAIL", got, want))
    # E: the failure must NAME the token and the doc.
    d = fixture(admin_only)
    res, _err = evaluate(d)
    findings = res[2] if res else []
    named = any("agent0-transfer-token" in f and "README.md" in f
                for f in findings)
    ok &= named
    print("  %-50s %s" % ("E the DIFFER names the token and the doc",
                          "PASS" if named else "FAIL"))
    # F: a token the code READS but no doc mentions must be required. This is
    # the arm that proves the requirement comes from CODE and not from a list
    # of mounts someone typed.
    d = fixture(both)
    # add a THIRD token to the bank and require the docs to be red
    p = os.path.join(d, "bank", "bank.py")
    s = open(p).read() + ('OTHER_TOKEN_FILE = _token_file("/run/secrets/other-token")\n')
    open(p, "w").write(s)
    got = run(d)
    good = (got == 1)
    ok &= good
    print("  %-50s %s (exit %d, want 1)"
          % ("F a NEW token in code reds the docs", "PASS" if good else "FAIL", got))
    print("TOKENCHECK SELFTEST:", "PASS" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(run(args[0] if args else "."))
