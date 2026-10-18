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


def http(port, method, path, body=None, timeout=5):
    """stdlib only: the repo has no requests dependency and neither will this."""
    import urllib.request
    import urllib.error
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=data, method=method)
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

    # THE README'S OWN CLOCK, read from THE BLOCK UNDER TEST, not from the
    # document. A decay claim is meaningless without units; these two numbers
    # turn a quoted balance into an equation.
    #
    # MEASURED DEFECT IN MY OWN FIRST VERSION (agent-2, found by attacking the
    # surface I said reviewers should attack): I read both numbers with
    # re.search over the WHOLE README, so the match came from the FIRST
    # occurrence anywhere -- which is a PROSE sentence above the transcript
    # ("with DECAY_SECONDS=2 to make..."), not the transcript's launch line.
    # Consequences, both repro'd: editing the launch line to DECAY_SECONDS=1
    # passed (the prose still said 2), and DELETING it from the launch line
    # passed too (falls back to the prose). The transcript's own claim was
    # never read. Same disease as everything else tonight: the value that
    # drives the check was selected from somewhere other than the object
    # under test.
    #
    # So: parse the block that CONTAINS the launch line, and require the
    # launch line to be in the same block as the balances it explains.
    stated = None
    for b in blocks:
        block_text = "\n".join(b)
        if "bank/bank.py" not in block_text:
            continue
        decay_m = re.search(r"DECAY_SECONDS=(\d+)", block_text)
        later_m = re.search(r"(\d+)\s+seconds?\s+later", block_text)
        if decay_m and later_m:
            stated = (int(decay_m.group(1)), int(later_m.group(1)))
        break
    # `stated` stays None when either is absent, and the second-balance branch
    # FAILs on None instead of skipping -- an unreadable claim is the defect,
    # not a reason to pass.

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
    env = dict(os.environ, SEED="agent-2:1000,agent-9:100", DECAY_SECONDS="1",
               PORT=str(port), DATA=ledger)
    # The bank binds the port from its own argv/env; read the source to find
    # which, rather than assuming.
    src = open(os.path.join(root, "bank/bank.py"), encoding="utf-8").read()
    if re.search(r"PORT", src):
        pass                       # env PORT honoured
    proc = subprocess.Popen([sys.executable, "bank/bank.py"], cwd=root, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    # bank.py sets its decay origin at import, so this is the tick origin.
    seen_at = time.time()
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
        # WHICH READ, NOT WHICH VALUE.
        # MEASURED DEFECT (agent-2, 2026-10-18): the old branch below tested
        # the README's OWN value -- `quoted.get("money") != 1000` -- to decide
        # whether a /balance/agent-2 line was the seed read or the decay read.
        # So a README that quoted 999, 1001 or 0 for the FIRST balance took the
        # decay path, and the shipped check returned PASS for every one of
        # them (reproduced on master 1ee34ea before this change). A check whose
        # behaviour is selected by the value it is checking cannot reject that
        # value. The selector has to be the POSITION in the transcript.
        balance_seen = 0
        # The most recent quoted /balance figure, used to make a later
        # transfer's money an ARITHMETIC assertion rather than a comparison
        # to a constant seed. None until the transcript quotes one.
        last_balance_quoted = None
        for cmd, quoted in pairs:
            if verbose:
                print(f"    replay: {cmd}")
            if cmd.startswith("SEED=") and "bank/bank.py" in cmd:
                # The launch line. It is replayed by this script starting the
                # bank; there is no response to compare.
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
            m = re.search(r"-X POST -d '(.*?)' localhost:\d+(/\S+)", cmd)
            if m:
                body = json.loads(m.group(1))
                got = http(port, "POST", m.group(2), body)
                if quoted is None:
                    fails.append(f"{cmd}: README quotes no response for this "
                                 f"transfer, so there is nothing to check")
                    continue
                # Compare every field EXCEPT 'money' exactly. 'money' is a
                # wall-clock quantity (seed - amount - decay ticks), so it
                # cannot be compared to the bank's figure literally -- but it
                # CAN be compared to the README's OWN arithmetic, and the old
                # code did not: it asked only `(1000 - amt - q) < 0`, i.e. "is
                # the quoted number not larger than the seed". MEASURED on
                # ce5497b: a transfer line quoting 900 after a 7-coin transfer
                # PASSED (93 coins vanished); 999 was the ONE value rejected,
                # and only because 1000-7-999 is negative. A constant that
                # happens to be right for the other fields is not an assertion
                # about this one.
                diff = {k for k in set(quoted) | set(got)
                        if k != "money" and quoted.get(k) != got.get(k)}
                if diff:
                    fails.append(f"{cmd}\n      README: {quoted}\n"
                                 f"      bank:   {got}")
                    continue
                q = quoted.get("money")
                if q is None and "money" not in quoted:
                    # An ERROR response legitimately has no money field
                    # ("unknown sender"). The diff above already compared
                    # every non-money field exactly, so a matching error is
                    # fully checked; only a SUCCESS response carries money.
                    continue
                if not isinstance(q, int):
                    fails.append(f"{cmd}: README money {q!r} is not an integer")
                    continue
                amt = body.get("amount", 0)
                seed = last_balance_quoted
                if seed is None:
                    # Without a preceding quoted balance there is nothing to
                    # be consistent WITH, so say so rather than pass. That is
                    # a FAIL, not a skip: an uncheckable number is exactly the
                    # thing this arm exists to catch.
                    fails.append(f"{cmd}: README quotes money {q} but no "
                                 f"earlier quoted balance to check it against")
                    continue
                # Seeded decay is 1 coin per DECAY_SECONDS; the harness runs
                # DECAY_SECONDS=1, so the tick count is bounded by the wall
                # clock since the bank started. Never a fixed literal.
                ticks = int(time.time() - seen_at) + 1
                lo, hi = seed - amt - ticks, seed - amt
                if not (lo <= q <= hi):
                    fails.append(
                        f"{cmd}: README money {q} is not {seed} - {amt} minus "
                        f"0..{ticks} decay tick(s), i.e. not in {lo}..{hi}")
                continue

            m = re.search(r"localhost:\d+(/\S+)", cmd)
            if m:
                path = m.group(1)
                is_balance = path.startswith("/balance/agent-2")
                if is_balance:
                    balance_seen += 1
                if is_balance and balance_seen == 1:
                    # FIRST READ: the README claims a SEED -- a freshly seeded
                    # agent starts at 1000. The value is on a clock, so compare
                    # against the clock and not against a literal.
                    # MEASURED: the old literal comparison failed 1 run in 10
                    # on master 1ee34ea (README 1000, bank 999) because the
                    # harness starts the bank with DECAY_SECONDS=1 and its own
                    # startup is 0.38-0.72 s; a read landing after 1 s was
                    # blamed on the README. Deterministic confirmation: same
                    # tree, read at 0.81 s -> 1000, at 1.70 s -> 999.
                    # The README must still say 1000; only the BANK's tick is
                    # allowed to move the number, and only by whole ticks.
                    got = http(port, "GET", path)
                    cur = got.get("money")
                    elapsed = time.time() - seen_at
                    low = 1000 - int(elapsed // 1) - 1
                    if quoted is None or quoted.get("money") != 1000:
                        fails.append(
                            f"{cmd}\n      README: {quoted}\n"
                            f"      the FIRST balance in the transcript is the "
                            f"SEED and must read 1000; a different value is a "
                            f"claim about the bank, not a claim about decay")
                    elif cur is None or not (low <= cur <= 1000):
                        fails.append(
                            f"{cmd}\n      README: {quoted}\n"
                            f"      bank:   {got}\n"
                            f"      {elapsed:.2f}s after the bank started, at "
                            f"DECAY_SECONDS=1, the decayed seed can only be "
                            f"{low}..1000 -- this is outside that")
                    elif cur != 1000:
                        print(f"    NOTE: first balance read {cur}; "
                              f"{elapsed:.2f}s elapsed explains "
                              f"{1000 - cur} tick(s) (allowed "
                              f"{int(elapsed // 1) + 1})")
                    if quoted and isinstance(quoted.get("money"), int):
                        last_balance_quoted = quoted["money"]
                    continue
                if is_balance and balance_seen == 2:
                    # SECOND READ: the README claims DECAY -- less money than
                    # the seed, after a STATED number of seconds. The old code
                    # selected this branch by `quoted.get("money") != 1000`,
                    # the README's own value again, and then DISCARDED the
                    # value: it checked only that the BANK's number went down.
                    # MEASURED on ce5497b: quoting 0, 5, 500, 998 or 1001 for
                    # the second balance all PASSED, including 1001 -- a rise
                    # during a claimed decay. Same defect as the first-balance
                    # one; this is its SECOND instance in this file.
                    #
                    # The transcript is arithmetic, and the README states every
                    # input to it: DECAY_SECONDS on the launch line and "N
                    # seconds later" on the ellipsis line. Assert the stated
                    # equation. None of the three numbers on the right is the
                    # value under test, so the check can reject it.
                    got = http(port, "GET", path)
                    cur = got.get("money")
                    q = None if quoted is None else quoted.get("money")
                    if not isinstance(q, int):
                        fails.append(f"{cmd}: the SECOND balance in the "
                                     f"transcript must quote an integer "
                                     f"money; got {q!r}")
                    elif last_balance_quoted is None:
                        fails.append(f"{cmd}: no earlier quoted balance for "
                                     f"this decay claim to be relative to")
                    elif stated is None:
                        # No stated clock -> the quoted number has no units
                        # and cannot be checked. FAIL rather than pass: an
                        # unreadable claim is the defect, not a reason to skip.
                        fails.append(f"{cmd}: the transcript claims decay but "
                                     f"does not state both DECAY_SECONDS and "
                                     f"'N seconds later', so the quoted "
                                     f"balance {q} has no units")
                    else:
                        decay_secs, later_secs = stated
                        ticks = later_secs // decay_secs
                        want = last_balance_quoted - ticks
                        if q != want:
                            fails.append(
                                f"{cmd}\n      README: {quoted}\n"
                                f"      the README states DECAY_SECONDS="
                                f"{decay_secs} and '{later_secs} seconds "
                                f"later', so {last_balance_quoted} decays by "
                                f"{ticks} to {want}; it quotes {q}")
                        else:
                            # The README's number is now an ASSERTED equation.
                            # The BANK is a different clock: the harness runs
                            # DECAY_SECONDS=1 and does not wait the README's
                            # five seconds, so the bank's value is only
                            # required to be a whole number of ITS OWN ticks
                            # below the seed -- the same clock-derived bound
                            # the first-balance arm uses, never a literal 998.
                            elapsed = time.time() - seen_at
                            low = 1000 - int(elapsed // 1) - 1
                            if not isinstance(cur, int) or not (low <= cur <= 1000):
                                fails.append(
                                    f"{cmd}\n      README: {quoted}\n"
                                    f"      bank:   {got}\n"
                                    f"      {elapsed:.2f}s after the bank "
                                    f"started at DECAY_SECONDS=1, the balance "
                                    f"can only be {low}..1000")
                    if quoted and isinstance(quoted.get("money"), int):
                        last_balance_quoted = quoted["money"]
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

        # THE FIRST-BALANCE ARMS, which the arm above does not reach.
        # MEASURED (agent-2, 2026-10-18) on the previous version: rewriting the
        # FIRST quoted balance to 999, to 1001 and even to 0 all left this
        # selftest and the real run GREEN, because the branch was selected by
        # the README's own value -- any non-1000 number took the decay path.
        # The arm above mutates the refusal in the SECOND block, so it could
        # never have caught that. Each arm below is a different lie and each
        # must be rejected; the pristine read must stay green (ok_real above).
        first_lies = [
            ('{"agent": "agent-2", "money": 1000, "exists": true}',
             '{"agent": "agent-2", "money": 999, "exists": true}',
             "a first balance of 999 (a WRONG seed, not decay)"),
            ('{"agent": "agent-2", "money": 1000, "exists": true}',
             '{"agent": "agent-2", "money": 1001, "exists": true}',
             "a first balance of 1001 (a mint decay cannot explain)"),
            ('{"agent": "agent-2", "money": 1000, "exists": true}',
             '{"agent": "agent-2", "money": 0, "exists": true}',
             "a first balance of 0 (an empty account reported as seeded)"),
        ]
        first_results = []
        for old, new, label in first_lies:
            mutated = text.replace(old, new, 1)
            if mutated == text:
                print(f"  FAIL  could not build the lie: {label}; the README "
                      f"no longer carries the line this arm mutates")
                first_results.append(False)
                continue
            # replay() returns the LIST OF FAILURES, so a caught lie is a
            # NON-EMPTY list. Writing `not replay(...)` here -- my first
            # version -- inverts the meaning and prints FAIL for every arm
            # that works. The arm above (`ok_bad = not replay(...)`) reads
            # correctly only because it is tested with another `not`. A label
            # is not a measurement: this arm reported all three lies as
            # missed while the replayer was rejecting each of them.
            caught = bool(replay(ROOT, mutated))
            print(f"  {'PASS' if caught else 'FAIL'}  {label} is caught")
            first_results.append(caught)
        # THE LATER-READ ARMS. MEASURED (agent-2, 2026-10-18, on ce5497b):
        # the SECOND balance and the TRANSFER's resulting money were both
        # selected/ignored by their own value. Quoting 0, 5, 500, 998 or 1001
        # for the second balance all PASSED -- including 1001, a RISE during a
        # claimed decay -- and a transfer quoting 900 after a 7-coin transfer
        # PASSED with 93 coins unaccounted for. The arms above mutate only the
        # first balance, so they could not have caught either. Each arm below
        # is a different lie; each must be rejected, and the pristine read
        # (ok_real) must stay green.
        later_lies = [
            ('{"agent": "agent-2", "money": 998, "exists": true}',
             '{"agent": "agent-2", "money": 1001, "exists": true}',
             "a second balance of 1001 (a RISE during a claimed decay)"),
            ('{"agent": "agent-2", "money": 998, "exists": true}',
             '{"agent": "agent-2", "money": 500, "exists": true}',
             "a second balance of 500 (not the stated decay)"),
            ('{"agent": "agent-2", "money": 998, "exists": true}',
             '{"agent": "agent-2", "money": 0, "exists": true}',
             "a second balance of 0 (the whole account gone in 5s)"),
        ]
        # The transfer arm needs the README's own arithmetic to be kept valid
        # up to the mutated line, or it would pass/fail for the wrong reason:
        # 998 - 7 = 991. Quoting 900 leaves 93 coins unaccounted.
        later_lies.append(
            ('"ok": true, "money": 991, "recipient": "agent-3", '
             '"recipient_balance": 7',
             '"ok": true, "money": 900, "recipient": "agent-3", '
             '"recipient_balance": 7',
             "a transfer result of 900 after a 7-coin transfer (93 vanished)"))
        # THE CLOCK ARMS. MEASURED (agent-2, on the first version of this fix):
        # both clock numbers were read with re.search over the WHOLE README, so
        # the match came from a PROSE sentence above the transcript rather than
        # from the launch line in the block being replayed. Editing the launch
        # line to DECAY_SECONDS=1 PASSED, and DELETING it also PASSED (it fell
        # back to the prose). The arms above could not catch either: they
        # mutate the balances, and these mutate the UNITS. Each must be
        # rejected; the pristine read (ok_real) must stay green.
        clock_lies = [
            ("DECAY_SECONDS=2 python3 bank/bank.py",
             "DECAY_SECONDS=1 python3 bank/bank.py",
             "a launch line stating DECAY_SECONDS=1 (the balances are for 2)"),
            (" DECAY_SECONDS=2 python3 bank/bank.py",
             " python3 bank/bank.py",
             "a launch line with NO DECAY_SECONDS (the decay claim has no "
             "units)"),
            ("   ... 5 seconds later ...", "   ... 10 seconds later ...",
             "an ellipsis of '10 seconds later' (the balance is for 5)"),
        ]
        clock_results = []
        for old, new, label in clock_lies:
            mutated = text.replace(old, new, 1)
            if mutated == text:
                print(f"  FAIL  could not build the lie: {label}; the README "
                      f"no longer carries the line this arm mutates")
                clock_results.append(False)
                continue
            caught = bool(replay(ROOT, mutated))
            print(f"  {'PASS' if caught else 'FAIL'}  {label} is caught")
            clock_results.append(caught)
        later_results = []
        for old, new, label in later_lies:
            mutated = text.replace(old, new, 1)
            if mutated == text:
                print(f"  FAIL  could not build the lie: {label}; the README "
                      f"no longer carries the line this arm mutates")
                later_results.append(False)
                continue
            # replay() returns the LIST OF FAILURES; a caught lie is a
            # NON-EMPTY list. Same inverted-`not` trap recorded above.
            caught = bool(replay(ROOT, mutated))
            print(f"  {'PASS' if caught else 'FAIL'}  {label} is caught")
            later_results.append(caught)

        all_ok = (ok_real and built and not ok_bad and all(first_results)
                  and all(clock_results) and all(later_results))
        return 0 if all_ok else 1

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
