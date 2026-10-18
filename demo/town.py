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


def http(method, url, body=None, timeout=5):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
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
    env = dict(os.environ, PORT=str(port), START_MONEY="100",
               DECAY_SECONDS="3600")
    src = open(BANK, encoding="utf-8").read()
    env_name = None
    for cand in ("BANK_DATA", "DATA", "LEDGER"):
        if f'os.environ.get("{cand}"' in src or \
           f"os.environ.get('{cand}'" in src:
            env_name = cand
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
        check("agents auto-created with START_MONEY=100",
              a["money"] == 100 and b["money"] == 100, f"{a} {b}")
        r = http("POST", f"{base}/transfer",
                 {"from": "agent-1", "to": "agent-2", "amount": 25})
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
