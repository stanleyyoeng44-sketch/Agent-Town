"""Agent bank: per-agent accounts, automatic decay, transfer API.

Standalone container (see Dockerfile next to this file). Agents talk to it
over HTTP at http://host.docker.internal:8790 — the same pattern as the
model proxy. The ledger lives in this container's /data volume, which NO
agent container mounts, so balances cannot be forged from agent-land.

Rules:
  - Accounts are created on first touch at ZERO. Creating an account never
    grants coins: a "new balance" is not a grant, so no endpoint is a mint.
    Coins enter an account only through SEED at startup (an operator act,
    e.g. SEED="agent-0:9290,agent-1:1000"; a bare name means START_MONEY) or
    through /admin/topup (admin token). See ensure_account/seed.
  - Every account loses 1 coin per DECAY_SECONDS (default 60s). Elapsed time
    is computed from a stored timestamp, so bank restarts do not lose or
    skip decay.
  - POST /transfer moves coins atomically and writes a notification into
    the recipient's mailbox on the shared volume, using the same wire
    format as agent-to-agent chat (MCP-INTERFACES.md §3.2).

The bank also mounts the agent-mailbox volume at /shared, write-only in
practice (it only writes /shared/mailbox/<agent>/incoming/*.msg). It runs
as uid 1000, the same numeric uid as the agents' user, which is what makes
the shared-volume writes work.
"""

import hmac
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8790"))
START_MONEY = int(os.environ.get("START_MONEY", "1000"))
DECAY_SECONDS = int(os.environ.get("DECAY_SECONDS", "60"))
# DATA is env-configurable so a reader can run a second bank, or verify the
# README bank section, without editing source. Default unchanged, so the
# container path is identical to before.
DATA = os.environ.get("DATA", "/data/accounts.json")
# MAILBOX is env-configurable for the SAME reason as DATA, and it was missed
# when DATA was fixed: a demo or test that redirects only the ledger still
# writes real payment notifications into every live agent's inbox. Default
# unchanged, so the container path is identical to before.
MAILBOX = os.environ.get("MAILBOX", "/shared/mailbox")
# The operator token is looked for wherever it may have been mounted: the
# documented /run/secrets path, and the path the control UI generates and can
# mount (/app). First existing file wins; with neither, the gated route
# answers 503 (refused), it does not crash.
def _token_file(*candidates: str) -> str:
    for path in candidates:
        if os.path.isfile(path):
            return path
    return candidates[0]


ADMIN_TOKEN_FILE = _token_file("/run/secrets/bank-admin-token",
                               "/app/bank-admin-token")
AGENT0_TOKEN_FILE = _token_file("/run/secrets/agent0-transfer-token",
                                "/app/agent0-transfer-token")
# PER-SENDER CREDENTIALS. The hole this closes, measured on master:
#
#   if sender == "agent-0":
#       ...check X-Agent0-Token...
#   # every other sender falls through to the transfer body with NO credential
#
# Only the sender NAMED agent-0 was authenticated. Any caller could POST
# {"from": "agent-7", "to": "agent-0", "amount": 5000} with no header at all
# and the bank would move agent-7's coins. This was proved on the live bank
# at 02:0xZ with one 50-coin transfer (agent-1 -> agent-7) sent by a third
# party, then reversed. It was reported, not exploited.
#
# The invariant that was missing is: A TRANSFER IS AUTHORISED BY THE SENDER,
# AND BY NOTHING ELSE. "sender == agent-0" is a special case of that, not an
# exception to it.
#
# WHERE THE SECRETS LIVE. A directory of one file per agent, named for the
# account. The bank cannot read an agent's container, so this is the operator
# (or the control plane) provisioned path; the agent's own MCP client reads
# the same file and sends it as X-Bank-Token. If the file for a sender is
# ABSENT the transfer is REFUSED (503), never allowed -- an unprovisioned
# agent loses the ability to spend rather than gaining the ability to spend
# anyone's coins.
#
# agent-0 keeps X-Agent0-Token as well, so the provisioned /app token that is
# already mounted and already works keeps working. Both are checked; either
# one passing authorises agent-0. That is deliberate: this fix must not
# break the one route that is known to work today.
TOKEN_DIR = os.environ.get("BANK_TOKEN_DIR", "/run/secrets/bank-tokens")
# The one agent whose credential has a legacy fixed location.
LEGACY_TOKEN_AGENT = "agent-0"
EXEMPT_ACCOUNTS = frozenset({"agent-0"})
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,60}$")

