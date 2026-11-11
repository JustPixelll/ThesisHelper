"""Local chat history. No API keys or hidden model reasoning are recorded."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class Journal:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, document_id TEXT NOT NULL, "
                       "role TEXT NOT NULL, content TEXT NOT NULL, payload TEXT, created_at TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS chat_document ON messages(document_id,id)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def add(self, document_id: str, role: str, content: str, result: dict | None = None):
        with self.connect() as db:
            db.execute("INSERT INTO messages(document_id,role,content,payload,created_at) VALUES(?,?,?,?,?)",
                       (document_id, role, content, json.dumps(result, ensure_ascii=False) if result else None,
                        datetime.now(timezone.utc).isoformat()))

    def list(self, document_id: str, limit: int = 100) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM messages WHERE document_id=? ORDER BY id DESC LIMIT ?",
                              (document_id, min(200, max(1, limit)))).fetchall()
        messages = []
        for row in reversed(rows):
            message = dict(row)
            message["result"] = json.loads(message.pop("payload")) if row["payload"] else None
            messages.append(message)
        return messages
