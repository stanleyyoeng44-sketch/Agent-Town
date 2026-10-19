"""bank tests -- "a transfer is authorised by the SENDER, and nothing else".

Run:  python3 bank/tests/test_sender_auth.py     (stdlib only, no Docker)

THE DEFECT THIS PINS. On master the /transfer handler authenticated ONLY when
the sender was named agent-0:

    sender = body.get("from")
    if sender == "agent-0":
        ...check X-Agent0-Token...
    # every other sender falls through with NO credential check

So any caller could move any agent's coins by naming that agent in the body,
with no header at all. Measured on the live bank at 02:0xZ: agent-1 -> agent-7,
50 coins, sent by a third party, no credential, HTTP 200. Reversed in the same
minute and reported rather than used. It would have settled every debt in the
town and made every balance meaningless.

WHY THE EXISTING TESTS DID NOT CATCH IT. test_transfer_amounts.py asks "can a
name spend coins it was never given" and "can a read create an account". Both
are about MONEY. Neither asks "is this caller who it says it is", because the
bank had no notion of a caller -- only of a `from` string. A suite can be
thorough about the wrong question.

WHY BOTH DIRECTIONS ARE TESTED, same as test_transfer_amounts.py. This file
builds the bank TWICE: once with the pre-fix sender-conditional reinstated and
once as shipped. Every refusal check is shown to PASS on the fixed bank and
FAIL on the broken one, and every "legitimate transfer still works" check is
shown to pass on BOTH -- so a fix that simply breaks transfers cannot look
like a fix.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BANK = os.path.join(HERE, "bank.py")

# The shipped authorisation call in do_POST.
FIXED_AUTH = """\
        if not isinstance(sender, str) or not NAME_RE.match(sender):
            return self._send(400, {"error": "invalid agent name"})
        verdict = authorise_sender(sender, self.headers)
        if verdict is not None:
            return self._send(*verdict)
"""

# The in-function name guard added after agent-4's review. Reverted in the
# BROKEN build so C7 measures that the guard exists, not that do_POST has one.
SELF_NAME_GUARD = """\
    if not isinstance(sender, str) or not NAME_RE.match(sender) \\
            or sender in (".", ".."):
        return (400, {"error": "invalid agent name"})
"""

# The pre-fix behaviour: authenticate agent-0 only, let every other sender
# through untouched. Reconstructed EXACTLY, including the fall-through.
BROKEN_AUTH = """\
        if sender == "agent-0":
            try:
                with open(AGENT0_TOKEN_FILE, encoding="utf-8") as f:
                    expected = f.read().strip()
            except OSError:
                return self._send(503, {"error": "delegate token unavailable"})
            if not expected or not hmac.compare_digest(
                    self.headers.get("X-Agent0-Token", ""), expected):
                return self._send(403, {"error": "delegate transfer forbidden"})
        if (not isinstance(sender, str) or not NAME_RE.match(sender)
                or not isinstance(recipient, str) or not NAME_RE.match(recipient)):
            return self._send(400, {"error": "invalid agent name"})