_lock = threading.Lock()
_accounts: dict[str, int] = {}
_last_decay: float | None = None


def _read_secret(path):
    """The secret at `path`, stripped, or None. A missing/unreadable file is
    None -- and None means REFUSE, never ALLOW."""
    try:
        with open(path, encoding="utf-8") as f:
            s = f.read().strip()
        return s or None
    except OSError:
        return None


def _digest_eq(presented, expected):
    """Constant-time compare that cannot raise.

    DEFECT FOUND BY agent-4 (2026-10-19 03:0xZ): hmac.compare_digest on two
    STR objects raises TypeError for non-ASCII -- "comparing strings with
    non-ASCII characters is not supported". So `X-Bank-Token: sècret` did not
    get a 403, it got a RemoteDisconnected and a traceback at this line. The
    server survived (thread per request) but an attacker learned that the
    header reached the comparison, and the refusal was a CRASH rather than a
    decision. Encoding both sides to bytes first removes the whole class:
    compare_digest is defined on bytes for exactly this reason.
    """
    try:
        a = presented.encode("utf-8", "surrogatepass")
        b = expected.encode("utf-8", "surrogatepass")
    except (AttributeError, UnicodeError):
        return False
    return hmac.compare_digest(a, b)


def authorise_sender(sender, headers):
    """None if `sender` proved itself, else (status, body) to send instead.

    FAIL-CLOSED IN EVERY DIRECTION:
      - sender is not a name this bank will even look up   -> 400
      - no credential file for this sender      -> 503 (refused, not allowed)
      - credential file present, no header      -> 403
      - header present but wrong                -> 403
      - header right                            -> None (authorised)

    agent-0 is the one account with a legacy fixed token path, and is allowed
    to present EITHER that token or its per-sender file. Nothing about this
    function reads the request BODY, so a caller cannot choose which credential
    it is judged against beyond the `from` field -- which is exactly the thing
    being authenticated.

    THIS FUNCTION VALIDATES THE NAME ITSELF, and that is a correction. agent-4
    found that it returned None (AUTHORISED) for a sender of '../agent-3' when
    a file with the right secret sat one level up: os.path.join(TOKEN_DIR,
    '../agent-3') escapes the directory. It was safe only because do_POST
    happened to check NAME_RE first, and do_POST was the only call site -- so
    the guarantee lived in the CALLER while the doc claimed it for the callee.
    A guard that holds only because of where it is called from is one refactor
    away from not holding. The check is now here, where the path is built, and
    do_POST keeps its own copy because a bad name should be a 400 and not leak
    through an auth path at all.
    """
    if not isinstance(sender, str) or not NAME_RE.match(sender) \
            or sender in (".", ".."):
        return (400, {"error": "invalid agent name"})
    candidates = []
    per_sender = _read_secret(os.path.join(TOKEN_DIR, sender))
    if per_sender:
        candidates.append(per_sender)
    if sender == LEGACY_TOKEN_AGENT:
        legacy = _read_secret(AGENT0_TOKEN_FILE)
        if legacy:
            candidates.append(legacy)
    if not candidates:
        return (503, {"error": "sender credential unavailable",
                      "sender": sender})
    # ACCEPT EITHER HEADER. agent-4 wrote an independent sender-auth patch
    # (sender-auth @d3bc995) that reads X-Bank-Sender-Token while this one
    # reads X-Bank-Token. Whichever merged first would silently decide which
    # client works, and the other half would 403 with no clue why -- measured
    # by him: on this bank, X-Bank-Token -> 200 and X-Bank-Sender-Token -> 403.
    # He made HIS bank accept both; doing the same here makes landing order
    # stop mattering. A header name is not a security boundary; the secret is.
    #
    # DEFECT FOUND BY agent-6 (2026-10-19 03:4xZ), and he was right: this used
    # to be `X-Bank-Token or X-Bank-Sender-Token`, which accepts either ONE by
    # PRECEDENCE. A compat client that sends BOTH -- exactly the client this
    # was built for, one that does not know which name the server wants -- was
    # then judged only on X-Bank-Token. Measured on the pre-fix file:
    #   wrong X-Bank-Token + CORRECT X-Bank-Sender-Token -> 403  (lockout)
    #   correct X-Bank-Token + wrong X-Bank-Sender-Token -> 200
    # The feature failed for the population it existed to serve. ANY header
    # carrying a correct secret authorises; none carrying a wrong one decides
    # against a correct one. Every value is checked, none is short-circuited.
    presented_all = [headers.get("X-Bank-Token", ""),
                     headers.get("X-Bank-Sender-Token", "")]
    if sender == LEGACY_TOKEN_AGENT:
        presented_all.append(headers.get("X-Agent0-Token", ""))
    for expected in candidates:
        for presented in presented_all:
            if presented and _digest_eq(presented, expected):
                return None
    return (403, {"error": "transfer forbidden", "sender": sender})


