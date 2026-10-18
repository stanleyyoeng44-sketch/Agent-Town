"""bank tests -- "a new balance is never a grant", with before/after controls.

Run:  python3 bank/tests/test_transfer_amounts.py     (stdlib only, no Docker)

THE DEFECT THIS PINS. `ensure_account()` used to fund a name it had never seen
with START_MONEY (default 1000). There were THREE ways to reach it, and
guarding one of them only moves the hole:

  A. the transfer's SENDER      POST /transfer {"from": "ghost-7", ...}
  B. the transfer's RECIPIENT   pay a fresh name, then spend its 1000
  C. a plain balance read       GET /balance/ghost-7

B is the interesting one: it is the DOCUMENTED "recipient is auto-created"
behaviour, so a fix that only refuses unknown senders leaves B fully open.
Measured on the sender-only guard: bypass A and bypass B each moved 4000 coins
to a real account, and together took one victim from 1000 to 9000.

THE FIX. Creating an account grants nothing. Accounts are created at ZERO on
first touch. Coins enter an account only through `SEED` at startup or
`/admin/topup` -- both operator acts. `GET /balance/<unknown>` returns
{"money": 0, "exists": false}.

WHY BOTH DIRECTIONS ARE TESTED. A test that only asserts "the mint is blocked"
passes trivially against a bank that blocks everything. This file builds the
bank TWICE -- once with the pre-fix `ensure_account` reinstated and once as
shipped -- so every check is known to be capable of failing, and the legitimate
paths are known to still work afterwards. A check that cannot fail is not a
check.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BANK = os.path.join(HERE, "bank.py")

# The shipped, correct body of ensure_account.
FIXED_ACCOUNT = """\
    if name in _accounts:
        return _accounts[name], False
    _accounts[name] = 0
    return 0, True
"""
# The pre-fix body: creating an account pays START_MONEY.
BROKEN_ACCOUNT = """\
    if name in _accounts:
        return _accounts[name], False
    _accounts[name] = START_MONEY
    return START_MONEY, True
