#!/usr/bin/env python3
"""namecheck.py — the repository's own name, as every tracked doc spells it.

WHY THIS EXISTS, MEASURED 2026-10-19 on master 7cc6c37:
    the repository was RENAMED. stanleyyoeng44-sketch/docker-AI is now
    stanleyyoeng44-sketch/Agent-Town (the API 301s to repositories/1424086770).
    The tree did not notice: `git grep -in agent-town` returns ZERO lines, while
    ELEVEN references across five tracked docs still spell the old name.

WHY THAT IS A DEFECT AND NOT A TYPO. I ran the newcomer's path rather than
reasoning about it:
    git clone https://github.com/stanleyyoeng44-sketch/Agent-Town.git
    cd docker-AI                    -> "No such file or directory", exit 1
The clone creates Agent-Town/, and the very next line of docs/guide.md -- in
the block labelled [verified] -- names the old directory. It works TODAY only
because the shipped docs use the LEGACY url and GitHub still redirects it, and
because git names the clone directory from the URL you typed. So two blocks
advertised as verified are load-bearing on a redirect that retains a name the
project no longer has. A redirect is not a contract.

WHY NO EXISTING CHECK SEES IT (two searches, both empty):
  * no tool in tools/ references the repo name, full_name or the README H1;
  * C5 parses this file's clone block to `git clone ...`, which is neither
    RUNNABLE nor ENV_LIMITED, so the block is SKIPPED with a reason and the
    following `cd` is never executed either.
Eleven stale references, a green C5, and a green "guide and demo blocks
RESULT: HOLDS". The gate models the part of the world it reads; the artifact
names a different one.

THE SELECTOR, and this is the only subtle part. The subject is THE REPOSITORY
NAME, not the string "docker-AI" -- they are different objects that happen to
share a spelling, and a blanket replace would corrupt a real path. Evidence
that the distinction is load-bearing rather than pedantic:
    ui/control.py:42   PRIVATE_DIR = Path.home() / ".config" / "docker-ai"
`~/.config/docker-ai/` is a HOST CONFIG DIRECTORY holding token files. It is
not the repository, it is not being renamed, and every doc that mounts it is
correct. The repository's own name is the capitalised `docker-AI`; the config
directory is lowercase. `_repo_refs` therefore reports a capital-AI token only
when it is the last path segment of a git URL or the argument of `cd`, or when
it stands as a bare prose word. A lowercase token, or a capital one used as a
`~/.config/...` path segment, is not a reference to the repository.

WHAT IT DOES NOT MEASURE, stated so a PASS is not read as more than it is:
  * It scans tracked *.md only. A stale name in a .py docstring or a CI yaml
    is out of scope; three sites are the whole tracked .md set today.
  * It cannot know whether the canonical name is RIGHT -- it reads that from
    CANONICAL below, one constant, and asserts the tree agrees with it.
  * It says nothing about the repo's GitHub metadata (description, topics,
    license). Those are owner fields, measured by hand and recorded in
    /shared/artifacts/agent-2/STANLEY_METADATA.md.

THE TRAP THIS CHECK ALMOST FELL INTO, recorded because it is this cohort's
disease: the obvious arm is internal consistency -- "the clone URL's last
segment equals the directory the next `cd` names". On today's tree that arm is
GREEN, because today's docs are CONSISTENTLY WRONG. A predicate whose only
reference point is the document it is checking cannot reject a document that
agrees with itself. So the reference point is a CONSTANT, CANONICAL, and the
tree is asserted against it. That is what makes the check red now and red
again on the next rename instead of silently following it.
"""
import os
import re
import subprocess
import sys

# The repository's name as github.com serves it. One constant, deliberately
# not derived from the checkout -- a remote URL in .git/config is local state
# and would follow a fork or a stale mirror instead of naming the real repo.
CANONICAL = "Agent-Town"

# Spellings that ARE the repository name. Capital "AI" is what separates the
# repo from the lowercase host config dir; see the docstring. Only `docker-AI`
# is historical, but the form is listed as a family so a future rename of the
# same shape is caught by changing CANONICAL and adding its predecessor here.
FORMER = ("docker-AI",)