def load():
    global _accounts, _last_decay
    try:
        with open(DATA) as f:
            d = json.load(f)
        _accounts = {k: int(v) for k, v in d.get("accounts", {}).items()}
        _last_decay = float(d.get("last_decay"))
    except (OSError, ValueError, TypeError):
        pass
    if _last_decay is None:
        _last_decay = time.time()


def save():
    os.makedirs(os.path.dirname(DATA), exist_ok=True)
    tmp = DATA + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"accounts": _accounts, "last_decay": _last_decay}, f)
    os.replace(tmp, DATA)


def decay_ticks() -> int:
    """Apply whole elapsed decay periods to every account. Returns tick count."""
    global _last_decay
    ticks = int((time.time() - _last_decay) // DECAY_SECONDS)
    if ticks > 0:
        for name in _accounts:
            if name not in EXEMPT_ACCOUNTS:
                _accounts[name] = max(0, _accounts[name] - ticks)
        _last_decay += ticks * DECAY_SECONDS
    return ticks


def ensure_account(name) -> tuple[int, bool]:
    """Return (balance, created) for an agent, creating it at ZERO if needed.

    A NEW account starts at 0 and NOTHING is granted by creating it. Funding a
    name the first time it is touched is a mint: any caller who can reach this
    API can name a fresh account, be granted START_MONEY, and move it to a real
    account. Guarding only the transfer's sender is not enough -- the RECIPIENT
    path and `GET /balance/<name>` are the same hole. Coins enter an account
    only through seed() (a startup operator act) or /admin/topup (an operator
    act with the admin token).

    The `created` flag is kept so a read may materialise an account at 0
    without granting it anything.
    """
    if name in _accounts:
        return _accounts[name], False
    _accounts[name] = 0
    return 0, True


def seed(name, amount) -> int:
    """Grant `amount` coins to `name`. An OPERATOR act, never a side effect.

    Called at startup for the SEED list and by /admin/topup. This is the only
    code path that increases the total money supply.
    """
    balance = _accounts.get(name, 0) + int(amount)
    _accounts[name] = balance
    return balance


def parse_seed(spec):
    """Parse 'agent-0:9290,agent-1:1000' -> [('agent-0', 9290), ...].

    A bare name with no ':' gets START_MONEY, so SEED="agent-1,agent-2" is the
    short form for the common case. Malformed entries raise rather than being
    skipped: a typo in a seed list must fail loudly, because a silently-skipped
    seed is a town full of broke agents.
    """
    out = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            name, _, amt = item.partition(":")
            name, amt = name.strip(), amt.strip()
            if not NAME_RE.match(name):
                raise ValueError(f"bad name in SEED: {name!r}")
            out.append((name, int(amt)))
        else:
            if not NAME_RE.match(item):
                raise ValueError(f"bad name in SEED: {item!r}")
            out.append((item, START_MONEY))
    return out


def notify(recipient, sender, amount, recipient_balance):
    """Drop a payment notification into the recipient's mailbox."""
    try:
        inbox = os.path.join(MAILBOX, recipient, "incoming")
        os.makedirs(inbox, exist_ok=True)
        path = os.path.join(inbox, f"{time.time_ns()}-bank.msg")
        with open(path, "w") as f:
            json.dump({
                "from": "bank",
                "text": (f"[bank] You received {amount} coins from "
                         f"{sender}. Your balance is now {recipient_balance}."),
                "time": time.strftime("%H:%M:%S"),
            }, f)
    except OSError as e:
        # A failed notification must not fail the transfer — the ledger
        # update is already committed. Log and move on.
        print(f"[bank] notify failed for {recipient}: {e}", flush=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path == "/accounts":
            with _lock:
                ticks = decay_ticks()
                if ticks:
                    save()
                return self._send(200, {"accounts": dict(_accounts),
                                        "decay_seconds": DECAY_SECONDS})
        m = re.match(r"^/balance/([A-Za-z0-9_.-]+)$", path)
        if m:
            name = m.group(1)
            with _lock:
                ticks = decay_ticks()
                # A READ MUST NOT WRITE. This used to call ensure_account(),
                # so asking about a name created it: `exists` said false while
                # the row landed on disk, a second read then said true, and
                # 2,000 reads of fresh names grew the ledger from 65 bytes to
                # ~33 KB with 2,001 accounts. That is a storage mint -- no
                # coins, but unbounded, unauthenticated, and invisible because
                # "a refused transfer writes nothing" already passed.
                # `exists` is now a fact about the ledger, not a report of what
                # the previous call did. 200-with-0 is friendlier to the MCP
                # client than 404 and it is honest about what the number means.
                exists = name in _accounts
                balance = _accounts.get(name, 0)
                if ticks:
                    save()          # decay DID happen; that is a real write
            return self._send(200, {"agent": name, "money": balance,
                                    "exists": exists})
        return self._send(404, {"error": f"unknown path {path}"})

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if path not in ("/transfer", "/admin/topup"):
            return self._send(404, {"error": f"unknown path {path}"})
        if path == "/admin/topup":
            try:
                with open(ADMIN_TOKEN_FILE, encoding="utf-8") as f:
                    expected = f.read().strip()
            except OSError:
                return self._send(503, {"error": "admin token unavailable"})
            # Use _digest_eq, not hmac.compare_digest(str, str). agent-6's
            # defect 2: this was an inline copy of the compare that C6 fixed
            # 245 lines above, and it kept the bug -- a non-ASCII admin token
            # produced a TypeError and a dropped connection instead of a 403.
            # One fixed callee, two call sites, and the call site that was not
            # migrated is the one nobody re-measured. A crash is not a decision.
            if not expected or not _digest_eq(
                    self.headers.get("X-Bank-Admin-Token", ""), expected):
                return self._send(403, {"error": "forbidden"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= 16384:
                return self._send(400, {"error": "invalid body size"})
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("expected object")
        except (ValueError, OSError):
            return self._send(400, {"error": "bad JSON body"})
        if path == "/admin/topup":
            amount = body.get("amount")
            target = body.get("agent", "agent-0")
            if not isinstance(target, str) or not NAME_RE.match(target):
                return self._send(400, {"error": "invalid agent name"})
            if type(amount) is not int or not 0 < amount <= 1_000_000:
                return self._send(400, {"error": "amount must be 1..1000000"})
            with _lock:
                decay_ticks()
                balance = seed(target, amount)
                save()
            print(f"[bank] operator topped up {target} by {amount}", flush=True)
            return self._send(200, {"agent": target, "money": balance})
        sender = body.get("from")
        recipient = body.get("to")
        amount = body.get("amount")
        # AUTHENTICATE THE SENDER -- EVERY sender, before anything else is
        # looked at. See SENDER_TOKEN_DIR above for the hole this closes.
        # Order matters: validate the name shape FIRST so a hostile `from`
        # cannot be used to build a filesystem path.
        if not isinstance(sender, str) or not NAME_RE.match(sender):
            return self._send(400, {"error": "invalid agent name"})
        verdict = authorise_sender(sender, self.headers)
        if verdict is not None:
            return self._send(*verdict)
        if not isinstance(recipient, str) or not NAME_RE.match(recipient):
            return self._send(400, {"error": "invalid agent name"})
        if sender == recipient:
            return self._send(400, {"error": "cannot transfer to yourself"})
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 1:
            return self._send(400, {"error": "amount must be a positive integer"})

        with _lock:
            ticks = decay_ticks()
            if ticks:
                # Decay has ALREADY been applied in memory, so it must be
                # persisted even if this transfer is refused. Dropping it here
                # would leave the in-memory ledger ahead of the file and the
                # elapsed time would be re-applied on the next save.
                save()
            # A sender must already exist: the bank cannot tell an agent from
            # someone who can spell its name, so an unknown sender is refused
            # rather than created. Creating it would let any caller open an
            # account and spend it in the same request.
            if sender not in _accounts:
                return self._send(400, {"error": "unknown sender"})
            s_bal = _accounts[sender]
            # Check funds BEFORE creating anything. The recipient used to be
            # created and saved above this check, so a REFUSED transfer still
            # wrote a row into the ledger: a caller could grow
            # /data/accounts.json without bound with failing transfers to fresh
            # names, and a 400 still mutated state. Creating the recipient is
            # part of committing the transfer, so it happens only after the
            # transfer is known to be legal.
            if s_bal < amount:
                return self._send(400, {"error": "insufficient funds",
                                        "money": s_bal})
            r_bal, _ = ensure_account(recipient)
            s_bal -= amount
            r_bal += amount
            _accounts[sender] = s_bal
            _accounts[recipient] = r_bal
            save()
        notify(recipient, sender, amount, r_bal)
        print(f"[bank] {sender} -> {recipient}: {amount} coins "
              f"({sender} now {s_bal}, {recipient} now {r_bal})", flush=True)
        return self._send(200, {"ok": True, "money": s_bal,
                                "recipient": recipient,
                                "recipient_balance": r_bal})


def decay_loop():
    while True:
        time.sleep(5)
        with _lock:
            ticks = decay_ticks()
            if ticks:
                save()
                print(f"[bank] decay applied: -{ticks} to each of "
                      f"{len(_accounts)} accounts", flush=True)


if __name__ == "__main__":
    load()
    seed_spec = os.environ.get("SEED", "")
    with _lock:
        seeded = [(name, amt) for name, amt in parse_seed(seed_spec)
                  if name not in _accounts]
        for name, amt in seeded:
            seed(name, amt)
        if seeded:
            save()
            print(f"[bank] seeded {len(seeded)} account(s): "
                  + ", ".join(f"{n}={a}" for n, a in seeded), flush=True)
    threading.Thread(target=decay_loop, daemon=True).start()
    print(f"[bank] on 0.0.0.0:{PORT}  seed={seed_spec or '(none)'}  "
          f"decay=1 coin/{DECAY_SECONDS}s  ledger={DATA}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
