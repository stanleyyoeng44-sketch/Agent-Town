"""Durable human/delegate chat history and outgoing queue (host only)."""

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

CHAT_DIR = Path(__file__).resolve().parent / "stanley-chat"
DB_PATH = CHAT_DIR / "history.sqlite3"
LEGACY_PATH = CHAT_DIR / "stanley-chat.jsonl"
HUMAN_INBOX = "/workspace/human/inbox"
HUMAN_OUTBOX = "/workspace/human/outbox"
_lock = threading.RLock()


def _connect():
    CHAT_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.execute("""CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY, sender TEXT NOT NULL, text TEXT NOT NULL,
        created_at TEXT NOT NULL, status TEXT NOT NULL, source TEXT NOT NULL
    )""")
    if LEGACY_PATH.exists():
        for i, line in enumerate(LEGACY_PATH.read_text(encoding="utf-8").splitlines()):
            try:
                record = json.loads(line)
                if record.get("from") not in ("stanley", "agent-0"):
                    continue
                db.execute("INSERT OR IGNORE INTO messages VALUES (?, ?, ?, ?, ?, ?)", (
                    f"legacy-{i}", record["from"], record["text"],
                    record.get("time", ""), "delivered", "imported setup history"))
            except (KeyError, ValueError):
                continue
    db.commit()
    return db


@contextmanager
def _db():
    db = _connect()
    try:
        with db:
            yield db
    finally:
        db.close()


def queue_to_delegate(text):
    message_id = uuid.uuid4().hex
    with _lock, _db() as db:
        db.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)", (
            message_id, "stanley", text, datetime.now(timezone.utc).isoformat(),
            "pending", "ui"))
    return message_id


def pending_messages():
    with _lock, _db() as db:
        return db.execute(
            "SELECT id, text FROM messages WHERE sender = 'stanley' AND status = 'pending' "
            "ORDER BY created_at").fetchall()


def mark_delivered(message_id):
    with _lock, _db() as db:
        db.execute("UPDATE messages SET status = 'delivered' WHERE id = ?", (message_id,))


def record_delegate(message_id, text):
    with _lock, _db() as db:
        db.execute("INSERT OR IGNORE INTO messages VALUES (?, ?, ?, ?, ?, ?)", (
            message_id, "agent-0", text, datetime.now(timezone.utc).isoformat(),
            "delivered", "delegate tool"))


def read_history():
    with _lock, _db() as db:
        rows = db.execute("SELECT id, sender, text, created_at, status, source "
                          "FROM messages ORDER BY created_at, rowid").fetchall()
    return [dict(zip(("id", "from", "text", "time", "status", "source"), row))
            for row in rows]
