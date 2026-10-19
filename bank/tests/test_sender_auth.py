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


def build(broken, prior_tip=False):
    """Copy bank.py to a temp dir with DATA, MAILBOX and TOKEN_DIR redirected,
    and (optionally) an EARLIER version's behaviour reinstated.

    broken=True      -> the PRE-FIX (master) behaviour: the sender gate is
                        absent, so these checks are calibrated against the
                        bank the security fix was written to replace.
    prior_tip=True   -> ONLY the two defects agent-6 found at 03:4xZ are put
                        back, leaving the per-sender gate in place. C8/C9 need
                        THIS control, not master: on master every transfer
                        with no credential is a 200, so "both headers -> 200"
                        is true of the broken bank too, for the unrelated
                        reason that it authenticates nobody. Passing on both
                        builds is decoration, and the whole-array case in
                        particular HAS to be measured against a bank that
                        does authenticate senders.

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

    # --- redirect the ADMIN token -------------------------------------------
    # C9 drives /admin/topup through HTTP. Without this the test reads the
    # container's real /run/secrets path, which does not exist here, so EVERY
    # admin call answers 503 "token unavailable" -- and C9 would "pass" its
    # 403 assertion by measuring a 503, or fail on both builds for a reason
    # that has nothing to do with the compare. agent-10 hit exactly this and
    # correctly refused to sign E2/E3. A redirect that is not asserted is how
    # a check silently measures the wrong thing.
    admin = os.path.join(work, "bank-admin-token")
    open(admin, "w", encoding="utf-8").write("ADMIN-SECRET\n")
    admin_lit = ('ADMIN_TOKEN_FILE = _token_file("/run/secrets/bank-admin-token",\n'
                 '                               "/app/bank-admin-token")')
    assert admin_lit in src, (
        "bank.py has no ADMIN_TOKEN_FILE assignment this test knows how to "
        "redirect; the admin path would be measured against a missing file")
    src = src.replace(admin_lit, "ADMIN_TOKEN_FILE = %r" % admin, 1)
    assert admin in src, "admin-token redirect did not take effect"

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
        # about.
        # REVERT C6 FIRST, on its own, before C8 changes the loop's shape:
        # C8's rewrite subsumes the C6 anchor, and a revert that assumes the
        # order of other reverts is a cascade -- one edit invalidating the
        # next is how the whole-array case bit us. Each defect is reverted
        # independently and asserted on its own.
        c6_anchor = "        if presented and _digest_eq(presented, expected):"
        assert c6_anchor in src, (
            "C6 calibration: the per-header _digest_eq call is not in bank.py, "
            "so the non-ASCII crash cannot be reconstructed")
        src = src.replace(
            c6_anchor,
            "        if presented and hmac.compare_digest(presented, expected):", 1)
        assert c6_anchor not in src, "C6 revert did not take effect"

    # --- C8/C9 control: the PREVIOUS TIP, not master ------------------------
    # agent-6 found both of these at 03:4xZ, i.e. they shipped AT 4610825 and
    # are absent from master only because master has no per-sender gate at
    # all. On master, "both headers -> 200" is TRUE for the unrelated reason
    # that master authenticates nobody, so a master control makes C8 pass on
    # both builds -- a PASS true of the wrong tool. Revert them on whatever
    # build is under test, leaving the gate in place.
    if broken or prior_tip:
        both_lit = ('    presented_all = [headers.get("X-Bank-Token", ""),\n'
                    '                     headers.get("X-Bank-Sender-Token", "")]\n')
        assert both_lit in src, (
            "C8 calibration: the all-headers list is not in bank.py, so the "
            "both-headers defect cannot be reconstructed")
        src = src.replace(
            both_lit,
            '    presented_all = [headers.get("X-Bank-Token", "")\n'
            '                     or headers.get("X-Bank-Sender-Token", "")]\n', 1)
        assert both_lit not in src, "C8 revert did not take effect"
        admin_lit = ('            if not expected or not _digest_eq(\n'
                     '                    self.headers.get("X-Bank-Admin-Token", ""), '
                     'expected):')
        assert admin_lit in src, (
            "C9 calibration: the admin _digest_eq call is not in bank.py, so "
            "the non-ASCII admin crash cannot be reconstructed")
        src = src.replace(
            admin_lit,
            "            if not expected or not hmac.compare_digest(\n"
            '                    self.headers.get("X-Bank-Admin-Token", ""), '
            "expected):", 1)
        assert admin_lit not in src, "C9 revert did not take effect"
    elif not broken:
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


def exercise(broken, port, prior_tip=False):
    """Run the matrix. Returns ({check: bool}, detail dict)."""
    work, path, tokens = build(broken, prior_tip=prior_tip)
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

        # ---- C8: BOTH HEADERS SENT, ONE WRONG ----------------------------
        # agent-6, 03:4xZ. The "accept either" feature used `or`, i.e. by
        # PRECEDENCE, so a client that sends BOTH (the compatible client this
        # exists for) was judged only on X-Bank-Token. A correct secret in the
        # other header must still authorise. On the pre-fix bank this is a 403.
        st, _ = call(port, "POST", "/transfer",
                     {"from": "agent-7", "to": "agent-2", "amount": 1},
                     headers={"X-Bank-Token": "WRONG",
                              "X-Bank-Sender-Token": "s3cret-for-agent-7"})
        done["C8 either header: a correct secret wins over a wrong one"] = \
            (st == 200)
        detail["C8_status"] = st
        # ...and a wrong secret in BOTH is still refused (no accidental allow)
        st, _ = call(port, "POST", "/transfer",
                     {"from": "agent-7", "to": "agent-2", "amount": 1},
                     headers={"X-Bank-Token": "WRONG",
                              "X-Bank-Sender-Token": "ALSO-WRONG"})
        done["C8b ...while two wrong secrets are still refused"] = (st == 403)

        # ---- C9: A NON-ASCII ADMIN TOKEN IS A 403, NOT A CRASH -----------
        # Same class as C6, in the inline copy agent-6 found at the admin
        # path: hmac.compare_digest(str, str) raises on non-ASCII. Driven
        # through the HTTP surface, because that is where the agent saw the
        # dropped connection.
        try:
            st, _ = call(port, "POST", "/admin/topup",
                         {"agent": "agent-2", "amount": 1},
                         headers={"X-Bank-Admin-Token": "sècret-ÿ"})
            # Exactly 403. (401,403) would also accept "503 unavailable",
            # which is what an unprovisioned admin file returns -- a check
            # that accepts the wrong tool's answer. C9c below pins the RIGHT
            # token to 200 so a build that simply refuses everything cannot
            # pass this row by refusing everything.
            done["C9 a non-ASCII admin token is a 403, not a crash"] = (st == 403)
            detail["C9_status"] = st
        except Exception as e:
            done["C9 a non-ASCII admin token is a 403, not a crash"] = False
            detail["C9_status"] = "raised %s" % type(e).__name__
        # the server must still be serving after that request
        try:
            st, _ = call(port, "GET", "/accounts")
            done["C9b ...and the server survived the attempt"] = (st == 200)
        except Exception as e:
            done["C9b ...and the server survived the attempt"] = False
            detail["C9b"] = "raised %s" % type(e).__name__
        # C9c is the CONTROL for C9: the RIGHT admin token must WORK. Without
        # it, a bank that 403s every admin request -- including one that
        # never reaches the compare -- would pass C9 for the wrong reason.
        st, _ = call(port, "POST", "/admin/topup",
                     {"agent": "agent-2", "amount": 1},
                     headers={"X-Bank-Admin-Token": "ADMIN-SECRET"})
        done["C9c CONTROL: the correct admin token still tops up"] = (st == 200)
        detail["C9c_status"] = st

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
    # C8/C9 were introduced at 4610825 and their control is the PREVIOUS TIP,
    # not master: on master no sender is authenticated, so "both headers ->
    # 200" is true there for the wrong reason. Measured against the previous
    # tip, the row distinguishes the fix from the bug it fixes.
    tip, tdet = exercise(False, 8803, prior_tip=True)

    print("%-56s %-8s %-8s %-8s" % ("check", "FIXED", "BROKEN", "PREV-TIP"))
    print("-" * 84)
    ok = True
    names = list(fixed)
    for name in names:
        f, b = fixed[name], broken.get(name, False)
        t = tip.get(name, False)
        print("%-56s %-8s %-8s %-8s" % (name, "PASS" if f else "FAIL",
                                        "PASS" if b else "FAIL",
                                        "PASS" if t else "FAIL"))
        if not f:
            ok = False

    # C8/C9 MUST be green on the shipped bank and red on the previous tip,
    # since that is the only build that differs from them in the relevant way.
    for name in names:
        if name.startswith(("C8 ")):
            if tip.get(name):
                print("CALIBRATION FAIL: %r also passes on the PREVIOUS TIP "
                      "-- it does not measure the both-headers fix" % name)
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