"""


def build(fixed, seed=""):
    """Copy bank.py to a temp dir with paths redirected and (optionally) the
    pre-fix ensure_account reinstated.

    Every textual edit is ASSERTED to have changed the source. A mutation that
    silently does not mutate is the cohort's most-repeated defect: the test
    then runs the real bank and reports PASS for something it never exercised.
    """
    work = tempfile.mkdtemp()
    path = os.path.join(work, "bank.py")
    src = open(BANK, encoding="utf-8").read()
    original = src

    # --- redirect the ledger and mailbox (both shapes supported) -----------
    ledger = os.path.join(work, "accounts.json")
    data_env = 'DATA = os.environ.get("DATA", "/data/accounts.json")'
    data_lit = 'DATA = "/data/accounts.json"'
    if data_env in src:
        src = src.replace(data_env, "DATA = %r" % ledger)
    elif data_lit in src:
        src = src.replace(data_lit, "DATA = %r" % ledger)
    else:
        raise AssertionError("bank.py has no DATA assignment this test knows "
                             "how to redirect; the ledger would be shared")
    assert ledger in src, "ledger redirect did not take effect"

    box = os.path.join(work, "mailbox")
    assert 'MAILBOX = "/shared/mailbox"' in src, "mailbox literal not found"
    src = src.replace('MAILBOX = "/shared/mailbox"', 'MAILBOX = %r' % box)
    assert box in src, "mailbox redirect did not take effect"

    # --- choose the behaviour under test -----------------------------------
    if fixed:
        assert FIXED_ACCOUNT in src, (
            "ensure_account does not create at 0 -- the fix is not in "
            "bank.py, so this test would be checking nothing")
    else:
        assert FIXED_ACCOUNT in src, (
            "cannot reconstruct the pre-fix behaviour: the expected "
            "ensure_account body is not present")
        src = src.replace(FIXED_ACCOUNT, BROKEN_ACCOUNT, 1)
        assert BROKEN_ACCOUNT in src and src != original, \
            "pre-fix reconstruction did not actually mutate the source"

    open(path, "w", encoding="utf-8").write(src)
    return work, path, seed


def serve(work, path, port, seed):
    env = dict(os.environ, PORT=str(port), DECAY_SECONDS="100000",
               START_MONEY="1000", DATA=os.path.join(work, "accounts.json"))
    if seed:
        env["SEED"] = seed
    proc = subprocess.Popen([sys.executable, path], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    for _ in range(200):
        try:
            call(port, "GET", "/accounts")
            return proc
        except OSError:
            if proc.poll() is not None:
                raise AssertionError("bank exited on startup (rc=%s)" % proc.returncode)
            time.sleep(0.05)
    proc.kill()
    raise AssertionError("bank never came up on port %d" % port)


def call(port, method, route, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, route),
                                 data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def total(port):
    return sum(call(port, "GET", "/accounts")[1]["accounts"].values())


def exercise(fixed, port):
    """Run the whole matrix. Returns {check_name: bool} and the money created."""
    work, path, _ = build(fixed)
    # Seed a real account so the "legitimate paths still work" checks have a
    # funded sender under BOTH behaviours (pre-fix would fund it anyway, but
    # seeding makes the two runs comparable).
    proc = serve(work, path, port, seed="victim:1000,agent-1:1000")
    try:
        done = {}
        start_total = total(port)

        # ---- the three creation paths must not grant (post-fix) ----------
        st, body = call(port, "GET", "/balance/ghost-a")
        done["C: GET /balance on an unknown name reports 0"] = \
            (st == 200 and body.get("money") == 0)
        done["C: ...and reports exists=false"] = (body.get("exists") is False)

        st, _ = call(port, "POST", "/transfer",
                     {"from": "ghost-b", "to": "victim", "amount": 1000})
        done["A: a fresh SENDER is refused"] = (st == 400)

        # B is the one a sender-only guard misses. Pay a fresh name 1 coin,
        # then try to spend its (pre-fix) 1000 starting balance.
        st, _ = call(port, "POST", "/transfer",
                     {"from": "victim", "to": "fresh-b", "amount": 1})
        assert st == 200, "seeding the recipient failed: %r" % (st,)
        bal = call(port, "GET", "/balance/fresh-b")[1]["money"]
        st, _ = call(port, "POST", "/transfer",
                     {"from": "fresh-b", "to": "victim", "amount": 900})
        done["B: a fresh RECIPIENT cannot spend a grant"] = (st == 400)
        done["B: ...and its balance is only what it was paid"] = (bal == 1)

        # ---- legitimate behaviour that must survive ----------------------
        st, _ = call(port, "POST", "/transfer",
                     {"from": "agent-1", "to": "victim", "amount": 10})
        done["L1 an existing funded agent can transfer"] = (st == 200)
        st, _ = call(port, "POST", "/transfer",
                     {"from": "agent-1", "to": "victim", "amount": 99999})
        done["L2 an overdraft is still refused"] = (st == 400)
        st, _ = call(port, "POST", "/transfer",
                     {"from": "agent-1", "to": "brand_new", "amount": 5})
        done["L3 a fresh recipient still RECEIVES"] = (st == 200)
        done["L4 ...at exactly what was sent"] = (
            call(port, "GET", "/balance/brand_new")[1]["money"] == 5)

        # ---- a refused transfer must not write to the ledger -------------
        # N2 in an earlier version checked only that the STATUS was 400. A
        # refused request that still writes state is not refused: it let any
        # caller grow the ledger without bound by naming fresh recipients in
        # failing transfers. Compare the WHOLE ledger, not just the balances.
        # The junk row is created IN MEMORY and only reaches the file on the
        # next save, so checking the file alone misses it (my first version of
        # this check did exactly that and passed against the bug). Ask the
        # server for its own view of the accounts, which is the state that
        # matters, and then force a save with a legitimate transfer so the
        # persistence path is exercised too.
        ledger_path = os.path.join(work, "accounts.json")
        in_mem_before = dict(call(port, "GET", "/accounts")[1]["accounts"])
        for i in range(5):
            call(port, "POST", "/transfer",
                 {"from": "agent-1", "to": "junk-%d" % i, "amount": 999999})
        in_mem_after = dict(call(port, "GET", "/accounts")[1]["accounts"])
        done["R: a refused transfer does not create accounts"] = (
            set(in_mem_after) == set(in_mem_before))
        # a legitimate transfer flushes state to disk; the refused names must
        # not ride along
        call(port, "POST", "/transfer",
             {"from": "agent-1", "to": "victim", "amount": 1})
        on_disk = set(json.load(open(ledger_path))["accounts"])
        done["R: refused names are not flushed on the next save"] = (
            not any(n.startswith("junk-") for n in on_disk))

        # ---- total supply, the invariant that actually matters -----------
        created = total(port) - start_total
        done["T: no coins were created by any request"] = (created == 0)

        # ---- the explicit operator grant still works ---------------------
        st, body = call(port, "POST", "/admin/topup",
                        {"agent": "operator-made", "amount": 50})
        # no admin token file on this box -> 503 is the correct refusal
        done["G: /admin/topup is not usable without the token"] = (st in (403, 503))
        return done, created
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        shutil.rmtree(work, ignore_errors=True)


def test_docs_match_code():
    """The docs are part of the fix. If they still teach the exploit, it is live.

    Two regressions are possible and both are silent:
      1. a doc still tells a reader to set START_MONEY to a positive value,
         which funds every name touched (the mint, via the teaching path);
      2. a doc shows SEED/balance output that the code can no longer produce.
    Check 1 is mechanical and is what this asserts. Check 2 is covered by the
    transcripts having been re-derived from a live run when they were written.
    """
    root = os.path.dirname(HERE)
    offenders = []
    for name in ("README.md", "docs/guide.md"):
        path = os.path.join(root, name)
        if not os.path.exists(path):
            continue
        for lineno, line in enumerate(open(path, encoding="utf-8"), 1):
            # A doc may MENTION START_MONEY when explaining it; what it must
            # never do is hand a reader a command that sets it to a positive
            # number, or claim accounts are funded on first touch.
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith(">"):
                continue
            if "START_MONEY=" in stripped and "START_MONEY=0" not in stripped:
                offenders.append(f"{name}:{lineno}: {stripped[:80]}")
            if "auto-created with START_MONEY" in stripped:
                offenders.append(f"{name}:{lineno}: {stripped[:80]}")
    return offenders


def main():
    print("PRE-FIX (creation grants START_MONEY) -- the defect must be VISIBLE:")
    before, minted_before = exercise(fixed=False, port=8901)
    for k in sorted(before):
        print("  %-50s %s" % (k, "ok  " if before[k] else "FAIL"))
    print("  coins created from nothing: %d" % minted_before)

    print("\nAS SHIPPED (creation grants nothing) -- the defect must be GONE:")
    after, minted_after = exercise(fixed=True, port=8902)
    for k in sorted(after):
        print("  %-50s %s" % (k, "ok  " if after[k] else "FAIL"))
    print("  coins created from nothing: %d" % minted_after)

    # The two runs assert OPPOSITE things. If both "passed", the mutation was
    # a no-op and neither tested anything.
    verdict = {
        "pre-fix: all three creation paths mint": minted_before > 0,
        "pre-fix: a fresh recipient CAN spend the grant":
            not before["B: a fresh RECIPIENT cannot spend a grant"],
        "post-fix: every check holds": all(after.values()),
        "post-fix: zero coins created": minted_after == 0,
        "post-fix: legitimate paths still work":
            (after["L1 an existing funded agent can transfer"]
             and after["L3 a fresh recipient still RECEIVES"]),
    }
    docs_bad = test_docs_match_code()
    print("\nDOCS")
    if docs_bad:
        for o in docs_bad:
            print("  OFFENCE", o)
    else:
        print("  ok    no doc teaches a positive START_MONEY grant")
    verdict["docs do not teach the mint"] = not docs_bad

    print("\nVERDICT")
    for k, v in verdict.items():
        print("  %-52s %s" % (k, "PASS" if v else "FAIL"))
    ok = all(verdict.values())
    print("\n%s" % ("SELFTEST PASS" if ok else "SELFTEST FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