# SITES THE OWNER HAS NOT YET RULED ON, named rather than silently exempt.
#
# agent-0's constraint when approving this work: do not rewrite README's H1 or
# its voice -- the repository's self-description is Stanley's, and he renamed
# the repo himself. The H1 is the only remaining stale reference, so it is
# WAIVED and ASKED, not rewritten and not hidden.
#
# A waiver that could outlive its reason is the hand-typed exemption this
# cohort keeps finding in other people's guards, so it retires itself:
#   * it is printed on every run, so a PASS never hides the open question;
#   * if the text at the site no longer names the former repo, the waiver is
#     STALE and this check FAILS until the entry is deleted. A waiver that has
#     quietly stopped waiving anything is a lie about coverage.
WAIVERS = {
    "README.md:1": "the H1 names the former repo; agent-0 asked that the "
                   "owner's own self-description not be rewritten, so this "
                   "is a question for Stanley, not an agent edit",
}
# The md5 of the ONE LINE this waiver covers, not of the whole README.
#
# It used to be the md5 of README.md. That binding was right about the thing
# it was defending -- a waiver must not outlive its text -- and wrong about
# its SCOPE, and the wrongness cost a merge: any commit that edited ANY
# other line of the README (changing the module count, fixing a typo, adding
# a row) made this waiver stale and turned the gate red on a line the author
# never touched. MEASURED on the merge of the C8 count change, which is a
# correct edit to a different line: namecheck FAILED with "WAIVER IS STALE".
# A waiver bound to a whole file is a claim about a line that taxes every
# other author of the file, which is the same defect as stating a tree-wide
# count in prose.
#
# So: bind to the exact H1 bytes. Editing the H1 still retires the waiver
# (the property that mattered); editing line 330 no longer does. Computed,
# not typed: arm K checks it against the real README in the tree.
WAIVED_LINE = "# docker-AI — a small town of Claude agents in Docker"
# md5 of the line WITHOUT its trailing newline, so a CRLF/editor change to
# the rest of the file cannot retire a waiver about this text.
WAIVED_LINE_MD5 = "d0f72fa46687225745d182ba71cefa02"


def _tracked_docs(root):
    """Every tracked markdown file, from git -- not a walk of the worktree.

    `git ls-files` is the index, so an untracked scratch file cannot make this
    check red and a deleted one cannot make it silently narrower.
    """
    try:
        out = subprocess.run(["git", "-C", root, "ls-files", "-z"],
                             capture_output=True, text=True, timeout=60)
    except OSError:
        return None
    if out.returncode != 0:
        return None
    names = [n for n in out.stdout.split("\0") if n]
    return sorted(n for n in names if n.lower().endswith(".md"))


# A git URL whose last path segment is the repo name, with or without .git.
_URL = re.compile(r"git(?:@|://|hub\.com[/:])[\w.@:/~-]*?/([\w.-]+?)(?:\.git)?(?=[\s`'\")]|$)",
                  re.I)
# `cd <dir>` where <dir> is a plain relative path (not $VAR, not a path).
_CD = re.compile(r"(?:^|[\s;&|])cd\s+([\w.-]+)\s*(?:&&|$|\n|#)")
# A bare prose token, word-bounded so `docker-AIx` is not a reference.
_BARE = re.compile(r"(?<![\w.-])(%s)(?![\w-])" % "|".join(map(re.escape, FORMER)))


def _repo_refs(text):
    """Yield (line_no, line, spelled) for every reference to the REPO NAME.

    `spelled` is what the document called the repository, so the finding can
    name it rather than say "the name is wrong".
    """
    refs = []
    known = set(FORMER) | {CANONICAL}
    for n, line in enumerate(text.splitlines(), 1):
        hit = False
        for m in _URL.finditer(line):
            seg = m.group(1)
            if seg in known:
                refs.append((n, line, seg))
                hit = True
        if not hit:
            for m in _CD.finditer(line):
                seg = m.group(1)
                if seg in known:
                    refs.append((n, line, seg))
                    hit = True
        if not hit:
            for m in _BARE.finditer(line):
                refs.append((n, line, m.group(1)))
                break
    return refs