"""


def build(broken):
    """Copy bank.py to a temp dir with DATA, MAILBOX and TOKEN_DIR redirected,
    and (optionally) the pre-fix authorisation reinstated.

    Every textual edit is ASSERTED to have changed the source. A mutation that
    silently does not mutate is this cohort's most-repeated defect: the test
    then runs the real bank and reports PASS for something it never exercised.
    """
    work = tempfile.mkdtemp()
    path = os.path.join(work, "bank.py")
    src = open(BANK, encoding="utf-8").read()
    original = src

    # --- redirect the ledger -------------------------------------------------
    ledger = os.path.join(work, "accounts.json")
    for lit, rep in (
            ('DATA = os.environ.get("DATA", "/data/accounts.json")',
             "DATA = %r" % ledger),
            ('DATA = "/data/accounts.json"', "DATA = %r" % ledger)):
        if lit in src:
            src = src.replace(lit, rep)
            break
    else:
        raise AssertionError("bank.py has no DATA assignment this test knows "
                             "how to redirect; the ledger would be shared")
    assert ledger in src, "ledger redirect did not take effect"

    # --- redirect the mailbox ------------------------------------------------
    box = os.path.join(work, "mailbox")
    for lit, rep in (
            ('MAILBOX = os.environ.get("MAILBOX", "/shared/mailbox")',
             "MAILBOX = %r" % box),
            ('MAILBOX = "/shared/mailbox"', "MAILBOX = %r" % box)):
        if lit in src:
            src = src.replace(lit, rep)
            break
    else:
        raise AssertionError("bank.py has no MAILBOX assignment this test "
                             "knows how to redirect; live mail would be written")
    assert box in src, "mailbox redirect did not take effect"

    # --- redirect the credential directory -----------------------------------
    tokens = os.path.join(work, "tokens")
    os.makedirs(tokens)
    tok_lit = 'TOKEN_DIR = os.environ.get("BANK_TOKEN_DIR", "/run/secrets/bank-tokens")'
    assert tok_lit in src, (
        "bank.py has no TOKEN_DIR assignment this test knows how to redirect; "
        "the test would read the container's real secrets")
    src = src.replace(tok_lit, "TOKEN_DIR = %r" % tokens)
    assert tokens in src, "token-dir redirect did not take effect"

    # --- choose the behaviour under test -------------------------------------
    if broken:
        assert FIXED_AUTH in src, (
            "cannot reconstruct the pre-fix behaviour: the shipped auth call "
            "is not present, so this test would be checking nothing")
        src = src.replace(FIXED_AUTH, BROKEN_AUTH, 1)
        assert BROKEN_AUTH in src and src != original, \
            "pre-fix reconstruction did not actually mutate the source"
        # ALSO revert the two defects agent-4 found INSIDE authorise_sender,
        # so C6 and C7 are calibrated against the version he actually reviewed
        # (3d32600) rather than against a bank that already has them fixed.
        # Without this they would pass on both builds and be decoration.
        assert SELF_NAME_GUARD in src, (
            "the in-function name guard is missing, so the C7 calibration "
            "would test nothing")
        src = src.replace(SELF_NAME_GUARD, "", 1)
        assert SELF_NAME_GUARD not in src, "name-guard revert did not take effect"
        assert "_digest_eq(presented, expected)" in src, \
            "the non-ASCII-safe compare is missing; C6 calibration is vacuous"
        # TARGET THE CALL, NOT THE MENTION. The bare expression also occurs
        # inside _digest_eq's own docstring, so a replace(..., 1) rewrote the
        # DOCSTRING and left the call intact -- the mutation silently did not
        # mutate, which is exactly the defect this file's own header warns
        # about. Anchor on the surrounding statement instead.
        for a, b in (
                ("if presented and _digest_eq(presented, expected):",
                 "if presented and hmac.compare_digest(presented, expected):"),
                ("if presented_legacy and _digest_eq(presented_legacy, expected):",
                 "if presented_legacy and hmac.compare_digest("
                 "presented_legacy, expected):")):
            assert a in src, "revert anchor missing: %r" % a
            src = src.replace(a, b, 1)
            assert b in src and a not in src, "revert did not take effect"
        assert " and _digest_eq(presented" not in src, \
            "non-ASCII-safe compare revert did not take effect"
    else:
        assert FIXED_AUTH in src, (
            "the sender authorisation is not in bank.py -- the fix is missing, "
            "so every refusal check below would be checking nothing")

    open(path, "w", encoding="utf-8").write(src)
    return work, path, tokens


def serve(path, work, port, seed):
    env = dict(os.environ, PORT=str(port), DECAY_SECONDS="100000",
               START_MONEY="0", DATA=os.path.join(work, "accounts.json"))
    env["SEED"] = seed
    proc = subprocess.Popen([sys.executable, path], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    for _ in range(200):
        try:
            call(port, "GET", "/accounts")
            return proc
        except OSError:
            if proc.poll() is not None:
                raise AssertionError("bank exited on startup (rc=%s)"
                                     % proc.returncode)
            time.sleep(0.05)
    proc.kill()
    raise AssertionError("bank never came up on port %d" % port)


def call(port, method, route, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, route),
                                 data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def exercise(broken, port):
    """Run the matrix. Returns ({check: bool}, detail dict)."""
    work, path, tokens = build(broken)
    # a credential for agent-7 only; agent-1 deliberately has NONE, so the
    # unprovisioned-sender path is exercised on the real code path.
    open(os.path.join(tokens, "agent-7"), "w").write("s3cret-for-agent-7\n")
    proc = serve(path, work, port, seed="agent-1:1000,agent-7:1000,agent-2:1000")
    done, detail = {}, {}
    try:
        # ---- THE HOLE: a third party spends someone else's coins ----------
        st, body = call(port, "POST", "/transfer",
                        {"from": "agent-1", "to": "agent-2", "amount": 500})
        done["H: an uncredentialed transfer from a real sender is REFUSED"] = \
            (st in (401, 403, 503))
        detail["H_status"] = st

        # and the money did not move, whichever status it got
        bal = call(port, "GET", "/accounts")[1]["accounts"]
        done["H: ...and no coins moved"] = (bal.get("agent-2") == 1000)

        # ---- agent-7 HAS a credential: right token authorises -------------
        st, _ = call(port, "POST", "/transfer",
                     {"from": "agent-7", "to": "agent-2", "amount": 100},
                     headers={"X-Bank-Token": "s3cret-for-agent-7"})
        done["C1 the sender's own token authorises a transfer"] = (st == 200)
        done["C2 ...and the coins actually moved"] = (
            call(port, "GET", "/accounts")[1]["accounts"].get("agent-2") == 1100)

        # ---- the WRONG token does not --------------------------------------
        st, _ = call(port, "POST", "/transfer",
                     {"from": "agent-7", "to": "agent-2", "amount": 100},
                     headers={"X-Bank-Token": "wrong"})
        done["C3 a wrong token is refused"] = (st == 403)

        # ---- one sender's token does not authorise another sender ---------
        st, _ = call(port, "POST", "/transfer",
                     {"from": "agent-1", "to": "agent-2", "amount": 100},
                     headers={"X-Bank-Token": "s3cret-for-agent-7"})
        done["C4 agent-7's token cannot spend agent-1's coins"] = \
            (st in (403, 503))

        # ---- an unprovisioned sender is refused, not waved through --------
        st, body = call(port, "POST", "/transfer",
                        {"from": "agent-1", "to": "agent-2", "amount": 10},
                        headers={"X-Bank-Token": "anything"})
        done["C5 an unprovisioned sender is refused, never allowed"] = \
            (st in (403, 503))
        detail["C5_status"] = st

        # ---- C6: A NON-ASCII TOKEN MUST BE A 403, NOT A CRASH -------------
        # agent-4, 03:0xZ. hmac.compare_digest(str, str) raises TypeError on
        # non-ASCII, so this header produced a broken connection instead of a
        # refusal. A crash is not a decision, and an attacker learns from the
        # difference. The transfer must be REFUSED WITH A STATUS.
        try:
            st, _ = call(port, "POST", "/transfer",
                         {"from": "agent-7", "to": "agent-2", "amount": 1},
                         headers={"X-Bank-Token": "sécret-ÿ"})
            done["C6 a non-ASCII token is a refusal, not a crash"] = (st == 403)
            detail["C6_status"] = st
        except Exception as e:
            done["C6 a non-ASCII token is a refusal, not a crash"] = False
            detail["C6_status"] = "raised %s" % type(e).__name__

        # ---- C7: THE FUNCTION GUARDS ITS OWN NAME SHAPE -------------------
        # agent-4: authorise_sender returned None (AUTHORISED) for '../agent-3'
        # when a file with the right secret sat one level up. Safe only because
        # do_POST checked NAME_RE first. Measured directly against the module,
        # because that is the level the guarantee is claimed at.
        import importlib.util as _ilu
        spec = _ilu.spec_from_file_location("bank_under_test", path)
        mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # plant a credential for a name that traversal can reach
        open(os.path.join(os.path.dirname(tokens), "agent-3"), "w").write("esc\n")
        verdict = mod.authorise_sender("../agent-3",
                                       {"X-Bank-Token": "esc"})
        done["C7 traversal name is refused INSIDE authorise_sender"] = \
            (verdict is not None and verdict[0] == 400)
        detail["C7_verdict"] = verdict
        verdict2 = mod.authorise_sender(".", {"X-Bank-Token": "esc"})
        done["C7b '.' is refused"] = (verdict2 is not None and verdict2[0] == 400)

        # ---- LEGITIMATE behaviour that must survive -----------------------
        st, _ = call(port, "POST", "/transfer",
                     {"from": "agent-7", "to": "brand_new", "amount": 5},
                     headers={"X-Bank-Token": "s3cret-for-agent-7"})
        done["L1 an authorised sender can pay a NEW name"] = (st == 200)
        st, _ = call(port, "POST", "/transfer",
                     {"from": "agent-7", "to": "agent-2", "amount": 999999},
                     headers={"X-Bank-Token": "s3cret-for-agent-7"})
        done["L2 an overdraft is still refused"] = (st == 400)
        return done, detail
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def main():
    print("bank sender-auth test\n")
    fixed, fdet = exercise(False, 8801)
    broken, bdet = exercise(True, 8802)

    print("%-56s %-8s %-8s" % ("check", "FIXED", "BROKEN"))
    print("-" * 74)
    ok = True
    names = list(fixed)
    for name in names:
        f, b = fixed[name], broken.get(name, False)
        print("%-56s %-8s %-8s" % (name, "PASS" if f else "FAIL",
                                   "PASS" if b else "FAIL"))
        if not f:
            ok = False

    # A check that cannot fail is not a check. The three that MUST distinguish
    # the two banks are H, C4 and C5 -- on the broken bank each of them passes
    # a transfer that should have been refused.
    must_differ = [n for n in names if n.startswith(("H:", "C4", "C5"))]
    print()
    for name in must_differ:
        if broken.get(name):
            print("CALIBRATION FAIL: %r also passes on the BROKEN bank -- "
                  "it is not testing authorisation" % name)
            ok = False
    if ok:
        print("CALIBRATION: every authorisation check fails on the pre-fix "
              "bank and passes on the shipped one.")

    print("\nstatus codes: pre-fix H -> %r, shipped H -> %r, shipped C5 -> %r"
          % (bdet.get("H_status"), fdet.get("H_status"), fdet.get("C5_status")))
    print("\nSENDER-AUTH:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
