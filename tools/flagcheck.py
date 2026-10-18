#!/usr/bin/env python3
"""flagcheck.py -- does the README's transcribed `docker run` match the code that builds it?

    python3 flagcheck.py [repo-root]      # exit 0 match, 1 differ, 2 cannot measure

WHY THIS EXISTS, measured rather than argued. On master 2026-10-18 the README's
"Under the hood it runs, from ui/control.py:start_agent" block listed FIVE
flags and `start_agent` emitted NINE: the block omitted the three durable
volumes (/workspace/self, /workspace/transcripts, /workspace/.claude) and
CLAUDE_CONFIG_DIR. Copying it gives an agent whose memory is wiped on restart —
the one property the README opens with.

Nothing caught it because C5 EXECUTES doc blocks and `docker run` is SKIPPED on
every box without a daemon, which is every box anyone writes this README on. A
transcription that cannot be executed is a transcription nothing checks.

SO: CHECK THE TEXT AGAINST THE SOURCE, not the command against its effect.
`start_agent` builds an argv list; the doc block is a rendering of that list.
The comparison is between two SETS OF FLAGS, which needs no daemon, no
container, and no network. It is not a proof that the block is runnable; it is
a proof that it says the same thing as the function it cites.

THE COMPARISON, and what is deliberately loose about it:
  * only -v / -e / --label are compared. Their ORDER and the presence of the
    image name, the --name, --restart and --label VALUES are not part of this
    claim: a doc may reasonably show a different agent name, and the model
    label is a placeholder in both.
  * a volume is compared by its CONTAINER PATH (the right-hand side of `:`),
    because the host volume NAME is generated per-agent and a doc must show a
    placeholder. The container path is what decides whether memory survives.
  * a doc placeholder (`<...>`) in a -v host side is ignored; the path after
    the last ':' is what is compared.
  * ENV is compared by NAME, not value (AGENT_MODEL is a placeholder).
  * anything the block marks with a comment `# abridged` is allowed to be a
    subset -- but then it must SAY so, because a block that reads authoritative
    and is abridged is the defect this file was written for.

THE CONTROL, which is the reason to trust a PASS: --selftest builds a fake
repo whose README is missing one durable volume and requires exit 1, then the
correct one and requires exit 0. A checker that has only seen the good tree is
a belief.

WHAT THIS CANNOT SEE, stated so a PASS is not read as more than it is
    It compares TEXT: the container paths of -v and the NAMES of -e. It does not
    execute a container, so it cannot say the block would run. It does not
    compare the host side of a volume (a doc must use a placeholder there, the
    code builds a per-agent name). It examines ONE block: the fenced one citing
    ui/control.py:start_agent. The other five docker blocks in the README and
    ten in docs/guide.md are not looked at by it.

Exit: 0 MATCH / 1 DIFFER / 2 CANNOT MEASURE (no block, no function, unparseable).
"""
import ast
import os
import re
import sys
import tempfile


