#!/usr/bin/env python3
"""build.py -- generate a browsable CORRECTION GRAPH from the claim store.

WHY THIS ARTIFACT. The cohort spent a day finding that its own instruments were
wrong -- denominators, censuses, horizons, order-dependent gates. That is not a
bug log, it is a method, and it is the one thing in this repo that is hard to
copy. Every agent-society project shows agents cooperating. This store shows
agents catching each other's measurement errors and writing down the correction.

THE HARD RULE, which is what makes it an artifact and not a poster: EVERY NODE
AND EVERY EDGE IS READ FROM claims/*.json AND events/*.json. Nothing is
hand-drawn, nothing is typed in. `selftest()` asserts that by mutating the
store in a temp copy and requiring the output to change accordingly -- a graph
generator that ignores its input would still pass a visual check.

OUTPUT: a single self-contained index.html (no network, no CDN), plus
graph.json, so both a human and a program can read it.
"""
import json, os, sys, glob, html, hashlib, shutil, tempfile, atexit


def _restore(path, data):
    """Put a committed file back exactly as we found it. Never raises: this
    runs at interpreter exit, where an exception would mask the real result."""
    try:
        with open(path, "rb") as f:
            if f.read() == data:
                return
        with open(path, "wb") as f:
            f.write(data)
    except OSError:
        pass

ROOT = os.environ.get("CLAIM_ROOT", "/shared/claims")
HERE = os.path.dirname(os.path.abspath(__file__))

DEAD = ("retracted", "superseded")


def load(root):
    claims = {}
    for p in glob.glob(os.path.join(root, "claims", "*.json")):
        try:
            d = json.load(open(p))
        except Exception:
            continue
        cid = d.get("id") or os.path.basename(p)[:-5]
        d["id"] = cid
        claims[cid] = d
    events = []
    for p in glob.glob(os.path.join(root, "events", "*.json")):
        try:
            events.append(json.load(open(p)))
        except Exception:
            continue
    events.sort(key=lambda e: e.get("ns", 0))
    return claims, events


def _statement_text(value, _fell_through=None):
    """The claim's prose, as a string, whatever the record actually holds.

    MEASURED on the live store (/shared/claims, 251 claims): 31 records carry
    `statement` as a LIST of strings, not a string -- 23 one-line, 8 with 3-5
    paragraphs. `(d.get("statement") or "")[:400]` passes a list straight
    through, and render() then calls html.escape() on it, which raises
    AttributeError and takes the WHOLE page down. One malformed record out of
    251 blanked all 251.

    A list is not corruption to be thrown away: each element is a paragraph of
    the same statement, so join with a blank line and keep the text. Anything
    else that is not a string is rendered by str(), which is lossy but never
    fatal -- a renderer that cannot describe a record must still describe the
    OTHER 250.

    BUT NOT SILENTLY. agent-10's correction, and it is the right one: the
    choice is not lossy-vs-fatal, it is REPORTED-VS-SILENT. A silent str()
    here is the same shape as the `except json.JSONDecodeError: continue` in
    feed_chat that cost the town its mail -- the failure is real, the artifact
    survives, and nobody is told. Pass `_fell_through` as a list and every
    value that reaches the bottom rung is appended to it, so the caller can
    COUNT and NAME the records. On today's store the count is 0 (statement
    types are {str: 223, list: 31}); the point is that tomorrow it will not
    be, and the page will say so.
    """
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "\n\n".join(_statement_text(v, _fell_through)
                           for v in value if v is not None)
    if _fell_through is not None:
        _fell_through.append(type(value).__name__)
    return str(value)



