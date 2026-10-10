"""SQLite record of every inbox message TradePilot has looked at (duplicate protection and audit).

    PROCESSING      trade being processed; if TradePilot stopped mid-way it is resumed on a later poll,
                    reusing the same trade_ref (never a second folder or register row)
    REPLYING        the reply is being sent; never retried, so a message is never answered twice
    COMPLETED       assessment done; reply_status says whether the reply was sent or failed
    FAILED          Outlook error before any reply (retried, max 3 attempts), or processing failed
                    (a failure notice was replied; not retried)
    IGNORED_SENDER  sender not in the allowed domain: nothing downloaded, never inspected again
    IGNORED_NO_PDF  no PDF attachments: no trade created, no reply (avoids reply loops)

The row '__start__' remembers when TradePilot first polled; only mail received after that is processed.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

PROCESSING, REPLYING, COMPLETED, FAILED = "PROCESSING", "REPLYING", "COMPLETED", "FAILED"
IGNORED_SENDER, IGNORED_NO_PDF = "IGNORED_SENDER", "IGNORED_NO_PDF"
FINAL = (REPLYING, COMPLETED, IGNORED_SENDER, IGNORED_NO_PDF, "START")
MAX_ATTEMPTS = 3
COLUMNS = ("trade_ref", "sender", "subject", "status", "received_at", "processed_at", "replied_at",
           "reply_status", "error")


def now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class State:
    def __init__(self, path: str):
        self.path = path
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS processed_messages (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                graph_message_id TEXT NOT NULL UNIQUE,
                trade_ref        TEXT UNIQUE,
                sender           TEXT,
                subject          TEXT,
                status           TEXT NOT NULL,
                received_at      TEXT,
                processed_at     TEXT,
                replied_at       TEXT,
                reply_status     TEXT,
                error            TEXT,
                attempts         INTEGER NOT NULL DEFAULT 0)""")

    def _db(self):
        return closing(sqlite3.connect(self.path, timeout=30, isolation_level=None))     # autocommit

    def start_time(self) -> str:
        """When TradePilot first polled (UTC), recorded on the first call."""
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO processed_messages (graph_message_id, status, processed_at) "
                       "VALUES ('__start__', 'START', ?)", (now_utc(),))
            return db.execute("SELECT processed_at FROM processed_messages "
                              "WHERE graph_message_id = '__start__'").fetchone()[0]

    def get(self, message_id: str) -> dict | None:
        with self._db() as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM processed_messages WHERE graph_message_id = ?", (message_id,)).fetchone()
        return dict(row) if row else None

    def should_process(self, message_id: str) -> bool:
        row = self.get(message_id)
        if row is None:
            return True
        if row["status"] in FINAL or row["replied_at"]:
            return False
        if row["status"] == FAILED and row["reply_status"]:          # failure notice already attempted
            return False
        return row["attempts"] < MAX_ATTEMPTS

    def save(self, message_id: str, status: str, **fields) -> None:
        """Insert or update the message's row; only the given fields change. PROCESSING counts an attempt."""
        unknown = set(fields) - set(COLUMNS)
        if unknown:
            raise ValueError(f"unknown state fields: {sorted(unknown)}")
        fields = dict(fields, status=status)
        fields.setdefault("processed_at", now_utc())
        cols = list(fields)
        attempt = 1 if status == PROCESSING else 0
        with self._db() as db:
            db.execute(
                f"INSERT INTO processed_messages (graph_message_id, attempts, {', '.join(cols)}) "
                f"VALUES (?, ?, {', '.join('?' for _ in cols)}) "
                f"ON CONFLICT(graph_message_id) DO UPDATE SET attempts = attempts + excluded.attempts, "
                + ", ".join(f"{c} = excluded.{c}" for c in cols),
                (message_id, attempt, *fields.values()))

    def rows(self) -> list[dict]:
        with self._db() as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute(
                "SELECT * FROM processed_messages WHERE graph_message_id != '__start__' ORDER BY id")]
