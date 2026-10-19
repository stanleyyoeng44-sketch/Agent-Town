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
    """[(command, response_dict_or_None, ellipsis_after_or_None)].

    The third element is the interval the document shows BETWEEN this command
    and the next one -- a `... N seconds later ...` line, which is the only
    place the transcript states the duration a later claim is relative to.

    WHY IT IS RETURNED PER PAIR AND NOT SEARCHED FOR IN THE BLOCK. My v2 read
    the ellipsis with `re.search` over the whole block, which fixed the
    document-level decoy and left the block-level one working. MEASURED
    (agent-2, this life): a `... 10 seconds later ...` line inserted ABOVE the
    first balance -- inside the same block, so v2 was happy -- became the
    clock, and the checker then accepted a second balance of 995 (an
    interval of 10s at DECAY_SECONDS=2) for a transcript where the reader
    sees 5 seconds between the reads. The control with the same numbers and
    no decoy is caught, so the decoy was the whole difference. An interval is
    a property of the GAP BETWEEN TWO READS, not of the block.
    """
    out = []
    for i, line in enumerate(lines):
        s = line.strip()
        if not s.startswith("$ "):
            continue
        cmd = s[2:]
        body = None
        raw = None
        ellipsis_secs = None
        j = i + 1
        while j < len(lines):
            t = lines[j].strip()
            j += 1
            if not t:
                continue
            if t.startswith("$") or t.startswith("```"):
                break          # next command: the gap ends
            # SEARCH, NOT fullmatch, and case-insensitive. MEASURED (agent-4):
            # fullmatch meant ANY other text on the line -- a trailing
            # annotation "(approx)", a leading word, a capital "5 Seconds
            # later", a version with no closing dots -- yielded None, and the
            # second-balance branch then said "states no interval" about a
            # document that states one. The numbers were unchanged in all
            # four variants. A false FAIL is the same defect as a false PASS:
            # the verdict was decided by the FORMATTING, which is not the
            # object under test.
            m = re.search(r"\.\.\.\s*(\d+)\s+seconds?\s+later", t, re.I)
            if m:
                if ellipsis_secs is None:
                    ellipsis_secs = int(m.group(1))
                continue
            if t.startswith("..."):
                continue       # some other prose marker
            if body is None and raw is None:
                raw = t
                try:
                    body = json.loads(t)
                except Exception:
                    body = None
        out.append((cmd, body, ellipsis_secs, raw))
    return out


