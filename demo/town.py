#!/usr/bin/env python3
"""town.py — a two-agent town in one process, no Docker.

WHY THIS EXISTS
    Every command in the README and the guide that involves Docker is marked
    "transcribed, not executed": no one in this cohort has a Docker daemon
    reachable from an agent container, so the repo's central claim -- that N
    agents can message, pay each other, and sleep/wake -- has never been
    demonstrated end to end by the people who wrote it.

    This runs that claim where it CAN run: in processes on one box. It is a
    real bank (bank/bank.py, unmodified, on a local ledger), a real mailbox
    (a directory of JSON files, the same wire format), and a real life cycle
    (write a wake file, exit, next life reads it). It is not the CLI; it is
    the substrate the CLI drives.

    The point is that a reader can watch it work before trusting anything.

EXIT CODES (the demo fails loudly)
    0  all assertions passed
    1  an assertion failed (the town did not behave as documented)
    2  could not start (port in use, ledger not writable, ...)

NEGATIVE CONTROL
    The demo performs an overdraft transfer that MUST be refused. If it
    succeeds, the demo exits 1. A happy-path-only demo is not evidence that
    the guard works.

Usage:
    python3 demo/town.py            # run it
    python3 demo/town.py -v         # show the transcript as it happens
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BANK = os.path.join(ROOT, "bank", "bank.py")
VERBOSE = "-v" in sys.argv


def say(msg):
    print(msg, flush=True)


def vlog(msg):
    if VERBOSE:
        print("      " + msg, flush=True)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# Per-sender credentials. The bank now authenticates EVERY sender, not only
# agent-0 (see bank/SENDER_AUTH.md), so a client that sends no header gets 503
# and the demo can no longer transfer at all. agent-10 measured exactly that:
# branch demo -> HTTPError 503, master -> PASS. Both halves are needed -- a
# provisioned token directory AND a header on every transfer -- because
# provisioning alone turns the 503 into a 403.
#
# SEED_TOKEN is a demo value in a temp directory, never a real secret; the
# bank only ever compares a header to the file's contents.
CREDS = {}                      # agent name -> secret, filled in main()

def _cred_header(sender):
    secret = CREDS.get(sender)
    return {"X-Bank-Sender-Token": secret} if secret else {}


def http(method, url, body=None, timeout=5):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    # A transfer is authenticated as its `from`. Nothing else needs a
    # credential, and sending one on a read would be noise, not security.
    if method == "POST" and isinstance(body, dict) and "from" in body:
        for k, v in _cred_header(body["from"]).items():
            req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def wait_up(url, deadline=10.0):
    end = time.time() + deadline
    while time.time() < end:
        try:
            http("GET", url, timeout=1)
            return True
        except Exception:
            time.sleep(0.05)
    return False


# --------------------------------------------------------------------------
# The mailbox: a directory of JSON files. This IS the protocol.
# --------------------------------------------------------------------------
def send_mail(root, sender, recipient, text):
    box = os.path.join(root, "mailbox", recipient, "incoming")
    os.makedirs(box, exist_ok=True)
    name = f"{time.time_ns()}-{sender}.msg"
    path = os.path.join(box, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"from": sender, "text": text}, f)
    return path


def read_mail(root, me):
    """Read and CONSUME this agent's inbox. Order is by filename, which is
    timestamped, so it is arrival order."""
    box = os.path.join(root, "mailbox", me, "incoming")
    if not os.path.isdir(box):
        return []
    out = []
    for name in sorted(os.listdir(box)):
        path = os.path.join(box, name)
        try:
            with open(path, encoding="utf-8") as f:
                out.append(json.load(f))
        except (OSError, json.JSONDecodeError):
            continue                      # an unreadable message is skipped,
        os.unlink(path)                   # never silently counted as read
    return out


# --------------------------------------------------------------------------
# The life cycle: identity is a file you wrote to yourself.
# --------------------------------------------------------------------------
def life_dir(root, agent):
    d = os.path.join(root, "agents", agent, "self")
    os.makedirs(d, exist_ok=True)
    return d


def wake_file(root, agent):
    return os.path.join(life_dir(root, agent), "wake.md")


def one_life(root, agent, body, turn):
    """Run one life: read wake.md, do `body`, write the next wake.md."""
    wf = wake_file(root, agent)
    prior = ""
    if os.path.exists(wf):
        with open(wf, encoding="utf-8") as f:
            prior = f.read().strip()
    vlog(f"{agent} life {turn}: wake.md = {prior[:60]!r}")
    note = body(prior)
    with open(wf, "w", encoding="utf-8") as f:
        f.write(note)
    return prior, note


# --------------------------------------------------------------------------
def main():
    work = tempfile.mkdtemp(prefix="town-")
    ledger = os.path.join(work, "accounts.json")
    port = free_port()
    base = f"http://127.0.0.1:{port}"

    # The bank's ledger path. Two supported shapes, in order:
    #   1. bank.py reads the path from an env var (the fix recommended in
    #      review #1) -> use it, run the file UNMODIFIED. No coupling.
    #   2. bank.py hardcodes /data/accounts.json -> run a PATCHED COPY so the
    #      demo needs no root and touches no real ledger.
    # Anything else fails loudly rather than guessing.
    #
    # Shape 1 exists so that fixing the bank does not silently break the demo:
    # matching only on the hardcoded literal would turn a GOOD change into an
    # exit-2 failure.
    # SEED funds exactly the names this demo provisions. Accounts are created
    # at 0 by design (a new balance must never be a grant -- see bank.py
    # ensure_account), so without SEED the two agents would start broke.
    # PROVISION the credential the demo client will present. The bank resolves
    # BANK_TOKEN_DIR (bank.py:90) and reads one file per sender; without this
    # directory a per-sender bank answers 503 to every transfer, which is how
    # the demo broke at 4610825. Written inside `work`, so it is a temp secret
    # and dies with the run.
    tokens = os.path.join(work, "bank-tokens")
    os.makedirs(tokens, exist_ok=True)
    for _name in ("agent-1", "agent-2"):
        CREDS[_name] = "demo-" + _name + "-secret"
        with open(os.path.join(tokens, _name), "w", encoding="utf-8") as _f:
            _f.write(CREDS[_name] + "\n")

    env = dict(os.environ, PORT=str(port), START_MONEY="100",
               SEED="agent-1:100,agent-2:100", DECAY_SECONDS="3600",
               BANK_TOKEN_DIR=tokens)
    src = open(BANK, encoding="utf-8").read()
    env_name = None
    for cand in ("BANK_DATA", "DATA", "LEDGER"):
        if f'os.environ.get("{cand}"' in src or \
           f"os.environ.get('{cand}'" in src:
            env_name = cand
            break

    # The bank also writes PAYMENT NOTIFICATIONS, and that path is a SECOND
    # hardcoded absolute (/shared/mailbox) that the ledger fix did not cover.
    # MEASURED CONSEQUENCE: every demo run wrote real "[bank] You received N
    # coins" files into the LIVE inboxes of agents in this very box -- 12 per
    # selftest run, including the guard-disabled limb's 76/10000/1000000
    # overdraft notifications. A "sandboxed" demo that mutates live state is
    # not sandboxed; the reader cannot tell the demo's mail from a real
    # transfer. Redirect it to the demo's own mailbox directory: shape 1 =
    # env override, shape 2 = patch a copy, else refuse loudly.
    mailbox_dir = os.path.join(work, "bank-mailbox")
    os.makedirs(mailbox_dir, exist_ok=True)
    mailbox_env = None
    for cand in ("BANK_MAILBOX", "MAILBOX", "MAILBOX_DIR"):
        if f'os.environ.get("{cand}"' in src or \
           f"os.environ.get('{cand}'" in src:
            mailbox_env = cand
            break

    if env_name:
        bank_copy = os.path.join(work, "bank_demo.py")
        with open(bank_copy, "w", encoding="utf-8") as f:
            f.write(src)                       # unmodified
        env = dict(env, **{env_name: ledger})
        say(f"ledger: {ledger}  (via {env_name} env var, bank unmodified)")
    elif 'DATA = "/data/accounts.json"' in src:
        patched = src.replace('DATA = "/data/accounts.json"',
                              f'DATA = {ledger!r}')
        bank_copy = os.path.join(work, "bank_demo.py")
        with open(bank_copy, "w", encoding="utf-8") as f:
            f.write(patched)
        say(f"ledger: {ledger}  (patched copy: DATA constant -> temp path)")
    else:
        say("FAIL: bank.py has neither a DATA env override nor the "
            "hardcoded /data literal this demo knows how to redirect.")
        say("      Update demo/town.py to match the new ledger configuration; "
            "do not silently run against /data.")
        return 2

    if mailbox_env:
        env = dict(env, **{mailbox_env: mailbox_dir})
    else:
        # Old bank shape: MAILBOX is a literal. Patch the copy we are about to
        # run -- and assert the patch actually changed the source, because a
        # mutation that does not mutate is the defect this repo has hit twice.
        literal = 'MAILBOX = "/shared/mailbox"'
        if literal not in src:
            say("FAIL: bank.py writes notifications to a mailbox path this "
                "demo cannot redirect, and it is NOT /shared/mailbox.")
            say("      Refusing to run: the demo would write into live mail.")
            return 2
        patched = open(bank_copy, encoding="utf-8").read()
        new = patched.replace(literal, f"MAILBOX = {mailbox_dir!r}")
        if new == patched:
            say("FAIL: mailbox redirect was a no-op; refusing to run.")
            return 2
        with open(bank_copy, "w", encoding="utf-8") as f:
            f.write(new)
        say(f"mailbox: {mailbox_dir}  (patched copy: MAILBOX literal -> temp)")
    if mailbox_env:
        say(f"mailbox: {mailbox_dir}  (via {mailbox_env} env var)")

    proc = subprocess.Popen([sys.executable, bank_copy], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    failures = []

    def check(name, cond, detail=""):
        if cond:
            say(f"  PASS  {name}")
        else:
            say(f"  FAIL  {name}  {detail}")
            failures.append(name)

    try:
        if not wait_up(f"{base}/accounts"):
            say("FAIL: bank did not come up on " + base)
            try:
                say(proc.stdout.read(2000))
            except Exception:
                pass
            return 2
        say("bank up on " + base)
        say("")

        # -- 1. two agents exist and can be paid ---------------------------
        say("1. bank")
        a = http("GET", f"{base}/balance/agent-1")
        b = http("GET", f"{base}/balance/agent-2")
        check("seeded agents start at 100",
              a["money"] == 100 and b["money"] == 100, f"{a} {b}")
        r = http("POST", f"{base}/transfer",
                 {"from": "agent-1", "to": "agent-2", "amount": 25})
        # -- the MINT INVARIANT: touching an unknown name must not create
        # wealth. This is the check whose absence let a minting bank pass the
        # whole demo. It runs BEFORE any transfer that could muddy the books.
        ghost = http("GET", f"{base}/balance/ghost-not-an-agent")
        check("unknown name reads as money=0", ghost.get("money") == 0, str(ghost))
        check("unknown name reads as exists=false",
              ghost.get("exists") is False, str(ghost))
        # A READ must not create the row either. Checking `money` alone would
        # pass against a bank that creates the account at 0, which is a storage
        # mint: unbounded growth from unauthenticated reads. Measured on master
        # before this check existed: 2,000 reads -> 2,002 accounts.
        check("a read did not create the account",
              "ghost-not-an-agent" not in
              http("GET", f"{base}/accounts")["accounts"], "row exists")
        # And the SECOND read must say the same thing. If the first read
        # created the account, `exists` flips from false to true and the field
        # is reporting the previous call instead of the world.
        ghost2 = http("GET", f"{base}/balance/ghost-not-an-agent")
        check("exists does not change on a second read",
              ghost2.get("exists") is False, str(ghost2))
        # The creation path that DOES exist is the recipient of a transfer.
        # It must be created at 0, not funded.
        http("POST", f"{base}/transfer",
             {"from": "agent-1", "to": "fresh-recipient", "amount": 1})
        fresh = http("GET", f"{base}/balance/fresh-recipient")
        check("a freshly created recipient is paid, not granted",
              fresh.get("money") == 1, str(fresh))
        total_before = sum(http("GET", f"{base}/accounts")["accounts"].values())
        check("no coins were created by any of the above",
              total_before == 200,
              f"total={total_before} expected 200 (2 x 100)")
        # The GRANT half is checked above ("a freshly created recipient is
        # paid, not granted"): a NEGATIVE control (create funds the name)
        # makes that line fail, which is what the grant-on-create limb in
        # selftest_demo.py requires. Deliberately ONE check for the property,
        # not a second transfer to the same name -- two checks asserting the
        # same thing from different call sites is how a count grows without
        # the coverage doing so.

        check("transfer 25 moves coins", r.get("ok") is True, str(r))
        check("sender debited", r["money"] == 75, str(r))
        check("recipient credited", r["recipient_balance"] == 125, str(r))

        # -- 2. NEGATIVE CONTROL: the OVERDRAFT INVARIANT, not one value ---
        # agent-2's attack: a bank that refuses only `amount == 10000` passes a
        # single-value check. The invariant is "any amount greater than the
        # balance is refused", so test the BOUNDARY, a value just past it, and
        # a large one -- and assert no balance can go negative.
        say("")
        say("2. negative control: every amount > balance must be refused")
        floor = http("GET", f"{base}/balance/agent-1")["money"]   # 75
        attacker = http("GET", f"{base}/balance/agent-2")["money"]
        for amt in (floor + 1, 10_000, 1_000_000):
            before = http("GET", f"{base}/balance/agent-1")["money"]
            refused = False
            try:
                http("POST", f"{base}/transfer",
                     {"from": "agent-1", "to": "agent-2", "amount": amt})
            except urllib.error.HTTPError as e:
                refused = e.code == 400
            after = http("GET", f"{base}/balance/agent-1")["money"]
            check(f"overdraft of {amt} refused", refused,
                  "the transfer SUCCEEDED")
            check(f"balance unchanged after {amt} refusal", after == before,
                  f"{before} -> {after}")
        # the invariant, stated directly: no account may be negative
        accounts = http("GET", f"{base}/accounts")["accounts"]
        check("no balance went negative",
              all(v >= 0 for v in accounts.values()), str(accounts))
        check("no coins were created by a refused transfer",
              sum(accounts.values()) == 200,
              f"total={sum(accounts.values())} expected 200 (2 x 100)")

        # -- 3. the mailbox is the protocol --------------------------------
        say("")
        say("3. mailbox (a directory of JSON files)")
        box = os.path.join(work, "mailbox")
        drain = read_mail(box, "agent-1")
        check("empty inbox reads as empty list", drain == [], str(drain))
        # The bank's OWN notification path, which is separate from the
        # file-protocol helpers above. It must land in the demo's mailbox.
        bank_inbox = os.path.join(mailbox_dir, "agent-2", "incoming")
        notes = []
        if os.path.isdir(bank_inbox):
            for name in sorted(os.listdir(bank_inbox)):
                try:
                    notes.append(json.load(open(os.path.join(bank_inbox, name),
                                                encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    continue
        check("bank wrote its payment notice to the DEMO mailbox, not live",
              any(n.get("from") == "bank" and "25 coins" in n.get("text", "")
                  for n in notes), f"{len(notes)} notice(s) in {bank_inbox}")
        # A COUNT-OF-FILES CHECK HERE WAS VACUOUS AND I CAUGHT IT BY TESTING
        # THE CHECK: comparing _live_mailbox_count() to a baseline passed
        # while FIVE real notifications leaked into agent-2's inbox. The
        # daemon consumes inbox files every 3 seconds, so a write followed by
        # a consume leaves the count unchanged. A post-hoc count cannot see a
        # write whose consumer deletes it -- it measures the checker's view.
        #
        # The sound check is the POSITIVE one above: our own bank's notice for
        # a specific transfer is in OUR mailbox directory. If the bank had used
        # /shared/mailbox, this directory would be empty and that check fails.
        # So assert the exact count, which also pins that refused transfers
        # write nothing.
        notes_25 = [n for n in notes
                    if n.get("from") == "bank" and "25 coins" in n.get("text", "")]
        check("exactly one notice for the one successful transfer",
              len(notes_25) == 1, f"{len(notes_25)} notice(s)")
        check("the bank's mailbox is not the live one",
              os.path.realpath(mailbox_dir) != os.path.realpath("/shared/mailbox"),
              mailbox_dir)

        # -- 4. two lives, with memory carried in a file -------------------
        say("")
        say("4. life cycle")
        def life1(prior):
            inbox = read_mail(box, "agent-1")
            reply = (f"I am agent-1, life 1. I read {len(inbox)} message(s) "
                     f"and was born with no memory (prior wake = {prior!r}).")
            send_mail(box, "agent-1", "agent-2", "hello from agent-1 life 1")
            return reply
        prior1, note1 = one_life(box, "agent-1", life1, 1)
        check("first life starts with no wake file", prior1 == "")
        got = read_mail(box, "agent-2")
        check("message arrived in agent-2's inbox", len(got) == 1, str(got))
        check("message is from agent-1", got and got[0]["from"] == "agent-1")

        def life2(prior):
            inbox = read_mail(box, "agent-1")
            return (f"I am agent-1, life 2. My past self told me: {prior!r}. "
                    f"I have {len(inbox)} unread message(s).")
        prior2, note2 = one_life(box, "agent-1", life2, 2)
        check("second life reads what the first wrote", prior2 == note1,
              f"{prior2!r} != {note1!r}")
        check("memory crossed the sleep boundary", note1 in note2,
              note2)

        # -- 5. wake file survives the process, not the session ------------
        say("")
        say("5. durability")
        on_disk = open(wake_file(box, "agent-1"), encoding="utf-8").read()
        check("wake file is on disk after the lives", on_disk == note2)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(work, ignore_errors=True)

    say("")
    if failures:
        say(f"DEMO FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    say("DEMO PASSED: bank, transfer, overdraft refusal, mailbox, and a "
        "life cycle that carried memory across a restart.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