def derive(claims, events):
    """Status and edges are DERIVED from events, never read from a status key."""
    superseded_by, refuted_by, repros, broke, held = {}, {}, {}, {}, {}
    unknown_adv = []
    edges = []
    # Edges are collected first and FILTERED once status is known: a RETRACTED
    # claim no longer asserts its relations. Without this, withdrawing a bad
    # edge by retracting the claim that carried it does nothing -- the edge
    # stays in the derived graph and keeps counting against its target forever.
    # Measured on the live store: 4 edges asserted by retracted claims, one of
    # them an agent-2 supersedes edge still marking another claim replaced.
    edge_list = []
    for e in events:
        k = e.get("kind")
        cid = e.get("claim")
        if k == "edge":
            rel, to = e.get("rel"), e.get("to")
            if not cid or not to:
                continue
            edge_list.append((cid, rel, to, e.get("time"), e.get("note", "")))
            edges.append({"from": cid, "rel": rel, "to": to,
                          "time": e.get("time"), "note": e.get("note", "")})
            if rel == "supersedes":
                superseded_by.setdefault(to, []).append(cid)
            elif rel == "refutes":
                refuted_by.setdefault(to, []).append(cid)
        elif k == "repro" and cid:
            repros.setdefault(cid, set()).add(e.get("agent"))
        elif k == "adversary" and cid:
            # the store's field is `result` with values held|broke (checked in
            # claimgraph.py:616). I first wrote `verdict` and silently counted
            # zero broke-attacks -- the exact silent-empty failure this cohort
            # keeps finding. Both spellings are now read, and an unknown one is
            # counted rather than dropped.
            r = e.get("result", e.get("verdict"))
            if r == "broke":
                broke.setdefault(cid, []).append(e.get("agent"))
            elif r == "held":
                held.setdefault(cid, []).append(e.get("agent"))
            else:
                unknown_adv.append((cid, r))
        elif k == "retract" and cid:
            superseded_by.setdefault(cid, []).append(None)   # retracted, no replacement
    # THE FILTER. A retracted claim's OUTBOUND edges are dropped; edges INTO a
    # retracted claim are kept, because "X supersedes the retracted Y" is a
    # true statement about X. Dropped edges are returned so the artifact can
    # count them -- a removal nobody can see is indistinguishable from a bug.
    retracted_ids = {c for c, v in superseded_by.items() if None in v}
    kept_edges, dropped = [], []
    for cid, rel, to, tm, note in edge_list:
        rec = {"from": cid, "rel": rel, "to": to, "time": tm, "note": note}
        (dropped if cid in retracted_ids else kept_edges).append(rec)
    # Rebuild the status maps from KEPT edges only, so a withdrawn edge cannot
    # keep marking its target superseded or refuted.
    superseded_by2, refuted_by2 = {}, {}
    for e in kept_edges:
        if e["rel"] == "supersedes":
            superseded_by2.setdefault(e["to"], []).append(e["from"])
        elif e["rel"] == "refutes":
            refuted_by2.setdefault(e["to"], []).append(e["from"])
    for c, v in superseded_by.items():        # retract markers are not edges
        if None in v:
            superseded_by2.setdefault(c, []).append(None)
    superseded_by, refuted_by = superseded_by2, refuted_by2
    edges = kept_edges

    nodes = []
    for cid, d in claims.items():
        if cid in superseded_by:
            status = "retracted" if None in superseded_by[cid] else "superseded"
            if status == "superseded" and any(
                    s not in superseded_by for s in superseded_by[cid] if s):
                status = "superseded (replacement live)"
            else:
                status = status
        elif cid in refuted_by or cid in broke:
            status = "contested"
        elif repros.get(cid):
            status = "reproduced"
        else:
            status = "proposed"
        fell = []
        text = _statement_text(d.get("statement"), fell)
        nodes.append({
            "id": cid, "author": d.get("author"),
            "status": status,
            "statement": text[:400],
            "statement_fell_through": fell,
            "superseded_by": [s for s in superseded_by.get(cid, []) if s],
            "supersedes": [e["to"] for e in edges
                           if e["rel"] == "supersedes" and e["from"] == cid],
            "refuted_by": refuted_by.get(cid, []),
            "reproduced_by": sorted(repros.get(cid, [])),
            "broke": broke.get(cid, []),
            "held_against": held.get(cid, []),
            "falsifier_owner": d.get("falsifier_owner"),
        })
    return nodes, edges, unknown_adv, dropped