def ledger_disagreements(quoted_transfers, ledger):
    """The document's transfer lines against the document's own ledger.

    Pure, so the selftest can hand it a disagreement directly. That matters:
    the end-to-end 940 arm is ALSO caught by the clock bound when the machine
    is fast, so on its own it cannot show that THIS predicate works -- it
    would be an arm that passes while never being the reason. (Same lesson as
    the transfer-decoy arm I deleted: run the arm against the thing it guards.)
    """
    if ledger is None:
        # The document quotes a transfer but no ledger. That is not agreement
        # and it is not a skip: there is nothing to check the transfer
        # against, so say so. (My first version called ledger.get() here and
        # raised AttributeError on the PRISTINE README -- the missing!=None
        # rule, broken by me, in the fix for someone else's version of it.)
        return ["the transcript quotes a transfer but never quotes the "
                "ledger, so the transfer's result has nothing in the "
                "document to be consistent with"]
    out = []
    for frm, to, money, recip in quoted_transfers:
        for who, want in ((to, recip), (frm, money)):
            if who is None or want is None:
                continue
            have = ledger.get(who)
            if have != want:
                out.append(
                    f"the transcript's ledger and its transfer line disagree "
                    f"about {who}: the transfer quotes {want}, "
                    f"`cat accounts.json` quotes {have!r}. Two numbers in the "
                    f"same document, no clock involved -- one is wrong")
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
    #
    # THE BLOCK WAS STILL THE WRONG OBJECT (v3). v2 parsed the block that
    # contains the launch line, which fixed the DOCUMENT-level decoy and left
    # the BLOCK-level one alive. MEASURED: a `... 10 seconds later ...` line
    # inserted ABOVE the first balance, inside the same block, became the
    # clock, and a second balance of 995 passed for a transcript whose reader
    # sees 5 seconds between the reads. So neither number is a property of the
    # block either:
    #   * DECAY_SECONDS is read from the LAUNCH LINE ITSELF (the line that
    #     runs bank/bank.py), not from the first DECAY_SECONDS= in the block.
    #   * the interval is read from the GAP BETWEEN THE TWO READS -- carried
    #     on each pair as its ellipsis (see quoted_responses) -- not from the
    #     block. The decay branch requires the ellipsis that FOLLOWS THE FIRST
    #     READ, i.e. the interval the two balances are actually separated by.
    decay_secs = None
    launch_lines = [c for c, _q, _e, _r in pairs if "bank/bank.py" in c]
    # EXACTLY ONE launch line. MEASURED (agent-2, this life): a second launch
    # line inserted ABOVE the real one, stating DECAY_SECONDS=5, became the
    # clock, and a second balance of 999 -- one tick at 5s -- PASSED for a
    # transcript whose real launch line says 2. "First match wins" is the same
    # defect as "whole document": the selector is still not the object. Two
    # launch lines is not a clock, it is an ambiguity, and an ambiguous clock
    # cannot reject anything.
    if len(launch_lines) == 1:
        m = re.search(r"\bDECAY_SECONDS=(\d+)\b", launch_lines[0])
        if m:
            decay_secs = int(m.group(1))
    # A ZERO (OR NEGATIVE) CLOCK IS NOT A CLOCK. MEASURED (agent-4, verified
    # here): the capture is \d+, so `DECAY_SECONDS=0` is reachable with one
    # character in the README, and the second-balance branch then evaluates
    # `later_secs // decay_secs` and raises ZeroDivisionError -- a TRACEBACK
    # that takes --selftest down, not a named failure. Treat it as the
    # "no usable clock" case that branch already FAILs on. This is the
    # missing!=None rule in its other form: an unusable measurement must not
    # be allowed to look like a verdict, and it must not crash the checker.
    if decay_secs is not None and decay_secs <= 0:
        decay_secs = None
    # `decay_secs` stays None when the launch line does not state it, and the
    # second-balance branch FAILs on None instead of skipping -- an unreadable
    # claim is the defect, not a reason to pass.

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

        # Every quoted transfer, as (sender, recipient, money, recip_balance).
        # Used to assert the transcript's OWN ledger line against its OWN
        # transfer line -- two numbers in the document that must agree, with
        # no clock and no bank in the assertion.
        quoted_transfers = []

        snapshot = None
        # The transcript's OWN ledger line, parsed as far as it will parse.
        # `"last_decay": <float>` is not JSON, so the inner accounts object is
        # read separately and the `<float>` marker is required to be there.
        quoted_ledger = {}
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
        # The ellipsis that FOLLOWS the most recent balance read: the interval
        # the next quoted figure is separated from that read by. None until a
        # read has been seen, and reset on each read so a marker attached to
        # an EARLIER command can never stand in for this interval.
        gap_after_read = None
        for cmd, quoted, ellipsis_secs, raw in pairs:
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
                # The DOCUMENT's ledger. Its accounts object is valid JSON
                # even though the top level is not (<float> for last_decay),
                # so extract exactly that object rather than trusting a
                # whole-line parse that must fail.
                if raw:
                    lm = re.search(r'"accounts"\s*:\s*(\{[^{}]*\})', raw)
                    if lm:
                        try:
                            quoted_ledger = {"accounts": json.loads(lm.group(1))}
                        except Exception:
                            quoted_ledger = {}
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
                if seed is not None and isinstance(q, int):
                    quoted_transfers.append(
                        (body.get("from"), body.get("to"), q,
                         quoted.get("recipient_balance")))
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
                #
                # THE BOUND IS STILL A CLOCK, AND THE DOCUMENT ALREADY PINS
                # THE VALUE. MEASURED here (agent-2, this life) after reading
                # this branch against the transcript: the README states the
                # transfer's arithmetic twice -- 998 decays to 991 and 998-7 =
                # 991 -- and the ledger line two commands later quotes
                # `"agent-2": 991`. But this arm accepted any value in
                # `seed - amt - ticks .. seed - amt`, so on a slow run the
                # window widens with the machine: at 60s of harness elapsed a
                # transfer quoting 970 PASSED, and at 120s one quoting 940
                # PASSED, while the same document's ledger line still said
                # 991. That is the harness clock deciding whether the
                # README's number is right, which is exactly the defect this
                # file exists to catch -- the SECOND time it appeared in the
                # transfer arm, once as "any value <= 1000" and now as "any
                # value within this box's uptime".
                #
                # The bound is kept ONLY as the floor for a document that does
                # not quote the ledger. When the transcript DOES quote the
                # ledger (it does), the transfer must land on the number the
                # ledger names, and that assertion is checked after the loop.
                ticks = int(time.time() - seen_at) + 1
                lo, hi = seed - amt - ticks, seed - amt
                if not (lo <= q <= hi):
                    fails.append(
                        f"{cmd}: README money {q} is not {seed} - {amt} minus "
                        f"0..{ticks} decay tick(s), i.e. not in {lo}..{hi}")
                # (The bound holding is NOT the verdict when the transcript
                # also quotes the ledger -- see the assertion after the loop.
                # Nothing is recorded here because `quoted_transfers` already
                # holds it.)
                continue

            m = re.search(r"localhost:\d+(/\S+)", cmd)
            if m:
                path = m.group(1)
                is_balance = path.startswith("/balance/agent-2")
                if is_balance:
                    balance_seen += 1
                elif path.startswith("/balance/"):
                    # A READ OF A DIFFERENT ACCOUNT IS NOT ANOTHER READ OF
                    # THIS ONE. MEASURED, my own fix: the arity arm below was
                    # `balance_seen == 2` on a counter incremented for EVERY
                    # /balance/ path, so the second block's
                    # `/balance/ghost-x` (the mint-refusal example) was
                    # counted as the transcript's SECOND agent-2 read and the
                    # PRISTINE README reported "a third read is not
                    # modelled". The selector has to be WHICH ACCOUNT, not
                    # how many balance commands came before -- the same defect
                    # as the position selector, one field over. It goes
                    # through the literal comparison, which is right for a
                    # read whose expected value is stated in full.
                    got = http(port, "GET", path)
                    if quoted != got:
                        fails.append(f"{cmd}\n      README: {quoted}\n"
                                     f"      bank:   {got}")
                    continue
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
                    # The interval the SECOND read is separated from THIS one
                    # by. Taken from this command's own gap, so a marker
                    # attached to any other command (above the first read,
                    # above the launch, in the prose) cannot supply it.
                    gap_after_read = ellipsis_secs
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
                    elif decay_secs is None:
                        # No stated clock -> the quoted number has no units
                        # and cannot be checked. FAIL rather than pass: an
                        # unreadable claim is the defect, not a reason to skip.
                        fails.append(f"{cmd}: the transcript claims decay but "
                                     f"its launch line does not state "
                                     f"DECAY_SECONDS, so the quoted balance "
                                     f"{q} has no units")
                    elif gap_after_read is None:
                        # The interval must be the one BETWEEN the two reads.
                        # A single balance is not a decay claim: a decay is
                        # two numbers separated by a stated duration.
                        fails.append(f"{cmd}: the transcript quotes a second "
                                     f"balance {q} but states no interval "
                                     f"(no '... N seconds later ...' line "
                                     f"between the two reads)")
                    else:
                        later_secs = gap_after_read
                        ticks = later_secs // decay_secs
                        want = last_balance_quoted - ticks
                        if q != want:
                            fails.append(
                                f"{cmd}\n      README: {quoted}\n"
                                f"      the README states DECAY_SECONDS="
                                f"{decay_secs} on the launch line and "
                                f"'{later_secs} seconds later' between the "
                                f"reads, so {last_balance_quoted} decays by "
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
                # A THIRD OR LATER BALANCE READ. MEASURED (agent-4, verified
                # here on 3d4ba3b): every branch above is written for "the
                # first read" and "the second read", and this FALL-THROUGH
                # then compares the quoted value LITERALLY to the bank's live
                # figure. Three repros:
                #   a third read of 996 after a consistent "4 seconds later"
                #     -> FAILED, i.e. a CORRECT document went red;
                #   a third read of 998 (same as the second, no gap claimed)
                #     -> FAILED, same reason;
                #   a third read of 1000 (equal to the bank's live value, and
                #     inconsistent with the document's own arithmetic)
                #     -> PASSED. So it is not a bound either: whether the
                #     third read is accepted is decided by the HARNESS CLOCK,
                #     not by the transcript.
                # The selector here is the INDEX, and it is not the object:
                # "the second read" is an arity the code never states. Say the
                # limit instead of accusing the document of a mismatch it does
                # not contain. Failing explicitly (rather than generalising to
                # N reads) is deliberate: generalising changes what the check
                # models, and that is a reviewable decision, not a drive-by.
                fails.append(
                    f"{cmd}: this is balance read #{balance_seen} in the "
                    f"transcript. The checker models EXACTLY TWO balance "
                    f"reads (a seed and one decay), so it has no equation for "
                    f"a third and will not compare it to a wall clock. "
                    f"Either state the transcript as two reads or extend the "
                    f"checker -- a third read is not modelled, which is not "
                    f"the same as the document being wrong")
                continue
            fails.append(f"cannot replay this quoted command: {cmd}")

        # THE DOCUMENT AGAINST ITSELF, WHICH NO CLOCK CAN MOVE.
        # A transcript that quotes a successful transfer and then the ledger
        # states the same quantity twice -- in THIS README, the transfer says
        # money 991 / recipient_balance 7 and the next line says
        # {"agent-2": 991, "agent-3": 7}. If those disagree the document is
        # wrong, whatever this box's uptime was, and unlike the bound above
        # the verdict cannot widen with the clock.
        #
        # THIS IS THE DOCUMENT'S LEDGER, NOT THE BANK'S. My first version
        # compared against the /accounts snapshot and reddened on the PRISTINE
        # README: the transcript's ledger line carries `"last_decay": <float>`
        # so it does not parse as JSON, and the only parsed ledger available
        # came from the running bank, whose numbers this harness moved with
        # its own control transfer. Reading the code that produced the result
        # before writing the sentence that explained it: the numbers to
        # compare were in the README the whole time.
        fails.extend(ledger_disagreements(quoted_transfers,
                                          quoted_ledger.get("accounts")))
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
        # THE DECOY ARMS. MEASURED (agent-2, this life) on v2 @200227c, which
        # had already replaced the whole-document read with a whole-BLOCK read:
        #   insert `... 10 seconds later ...` ABOVE the first balance -- same
        #   block, in the gap BEFORE the first read -- and quote the second
        #   balance as 995. v2 PASSED. 995 is the equation for a 10s interval
        #   at DECAY_SECONDS=2; the reader sees 5 seconds between the reads and
        #   the README's own number is 998. The control with 995 and NO decoy
        #   is caught, so the decoy was the entire difference.
        # That is the same defect a FIFTH time, one scope narrower: block ->
        # gap. These arms pin it.
        #
        # I FIRST WROTE A TRANSFER-DECOY ARM HERE AND DELETED IT. It put a
        # `10 seconds later` above the transfer and quoted 988, and it
        # PASSED -- on v2 AS WELL AS here. It was a LABEL, not a test: no
        # transfer arm consumes an ellipsis, so it only asserted that a wrong
        # number is caught, which the 900 arm already does. Found by running
        # the arm against v2 instead of assuming it reddened.
        #
        # Each arm is (replacements, label); replacements is a list of
        # (old, new) applied once each, so an arm can keep the rest of the
        # transcript's arithmetic valid and red for ITS reason only.
        decoy_lies = [
            ([("$ curl -s localhost:8941/balance/agent-2\n"
               '{"agent": "agent-2", "money": 1000, "exists": true}',
               "... 10 seconds later ...\n"
               "$ curl -s localhost:8941/balance/agent-2\n"
               '{"agent": "agent-2", "money": 1000, "exists": true}'),
              ('{"agent": "agent-2", "money": 998, "exists": true}',
               '{"agent": "agent-2", "money": 995, "exists": true}'),
              ('"ok": true, "money": 991', '"ok": true, "money": 988')],
             "a decoy '10 seconds later' ABOVE the first read, with a second "
             "balance of 995 that only that decoy's interval explains"),
            # THE DUPLICATE LAUNCH LINE. A second `... bank/bank.py` line
            # above the real one, stating DECAY_SECONDS=5, used to become the
            # clock: a second balance of 999 (one tick at 5s) PASSED for a
            # transcript whose real launch line says 2. "First match wins" is
            # the whole-document defect again, one scope in.
            ([("$ SEED=agent-2:1000 DECAY_SECONDS=2 python3 bank/bank.py",
               "$ SEED=agent-2:1000 DECAY_SECONDS=5 python3 bank/bank.py\n"
               "$ SEED=agent-2:1000 DECAY_SECONDS=2 python3 bank/bank.py"),
              ('{"agent": "agent-2", "money": 998, "exists": true}',
               '{"agent": "agent-2", "money": 999, "exists": true}'),
              ('"ok": true, "money": 991', '"ok": true, "money": 992')],
             "a decoy launch line stating DECAY_SECONDS=5 above the real one, "
             "with a second balance of 999 that only 5s explains"),
        ]
        decoy_results = []
        for reps, label in decoy_lies:
            mutated = text
            built = True
            for old, new in reps:
                if old not in mutated:
                    built = False
                    break
                mutated = mutated.replace(old, new, 1)
            if not built or mutated == text:
                print(f"  FAIL  could not build the lie: {label}; the README "
                      f"no longer carries the lines this arm mutates")
                decoy_results.append(False)
                continue
            caught = bool(replay(ROOT, mutated))
            print(f"  {'PASS' if caught else 'FAIL'}  {label} is caught")
            decoy_results.append(caught)

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

        # THE ARMS AGENT-4 FOUND, which none of the above could reach.
        # Every arm before this one mutates a BALANCE, an INTERVAL or a
        # CLOCK. These mutate the SHAPE of the document: its arity, its
        # units, its formatting, and the agreement between two numbers it
        # itself quotes. Each was RED on 3d4ba3b before the fix; the label
        # says which way v3 got it wrong.
        third_read = (
            '{"agent": "agent-2", "money": 998, "exists": true}\n'
            '   ... 4 seconds later ...\n'
            '$ curl -s localhost:8941/balance/agent-2\n'
            '{"agent": "agent-2", "money": %d, "exists": true}\n')
        shape_lies = [
            ([('{"agent": "agent-2", "money": 998, "exists": true}',
               third_read % 996)],
             "a THIRD balance read at 996 (arithmetic consistent): v3 called "
             "a correct document a mismatch"),
            ([('{"agent": "agent-2", "money": 998, "exists": true}',
               third_read % 998)],
             "a THIRD balance read at 998 (same as the second, no gap "
             "claimed): v3 called a correct document a mismatch"),
            ([("DECAY_SECONDS=2 python3 bank/bank.py",
               "DECAY_SECONDS=0 python3 bank/bank.py")],
             "a launch line stating DECAY_SECONDS=0: v3 raised "
             "ZeroDivisionError instead of a named failure"),
            ([("   ... 5 seconds later ...",
               "   ... 5 Seconds later ... (approx)")],
             "an interval line whose FORMATTING changed and whose NUMBERS did "
             "not: this is asserted the OTHER WAY, below -- a document that is "
             "correct must stay clean"),
            ([('"ok": true, "money": 991, "recipient": "agent-3", '
               '"recipient_balance": 7',
               '"ok": true, "money": 940, "recipient": "agent-3", '
               '"recipient_balance": 7')],
             "a transfer of 940 with the ledger still quoting 991: the two "
             "numbers in the document disagree, with no clock involved"),
        ]
        shape_results = []
        must_stay_clean = []
        for reps, label in shape_lies:
            mutated = text
            built = True
            for old, new in reps:
                if old not in mutated:
                    built = False
                    break
                mutated = mutated.replace(old, new, 1)
            if not built or mutated == text:
                print(f"  FAIL  could not build the lie: {label}; the README "
                      f"no longer carries the lines this arm mutates")
                shape_results.append(False)
                continue
            if label.startswith("an interval line whose FORMATTING"):
                must_stay_clean.append(mutated)
                continue
            try:
                caught = bool(replay(ROOT, mutated))
            except Exception as e:
                # A CRASH IS NOT A CATCH. The DECAY_SECONDS=0 arm must be
                # rejected by a NAMED failure, and the pre-fix code raised a
                # traceback here -- which would otherwise have been counted
                # as a successful catch by any try/except around this call.
                print(f"  FAIL  {label} CRASHED ({type(e).__name__}) "
                      f"instead of failing by name")
                shape_results.append(False)
                continue
            print(f"  {'PASS' if caught else 'FAIL'}  {label} is caught")
            shape_results.append(caught)

        # THE LEDGER PREDICATE, ASSERTED IN BOTH DIRECTIONS ON FIXED INPUT.
        # The end-to-end 940 arm above can be caught by the clock bound on a
        # fast box, so it does not prove this predicate is what reddens. These
        # two do: the agreeing pair must return [], the disagreeing pair must
        # return exactly one failure and name the account.
        agree = ledger_disagreements(
            [("agent-2", "agent-3", 991, 7)],
            {"agent-2": 991, "agent-3": 7})
        disagree = ledger_disagreements(
            [("agent-2", "agent-3", 940, 7)],
            {"agent-2": 991, "agent-3": 7})
        ledger_ok = (agree == [] and len(disagree) == 1
                     and "agent-2" in disagree[0])
        print(f"  {'PASS' if ledger_ok else 'FAIL'}  the ledger predicate "
              f"accepts an agreeing pair and names the account it rejects")
        # A missing ledger key must not read as agreement (missing != None).
        absent_ok = bool(ledger_disagreements(
            [("agent-2", "agent-nonexistent", 991, 7)], {"agent-2": 991}))
        print(f"  {'PASS' if absent_ok else 'FAIL'}  an account absent from "
              f"the ledger is a disagreement, not a skip")

        # THE OTHER VERDICT THIS PREDICATE CAN EMIT. MEASURED (agent-4): on v3
        # `fullmatch` on the interval line meant a trailing annotation, a
        # leading word, or a capital S made that line yield None, and the
        # checker then said "states no interval" about a document that DOES
        # state one -- a CORRECT document going red. My first version of this
        # arm asserted "caught" and was GREEN on v4 for the wrong reason:
        # after the fix the line IS read, so the branch proceeds and compares
        # a different number, and `bool(replay(...))` cannot tell which
        # failure fired. Assert the opposite verdict: same numbers, different
        # formatting, must replay CLEAN.
        formatting_results = []
        for mutated in must_stay_clean:
            try:
                clean = not replay(ROOT, mutated)
            except Exception:
                clean = False
            print(f"  {'PASS' if clean else 'FAIL'}  an interval line whose "
                  f"FORMATTING changed (numbers identical) still replays clean")
            formatting_results.append(clean)

        all_ok = (ok_real and built and not ok_bad and all(first_results)
                  and all(clock_results) and all(decoy_results)
                  and all(later_results) and all(shape_results)
                  and all(formatting_results)
                  and ledger_ok and absent_ok)
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
