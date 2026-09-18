from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class TranslationCache:
    def __init__(self, database_path: Path, max_entries: int = 20000):
        self.database_path = database_path
        self.max_entries = max_entries
        self._init_lock = threading.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            with self._connection() as db:
                db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS translations (
                        cache_key TEXT PRIMARY KEY,
                        response_json TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        last_used_at INTEGER NOT NULL,
                        hit_count INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_translations_last_used "
                    "ON translations(last_used_at)"
                )
            self._initialized = True

    def get(self, cache_key: str) -> dict[str, Any] | None:
        self.initialize()
        now = int(time.time())
        with self._connection() as db:
            row = db.execute(
                "SELECT response_json FROM translations WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE translations SET last_used_at = ?, hit_count = hit_count + 1 "
                "WHERE cache_key = ?",
                (now, cache_key),
            )
        try:
            value = json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            self.delete(cache_key)
            return None
        return value if isinstance(value, dict) else None

    def put(self, cache_key: str, response: dict[str, Any]) -> None:
        self.initialize()
        now = int(time.time())
        payload = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
        with self._connection() as db:
            db.execute(
                """
                INSERT INTO translations(cache_key, response_json, created_at, last_used_at, hit_count)
                VALUES (?, ?, ?, ?, 0)
                ON CONFLICT(cache_key) DO UPDATE SET
                    response_json = excluded.response_json,
                    last_used_at = excluded.last_used_at
                """,
                (cache_key, payload, now, now),
            )
        self.prune_if_needed()

    def delete(self, cache_key: str) -> None:
        self.initialize()
        with self._connection() as db:
            db.execute("DELETE FROM translations WHERE cache_key = ?", (cache_key,))

    def prune_if_needed(self) -> None:
        self.initialize()
        with self._connection() as db:
            row = db.execute("SELECT COUNT(*) FROM translations").fetchone()
            count = int(row[0]) if row else 0
            excess = count - self.max_entries
            if excess <= 0:
                return
            db.execute(
                "DELETE FROM translations WHERE cache_key IN ("
                "SELECT cache_key FROM translations ORDER BY last_used_at ASC LIMIT ?)",
                (excess,),
            )

    def count(self) -> int:
        self.initialize()
        with self._connection() as db:
            row = db.execute("SELECT COUNT(*) FROM translations").fetchone()
        return int(row[0]) if row else 0