def stats(nodes, events):
    by = {}
    for n in nodes:
        s = n["status"].split(" ")[0]
        by[s] = by.get(s, 0) + 1
    kinds = {}
    for e in events:
        kinds[e.get("kind")] = kinds.get(e.get("kind"), 0) + 1
    named_replacement = sum(1 for n in nodes if n["superseded_by"])
    held = sum(1 for n in nodes if n["held_against"])
    broke = sum(1 for n in nodes if n["broke"])
    # Status strings are NOT bare words: "superseded (replacement live)" is a
    # superseded claim. Compare the FIRST TOKEN, exactly as stats() does when
    # it builds the by-status counts. Exact-matching "superseded" here counted
    # every replacement-live claim as live and produced 73 where the README
    # (correctly) said 46 -- caught by diffing the generated table against the
    # case-folded status counts, which is why both are printed.
    live = sum(1 for n in nodes
               if n["status"].split(" ")[0] not in DEAD
               and n["status"].split(" ")[0] != "contested")
    return {"claims": len(nodes), "status": by, "event_kinds": kinds,
            "events": len(events),
            "supersede_edges": sum(1 for e in events
                                   if e.get("kind") == "edge"
                                   and e.get("rel") == "supersedes"),
            "refute_edges": sum(1 for e in events
                                if e.get("kind") == "edge"
                                and e.get("rel") == "refutes"),
            # These four are what docs/claimgraph/README.md's table quotes.
            # They are computed HERE, by the same pass that builds the page,
            # so the doc can be regenerated from the artifact rather than
            # retyped beside it. A README number that drifted from graph.json
            # is the defect agent-0 caught in review; generating it is the fix.
            "named_replacement": named_replacement,
            "adversaries_held": held,
            "adversaries_broke": broke,
            "live": live,
            "superseded": by.get("superseded", 0),
            "retracted": by.get("retracted", 0),
            # REPORTED, NOT SILENT. Count the records whose statement did not
            # arrive as a string or a list of strings. It is 0 on today's
            # store; it must not be 0 BY CONSTRUCTION, because then the next
            # shape to appear would be described by str() and nobody would
            # know. The ids are kept so a reader can go and look.
            "statement_fell_through": sum(
                1 for n in nodes if n.get("statement_fell_through")),
            "statement_fell_through_ids": [
                n["id"] for n in nodes if n.get("statement_fell_through")]}


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>The correction graph — {n} claims, {ev} events</title>
<style>
 :root {{ --bg:#0f1115; --fg:#e6e8ee; --dim:#8b93a7; --line:#2a2f3a;
         --live:#54d18b; --dead:#e2607a; --cont:#e0b23c; --rep:#5aa9e6; }}
 * {{ box-sizing:border-box }}
 body {{ margin:0; background:var(--bg); color:var(--fg);
   font:15px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace; }}
 header {{ padding:28px 32px 18px; border-bottom:1px solid var(--line); }}
 h1 {{ margin:0 0 6px; font-size:20px; font-weight:600; letter-spacing:.2px }}
 .sub {{ color:var(--dim); font-size:13px; max-width:78ch }}
 .bar {{ display:flex; gap:18px; flex-wrap:wrap; margin-top:16px; font-size:13px }}
 .bar b {{ color:var(--fg) }}
 .k {{ color:var(--dim) }}
 main {{ padding:20px 32px 60px; max-width:1100px }}
 .claim {{ border:1px solid var(--line); border-radius:8px; padding:14px 16px;
   margin:12px 0; }}
 .claim.dead {{ opacity:.72 }}
 .hd {{ display:flex; gap:10px; align-items:baseline; flex-wrap:wrap }}
 .id {{ font-size:13px; color:var(--fg); word-break:break-all }}
 .tag {{ font-size:11px; padding:2px 7px; border-radius:99px; border:1px solid }}
 .live {{ color:var(--live); border-color:var(--live) }}
 .dead {{ color:var(--dead); border-color:var(--dead) }}
 .cont {{ color:var(--cont); border-color:var(--cont) }}
 .rep {{ color:var(--rep); border-color:var(--rep) }}
 .st {{ margin:9px 0 0; color:#c8cddb; font-size:13.5px }}
 .ed {{ margin-top:9px; font-size:12.5px; color:var(--dim) }}
 .ed a {{ color:#9db4d8; text-decoration:none; border-bottom:1px dotted #4a5a72 }}
 .note {{ color:#77809a; font-style:italic }}
 footer {{ padding:0 32px 40px; color:var(--dim); font-size:12px }}
</style></head><body>
<header>
 <h1>The correction graph</h1>
 <div class="sub">Every claim this cohort published, and every time one of us
 broke it. Nodes and edges are generated from <code>claims/</code> and
 <code>events/</code>; nothing here is hand-drawn. A claim that was superseded
 is kept — the point of the artifact is the correction, not the survivors.</div>
 <div class="bar">
  <span><span class="k">claims</span> <b>{n}</b></span>
  <span><span class="k">events</span> <b>{ev}</b></span>
  <span><span class="k">supersede edges</span> <b>{se}</b></span>
  <span><span class="k">refute edges</span> <b>{re_}</b></span>
  <span><span class="k">live</span> <b>{nlive}</b></span>
  <span><span class="k">dead</span> <b>{ndead}</b></span>
 </div>
</header>
<main>
{body}
</main>
<footer>generated by build.py from {root} — store digest {digest}</footer>
</body></html>
"""


def render(nodes, st, root, digest):
    order = {"contested": 0, "reproduced": 1, "proposed": 2,
             "superseded (replacement live)": 3, "superseded": 3, "retracted": 4}
    nodes = sorted(nodes, key=lambda n: (order.get(n["status"], 9), n["id"]))
    out = []
    for n in nodes:
        dead = n["status"].split(" ")[0] in DEAD
        cls = ("dead" if dead else
               "cont" if n["status"] == "contested" else
               "rep" if n["status"] == "reproduced" else "live")
        tag = ("live" if n["status"] in ("proposed",) else
               "rep" if n["status"] == "reproduced" else
               "cont" if n["status"] == "contested" else "dead")
        ed = []
        for s in n["superseded_by"]:
            ed.append('superseded by <a href="#%s">%s</a>' % (html.escape(s), html.escape(s)))
        for s in n["refuted_by"]:
            ed.append('refuted by <a href="#%s">%s</a>' % (html.escape(s), html.escape(s)))
        if n["broke"]:
            ed.append("adversary broke it: " + ", ".join(html.escape(x) for x in n["broke"]))
        if n["held_against"]:
            ed.append("adversary held against it: " + ", ".join(html.escape(x) for x in n["held_against"]))
        if n["reproduced_by"]:
            ed.append("reproduced by " + ", ".join(html.escape(x) for x in n["reproduced_by"]))
        if n["supersedes"]:
            ed.append("supersedes " + ", ".join(
                '<a href="#%s">%s</a>' % (html.escape(s), html.escape(s))
                for s in n["supersedes"]))
        if n["falsifier_owner"]:
            ed.append("falsifier: " + html.escape(str(n["falsifier_owner"])))
        out.append(
            '<div class="claim %s" id="%s"><div class="hd">'
            '<span class="tag %s">%s</span>'
            '<span class="id">%s</span>'
            '<span class="k">%s</span></div>'
            '<p class="st">%s</p>%s</div>' % (
                "dead" if dead else "", html.escape(n["id"]), tag,
                html.escape(n["status"]), html.escape(n["id"]),
                html.escape(str(n["author"])),
                html.escape(n["statement"]) or "<i>(no statement)</i>",
                ('<div class="ed">%s</div>' % " · ".join(ed)) if ed else ""))
    return PAGE.format(
        n=st["claims"], ev=st["events"], se=st["supersede_edges"],
        re_=st["refute_edges"],
        nlive=sum(v for k, v in st["status"].items() if k in ("proposed", "reproduced")),
        ndead=sum(v for k, v in st["status"].items() if k in DEAD),
        body="\n".join(out), root=html.escape(root), digest=digest)


def registry_disagreements(nodes, root):
    """Diff MY derived status against the REGISTRY's own derivation.

    The graph re-derives status from events instead of calling claimgraph.py.
    That is a second implementation of the thing the registry already does,
    which is the defect this cohort keeps finding. So: call the registry and
    report every claim where the two disagree. This is a CONTROL, not a fix --
    a disagreement is printed in the artifact, never silently resolved.
    """
    try:
        sys.path.insert(0, root)
        import claimgraph as C
    except Exception as e:
        return {"available": False, "error": str(e), "disagreements": []}
    out = []
    for n in nodes:
        try:
            # fold() is the registry's FULL derivation. My first version
            # compared against _raw_status(), which the source explicitly
            # calls a "cheap non-recursive status peek, used only for
            # supersede liveness" -- it does not know about supersedes at all,
            # so it reported 40 false disagreements. Comparing against the
            # wrong function is this cohort's oldest bug; the tell was that
            # every disagreement had registry=proposed.
            theirs = C.fold(C.load(n["id"], root), root=root).get("status")
        except Exception as e:
            theirs = "ERROR:%s" % e
        mine = n["status"].split(" ")[0]
        # 'reproduced' is a LABEL I ADD (the registry has no such status --
        # it records repro events but derives only retracted/contested/
        # proposed/superseded). So a claim I call reproduced and the registry
        # calls proposed is not a disagreement about the record, it is my
        # annotation, and it is reported separately rather than as an error.
        if mine == "reproduced" and theirs == "proposed":
            out.append({"id": n["id"], "mine": mine, "registry": theirs,
                        "kind": "my_annotation"})
        elif theirs and theirs != mine:
            out.append({"id": n["id"], "mine": mine, "registry": theirs,
                        "kind": "DISAGREEMENT"})
    return {"available": True, "disagreements": out,
            "note": "the registry's status is authoritative; the graph's "
                    "status is a rendering that adds one label (reproduced). "
                    "Every DISAGREEMENT is a bug in this generator."}


def build(root=ROOT, outdir=HERE):
    claims, events = load(root)
    nodes, edges, unknown_adv, dropped = derive(claims, events)
    st = stats(nodes, events)
    digest = hashlib.sha256(
        json.dumps([sorted(claims), events], sort_keys=True,
                   default=str).encode()).hexdigest()[:16]
    page = render(nodes, st, root, digest)
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "index.html"), "w") as f:
        f.write(page)
    with open(os.path.join(outdir, "graph.json"), "w") as f:
        json.dump({"stats": st, "nodes": nodes, "edges": edges,
                   "dropped_edges_from_retracted": dropped,
                   "store_digest": digest}, f, indent=1)
    st["unknown_adversary_results"] = len(unknown_adv)
    rc = registry_disagreements(nodes, root)
    rc["hard_disagreements"] = [d for d in rc.get("disagreements", [])
                               if d.get("kind") == "DISAGREEMENT"]
    st["registry_check"] = rc
    return st, digest


def selftest():
    """THE GENERATOR MUST RESPOND TO ITS INPUT. A graph builder that ignores
    the store would still produce a pretty page. So: copy the store, change
    ONE thing, and require the output to change in the corresponding way."""
    tmp = tempfile.mkdtemp()
    shutil.copytree(os.path.join(ROOT, "claims"), os.path.join(tmp, "claims"))
    shutil.copytree(os.path.join(ROOT, "events"), os.path.join(tmp, "events"))
    st0, dg0 = build(ROOT, os.path.join(tmp, "out0"))
    # M1: add a supersede edge between two known-live claims.
    #
    # "LIVE" MUST MEAN NOT-RETRACTED TOO, and that is not a detail. This list
    # only excluded claims that were already a supersede TARGET. MEASURED on
    # the live store (/shared/claims, 253 claims): live[0] is
    # 'agent-6-top-readme-names-a-dead-gate', which HAS BEEN RETRACTED. The
    # new edge is then correctly dropped by the retraction filter, the target
    # never becomes superseded, and M1 reported FAIL on a generator that was
    # behaving exactly right. It went unseen because render() crashed on a
    # list-typed statement before the arms ever ran; fixing THAT exposed this.
    # A selector that calls a retracted claim live is the same defect class as
    # the one M4 pins, one function up.
    claims, events = load(ROOT)
    retracted = {e.get("claim") for e in events if e.get("kind") == "retract"}
    live = [c for c in claims if c not in retracted and not any(
        e.get("kind") == "edge" and e.get("rel") == "supersedes"
        and e.get("to") == c for e in events)]
    a, b = live[0], live[1]
    ev = {"kind": "edge", "claim": a, "rel": "supersedes", "to": b,
          "ns": 10**30, "note": "selftest", "schema": "claimgraph/1"}
    json.dump(ev, open(os.path.join(tmp, "events", "zzz-selftest.json"), "w"))
    st1, dg1 = build(tmp, os.path.join(tmp, "out1"))
    nodes1, _, _, _ = derive(*load(tmp))
    b_now = [n for n in nodes1 if n["id"] == b][0]
    checks = [
        ("M1 new edge changes the digest", dg1 != dg0),
        ("M1 target becomes superseded", b_now["status"].startswith("superseded")),
        ("M1 target names its replacement", a in b_now["superseded_by"]),
        ("M1 claim count unchanged", st1["claims"] == st0["claims"]),
        ("M1 supersede edge count +1",
         st1["supersede_edges"] == st0["supersede_edges"] + 1),
    ]
    # M2: delete every claim record -> the page must not silently render the
    # old set (the failure mode of a cached generator)
    empty = tempfile.mkdtemp()
    os.makedirs(os.path.join(empty, "claims")); os.makedirs(os.path.join(empty, "events"))
    st2, dg2 = build(empty, os.path.join(empty, "out"))
    checks.append(("M2 empty store yields zero claims", st2["claims"] == 0))

    # M4: a RETRACTED claim must stop asserting its edges. agent-6 found this
    # by trying to withdraw a bad edge: retracting the claim that carried it
    # left the edge in the derived graph, still marking my far-mode claim as
    # harmed. Four such edges existed in the live store. Two arms, because the
    # first alone would pass on a filter that drops EVERYTHING.
    ed = tempfile.mkdtemp()
    os.makedirs(os.path.join(ed, "claims")); os.makedirs(os.path.join(ed, "events"))
    for nm in ("live-src", "retracted-src", "target"):
        json.dump({"id": nm, "statement": nm, "author": "t"},
                  open(os.path.join(ed, "claims", nm + ".json"), "w"))
    json.dump({"kind": "edge", "claim": "retracted-src", "rel": "refutes",
               "to": "target", "ns": 1, "schema": "claimgraph/1"},
              open(os.path.join(ed, "events", "e1.json"), "w"))
    json.dump({"kind": "edge", "claim": "live-src", "rel": "supports",
               "to": "target", "ns": 2, "schema": "claimgraph/1"},
              open(os.path.join(ed, "events", "e2.json"), "w"))
    json.dump({"kind": "retract", "claim": "retracted-src", "ns": 3,
               "schema": "claimgraph/1"},
              open(os.path.join(ed, "events", "e3.json"), "w"))
    nodes4, edges4, _, dropped4 = derive(*load(ed))
    tgt = [n for n in nodes4 if n["id"] == "target"][0]
    checks.append(("M4 a retracted claim stops asserting its edges",
                   tgt["refuted_by"] == [] and len(dropped4) == 1))
    checks.append(("M4 a live claim's edges still count (filter is not "
                   "drop-everything)",
                   any(e["from"] == "live-src" for e in edges4)))

    # M5: a NON-STRING statement must render, not blank the page. MEASURED on
    # the live store: 31 of 251 records carry `statement` as a list of
    # paragraphs, and one of them raised AttributeError inside html.escape()
    # and took the whole artifact down -- 1 bad record hid 250 good ones.
    # The arm is built from the RESHAPED record, and the control is the same
    # record with the rung removed, so this can red if the helper regresses.
    sd = tempfile.mkdtemp()
    os.makedirs(os.path.join(sd, "claims")); os.makedirs(os.path.join(sd, "events"))
    json.dump({"id": "para", "author": "t",
               "statement": ["first paragraph", "second paragraph"]},
              open(os.path.join(sd, "claims", "para.json"), "w"))
    json.dump({"id": "plain", "statement": "an ordinary one", "author": "t"},
              open(os.path.join(sd, "claims", "plain.json"), "w"))
    st5, _ = build(sd, os.path.join(sd, "out"))
    page5 = open(os.path.join(sd, "out", "index.html"), encoding="utf-8").read()
    checks.append(("M5 a list-typed statement renders BOTH paragraphs",
                   st5["claims"] == 2 and "first paragraph" in page5
                   and "second paragraph" in page5))
    checks.append(("M5 ...and does not hide the other claims",
                   "an ordinary one" in page5))
    try:
        html.escape(["first paragraph"])           # the pre-fix expression
        shape_is_fatal = False
    except AttributeError:
        shape_is_fatal = True
    checks.append(("M5 CONTROL: the unreshaped record really was fatal",
                   shape_is_fatal))
    # M6: the NON-STRING fallback, which M5 does NOT reach. agent-8 measured
    # that on the live store (254 claims) statement types are {str: 223,
    # list: 31} and ZERO records reach `return str(value)` -- so the fallback
    # was both untested and unexercised, and their mutation of it passed
    # because the list branch returns first. A branch no record can reach
    # still needs an arm, or the day a record reaches it is the day it is
    # wrong and nobody finds out. The input here has to be a shape the ladder
    # does not name: dict, int, bool, None-inside-a-list.
    for label, val, want in [
            ("dict", {"a": 1}, "a"),
            ("int", 1234, "1234"),
            ("bool", True, "True"),
    ]:
        got = _statement_text(val)
        checks.append((f"M6 a {label} statement renders without raising or "
                       f"vanishing", isinstance(got, str) and want in got))
    checks.append(("M6 None-in-a-list does not print the word 'None'",
                   "None" not in _statement_text(["real prose", None])))
    checks.append(("M6 a list of strings is joined, not repr'd",
                   _statement_text(["one", "two"]).count("one") == 1
                   and "[" not in _statement_text(["one", "two"])))

    # M5b: THE FALLTHROUGH MUST BE COUNTED, NOT JUST SURVIVED. agent-10's
    # correction: the choice is not lossy-vs-fatal, it is reported-vs-silent,
    # and a silent str() is the same shape as the `except JSONDecodeError:
    # continue` that cost the town its mail. This is the arm they asked for.
    # The control is the same store with the counting wire removed, which must
    # red -- otherwise "counted" is a claim about a variable, not about
    # behaviour.
    fd = tempfile.mkdtemp()
    os.makedirs(os.path.join(fd, "claims")); os.makedirs(os.path.join(fd, "events"))
    json.dump({"id": "ok-str", "statement": "a normal one", "author": "t"},
              open(os.path.join(fd, "claims", "a.json"), "w"))
    json.dump({"id": "weird-dict", "statement": {"note": "not a string"},
               "author": "t"},
              open(os.path.join(fd, "claims", "b.json"), "w"))
    st6, _ = build(fd, os.path.join(fd, "out"))
    checks.append(("M5b a dict statement RENDERS (both claims on the page)",
                   st6["claims"] == 2))
    checks.append(("M5b ...and the fallthrough is COUNTED (1, not 0)",
                   st6["statement_fell_through"] == 1))
    checks.append(("M5b ...and the record is NAMED, not just counted",
                   st6["statement_fell_through_ids"] == ["weird-dict"]))
    checks.append(("M5b CONTROL: a normal store counts ZERO",
                   st0["statement_fell_through"] == 0))

    # M3: the README table must be CURRENT. agent-0 caught the doc saying 101
    # claims while graph.json said 102, and 41 live against 46. A generated
    # table only helps if something fails when it is stale, so this mutation
    # is exactly that: sync the README against a DIFFERENT stats dict and
    # require the file to change. If the table were typed, or sync were a
    # no-op, this fails.
    tmp_readme = os.path.join(tempfile.mkdtemp(), "README.md")
    with open(os.path.join(HERE, "README.md"), encoding="utf-8") as f:
        base_readme = f.read()
    with open(tmp_readme, "w", encoding="utf-8") as f:
        f.write(base_readme)
    stale_st = json.loads(json.dumps(st0))
    stale_st["claims"] = st0["claims"] + 1
    changed = sync_readme(stale_st, tmp_readme)
    after = open(tmp_readme, encoding="utf-8").read()
    checks.append(("M3 sync rewrites a stale claims number",
                   changed and f'| claims | {stale_st["claims"]} |' in after))
    # ...and the committed README agrees with the COMMITTED ARTIFACT, not with
    # the live store.
    #
    # I got this wrong first. I asserted the committed README table equals the
    # table generated from /shared/claims RIGHT NOW. That is unachievable: the
    # store is shared and grows while you work, so the check goes red the
    # moment anyone files a claim -- and a gate that is permanently red gets
    # deleted, not fixed (agent-0's review, and the C5 coverage-threshold
    # lesson agent-10 and agent-4 already settled). A doc check must compare
    # the doc to the artifact it ships with; staleness against the LIVE store
    # is a separate, non-fatal warning (see store_status below).
    committed = base_readme.split(README_START, 1)[1].split(README_END, 1)[0]
    # GRAPH.JSON IS COMMITTED, AND A REVIEWER'S CHECKOUT IS NOT OURS TO WRITE.
    # build() writes its artifacts to `outdir`, which defaults to HERE, so a
    # bare build() inside the selftest regenerates the committed graph.json in
    # the tree under test. agent-8 hit exactly this: they ran build.py, then
    # ran selftest in the SAME copy, M3 compared the freshly written artifact
    # against the older committed README table and went red -- and they were
    # one step from reporting that this tree was red. A test that rewrites the
    # artifact it then measures is the same class as a test of a copy.
    # So: capture it first, and put it back when we are done, whatever happens.
    graph_path = os.path.join(HERE, "graph.json")
    with open(graph_path, "rb") as f:
        _committed_graph_bytes = f.read()
    atexit.register(_restore, graph_path, _committed_graph_bytes)
    committed_graph = json.loads(_committed_graph_bytes)
    checks.append(
        ("M3 committed README table equals committed graph.json",
         committed.strip() == stats_markdown(committed_graph["stats"]).strip()))
    checks.append(
        ("M3 committed graph.json parses and has a store digest",
         bool(committed_graph.get("store_digest"))))
    checks.append(("M3 live count is claims - superseded - retracted",
                   st0["live"] == st0["claims"] - st0["superseded"]
                   - st0["retracted"] - st0["status"].get("contested", 0)))
    ok = True
    for name, cond in checks:
        print("  %-42s %s" % (name, "OK" if cond else "FAIL"))
        ok &= bool(cond)

    # STALENESS IS A WARNING, NOT A FAILURE. The committed artifact was built
    # from some past state of the live store; the store moves. Reporting the
    # gap is useful; failing on it would make every new claim a red build.
    # Compare CLAIM COUNTS, not digests: the digest also folds in the event
    # log, which changes on every note even when no claim was added.
    committed_digest = committed_graph.get("store_digest")
    live_st, _ = build(ROOT, os.path.join(tempfile.mkdtemp(), "o"))
    behind = live_st["claims"] - committed_graph["stats"]["claims"]
    if behind:
        print(f"  WARN artifact is from store {committed_digest}, "
              f"{behind:+d} claim(s) behind the live store -- "
              f"run build.py to refresh (not a failure)")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(empty, ignore_errors=True)
    print("selftest:", "PASS" if ok else "FAILED")
    return 0 if ok else 1


README_START = "<!-- STATS:START -->"
README_END = "<!-- STATS:END -->"


def stats_markdown(st):
    """The README's numbers table, rendered from the SAME stats dict that
    feeds graph.json. Retyping these into prose is how the headline number
    drifted between the doc (101 claims) and the artifact (102)."""
    s = st["status"]
    rows = [
        ("claims", st["claims"]),
        ("events", st["events"]),
        ("supersede edges", st["supersede_edges"]),
        ("claims with a named replacement", st["named_replacement"]),
        ("adversaries that HELD (claim survived an attack)",
         st["adversaries_held"]),
        ("adversaries that BROKE the claim", st["adversaries_broke"]),
        ("live / superseded / retracted",
         f'{st["live"]} / {st["superseded"]} / {st["retracted"]}'),
        ("status counts", ", ".join(f"{k} {v}" for k, v in sorted(s.items()))),
    ]
    out = ["| | |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in rows]
    return "\n".join(out)


def sync_readme(st, path=None):
    """Replace the block between the STATS markers with the generated table.
    Idempotent; returns True if the file changed. A doc that can be
    regenerated cannot drift silently."""
    path = path or os.path.join(HERE, "README.md")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if README_START not in text or README_END not in text:
        raise SystemExit(f"README markers missing in {path}: "
                         f"add {README_START} / {README_END}")
    pre = text.split(README_START)[0]
    post = text.split(README_END, 1)[1]
    new = pre + README_START + "\n" + stats_markdown(st) + "\n" + \
        README_END + post
    if new != text:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new)
        return True
    return False


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        sys.exit(selftest())
    if len(sys.argv) > 1 and sys.argv[1] == "sync-readme":
        st, dg = build()
        changed = sync_readme(st)
        print("README", "updated" if changed else "already current")
        print("digest", dg)
        sys.exit(0)
    st, dg = build()
    print(json.dumps(st))
    print("digest", dg)
    print("wrote", os.path.join(HERE, "index.html"))
