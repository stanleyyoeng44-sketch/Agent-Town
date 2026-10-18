#!/usr/bin/env python3
"""portcheck.py -- does every doc's port PUBLISH serve every code-level CONSUMER?

    python3 portcheck.py [repo-root]      # exit 0 all serve / 1 a consumer is cut off / 2 cannot measure

WHY THIS EXISTS, measured rather than argued. On master 2026-10-18
docs/CLOUD-DEPLOYMENT.md published the bank as

    -p 127.0.0.1:8790:8790

while every agent's tool server reaches it at `http://host.docker.internal:8790`
(agent/mcp-stdio.py:47), which on plain Linux resolves to the docker0 bridge
address (the same document says so at :29). A publish bound to a specific IP
accepts ONLY that IP -- verified in this container by binding a listener to
127.0.0.2 and watching a probe to 127.0.0.1 be refused -- so the documented
stack shipped a bank that no agent could reach. Every `check_money` and
`transfer` would get ECONNREFUSED on a migrated host.

The document's OWN verification step was `curl 127.0.0.1:8790` FROM THE HOST,
which is the one observer that cannot see the failure. A doc can be self-
consistent and still cut off the caller that matters.

SO: the invariant is not "the doc matches a template". It is
    for every port, {classes of caller that reach it} <= {classes the doc publishes to}
Consumers are READ FROM CODE (a URL literal in agent/, ui/, bank/). Publishers
are READ FROM DOCS (a `-p` line inside a fenced docker run). Neither side is
transcribed by hand, so neither can be quietly retyped.

WHAT COUNTS AS A CONSUMER, and the two false positives that produced this rule.
The first version read EVERY http://host:port literal in the named sources. On
the fixed tree it reported two findings that were not bugs:
  * :8080 -- ui/control.py:40 BINDS that port itself
    (PORT = int(os.environ.get("CONTROL_PORT","8080"))) and runs on the host,
    so nothing needs to publish it; agent/agent-daemon.py:1018 mentions it
    inside a PRINted banner string, which is not a caller at all.
  * :8787 -- the proxy is started with `--bind $docker0`, not a `-p` mapping.
    A `--bind` IS a publish (it is the same decision made by the process
    instead of by docker), and its host IP carries the same class.
So a URL literal is a CONSUMER only when it is actually dereferenced, and a
doc PUBLISHES a port via `-p` OR via `--bind`. Both corrections came from
running the tool on the fixed tree and finding it accused two correct lines;
a check that reports a non-bug is the same failure as one that misses a bug,
and it is the one that gets a check deleted.

A literal inside a string that is PRINTED or inside a f-string banner is not
a caller. The rule used here is deliberately narrow and stated as a limit:
only module-level `X = ...http://...` assignments and call arguments count.
It will still miss a URL built by concatenation, and it says so below.

WHAT IS COMPARED, and the deliberate looseness:
  * A consumer URL's HOST decides its class:
        127.0.0.1 / localhost / ::1   -> "loopback"   (a host-side process)
        host.docker.internal          -> "bridge"     (a container, via docker0)
        anything else non-loopback    -> "bridge"     (assumed container-side)
  * A publish form `[-p [HOSTIP:]HOSTPORT:CONTPORT]` covers:
        no HOSTIP                    -> {loopback, bridge}   (binds 0.0.0.0)
        127.0.0.1 / ::1              -> {loopback}
        $docker0 or ${docker0}       -> {bridge}   (the doc defines docker0 as
                                                     the bridge IP -- see below)
        any other literal IP         -> set()      (a specific NIC is neither
                                                     loopback nor the bridge)
  * Ports are matched on the HOST side of the mapping. A publish for :8790
    says nothing about :8787.
  * A doc that starts the service but publishes no port at all is a DIFFER
    for every class -- "no publish" is the strongest form of cutting a caller
    off, and it is the easiest one to introduce by deleting a line.

THE $docker0 CONVENTION, stated because it is an assumption. This file treats
the token `$docker0` in a publish as "the docker0 bridge IP". That is not
parsed from the shell; it is a convention the deployment doc itself establishes
(`docker0=$(ip -o addr show docker0 ...)` immediately above the publish). A
literal bridge IP would be covered too if it equalled a value we could read,
but we do not read `ip addr` here -- no network, no daemon. So: $docker0 is
honoured, a literal 172.17.x.x is NOT assumed to be the bridge. That is
conservative: an unrecognised literal yields set() and therefore a FAIL naming
the consumer, which is the direction that costs nothing to be wrong in.

THE CONTROL. --selftest builds fixtures and requires BOTH outcomes:
    unbound `-p 8790:8790`                      -> PASS  (serves both)
    `-p 127.0.0.1:8790:8790` alone              -> FAIL  (cuts the container)
    `-p $docker0:8790:8790` alone               -> FAIL  (cuts the host UI)
    both publishes                              -> PASS
    no publish line at all                      -> FAIL
A checker that has only seen the good tree is a belief.

WHAT THIS CANNOT SEE, so a PASS is not read as more than it is:
    It does not execute docker. It cannot tell you a repeated `-p` for one
    container port is accepted by your docker version (it is believed to be,
    but this file does not measure it). It does not read the running system's
    interfaces, so `$docker0` is taken on convention. It does not check
    whether the service actually LISTENS on the published port, only that the
    doc publishes the port the code calls. It examines `-p` lines in .md
    files; a publish done by name in a compose file is invisible to it.

Exit: 0 every consumer served / 1 at least one cut off / 2 nothing measurable.
"""
import os
import re
import sys

