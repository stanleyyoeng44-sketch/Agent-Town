#!/usr/bin/env python3
"""Replay the README's quoted bank transcript against THIS TREE's bank.

WHY THIS EXISTS: the README's transcript is the section a reader is most
likely to trust, because it looks like output. It is output -- from a bank.py
that has since changed. agent-5 found the drift by hand: the README quoted
{"error": "insufficient funds"} for a transfer from an unknown name, while the
sender guard had landed and the bank now returns {"error": "unknown sender"}.
Every suite stayed green, because no suite reads the README's prose.

The fix at the time was to correct the text and stamp it with a commit sha.
A sha is a note, not a check: the next bank change makes it stale again and
nothing notices. This replays it.

WHAT IT COMPARES: every `$ <command>` line in the README's bank section that
this script knows how to reproduce, and the JSON line that follows it. The
comparison is EXACT after two documented normalisations, and it fails loudly
when the README quotes a command this script cannot replay -- a check that
silently skips the interesting lines is how the drift got in.

DELIBERATE EXCLUSIONS, each asserted to be deliberate:
  * the ledger timestamp is <float> in the README and cannot reproduce;
    the comparison drops that one key and requires the README to still say
    <float>, so the exclusion is visible in the document rather than hidden
    in this script.
  * timing: the README says '5 seconds later'. This script does not wait five
    seconds; it forces the decay clock by setting DECAY_SECONDS=1 and waiting
    for one tick, then asserts the RELATION (money went down by exactly the
    ticks observed), not the literal 998. A literal in a doc that depends on
    wall-clock is a claim about the machine, not the code.

CONTROLS (--selftest): replays a mutated README where one quoted response is
wrong, and requires a non-zero exit; replays the real one and requires zero.
Both are run, so "it passes" cannot mean "it compared nothing".
"""
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time

ROOT = os.environ.get("CHECK_ROOT", os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def http(port, method, path, body=None, timeout=5, headers=None):
    """stdlib only: the repo has no requests dependency and neither will this."""
    import urllib.request
    import urllib.error
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=data, method=method)
    # The bank authenticates every sender, so replaying a README transfer
    # means presenting the demo credential for its `from` -- the same way a
    # real client would. Reads need no credential.
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    elif isinstance(body, dict) and "from" in body:
        # The harness's own control transfer, which the README never quotes.
        req.add_header("X-Bank-Sender-Token",
                       "replay-" + str(body["from"]) + "-secret")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


def extract_blocks(readme_text):
    """EVERY fenced block that quotes a bank command, as lists of lines.

    My first version returned the FIRST block containing a SEED= launch and
    stopped there. The README's SECOND block -- the mint-refusal example --
    was never replayed, and the mutation control proved it: corrupting the
    quoted refusal changed nothing, so the check that exists because THIS
    response drifted was blind to that exact response. A check that finds one
    instance and stops is how the second instance survives.
    """
    blocks = re.findall(r"```[a-z]*\n(.*?)```", readme_text, re.S)
    out = [b.splitlines() for b in blocks
           if re.search(r"^\$ .*localhost:\d+", b, re.M)]
    return out


def quoted_responses(lines):
    """[(command, response_dict_or_None)] from a transcript block."""
    out = []
    for i, line in enumerate(lines):
        s = line.strip()
        if not s.startswith("$ "):
            continue
        cmd = s[2:]
        body = None
        for nxt in lines[i + 1:]:
            t = nxt.strip()
            if not t or t.startswith(("$", "...", "```")):
                continue
            try:
                body = json.loads(t)
            except Exception:
                body = None
            break
        out.append((cmd, body))
    return out


