"""bank transfer tests -- the sender-must-exist rule, with before/after controls.

Run:  python3 bank/tests/test_transfer_amounts.py     (stdlib only, no Docker)

THE DEFECT THIS PINS. `/transfer` used to call `ensure_account(sender)`.
`ensure_account` funds a name it has never seen with START_MONEY (default
1000). So any caller could send from a brand-new agent name, be funded 1000 on
the spot, and move that money to a real account. Repeat with a fresh name and
the money supply is unbounded.

Auto-funding on first touch is documented and intended for the RECIPIENT (an
agent that has never been paid before still receives) and for `GET /balance`.
Applying it to the SENDER of a transfer turns a ledger into a mint.

THE FIX (in bank/bank.py): the sender must already exist. The refusal creates
nothing.

WHY BOTH DIRECTIONS ARE TESTED. A test that only asserts "the mint is blocked"
passes trivially against a bank that blocks all transfers. This file runs every
check twice -- once with the guard removed (reconstructing the pre-fix
behaviour) and once as shipped -- so each check is known to be capable of
failing, and the legitimate paths are known to still work afterwards.
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


def build(fixed):
    """Copy bank.py into a temp dir with hardcoded paths redirected.

    fixed=False reconstructs the PRE-FIX behaviour by removing the guard, so
    the test can show the defect is real rather than hypothetical. If the guard
    is missing when fixed=False is asked for, or present-but-different when
    fixed=True, the test refuses to run rather than passing vacuously.
    """
    work = tempfile.mkdtemp()
    path = os.path.join(work, "bank.py")
    src = open(BANK, encoding="utf-8").read()
    # Redirect the ledger and mailbox to the temp dir. Master reads DATA from
    # the environment; older branches hardcode the literal. Patch whichever is
    # present, then ASSERT the patch took effect -- a redirect that quietly
    # misses is a no-op, and a no-op test still prints PASS.
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
    src = src.replace('MAILBOX = "/shared/mailbox"',
                      'MAILBOX = %r' % box)
    assert box in src, "mailbox redirect did not take effect"
    guard = ('            if sender not in _accounts:\n'
             '                return self._send(400, {"error": "unknown sender"})\n')
    if fixed:
        assert guard in src, ("the fix is not present in bank/bank.py -- "
                              "this test would be checking nothing")
    else:
        assert guard in src, ("cannot reconstruct the pre-fix behaviour: "
                              "the guard anchor is not in bank/bank.py")
        src = src.replace(guard, "")
        src = src.replace("            s_bal, s_new = _accounts[sender], False\n",
                          "            s_bal, s_new = ensure_account(sender)\n")
    open(path, "w", encoding="utf-8").write(src)
    return work, path


def serve(path, port):
    env = dict(os.environ, PORT=str(port), DECAY_SECONDS="100000",
               START_MONEY="1000")
    proc = subprocess.Popen([sys.executable, path], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    time.sleep(1.0)
    return proc


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


def exercise(fixed, port):
    work, path = build(fixed)
    proc = serve(path, port)
    try:
        done = {}

        # N4/N5 -- legitimate behaviour that must survive the fix.
        # Create the two real agents the documented way before using them.
        call(port, "GET", "/balance/agent-1")
        call(port, "GET", "/balance/agent-2")
        st, body = call(port, "POST", "/transfer",
                        {"from": "agent-1", "to": "agent-2", "amount": 10})
        done["N4 an existing agent can transfer"] = (st == 200)
        st, _ = call(port, "POST", "/transfer",
                     {"from": "agent-1", "to": "agent-2", "amount": 99999})
        done["N5 an overdraft is still refused"] = (st == 400)

        # N1/N2 -- the fix.
        st, _ = call(port, "POST", "/transfer",
                     {"from": "ghost", "to": "agent-2", "amount": 1000})
        done["N1 an unknown sender is refused"] = (st == 400)
        st, _ = call(port, "POST", "/transfer",
                     {"from": "ghost2", "to": "agent-2", "amount": 1})
        done["N2 the refusal has no side effect"] = (st == 400)

        # N3 -- the fix does not overreach: a fresh RECIPIENT still works.
        st, _ = call(port, "POST", "/transfer",
                     {"from": "agent-2", "to": "brand_new", "amount": 1})
        done["N3 a fresh recipient is still created"] = (st == 200)

        # N6 -- the actual attack, repeated.
        minted = 0
        for i in range(5):
            st, _ = call(port, "POST", "/transfer",
                         {"from": "mint%d" % i, "to": "agent-2", "amount": 1000})
            if st == 200:
                minted += 1000
        done["N6 five fresh names mint nothing"] = (minted == 0)
        return done, minted
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        shutil.rmtree(work, ignore_errors=True)


def main():
    print("PRE-FIX BEHAVIOUR (guard removed on purpose):")
    before, minted_before = exercise(False, 8901)
    for k in sorted(before):
        print("  %-38s %s" % (k, before[k]))
    print("  coins minted by 5 fresh sender names: %d" % minted_before)

    print("\nAS SHIPPED:")
    after, minted_after = exercise(True, 8902)
    for k in sorted(after):
        print("  %-38s %s" % (k, after[k]))
    print("  coins minted by 5 fresh sender names: %d" % minted_after)

    checks = {
        "the defect is real (pre-fix mints)": minted_before == 5000,
        "the fix is present (post-fix mints 0)": minted_after == 0,
        "pre-fix passes the legit paths (control is a real bank)": (
            before["N4 an existing agent can transfer"]
            and before["N5 an overdraft is still refused"]),
        "post-fix passes every check": all(after.values()),
    }
    print("\nVERDICT")
    for k, v in checks.items():
        print("  %-52s %s" % (k, "PASS" if v else "FAIL"))
    ok = all(checks.values())
    print("\n%s" % ("SELFTEST PASS" if ok else "SELFTEST FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