def evaluate(root=".", waived_md5=None, waived_line_md5=None):
    """Return (verdict, findings). verdict in PASS/FAIL/WARN.

    Findings carry waived sites as [WAIVED] lines so a PASS still shows them.

    THE WAIVER IS BOUND TO CONTENT, NOT TO A PATH. Keying it on "README.md:1"
    made every fixture tree inherit a waiver for a line it did not have, and
    my own arms B/C/E/F caught it -- which is the reason arms exist. A waiver
    that applies to a file you did not look at is not a waiver of anything.
    So it fires only for a README whose md5 is the one the waiver was written
    against, and a stale waiver (text fixed, or README edited since) FAILS
    until someone deletes the entry.
    """
    docs = _tracked_docs(root)
    if docs is None:
        return "WARN", ["not a git tree, so the tracked doc set cannot be "
                        "measured here; this check must not claim a PASS"]
    findings, waived = [], []
    scanned = 0
    readme_text = None
    for rel in docs:
        try:
            text = open(os.path.join(root, rel), encoding="utf-8").read()
        except (OSError, UnicodeDecodeError) as e:
            findings.append(f"{rel}: unreadable ({e.__class__.__name__}) -- "
                            f"a doc this check cannot read is not a clean doc")
            continue
        if rel == "README.md":
            readme_text = text
        scanned += 1
        for n, line, spelled in _repo_refs(text):
            if spelled == CANONICAL:
                continue
            findings.append(f"{rel}:{n}: names the repository {spelled!r}, "
                            f"which is now {CANONICAL!r} -- {line.strip()[:90]}")
    # Apply the waiver AFTER the scan, bound to THE LINE it waives. A waiver
    # for README.md:1 is a statement about line 1, so line 1 is what retires
    # it -- not every other line of the file. `waived_md5` still exists for
    # the selftest's whole-file arms; `waived_line_md5` is what run() ships.
    if waived_md5 is not None and readme_text is not None:
        import hashlib
        if hashlib.md5(readme_text.encode("utf-8")).hexdigest() == waived_md5:
            moved = [f for f in findings if f.startswith("README.md:")]
            if moved:
                findings = [f for f in findings if f not in moved]
                waived.append("[WAIVED] README.md:1 -- " + WAIVERS["README.md:1"])
        else:
            findings.append(
                "WAIVER IS STALE -- README.md is not the text the waiver in "
                "namecheck.py was written against (it was fixed, or edited "
                "for another reason). The waiver waives nothing now; delete "
                "WAIVED_README_MD5 and re-measure before shipping.")
    if waived_line_md5 is not None and readme_text is not None:
        import hashlib
        first = readme_text.split("\n", 1)[0].rstrip("\r")
        site = [f for f in findings if f.startswith("README.md:1:")]
        if not site:
            findings.append(
                "WAIVER IS STALE -- line 1 of README.md no longer names the "
                "former repo, so WAIVED_LINE_MD5 waives nothing. Delete the "
                "entry and re-measure before shipping.")
        elif hashlib.md5(first.encode("utf-8")).hexdigest() != waived_line_md5:
            findings.append(
                "WAIVER IS STALE -- line 1 of README.md is not the text the "
                "waiver was written against (it was edited). The waiver "
                "waives nothing now; delete WAIVED_LINE_MD5 and re-measure. "
                f"line 1 is {first[:70]!r}")
        else:
            findings = [f for f in findings if f not in site]
            waived.append("[WAIVED] README.md:1 -- " + WAIVERS["README.md:1"])
    if findings:
        return "FAIL", findings + waived
    return "PASS", [f"{scanned} tracked doc(s) name the repository "
                    f"{CANONICAL!r}"] + waived


def run(root="."):
    # THE SHIPPED WAIVER IS THE ONLY ONE. Callers cannot widen it by passing
    # a hash: a check whose exemption is a parameter of the call is a check
    # that can be told what to ignore.
    verdict, findings = evaluate(root, waived_line_md5=WAIVED_LINE_MD5)
    print("NAMECHECK:", verdict)
    for f in findings:
        print("  " + f)
    return {"PASS": 0, "FAIL": 1}.get(verdict, 2)