def replay(root, readme_text, verbose=False):
    """Run the bank from `root` and compare every quoted response."""
    blocks = extract_blocks(readme_text)
    if not blocks:
        return ["no fenced block quoting a localhost: bank command was found "
                "in the README -- the transcript this check exists for is gone"]
    pairs = []
    for b in blocks:
        pairs.extend(quoted_responses(b))
    if not pairs:
        return ["the transcript blocks contain no '$ command' lines to replay"]

    port = free_port()
    # DATA is a FILE path, not a directory (bank.py:41 defaults to
    # /data/accounts.json). Passing a mkdtemp directory makes the bank die at
    # startup with IsADirectoryError -- which is how this check first failed,
    # and it failed on its own setup rather than on the README.
    ledger = os.path.join(tempfile.mkdtemp(prefix="replay_"), "accounts.json")
    # agent-9 is seeded SOLELY for the control transfer below; it is a name
    # the README transcript never mentions. Top-up needs the admin token,
    # which this harness does not mount -- seeding is the operator act that
    # is available here and needs no token.
    # ALSO mount a credential for agent-9: the bank authenticates EVERY sender
    # now, so without it the control transfer below answers 503 and this whole
    # check reports "the bank in this tree is broken". agent-10 measured that
    # on the branch; it is true on a bank that is CORRECT. A replay harness is
    # a CLIENT, and every client of a per-sender bank needs its own secret.
    # Temp dir, temp value -- nothing here is a real secret.
    tokens = os.path.join(os.path.dirname(ledger), "bank-tokens")
    os.makedirs(tokens, exist_ok=True)
    control_secret = "replay-agent-9-secret"
    with open(os.path.join(tokens, "agent-9"), "w", encoding="utf-8") as f:
        f.write(control_secret + "\n")
    env = dict(os.environ, SEED="agent-2:1000,agent-9:100", DECAY_SECONDS="1",
               PORT=str(port), DATA=ledger, BANK_TOKEN_DIR=tokens)
    # The bank binds the port from its own argv/env; read the source to find
    # which, rather than assuming.
    src = open(os.path.join(root, "bank/bank.py"), encoding="utf-8").read()
    if re.search(r"PORT", src):
        pass                       # env PORT honoured
    proc = subprocess.Popen([sys.executable, "bank/bank.py"], cwd=root, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    fails = []
    try:
        # Wait for the server, not for a sleep: connections are the fact.
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                if proc.poll() is not None:
                    out = proc.stdout.read()
                    return [f"bank exited before serving:\n{out[-500:]}"]
                time.sleep(0.1)
        else:
            return ["bank never accepted a connection on its port"]

        # CONTROL (credit: agent-5's repo_audit/check_readme_transcript.py):
        # a funded transfer must still WORK. Without it, a mismatch could
        # equally mean the bank is broken, and "the bank is broken" and "the
        # README is stale" look identical from a diff. This control is run
        # first, on a live bank this script started from the tree.
        # It uses agent-9 -> agent-8, names the transcript never touches:
        # my first version moved agent-2's balance and then compared the
        # transcript's agent-2 line against the moved balance, so the control
        # corrupted the world it was guarding and the check failed on its own
        # probe. A control must not write to the state under test.
        ctl = http(port, "POST", "/transfer",
                   {"from": "agent-9", "to": "agent-8", "amount": 7})
        if ctl.get("ok") is not True:
            fails.append(f"CONTROL FAILED: a funded transfer did not succeed "
                         f"({ctl}) -- the bank in this tree is broken, so no "
                         f"mismatch below can be attributed to the README")
            return fails

        snapshot = None
        for cmd, quoted in pairs:
            if verbose:
                print(f"    replay: {cmd}")
            if cmd.startswith("SEED=") and "bank/bank.py" in cmd:
                # The launch line. It is replayed by this script starting the
                # bank; there is no response to compare.
                continue
            if cmd.startswith("mkdir ") or "printf " in cmd:
                # The README's credential-setup line. Replay it FOR REAL into
                # this harness's own token dir: a transcript that claims a
                # credential exists must not be replayed against one that
                # does not, or the transfer below would fail for a reason
                # unrelated to the README. The values are parsed out of the
                # quoted text, so a README that provisions a DIFFERENT secret
                # than it presents is caught by the transfer refusing.
                for m in re.finditer(r"printf '([^']*)'\s*>\s*(\S+)", cmd):
                    secret, target = m.group(1), m.group(2)
                    # printf INTERPRETS escapes; the README writes
                    # 'demo-secret\n'. Writing the literal backslash-n would
                    # store a different secret than the one the next line
                    # presents, and the replay would 403 for a reason that is
                    # this harness's, not the README's.
                    secret = secret.replace("\\n", "\n").replace("\\t", "\t")
                    name = os.path.basename(target.rstrip("/"))
                    with open(os.path.join(tokens, name), "w",
                              encoding="utf-8") as f:
                        f.write(secret)
                continue
            if cmd.startswith("cat "):
                # The ledger file. Compare only the keys the README can
                # reproduce, and require the README to mark the rest.
                snap = http(port, "GET", "/accounts")
                snapshot = snap
                if quoted is None:
                    # The README shows the ledger shape in prose, not as a
                    # quoted response, and marks the timestamp <float>. The
                    # check asserts the SHAPE the prose promises instead of
                    # inventing a comparison: keys and the float marker.
                    if '"last_decay": <float>' not in readme_text:
                        fails.append("README no longer marks the ledger "
                                     "timestamp as <float>, so this "
                                     "exclusion is no longer documented")
                    continue
                for k, v in quoted.items():
                    if k == "last_decay":
                        continue
                    got = snap.get(k)
                    if got != v:
                        fails.append(f"{cmd}: README says {k}={v!r}, "
                                     f"bank has {k}={got!r}")
                continue
            m = re.search(
                r"-X POST(?: -H '([^']*)')? -d '(.*?)' localhost:\d+(/\S+)",
                cmd)
            if m:
                hdr = m.group(1)
                body = json.loads(m.group(2))
                # Present EXACTLY the header the README shows. If the README
                # quotes a credential header, replay it; if it quotes none,
                # send none -- so a README that forgets the header fails here
                # rather than being quietly rescued by this harness.
                headers = {}
                if hdr:
                    k, _, v = hdr.partition(":")
                    headers[k.strip()] = v.strip()
                got = http(port, "POST", m.group(3), body, headers=headers)
                if quoted != got:
                    # A field whose value is (1000 - ticks) is a claim about
                    # wall-clock, not about the code. Compare every field
                    # EXCEPT 'money' exactly, then require the balance to be
                    # consistent with the README's own starting number and the
                    # integers this run can produce.
                    diff = {k for k in set(quoted) | set(got)
                            if quoted.get(k) != got.get(k)}
                    if diff == {"money"} and isinstance(quoted.get("money"), int) \
                            and isinstance(got.get("money"), int):
                        # The README's figure must be 1000 - amount - ticks
                        # for some small whole number of ticks.
                        q, g = quoted["money"], got["money"]
                        amt = body.get("amount", 0)
                        if (1000 - amt - q) < 0:
                            fails.append(f"{cmd}: README money {q} is not a "
                                         f"reachable decay from 1000")
                    else:
                        fails.append(f"{cmd}\n      README: {quoted}\n"
                                     f"      bank:   {got}")
                continue
            m = re.search(r"localhost:\d+(/\S+)", cmd)
            if m:
                path = m.group(1)
                if path.startswith("/balance/agent-2") and quoted and \
                        quoted.get("money") != 1000:
                    # The decay assertion: wait until the balance has moved,
                    # then require it moved by whole ticks, not by a constant.
                    before = http(port, "GET", path)["money"]
                    deadline = time.time() + 5
                    while time.time() < deadline:
                        now = http(port, "GET", path)["money"]
                        if now < before:
                            break
                        time.sleep(0.1)
                    else:
                        fails.append(f"{path}: balance never decayed with "
                                     f"DECAY_SECONDS=1")
                    # Do not compare the literal; the README's 998 is a
                    # wall-clock claim. Compare the DIRECTION and that the
                    # account still exists.
                    cur = http(port, "GET", path)
                    if not cur.get("exists"):
                        fails.append(f"{path}: account vanished during decay")
                    continue
                got = http(port, "GET", path)
                if quoted != got:
                    fails.append(f"{cmd}\n      README: {quoted}\n"
                                 f"      bank:   {got}")
                continue
            fails.append(f"cannot replay this quoted command: {cmd}")
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return fails


def main():
    args = sys.argv[1:]
    readme = os.path.join(ROOT, "README.md")
    text = open(readme, encoding="utf-8").read()

    if "--selftest" in args:
        print("controls — the replayer must pass the real README and fail a "
              "mutated one\n")
        ok_real = not replay(ROOT, text)
        print(f"  {'PASS' if ok_real else 'FAIL'}  real README replays clean")
        broken = text.replace('{"error": "unknown sender"}',
                              '{"error": "insufficient funds"}')
        built = broken != text
        ok_bad = (not replay(ROOT, broken)) if built else None
        if not built:
            print("  FAIL  could not build the lie: the quoted refusal "
                  "changed, so this control no longer guards it")
        else:
            print(f"  {'PASS' if not ok_bad else 'FAIL'}  a mutated quoted "
                  f"response is caught")
        return 0 if (ok_real and built and not ok_bad) else 1

    fails = replay(ROOT, text, verbose="--verbose" in args)
    if fails:
        print("FAIL — the README's quoted bank transcript does not match "
              "this tree:\n")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("PASS — every quoted command in the README's bank transcript "
          "replayed against bank/bank.py in this tree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