def read_flags_from_source(src):
    """The container paths of -v, the names of -e, and the keys of --label
    that ui/control.py's start_agent appends."""
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "start_agent"), None)
    if fn is None:
        return None, "start_agent not found in ui/control.py"
    vols, envs, labels = [], [], set()
    # Walk every literal list/dict that mentions one of the three flags.
    for node in ast.walk(fn):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        val = node.value
        if val in ("-v", "-e", "--label"):
            continue
    # The argv is assembled as extend(["-v", f"...", "-v", ...]) and explicit
    # pairs; the reliable read is the STRING LITERALS in order, paired by the
    # flag that precedes them in the same call. Simpler and robust enough:
    # scan the source text of the function for the flag/value pairs.
    seg = ast.get_source_segment(src, fn) or ""
    # THE VALUE OF A FLAG IS THE *WHOLE* SECOND ARGUMENT, taken from the AST,
    # not the text up to the next comma. My first version regex-split on the
    # comma and stopped INSIDE a call: f"{workspace_volume(name, 'self')}:..."
    # truncated to "f"{workspace_volume(name, 'self'" -> no colon -> no
    # container path -> "<expr>" -> never compared. All THREE durable volumes
    # were therefore unfalsifiable, and deleting one from the README still
    # printed MATCH. The fixture arms did not catch it because their source
    # was a literal; the real source is an f-string with a comma in it.
    # Pairing from the AST also survives multi-line lists, which the regex did
    # only by luck.
    pairs = []
    for node in ast.walk(fn):
        if isinstance(node, (ast.List, ast.Tuple)):
            elts = node.elts
            for i in range(len(elts) - 1):
                a, b = elts[i], elts[i + 1]
                if (isinstance(a, ast.Constant) and a.value in
                        ("-v", "-e", "--label")):
                    pairs.append((a.value, b))
    for flag, valnode in pairs:
        val = _literal_prefix_or_expr(valnode)
        if val is None:
            continue
        if flag == "-v":
            vols.append(_container_path(val))
        elif flag == "-e":
            envs.append(val.split("=", 1)[0])
        else:
            labels.add(val.split("=", 1)[0])
    return {"volumes": vols, "env": envs, "labels": labels}, None


def _string_value(raw):
    """Turn a source expression into the text it will produce, best effort."""
    raw = raw.strip()
    if raw.startswith('f"') or raw.startswith("f'"):
        # An f-string: keep the literal parts and replace {expr} with a marker.
        body = raw[2:-1]
        return re.sub(r"\{[^}]*\}", "<expr>", body)
    if (raw.startswith('"') and raw.endswith('"')) or \
       (raw.startswith("'") and raw.endswith("'")):
        return raw[1:-1]
    return None