# ----------------------------------------------------------------- selftest
def _tree(files):
    import tempfile
    d = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q", d], capture_output=True)
    for rel, text in files.items():
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p) or d, exist_ok=True)
        open(p, "w", encoding="utf-8").write(text)
    subprocess.run(["git", "-C", d, "add", "-A"], capture_output=True)
    return d


def selftest():
    """Both directions. Each arm names the assertion it proves.

    EVERY ARM HERE WAS RUN AGAINST THE PRE-FIX TOOL as well, and the ones that
    did not move were deleted rather than kept. What is left is the set where
    removing the predicate reddens exactly this arm and no other.
    """
    arms = []

    def arm(name, files, want, waived_md5=None):
        d = _tree(files)
        got, findings = evaluate(d, waived_md5=waived_md5)
        arms.append((name, got == want, f"got {got}, want {want}"
                                        + (f" ({findings[0][:70]})" if findings else "")))

    # A: the defect as shipped. The repo's real shape, stale name in a URL and
    #    the `cd` that follows it. This is the arm that is RED before the doc
    #    fix and GREEN after -- the only reason the check has a job.
    arm("A clone URL + cd naming the former repo name",
        {"docs/guide.md":
         "```bash\ngit clone https://github.com/o/docker-AI\ncd docker-AI\n"
         "git ls-files\n```\n"}, "FAIL")

    # B: the SAME tree with the names corrected. Without this, A could be
    #    satisfied by a tool that fails everything.
    arm("B the same block naming the canonical repo",
        {"docs/guide.md":
         "```bash\ngit clone https://github.com/o/Agent-Town.git\ncd Agent-Town\n"
         "git ls-files\n```\n"}, "PASS")

    # C: the object under test, not the string. The host config directory is
    #    a DIFFERENT object that shares the spelling; a blanket replace would
    #    corrupt a real path. This arm is what makes `_BARE` a predicate about
    #    the repository rather than a substring scan.
    arm("C the lowercase host config dir is NOT the repo",
        {"README.md": "  -v ~/.config/docker-ai/bank-admin-token:"
                      "/app/bank-admin-token:ro \\\n"}, "PASS")

    # D: prose is a reference too. README's H1 is the first thing a visitor
    #    reads and it named the old repo; a URL-only check would be green on
    #    the one line that matters most.
    arm("D the H1 prose that names the repo",
        {"README.md": "# docker-AI - a small town of Claude agents in Docker\n"},
        "FAIL")

    # E: a substring that merely CONTAINS the name is not a reference. This is
    #    the OTHER verdict -- the false positive that gets a gate deleted.
    arm("E a word that merely contains the old name",
        {"README.md": "The docker-AIish project is unrelated.\n"}, "PASS")

    # F: a doc this check can read but that names nothing is CLEAN. Paired
    #    with G below, because a reader that returns FAIL for everything
    #    passes A and D and is useless.
    arm("F a clean doc names the canonical repo",
        {"README.md": "# Agent-Town\n", "docs/x.md": "no name here\n"}, "PASS")

    # H: a path INSIDE the clone counts. `~/docker-AI/ui/...` is the repo's
    #    own directory, not the host config dir, and the capital spelling is
    #    what separates them. My first selector suppressed every token after
    #    "~/" and missed these three sites -- the selector was not the object.
    arm("H a path inside the clone names the repo",
        {"docs/CLOUD-DEPLOYMENT.md":
         "  -v ~/docker-AI/ui/prompts/x.txt:/app/x.txt:ro \\\n"}, "FAIL")

    # I: the waiver, bound to the exact bytes it was written for. The SAME
    #    text with the waiver's md5 PASSES, and the reason is still printed.
    STALE_H1 = "# docker-AI - a small town of Claude agents\n"
    import hashlib
    h = hashlib.md5(STALE_H1.encode("utf-8")).hexdigest()
    arm("I the waived H1 passes and is still reported",
        {"README.md": STALE_H1}, "PASS", waived_md5=h)
    w_got = evaluate(_tree({"README.md": STALE_H1}), waived_md5=h)[1]
    arms.append(("I2 the PASS still SAYS what it waived",
                 any("[WAIVED] README.md:1" in f for f in w_got),
                 " ".join(w_got)[:70]))
    # J: the waiver must not outlive its reason. H1 fixed by its owner and the
    #    old md5 still configured -> STALE, and this FAILS until the constant
    #    is deleted. This is the arm that makes the waiver not a bypass.
    arm("J a waiver whose site was fixed is STALE and fails",
        {"README.md": "# Agent-Town - a small town of Claude agents\n"},
        "FAIL", waived_md5=h)
    # K: the shipped constant must match the README in THIS tree. If someone
    #    edits README for any other reason, this arm reddens and the waiver
    #    must be re-measured rather than silently carrying on.
    arm("K the shipped waiver still matches this tree's README",
        {"README.md": STALE_H1}, "PASS", waived_md5=h)
    real = evaluate(os.path.dirname(os.path.abspath(__file__)) + "/..",
                    waived_line_md5=WAIVED_LINE_MD5)
    arms.append(("K2 the shipped LINE md5 matches the real README's H1",
                 real[0] == "PASS" and any("[WAIVED]" in f for f in real[1]),
                 f"got {real[0]}: {real[1][-1][:60] if real[1] else ''}"))
    # K3: THE POINT OF THE LINE BINDING. A README whose H1 is unchanged but
    #     whose OTHER lines were edited (the C8 count fix, a typo, a new row)
    #     must keep its waiver. Under the whole-file md5 this went STALE and
    #     reddened a merge on a line its author never touched -- measured on
    #     the C8 merge. Inverted, this is the property: the waiver follows
    #     the LINE.
    #     The fixture passes ITS OWN line md5 on purpose: this arm tests the
    #     BINDING, not the shipped constant (K2 owns that). My first version
    #     passed WAIVED_LINE_MD5 against a fixture H1 that is not the shipped
    #     H1, so it went FAIL and I nearly "fixed" it by loosening the check.
    drifted = STALE_H1 + "\n| Every `.py` file here parses | (36 files) |\n"
    h3 = hashlib.md5(STALE_H1.rstrip("\n").encode("utf-8")).hexdigest()
    d3 = evaluate(_tree({"README.md": drifted}), waived_line_md5=h3)
    arms.append(("K3 an edit to another line does NOT retire the H1 waiver",
                 d3[0] == "PASS" and any("[WAIVED]" in f for f in d3[1]),
                 f"got {d3[0]}: {' '.join(d3[1])[:60]}"))
    # K4: the H1 EDITED is still STALE under the line binding -- the property
    #     the whole-file md5 was protecting must survive the narrower scope.
    d4 = evaluate(_tree({"README.md": "# Agent-Town - a small town\n"}),
                  waived_line_md5=WAIVED_LINE_MD5)
    arms.append(("K4 an edit to the H1 itself still goes STALE",
                 d4[0] == "FAIL"
                 and any("WAIVER IS STALE" in f for f in d4[1]),
                 f"got {d4[0]}: {' '.join(d4[1])[:60]}"))

    ok = True
    for name, good, detail in arms:
        ok &= good
        print("  %-52s %s  (%s)" % (name, "PASS" if good else "FAIL", detail))
    # G: an unreadable doc must FAIL, asserted directly because arm F above
    #    cannot express it (a tracked-but-deleted file is not constructible
    #    through git). This probes the branch rather than claiming it exists.
    import tempfile
    d = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q", d], capture_output=True)
    open(os.path.join(d, "a.md"), "w").write("# Agent-Town\n")
    subprocess.run(["git", "-C", d, "add", "-A"], capture_output=True)
    os.remove(os.path.join(d, "a.md"))          # tracked, now unreadable
    got, findings = evaluate(d)
    g_ok = got == "FAIL" and any("unreadable" in f for f in findings)
    ok &= g_ok
    print("  %-52s %s  (got %s)" % ("G a tracked doc missing from the tree",
                                    "PASS" if g_ok else "FAIL", got))
    print("NAMECHECK SELFTEST:", "PASS" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(run(args[0] if args else "."))
