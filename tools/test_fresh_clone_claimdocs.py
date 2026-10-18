"""A fresh clone must be able to run the docs check, and must still be able to
FAIL it.

THE DEFECT (mine, found by running the thing a reader runs): from a clone of
this repository, `python3 docs/claimgraph/build.py selftest` died on the first
`shutil.copytree` with a FileNotFoundError, because `/shared/claims` is a
runtime artifact of this cohort's box and is NOT in the repo. A traceback and
rc=1 is the worst of both worlds: it looks like a broken generator, it is only
seen by the person who cloned rather than lived in the tree, and it teaches the
reader to skip the docs check.

THE RULE, and it is the one the rest of the cohort settled tonight: an absent
input is a NAMED state, never a pass and never a crash. The clone can check the
subset that needs only shipped files -- the committed graph.json parses, and
the committed README table equals it -- and must say plainly that CURRENCY is
UNMEASURED, because currency needs the live store.

AND THE PART I GOT WRONG FIRST: my own patch printed
    selftest: FAILED
and exited 0. A verdict that says FAIL while the process certifies success is
exactly the defect class this cohort spent the day on -- the receipt and the
result had come apart. The control below catches that too: it asserts on the
EXIT CODE, not on the printed word.

Run:  python3 tools/test_fresh_clone_claimdocs.py
Exit: 0 both directions behave; 1 either does not.
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(HERE, "docs", "claimgraph")


def make_clone_tree(tmp):
    """A clone-shaped tree: the docs tooling plus the README it renders into,
    and NO claim store anywhere near it."""
    dst = os.path.join(tmp, "docs", "claimgraph")
    os.makedirs(os.path.dirname(dst))
    shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(os.path.join(HERE, "README.md"), os.path.join(tmp, "README.md"))
    return os.path.join(dst, "build.py")


def run(build, tmp):
    absent = os.path.join(tmp, "no-such-store")
    env = dict(os.environ, CLAIM_ROOT=absent)
    p = subprocess.run([sys.executable, "-I", build, "selftest"],
                       capture_output=True, text=True, env=env, timeout=300)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main():
    tmp = tempfile.mkdtemp(prefix="fresh-clone-")
    lines, ok = [], True
    try:
        build = make_clone_tree(tmp)

        # A. The clone state: no store. Must NAME the absence and pass.
        rc, out = run(build, tmp)
        a_ok = (rc == 0 and "SKIP" in out and "NOT EVALUATED" in out)
        ok = ok and a_ok
        lines.append("  %-5s A no store: rc=0 and the absence is NAMED"
                     % ("PASS" if a_ok else "FAIL"))
        if not a_ok:
            lines.append("        rc=%s  out=%r" % (rc, out[-400:]))

        # B. The clone state must still be able to FAIL: corrupt the shipped
        #    README table so it disagrees with the shipped graph.json. This is
        #    the check the clone CAN do, so it must bite.
        readme = os.path.join(tmp, "README.md")
        with open(readme, encoding="utf-8") as f:
            before = f.read()
        with open(os.path.join(tmp, "docs", "claimgraph", "README.md"),
                  encoding="utf-8") as f:
            shipped = f.read()
        # build.py reads the README beside itself (HERE), not the repo root.
        target = os.path.join(tmp, "docs", "claimgraph", "README.md")
        mutated = None
        import re
        m = re.search(r"\|\s*claims\s*\|\s*(\d+)\s*\|", shipped)
        if m:
            mutated = shipped[:m.start(1)] + "999" + shipped[m.end(1):]
            with open(target, "w", encoding="utf-8") as f:
                f.write(mutated)
            rc, out = run(build, tmp)
            # ASSERT ON WHAT FAILED, not merely THAT something did. `rc != 0`
            # alone is satisfied by the crash this test exists to remove, so B
            # would pass vacuously on the unfixed tool. The row must be the
            # table check, and there must be no traceback. (Same trap I keep
            # walking into: a case that behaves the same before and after the
            # fix it names.)
            row = [l for l in out.splitlines()
                   if "M3 committed README table" in l and "FAIL" in l]
            b_ok = (rc != 0 and bool(row) and "Traceback" not in out)
            ok = ok and b_ok
            lines.append("  %-5s B corrupted table: rc!=0 (the clone check bites)"
                         % ("PASS" if b_ok else "FAIL"))
            if not b_ok:
                lines.append("        rc=%s  out=%r" % (rc, out[-400:]))
            with open(target, "w", encoding="utf-8") as f:
                f.write(shipped)
        else:
            ok = False
            lines.append("  FAIL  B could not find a claims row in the "
                         "generated table to corrupt -- the control did not "
                         "reach its trigger (NOT a pass)")
        lines.append("        (readme unchanged check: %s)"
                     % ("same" if open(readme, encoding="utf-8").read() == before
                        else "MUTATED -- the probe wrote outside its tree"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n".join(lines))
    print("\nFRESH-CLONE-CLAIMDOCS %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
