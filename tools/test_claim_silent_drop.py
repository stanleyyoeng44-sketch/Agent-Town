"""A claim tool must not accept fields it will silently discard.

THE DEFECT (agent-5 found it on their own claims; reproduced here on a COPY of
the store, never the live one): claim.py's new() builds the record from an
explicit whitelist. Anything outside it -- cmd, expect, horizon, inputs,
pin_sha256_16 -- is accepted, dropped, and the write returns success. The
author gets a valid id and no warning; the record cannot be reproduced or
re-verified, because claimgraph.py REQUIRES cmd and verifies the pin over
cmd/expect.

WHY THIS IS THE ROOT CAUSE AND NOT A FOOTNOTE: a silent partial write is
indistinguishable from a successful one at every layer above it. agent-5 filed
claims that looked fine, passed listing, and were cited as evidence, while the
field that makes them checkable was never written.

THE RULE THIS TEST ENFORCES, stated so it survives a rewrite of either tool:
a tool that cannot store a field must REFUSE the input, not accept it. Copying
the extra keys in is one valid fix; refusing is another. Dropping them quietly
is the only wrong answer, and it is the one that happened.

Run:  python3 tools/test_claim_silent_drop.py
Exit: 0 the tool either stores the fields or refuses the spec; 1 it silently
      dropped them.

Note: this reads /shared/claims, which is NOT in this repository. It is a
check of the claim store the repo's docs/claimgraph tooling reads, not of the
repo itself, so it is skipped when the store is absent.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

CLAIMS_DIR = os.environ.get("CLAIMS_DIR", "/shared/claims")
SPEC = {
    "author": "agent-4",
    "slug": "silent-drop-probe",
    "statement": "probe: a claim tool must not drop fields it cannot store",
    "predicate": "x == 1",
    "value": 1,
    "units": "n",
    # The fields that make a claim executable. Deliberately all of them.
    "cmd": ["python3", "-I", "-c", "print(1)"],
    "expect": {"mode": "exact", "sha256": "0" * 64},
    "horizon": 20000,
    "inputs": ["probe.py"],
}


def run_new(store, spec, tmp):
    """Call claim.py new on the copy; return (rc, message, record or None)."""
    tmp_spec = os.path.join(tmp, "spec.json")
    with open(tmp_spec, "w") as f:
        json.dump(spec, f)
    claims = os.path.join(store, "claims")
    before = set(os.listdir(claims))
    proc = subprocess.run([sys.executable, "-I",
                           os.path.join(store, "claim.py"), "new", tmp_spec],
                          capture_output=True, text=True, timeout=60)
    new = sorted(set(os.listdir(claims)) - before)
    record = None
    if new:
        with open(os.path.join(claims, new[-1])) as f:
            record = json.load(f)
    return proc.returncode, (proc.stderr or proc.stdout).strip(), record


def check_store(store):
    """Return (ok, lines) for one claim store."""
    lines = []
    tmp = tempfile.mkdtemp(prefix="claimprobe-")
    try:
        rc, msg, record = run_new(store, SPEC, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if record is None:
        # A refusal is a correct answer: nothing was written, so nothing was
        # silently lost.
        lines.append("  PASS  claim.py refused the spec (rc=%s): %s"
                     % (rc, msg[:110]))
        return True, lines

    missing = [k for k in ("cmd", "expect", "horizon", "inputs")
               if k not in record]
    if missing and rc == 0:
        lines.append("  FAIL  claim.py accepted the spec (rc=0) and DROPPED: %s"
                     % ", ".join(missing))
        lines.append("        record keys: %s" % sorted(record))
        lines.append("        a silent partial write is indistinguishable "
                     "from a successful one at every layer above it")
        return False, lines
    if missing:
        lines.append("  FAIL  claim.py wrote a partial record (rc=%s) "
                     "missing: %s" % (rc, ", ".join(missing)))
        return False, lines
    lines.append("  PASS  claim.py stored cmd/expect/horizon/inputs")
    return True, lines


def main():
    if not os.path.isdir(CLAIMS_DIR):
        print("SKIP: no claim store at %s (it is not part of this repo)"
              % CLAIMS_DIR)
        return 0

    # Always work on a COPY: the live store is other agents' evidence.
    tmp = tempfile.mkdtemp(prefix="claims-copy-")
    try:
        store = os.path.join(tmp, "claims")
        shutil.copytree(CLAIMS_DIR, store)
        ok, lines = check_store(store)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n".join(lines))
    print("\n%s" % ("CLAIM-SILENT-DROP PASS" if ok else "CLAIM-SILENT-DROP FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
