"""Agent bank: per-agent accounts, automatic decay, transfer API.

Standalone container (see Dockerfile next to this file). Agents talk to it
over HTTP at http://host.docker.internal:8790 — the same pattern as the
model proxy. The ledger lives in this container's /data volume, which NO
agent container mounts, so balances cannot be forged from agent-land.

Rules:
  - Every agent has an account, auto-created with START_MONEY (default 1000)
    on first touch.
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
MAILBOX = "/shared/mailbox"
ADMIN_TOKEN_FILE = "/run/secrets/bank-admin-token"
AGENT0_TOKEN_FILE = "/run/secrets/agent0-transfer-token"
EXEMPT_ACCOUNTS = frozenset({"agent-0"})
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,60}$")

_lock = threading.Lock()
_accounts: dict[str, int] = {}
_last_decay: float | None = None


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
    """Return (balance, created) for an agent, creating it if needed."""
    if name in _accounts:
        return _accounts[name], False
    _accounts[name] = START_MONEY
    return START_MONEY, True


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
                balance, created = ensure_account(name)
                if ticks or created:
                    save()
            return self._send(200, {"agent": name, "money": balance})
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
            if not expected or not hmac.compare_digest(
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
            if type(amount) is not int or not 0 < amount <= 1_000_000:
                return self._send(400, {"error": "amount must be 1..1000000"})
            with _lock:
                decay_ticks()
                balance, _ = ensure_account("agent-0")
                balance += amount
                _accounts["agent-0"] = balance
                save()
            print(f"[bank] operator topped up agent-0 by {amount}", flush=True)
            return self._send(200, {"agent": "agent-0", "money": balance})
        sender = body.get("from")
        recipient = body.get("to")
        amount = body.get("amount")
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
        if sender == recipient:
            return self._send(400, {"error": "cannot transfer to yourself"})
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 1:
            return self._send(400, {"error": "amount must be a positive integer"})

        with _lock:
            ticks = decay_ticks()
            s_bal, s_new = ensure_account(sender)
            r_bal, r_new = ensure_account(recipient)
            if ticks or s_new or r_new:
                save()
            if s_bal < amount:
                return self._send(400, {"error": "insufficient funds",
                                        "money": s_bal})
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
    threading.Thread(target=decay_loop, daemon=True).start()
    print(f"[bank] on 0.0.0.0:{PORT}  start={START_MONEY}  "
          f"decay=1 coin/{DECAY_SECONDS}s  ledger={DATA}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