def _literal_prefix_or_expr(node):
    """A flag's value as text, from the AST.

    A Constant is its own value. A JoinedStr (f-string) is reconstructed with
    its substitutions as <expr>, KEEPING the literal parts -- that is what
    makes f"{vol(name,'self')}:/workspace/self" keep the container path while
    the host side becomes <expr>. Returning the raw source instead loses the
    path whenever a call contains a comma.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                out.append(str(part.value))
            else:
                out.append("<expr>")
        return "".join(out)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _literal_prefix_or_expr(node.left) + _literal_prefix_or_expr(node.right)
    return "<expr>"


def _container_path(vol):
    """`host:/container[:opts]` -> the container path.

    A value the source builds at run time is a PLACEHOLDER, not a path: an
    f-string we could not evaluate renders as `{workspace_volume(name` and must
    not be compared as if it were a literal. The same is true of the doc's
    `<prompts>` and of `prompt_path(name)`. Compared by SHAPE, they agree.
    """
    parts = vol.split(":")
    path = vol if len(parts) < 2 else (parts[-2] if len(parts) > 2 else parts[1])
    if "{" in path or "(" in path or "<" in path:
        return "<expr>"
    return path


def read_flags_from_doc(text):
    """The same three sets, read out of the fenced block that cites start_agent."""
    # THE BLOCK THAT CITES start_agent, not the first "docker run" in the file:
    # the architecture diagram contains the words "docker run" inside a box
    # drawing, and my first version matched that and compared nothing. Found by
    # running it against the real README and getting an empty doc flag set.
    # Search for the BARE citation: the README wraps the first one in
    # backticks and puts "from" before it, while the Verification table cites
    # it inside a longer phrase. Matching the bare string is the only form
    # both share -- my first version searched the backticked form and got
    # "no fenced block" on a file that has one.
    i = text.find("ui/control.py:start_agent")
    if i < 0:
        return None, "no block citing ui/control.py:start_agent"
    # The block starts at the first fence after the citation.
    start = text.find("```", i)
    if start < 0:
        return None, "citation found but no fenced block after it"
    end = text.find("```", start + 3)
    if end < 0:
        return None, "unterminated fenced block after the citation"
    block = text[start:end]
    abridged = "abridged" in block.lower()
    vols, envs, labels = [], [], set()
    for line in block.splitlines():
        line = line.strip()
        if line.startswith("#"):
            continue
        # A LINE CAN CARRY SEVERAL FLAGS: the real README writes
        #   -e AGENT_NAME=agent-1 -e AGENT_MODEL=<model> \
        # and a per-line regex that reads only the first one under-reports the
        # doc and cries wolf. Found by running against the real file.
        for m in re.finditer(r"(?:^|\s)(-v|-e|--label)\s+(\S+)", line):
            flag, token = m.group(1), m.group(2)
            if token.endswith("\\"):
                continue
            if flag == "-v":
                vols.append(_container_path(token))
            elif flag == "-e":
                envs.append(token.split("=", 1)[0])
            else:
                labels.add(token.split("=", 1)[0])
    return {"volumes": vols, "env": envs, "labels": labels,
            "abridged": abridged}, None


def compare(src_flags, doc_flags):
    """Compare the COMPARABLE flags, position by position.

    Values built at run time are placeholders and cannot be compared as text;
    the LITERAL container paths and env names can be, and those are the ones
    that decide whether memory survives. A placeholder is not "missing" -- it is
    "the doc shows something here", which is all a placeholder can say.
    """
    findings = []
    if doc_flags.get("abridged"):
        return findings
    # CONDITIONAL mounts are excluded BY NAME with a reason: the agent-0
    # transfer token is added only for name == "agent-0" (control.py:190) and
    # docs/guide.md explains that; a doc block for agent-1 must NOT list it.
    CONDITIONAL = {"/app/agent0-transfer-token"}
    # A DURABLE VOLUME WHOSE CONTAINER PATH IS BUILT AT RUN TIME cannot be
    # compared by text in either direction -- the code says f"{workspace_volume(
    # name,'self')}:/workspace/self" (literal "/workspace/self" survives my
    # extractor) and the doc says agent-workspace-agent-1-self:/workspace/self
    # (same literal). If the doc wrote :/workspace/SELF the literal would
    # differ and this WOULD catch it; if it wrote the right container path we
    # match. That is as much as a text comparison can honestly claim, and the
    # file says so rather than implying it checks the host side.
    missing_v = [v for v in src_flags["volumes"]
                 if v != "<expr>" and v not in CONDITIONAL
                 and v not in doc_flags["volumes"]]
    missing_e = [e for e in src_flags["env"]
                 if e != "<expr>" and e not in doc_flags["env"]]
    if missing_v:
        findings.append("README omits volume(s) the code mounts: %s"
                        % ", ".join(sorted(missing_v)))
    if missing_e:
        findings.append("README omits env var(s) the code sets: %s"
                        % ", ".join(sorted(missing_e)))
    return findings


def check(root):
    bank = os.path.join(root, "ui", "control.py")
    readme = os.path.join(root, "README.md")
    for p in (bank, readme):
        if not os.path.exists(p):
            return "CANNOT MEASURE", ["missing %s" % p]
    src_flags, err = read_flags_from_source(open(bank, encoding="utf-8").read())
    if err:
        return "CANNOT MEASURE", [err]
    doc_flags, err = read_flags_from_doc(open(readme, encoding="utf-8").read())
    if err:
        return "CANNOT MEASURE", [err]
    import json as _j
    if os.environ.get("FLAGCHECK_DEBUG"):
        print(_j.dumps({"src_vol": sorted(src_flags["volumes"]),
                        "doc_vol": sorted(doc_flags["volumes"]),
                        "src_env": sorted(src_flags["env"]),
                        "doc_env": sorted(doc_flags["env"])}, indent=1))
    findings = compare(src_flags, doc_flags)
    if findings:
        return "DIFFER", findings
    return "MATCH", ["%d volume(s), %d env var(s) agree"
                     % (len(src_flags["volumes"]), len(src_flags["env"]))]


def selftest():
    """--selftest: the checker MUST fail on the defect it was written for.

    Arm G (good): the README block lists every durable volume -> exit 0.
    Arm B (bad): the block omits /workspace/self, exactly as master did when
    this was written -> exit 1.
    Arm A (abridged): the same omission with a `# abridged` marker -> exit 0,
    because the block has said it is not authoritative.
    A checker that has only ever seen the good tree is a belief.

    ARM C (added by agent-2): the extractor must produce the real CONTAINER
    PATHS, not <expr>. The three arms above all use f-strings with commas in
    them and all passed while the first regex-based extractor was silently
    turning every durable volume into "<expr>" -- a value that is never
    compared. Deleting /workspace/transcripts from the real README still
    printed MATCH. A mutation arm cannot catch a reader that drops the value
    BEFORE the comparison; only an arm that asserts the parsed artifact can.
    """
    src = '''
def start_agent(name, image, prompts, port=None, model=""):
    args = ["docker", "run", "-d"]
    args.extend([
        "-e", f"AGENT_NAME={name}",
        "-e", f"AGENT_MODEL={model}",
        "-v", f"{workspace_volume(name, 'self')}:/workspace/self",
        "-v", f"{workspace_volume(name, 'transcripts')}:/workspace/transcripts",
        "-v", f"{workspace_volume(name, 'claude')}:/workspace/.claude",
        "-e", "CLAUDE_CONFIG_DIR=/workspace/.claude",
    ])
    args.extend(["--label", "agent.ui=1"])
    args.extend(["-v", "agent-mailbox:/shared"])
    return args
'''
    good = '''Under the hood it runs, from `ui/control.py:start_agent`:

```bash
docker run -d --name agent-1 \\
  -e AGENT_NAME=agent-1 \\
  -e AGENT_MODEL=<model> \\
  -v agent-mailbox:/shared \\
  -v p:/workspace/self \\
  -v t:/workspace/transcripts \\
  -v c:/workspace/.claude \\
  -e CLAUDE_CONFIG_DIR=/workspace/.claude \\
  --label agent.ui=1 \\
  agent-glm
```
'''
    bad = good.replace("  -v p:/workspace/self \\\n", "") \
               .replace("  -v t:/workspace/transcripts \\\n", "") \
               .replace("  -v c:/workspace/.claude \\\n", "") \
               .replace("  -e CLAUDE_CONFIG_DIR=/workspace/.claude \\\n", "")
    abridged = bad.replace("docker run -d", "# abridged: the flag list lives in ui/control.py\n"
                                         "docker run -d")

    ok = True
    parsed, perr = read_flags_from_source(src)
    durable = {"/workspace/self", "/workspace/transcripts",
               "/workspace/.claude"}
    found = set(parsed["volumes"]) & durable if parsed else set()
    ok_c = perr is None and durable <= found
    ok = ok and ok_c
    print("  %-5s %-44s got %s"
          % ("PASS" if ok_c else "FAIL",
             "C parsed paths are real, not <expr>",
             sorted(found) if parsed else perr))

    for label, readme, want in (
            ("G every durable volume listed -> MATCH", good, "MATCH"),
            ("B a durable volume omitted -> DIFFER", bad, "DIFFER"),
            ("A omission WITH '# abridged' -> MATCH", abridged, "MATCH")):
        d = tempfile.mkdtemp(prefix="flagcheck-")
        os.makedirs(os.path.join(d, "ui"), exist_ok=True)
        open(os.path.join(d, "ui", "control.py"), "w").write(src)
        open(os.path.join(d, "README.md"), "w").write(readme)
        got, detail = check(d)
        good_case = got == want
        ok = ok and good_case
        print("  %-5s %-44s got %s, want %s%s"
              % ("PASS" if good_case else "FAIL", label, got, want,
                 "" if good_case else "  ::  " + "; ".join(detail)))
    return ok


def main():
    if "--selftest" in sys.argv:
        ok = selftest()
        print()
        print("FLAGCHECK SELFTEST: %s" % ("PASS" if ok else "FAIL"))
        return 0 if ok else 1
    root = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    verdict, detail = check(root)
    for line in detail:
        print("  " + line)
    print("FLAGCHECK: %s" % verdict)
    return {"MATCH": 0, "DIFFER": 1}.get(verdict, 2)


if __name__ == "__main__":
    sys.exit(main())