# Which source files are treated as consumers, and which docs as publishers.
# NAMED, not guessed, so deleting an entry is a visible edit rather than a
# silent narrowing (the M3b lesson).
CONSUMER_SOURCES = ("agent/mcp-stdio.py", "agent/agent-daemon.py",
                    "agent/agent-run.py", "ui/control.py", "bank/bank.py")
PUBLISHER_DOCS = ("README.md", "docs/CLOUD-DEPLOYMENT.md", "docs/guide.md")

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
BRIDGE_HOSTS = {"host.docker.internal"}
URL_RE = re.compile(r"https?://([A-Za-z0-9_.:\[\]-]+?):(\d+)")
# `-p [ip:]hostport:contport` -- docker's publish. `--bind $docker0` -- the
# process publishes itself; same decision, same class, so it is read here too.
PUBLISH_RE = re.compile(r"(?:^|\s)-p\s+([^\s\\]+)")
BIND_RE = re.compile(r"--bind[= ]+([^\s\\]+)")
# A line that SETS a port variable (`PORT = int(... "8080")`) is the listener.
LISTENER_RE = re.compile(
    r"(?:^|\s)(?:PORT|_PORT|port|bind|BIND|HOST|host)\s*=\s*"
    r"(?:int\()?[^\n]*(?:\"[0-9]+\"|'[0-9]+'|--port)")
# Printed banners: a URL mentioned in output is not a caller.
PRINT_RE = re.compile(r"\b(?:print|log|logger|debug|warn|error|info)\s*\(")


def classify_host(host):
    if host in LOOPBACK_HOSTS:
        return "loopback"
    if host in BRIDGE_HOSTS:
        return "bridge"
    # A literal IP or another name. Assume the container's view, which is the
    # assumption that FAILS SAFE: it demands a bridge-covering publish.
    return "bridge"


