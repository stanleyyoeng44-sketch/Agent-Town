#!/usr/bin/env python3
"""The operator token must be reachable where the UI actually mounts it.

THE DEFECT THIS PINS. bank.py read the admin and delegate tokens only from
/run/secrets/. The control UI generates the admin token at
~/.config/docker-ai/bank-admin-token and mounts the delegate token at
/app/agent0-transfer-token. Neither is /run/secrets, and the README's bank
`docker run` mounts no token at all. So on the documented path:

    POST /admin/topup  -> 503 {"error": "admin token unavailable"}
    POST /transfer from agent-0 -> 503 {"error": "delegate token unavailable"}

Measured on master 1221bea before this fix. Under the create-at-0 rule that
means NOTHING CAN BE FUNDED: the bank is read-only in practice, and the
failure looks like a token mistake by the operator rather than a bank bug.

WHY BOTH DIRECTIONS. A test that only asserts "topup works with a token"
passes against a bank that ignores the token entirely (every request 200), and
one that only asserts "topup without a token fails" passes against a bank that
is simply broken. This file checks: no token -> 503; token at the mounted path
-> 200 and the coins arrive; WRONG token -> 403 and no coins move.

Run: python3 bank/tests/test_token_paths.py   (stdlib only, no Docker)
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
TOKEN = "test-operator-token"


def build():
    """Copy bank.py with the ledger, mailbox and token paths redirected.

    Each edit is ASSERTED to have changed the source: a no-op edit would make
    this file test the real /run/secrets paths and pass or fail for reasons
    unrelated to the bank.
    """
    work = tempfile.mkdtemp()
    path = os.path.join(work, "bank.py")
    src = open(BANK, encoding="utf-8").read()

    ledger = os.path.join(work, "accounts.json")
    for old in ('DATA = os.environ.get("DATA", "/data/accounts.json")',
                'DATA = "/data/accounts.json"'):
        if old in src:
            src = src.replace(old, "DATA = %r" % ledger)
            break
    assert ledger in src, "ledger redirect did not take effect"

    box = os.path.join(work, "mailbox")
    for old in ('MAILBOX = os.environ.get("MAILBOX", "/shared/mailbox")',
                'MAILBOX = "/shared/mailbox"'):
        if old in src:
            src = src.replace(old, "MAILBOX = %r" % box)
            break
    assert box in src, "mailbox redirect did not take effect"

    # Point the token files at a file we control. This is the redirect under
    # test: it must exist in the bank (the _token_file candidates), otherwise
    # the bank cannot be given a token at all.
    token_file = os.path.join(work, "admin-token")
    with open(token_file, "w") as f:
        f.write(TOKEN + "\n")
    anchor = '"/run/secrets/bank-admin-token",'
    assert anchor in src, ("bank.py has no multi-path token lookup "
                           "(_token_file); the fix is absent")
    src = src.replace(anchor, '%r,' % token_file, 1)
    assert token_file in src, "token path redirect did not take effect"

    open(path, "w", encoding="utf-8").write(src)
    return work, path, token_file


def serve(path, port, work):
    env = dict(os.environ, PORT=str(port), DECAY_SECONDS="100000",
               SEED="agent-1:1000", DATA=os.path.join(work, "accounts.json"))
    proc = subprocess.Popen([sys.executable, path], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    for _ in range(100):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/accounts" % port,
                                   timeout=1)
            return proc
        except Exception:
            time.sleep(0.05)
    proc.kill()
    raise SystemExit("bank did not start")


def call(port, route, body, token=None):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-Bank-Admin-Token"] = token
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, route),
                                 data=json.dumps(body).encode(), method="POST",
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def main():
    work, path, token_file = build()
    port = 8931
    proc = serve(path, port, work)
    results = {}
    try:
        # With the token file PRESENT, a caller who sends no token is
        # refused as forbidden (403) -- not 503. 503 means no token file
        # anywhere, which is the pre-fix symptom and is checked separately
        # below. Asserting 503 here would have been an assertion about the
        # fixture, not about the bank.
        st, body = call(port, "/admin/topup", {"agent": "agent-1", "amount": 5})
        results["no token supplied -> refused"] = (st in (401, 403))

        st, body = call(port, "/admin/topup", {"agent": "agent-1", "amount": 5},
                        token="wrong")
        results["a WRONG token is refused"] = (st == 403)
        req = urllib.request.Request(
            "http://127.0.0.1:%d/balance/agent-1" % port)
        with urllib.request.urlopen(req, timeout=5) as r:
            money_after_bad = json.loads(r.read())["money"]

        st, body = call(port, "/admin/topup", {"agent": "agent-1", "amount": 100},
                        token=TOKEN)
        results["the mounted token works"] = (st == 200
                                              and body.get("money") == 1100)
        results["a refused token moved no coins"] = (money_after_bad == 1000)
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        shutil.rmtree(work, ignore_errors=True)

    # --- the pre-fix symptom: NO token file at any candidate path --------
    # This is the observable that matters. On master the bank looked only in
    # /run/secrets, the UI mounted at /app, and the documented quickstart
    # mounted nothing at all -- so this is what an operator actually saw.
    work2, path2, token_file2 = build()
    os.remove(token_file2)          # the fix's lookup finds nothing now
    proc2 = serve(path2, 8932, work2)
    try:
        st, body = call(8932, "/admin/topup", {"agent": "agent-1", "amount": 5})
        results["no token file anywhere -> 503 unavailable"] = (
            st == 503 and "unavailable" in body.get("error", ""))
    finally:
        proc2.terminate()
        proc2.wait(timeout=5)
        shutil.rmtree(work2, ignore_errors=True)

    for k in sorted(results):
        print("  %-38s %s" % (k, "PASS" if results[k] else "FAIL"))
    ok = all(results.values())
    print("\n%s" % ("SELFTEST PASS" if ok else "SELFTEST FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