def read_consumers(root):
    """[(source, line_no, host, port, klass)] for URLs that are DEREFERENCED.

    Exclusions, each earned by a false positive on a CORRECT tree:
      * a URL inside a module/class/function DOCSTRING is prose. ui/control.py:3
        says "Run on the host: python3 control.py (serves http://localhost:8080)"
        -- that is documentation, and my first version reported :8080 as a
        consumer nothing publishes. It is not a caller.
      * a URL inside a print/banner call is output, not a call.
      * a line that BINDS the port it names is a listener, not a consumer.
    Docstrings are found with ast, not by guessing at quotes.
    """
    out = []
    for rel in CONSUMER_SOURCES:
        path = os.path.join(root, rel)
        try:
            with open(path, encoding="utf-8") as f:
                src = f.read()
        except OSError:
            continue
        doc_lines = _docstring_line_spans(src)
        for i, line in enumerate(src.splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if i in doc_lines:
                continue
            if LISTENER_RE.search(line) or PRINT_RE.search(line):
                continue
            for m in URL_RE.finditer(line):
                host, port = m.group(1), m.group(2)
                out.append((rel, i, host, port, classify_host(host)))
    return out


def _docstring_line_spans(src):
    """Line numbers occupied by module/class/function docstrings.

    A docstring is the first Expr(Constant(str)) in a Module/ClassDef/FuncDef.
    Using the AST means a triple-quote inside a real string, or a comment that
    looks like prose, cannot confuse it.
    """
    import ast
    spans = set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return spans
    nodes = [tree]
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                d = body[0]
                spans.update(range(d.lineno, (d.end_lineno or d.lineno) + 1))
    return spans


def parse_publish(spec):
    """('8790', '-p 8790:8790') -> (port, coverage_set, host_ip or None).

    Accepts:  8790:8790 | 127.0.0.1:8790:8790 | $docker0:8790:8790
    """
    spec = spec.rstrip("\\").strip().strip('"\'')
    parts = spec.split(":")
    if len(parts) == 2:
        hostport, _cont = parts
        ip = None
    elif len(parts) == 3:
        ip, hostport, _cont = parts
    else:
        return None
    if not hostport.isdigit():
        return None
    if ip is None:
        return hostport, {"loopback", "bridge"}, None
    if ip in LOOPBACK_HOSTS:
        return hostport, {"loopback"}, ip
    if ip in ("$docker0", "${docker0}"):
        return hostport, {"bridge"}, ip
    return hostport, set(), ip


def scan_publish_lines(lines):
    """({port: (covered_set, [(lineno, raw, ip)])}, [(lineno, ip, cov)]).

    ONE predicate, called from BOTH sides of the supply assertion: the tool's
    own read of PUBLISHER_DOCS, and publishing_docs_in_tree() which is what
    the gate compares it against. They were nearly two predicates before this
    -- the gate hand-rolled its own census -- and a hand-copied predicate
    drifts silently (agent-2 measured exactly that in C12 today). If the
    tool's notion of a publish changes, the census changes with it, which is
    the right coupling: the assertion asks whether the tool READ the docs
    that publish BY ITS OWN DEFINITION of publish.
    """
    ports = {}
    binds = []
    for i, line in enumerate(lines, 1):
            if line.lstrip().startswith("#"):
                continue
            for m in PUBLISH_RE.finditer(line):
                parsed = parse_publish(m.group(1))
                if not parsed:
                    continue
                port, cov, ip = parsed
                prev = ports.setdefault(port, [set(), []])
                prev[0] |= cov
                prev[1].append((i, m.group(1).strip(), ip))
            # `--bind $docker0` publishes the port that PROCESS serves, but the
            # flag does not name a port. Two options: guess from nearby text,
            # or refuse to guess. Guessing is how a check acquires a false
            # verdict, so this records the bind as UNATTRIBUTED and the port
            # stays unclaimed unless a `-p` says otherwise.
            #
            # THIS IS A STATED GAP, not an oversight: a service published only
            # by `--bind` (today: the model proxy on :8787) is invisible to
            # this file's coverage accounting. The consumer scan therefore
            # IGNORES ports that no `-p` mentions and whose only publisher is
            # an unattributed --bind -- see evaluate(). Concretely, :8787 is
            # published by `cbproxyv4.py --bind $docker0` (CLOUD:132) and the
            # doc says so in prose; we do not pretend to have verified it.
            for m in BIND_RE.finditer(line):
                ip = m.group(1).strip()
                cov = ({"bridge"} if ip in ("$docker0", "${docker0}")
                       else {"loopback", "bridge"} if ip in ("0.0.0.0",)
                       else {"loopback"} if ip in LOOPBACK_HOSTS else set())
                binds.append((i, ip, cov))
    return ports, binds


def has_publish(lines):
    """True iff these lines contain a publish this tool would act on.

    The SAME parser decides, so the census below cannot disagree with the
    read above about what "publishes" means.
    """
    ports, _ = scan_publish_lines(lines)
    return bool(ports)


def publishing_docs_in_tree(root, skip_dirs=()):
    """SET of docs in the tree that publish, by this file's own parser.

    Walked with os.walk, NOT `git ls-files`. Measured reason: pipeline.py's
    `_mutant_tree` copies the tree WITHOUT `.git`, so every connection arm
    runs the tool in a directory where a git-based census returns 0 -- and
    `tool_read >= 0` is satisfied by a tool that read nothing. A ground truth
    that is absent exactly where the assertion is exercised is not a ground
    truth. 0 from this function on a tree that HAS docs is an error in the
    caller's hands, not a pass: see the reach check in repo_check.py.
    """
    found = set()
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        dirnames[:] = sorted(
            d for d in dirnames
            if not d.startswith(".") and d != "__pycache__"
            and os.path.normpath(os.path.join(rel_dir, d)) not in skip_dirs
            and os.path.normpath(os.path.join(rel_dir, d)) != ".")
        for fn in filenames:
            if not fn.endswith(".md"):
                continue
            full = os.path.join(dirpath, fn)
            try:
                with open(full, encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
            except OSError:
                continue
            if has_publish(lines):
                found.add(os.path.relpath(full, root).replace(os.sep, "/"))
    return found


def read_publishers(root):
    """({doc: {port: (covered_set, [lines])}}, {doc: [unattributed binds]}).

    The second return is a REPORTED GAP, not a silent one: a `--bind` names an
    interface but no port, so it cannot be joined to a consumer's port without
    guessing. It is printed when non-empty so a MATCH is never read as
    `everything was measured`.
    """
    out = {}
    unattributed = {}
    for rel in PUBLISHER_DOCS:
        path = os.path.join(root, rel)
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            continue
        ports, binds = scan_publish_lines(lines)
        out[rel] = ports
        unattributed[rel] = binds
    return out, unattributed


def evaluate(root):
    consumers = read_consumers(root)
    publishers, unattributed = read_publishers(root)
    if not consumers:
        return None, "no consumer URL literals found in %s" % (CONSUMER_SOURCES,)
    if not publishers:
        return None, "no publisher docs found in %s" % (PUBLISHER_DOCS,)
    findings = []
    gaps = []
    checked = 0
    for doc, ports in sorted(publishers.items()):
        for port in sorted(ports):
            needed = {c[4] for c in consumers if c[3] == port}
            if not needed:
                continue
            covered = ports[port][0]
            missing = needed - covered
            checked += 1
            if missing:
                callers = ["%s:%d (%s, %s)" % (c[0], c[1], c[2], c[4])
                           for c in consumers if c[3] == port and c[4] in missing]
                findings.append(
                    "%s publishes :%s for %s but NOT for %s -- cut off: %s"
                    % (doc, port,
                       "/".join(sorted(covered)) or "nothing",
                       "/".join(sorted(missing)), "; ".join(callers)))
    # A port some code calls that NO document publishes via -p. Before calling
    # that a DIFFER, check whether a `--bind` in that doc could be publishing
    # it: `--bind $docker0` on the proxy's own port is a real publish that
    # this file cannot attribute to a number. That is a GAP, reported
    # separately -- NEVER silently counted as covered, never a false FAIL.
    published_ports = set()
    for ports in publishers.values():
        published_ports |= set(ports)
    bound_any = any(b for b in unattributed.values())
    for port in sorted({c[3] for c in consumers}):
        if port in published_ports:
            continue
        callers = ["%s:%d" % (c[0], c[1]) for c in consumers if c[3] == port]
        if bound_any:
            gaps.append(":%s called from %s; no -p publishes it, and a --bind "
                        "exists that this file cannot attribute to a port "
                        "(NOT VERIFIED)" % (port, "; ".join(callers)))
        else:
            findings.append("no document publishes :%s, called from %s"
                            % (port, "; ".join(callers)))
    # WHICH DOCS WERE ACTUALLY READ. See _publisher_docs_read() in
    # repo_check.py for the attack this makes visible: narrowing
    # PUBLISHER_DOCS and padding the publish count back leaves the pair count
    # constant while a third of the supply side is unread. A DOC count cannot
    # be padded by editing a doc you already read.
    docs_read = sorted(publishers)
    docs_absent = [d for d in PUBLISHER_DOCS if d not in publishers]
    return (checked, findings, gaps, docs_read, docs_absent), None


def run(root="."):
    result, err = evaluate(root)
    if err:
        print("PORTCHECK: CANNOT MEASURE --", err)
        return 2
    checked, findings, gaps, docs_read, docs_absent = result
    for g in gaps:
        print("  GAP (not measured):", g)
    if findings:
        # THE NAMES ARE PRINTED ON BOTH VERDICTS. A gate that compares SETS
        # can only do that for verdicts whose message carries the set, and
        # reading only the MATCH line would make the assertion absent exactly
        # on the trees that are failing -- the same reach bug one level up.
        print("PORTCHECK: DIFFER (%d publish site(s) checked; %d doc(s) read: "
              "%s)" % (checked, len(docs_read), ", ".join(docs_read)))
        for f in findings:
            print("  -", f)
        return 1
    print("PORTCHECK: MATCH (%d publish site(s) serve every code consumer; "
          "%d doc(s) read: %s)"
          % (checked, len(docs_read), ", ".join(docs_read)))
    if gaps:
        print("  NOTE: MATCH covers -p publishes only; %d port(s) rest on an "
              "unattributable --bind (see GAP above)." % len(gaps))
    return 0


def selftest():
    """Both directions, on fixtures. Each arm names the assertion it proves."""
    import tempfile
    base_code = (
        'BANK_URL = os.environ.get("BANK_URL", "http://host.docker.internal:8790")\n'
        'UI = "http://127.0.0.1:8790" + path\n')
    results = []

    def fixture(publish_lines, docs=None):
        d = tempfile.mkdtemp()
        for rel, text in (("agent/mcp-stdio.py",
                           'BANK_URL = os.environ.get("BANK_URL", '
                           '"http://host.docker.internal:8790")\n'),
                          ("ui/control.py",
                           '    "http://127.0.0.1:8790" + path,\n'),
                          ("agent/agent-daemon.py", 'X = 1\n'),
                          ("agent/agent-run.py", 'X = 1\n'),
                          ("bank/bank.py", 'X = 1\n')):
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").write(text)
        os.makedirs(os.path.join(d, "docs"), exist_ok=True)
        body = "```bash\ndocker run -d --name agent-bank \\\n"
        for pl in publish_lines:
            body += "  -p %s \\\n" % pl
        body += "  agent-bank\n```\n"
        # `docs` lets an arm build a tree whose publisher set is KNOWN TO THE
        # ARM. Reading the module-level PUBLISHER_DOCS here made the H and J
        # arms fail with a perfectly legal edit to that constant -- an arm
        # that bites correct code. Measured on a tree where REBUILD.md had
        # been substituted in.
        for rel in (docs if docs is not None else PUBLISHER_DOCS):
            p = os.path.join(d, rel)
            open(p, "w").write(body)
        return d

    arms = [
        ("A unbound publish serves both", ["8790:8790"], 0),
        ("B loopback-only cuts the container", ["127.0.0.1:8790:8790"], 1),
        ("C docker0-only cuts the host UI", ["$docker0:8790:8790"], 1),
        ("D both publishes serve both",
         ["127.0.0.1:8790:8790", "$docker0:8790:8790"], 0),
        ("E no publish line at all", [], 1),
    ]
    ok = True
    for name, pubs, want in arms:
        d = fixture(pubs)
        got = run(d)
        good = (got == want)
        ok &= good
        print("  %-44s %s  (exit %d, want %d)"
              % (name, "PASS" if good else "FAIL", got, want))
    # F: the failure MESSAGE must name the cut-off caller, not just the count.
    d = fixture(["127.0.0.1:8790:8790"])
    _res, _err = evaluate(d)
    findings = _res[1] if _res else []
    named = any("host.docker.internal" in f and "mcp-stdio" in f
                for f in findings)
    ok &= named
    print("  %-44s %s" % ("F the FAIL names the cut-off caller",
                          "PASS" if named else "FAIL"))
    # G: the supply-side DOC count must move when a publisher is dropped.
    d_full = fixture(["0.0.0.0:8790:8790"])
    r_full, _e = evaluate(d_full)
    full_n = len(r_full[3]) if r_full else -1
    saved = PUBLISHER_DOCS
    try:
        globals()["PUBLISHER_DOCS"] = tuple(saved[:-1])
        r_narrow, _e2 = evaluate(d_full)
        narrow_n = len(r_narrow[3]) if r_narrow else -1
    finally:
        globals()["PUBLISHER_DOCS"] = saved
    g_ok = (full_n == len(saved)) and (narrow_n < full_n)
    ok &= g_ok
    print("  %-44s %s  (%d -> %d doc(s) read)"
          % ("G docs read shrinks when a publisher doc is dropped",
             "PASS" if g_ok else "FAIL", full_n, narrow_n))
    # H: the census and the tool must agree as SETS on a tree that publishes.
    # A COUNT is a lossy projection of a set: agent-8's round-3 attack
    # substituted a non-publishing doc for a publishing one and the count was
    # unchanged (3 read, 3 in the tree, guide.md never opened). This arm
    # asserts the thing the gate will assert.
    # H: on a tree where the constant names exactly the publishers, the sets
    # are EQUAL. (The first version of this arm added a fourth doc to the
    # fixture and asserted equality anyway -- it went red on a correct
    # census. An arm that bites correct code is not an arm.)
    _h_docs = ("README.md", "docs/CLOUD-DEPLOYMENT.md", "docs/guide.md")
    d_h = fixture(["0.0.0.0:8790:8790"], docs=_h_docs)
    _h_saved = PUBLISHER_DOCS
    try:
        globals()["PUBLISHER_DOCS"] = _h_docs
        r_h, _e3 = evaluate(d_h)
        read_h = set(r_h[3]) if r_h else set()
        tree_h = publishing_docs_in_tree(d_h)
    finally:
        globals()["PUBLISHER_DOCS"] = _h_saved
        import shutil as _sh
        _sh.rmtree(d_h, ignore_errors=True)
    h_ok = read_h == tree_h and len(tree_h) == len(_h_docs)
    ok &= h_ok
    print("  %-44s %s  (read %s, tree %s)"
          % ("H census matches the read set on a clean tree",
             "PASS" if h_ok else "FAIL", sorted(read_h), sorted(tree_h)))
    # J: THE ROUND-3 ATTACK ITSELF. Substitute a non-publishing doc for a
    # publishing one in PUBLISHER_DOCS. The COUNT is unchanged (3 read, 3 in
    # the tree) -- so a count assertion passes -- while the SETS differ and
    # the unread publisher is named. This is the arm that proves the gate's
    # assertion is connected to the defect agent-8 measured.
    # Build the fixture with an arm-local doc list, so the arm does not depend
    # on which names the shipped constant happens to carry.
    _j_docs = ("README.md", "docs/CLOUD-DEPLOYMENT.md", "docs/guide.md")
    d_j = fixture(["0.0.0.0:8790:8790"], docs=_j_docs)
    _j_saved = PUBLISHER_DOCS
    try:
        # Drop docs/guide.md from the constant and substitute a tracked doc
        # that publishes NOTHING for it. guide.md keeps its real publish, so
        # the tree still has three publishers while the tool opens
        # {README, CLOUD, SUBST} -- count 3 on both sides, set difference
        # {docs/guide.md}. Using the LAST entry instead would have removed a
        # real publish and made the count move, which is the cheap case.
        dropped = "docs/guide.md"
        open(os.path.join(d_j, "SUBST.md"), "w").write("# docker build only\n")
        globals()["PUBLISHER_DOCS"] = (tuple(d for d in _j_docs
                                             if d != dropped)
                                       + ("SUBST.md",))
        try:
            r_j, _e5 = evaluate(d_j)
        finally:
            globals()["PUBLISHER_DOCS"] = _j_saved
        read_j = set(r_j[3]) if r_j else set()
        tree_j = publishing_docs_in_tree(d_j)
    finally:
        import shutil as _sh3
        _sh3.rmtree(d_j, ignore_errors=True)
    j_ok = (len(read_j) == len(tree_j) and read_j != tree_j
            and dropped in tree_j - read_j)
    ok &= j_ok
    print("  %-44s %s  (count %d==%d, unread %s)"
          % ("J substitution keeps the COUNT and moves the SET",
             "PASS" if j_ok else "FAIL", len(read_j), len(tree_j),
             sorted(tree_j - read_j)))
    # I: and the census must SEE an unlisted doc that publishes. The tool
    # does not read it -- that is the defect -- so the difference has to show
    # as a set difference, which is the only message the gate has.
    d_i = fixture(["0.0.0.0:8790:8790"], docs=_h_docs)
    _i_saved = PUBLISHER_DOCS
    try:
        globals()["PUBLISHER_DOCS"] = _h_docs
        open(os.path.join(d_i, "STRAY.md"), "w").write(
            "```\ndocker run --name agent-bank -p 8792:8792 img\n```\n")
        r_i, _e4 = evaluate(d_i)
        read_i = set(r_i[3]) if r_i else set()
        tree_i = publishing_docs_in_tree(d_i)
    finally:
        globals()["PUBLISHER_DOCS"] = _i_saved
        import shutil as _sh2
        _sh2.rmtree(d_i, ignore_errors=True)
    i_ok = "STRAY.md" in tree_i and "STRAY.md" not in read_i
    ok &= i_ok
    print("  %-44s %s  (unread: %s)"
          % ("I an unlisted publishing doc is VISIBLE to the census",
             "PASS" if i_ok else "FAIL", sorted(tree_i - read_i)))
    print("PORTCHECK SELFTEST:", "PASS" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(run(args[0] if args else "."))
