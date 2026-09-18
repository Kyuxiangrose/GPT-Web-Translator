"""Durable token and CNY cost accounting at the successful API boundary."""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MAX_SAFE_INTEGER = 9007199254740991
PRICING_VERSION = "deepseek-cn-2026-08-17"
# Integer nano-yuan per token. Official prices are CNY per 1M tokens.
OFF_PEAK_NANO_PER_TOKEN = {
    "deepseek-v4-flash": (50, 1500, 4500),
    "deepseek-v4-pro": (150, 4500, 13500),
    "deepseek-v4-flash-vision-exp": (50, 1500, 4500),
}
MODEL_ALIASES = {
    "deepseek-flash": "deepseek-v4-flash",
    "deepseek-pro": "deepseek-v4-pro",
    "deepseek-v4-flash-0731": "deepseek-v4-flash",
    "deepseek-v4-pro-0813": "deepseek-v4-pro",
}
_SCHEMA_LOCK = threading.Lock()
_INITIALIZED_DATABASES: set[Path] = set()


def clean_usage(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {
        key: value[key]
        for key in (
            "prompt_tokens", "completion_tokens", "total_tokens",
            "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
        )
        if type(value.get(key)) is int and 0 <= value[key] <= MAX_SAFE_INTEGER
    }


def calculate_cost_nano_yuan(
    usage_value: Any, model_value: Any, created_value: Any
) -> tuple[int | None, str | None]:
    """Calculate one request from its exact usage and the published CNY tariff."""
    usage = clean_usage(usage_value)
    model = str(model_value or "").strip().lower()
    model = MODEL_ALIASES.get(model, model)
    prices = OFF_PEAK_NANO_PER_TOKEN.get(model)
    required = (
        "prompt_tokens", "completion_tokens", "total_tokens",
        "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
    )
    if prices is None or any(key not in usage for key in required):
        return None, None
    hit = usage["prompt_cache_hit_tokens"]
    miss = usage["prompt_cache_miss_tokens"]
    completion = usage["completion_tokens"]
    if hit + miss != usage["prompt_tokens"]:
        return None, None
    if usage["prompt_tokens"] + completion != usage["total_tokens"]:
        return None, None
    created = created_value if type(created_value) in (int, float) else time.time()
    try:
        utc_hour = datetime.fromtimestamp(created, timezone.utc).hour
    except (OverflowError, OSError, ValueError):
        utc_hour = datetime.now(timezone.utc).hour
    # Beijing peak windows 09:00-12:00 and 14:00-18:00.
    peak = 1 <= utc_hour < 4 or 6 <= utc_hour < 10
    multiplier = 2 if peak else 1
    hit_price, miss_price, output_price = prices
    amount = multiplier * (
        hit * hit_price + miss * miss_price + completion * output_price
    )
    if amount > MAX_SAFE_INTEGER:
        return None, None
    period = "peak" if peak else "off_peak"
    return amount, f"{PRICING_VERSION}:{model}:{period}"


class TokenUsageStore:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        self._initialized = False

    @contextmanager
    def _connection(self):
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.database_path, timeout=15)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _columns(db: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}

    @classmethod
    def _add_column(cls, db: sqlite3.Connection, table: str, declaration: str) -> bool:
        name = declaration.split()[0]
        if name in cls._columns(db, table):
            return False
        db.execute(f"ALTER TABLE {table} ADD COLUMN {declaration}")
        return True

    def initialize(self) -> None:
        database_key = self.database_path.resolve()
        with _SCHEMA_LOCK:
            if self._initialized:
                return
            if database_key in _INITIALIZED_DATABASES and self.database_path.exists():
                self._initialized = True
                return
            with self._connection() as db:
                db.execute("""CREATE TABLE IF NOT EXISTS token_history (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    store_id TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    missing_usage INTEGER NOT NULL DEFAULT 0,
                    total_cost_nano_yuan INTEGER NOT NULL DEFAULT 0,
                    missing_cost INTEGER NOT NULL DEFAULT 0,
                    started_at INTEGER NOT NULL)""")
                db.execute("""CREATE TABLE IF NOT EXISTS token_pages (
                    page_id TEXT PRIMARY KEY,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    missing_usage INTEGER NOT NULL DEFAULT 0,
                    total_cost_nano_yuan INTEGER NOT NULL DEFAULT 0,
                    missing_cost INTEGER NOT NULL DEFAULT 0)""")
                db.execute("""CREATE TABLE IF NOT EXISTS token_events (
                    event_id TEXT PRIMARY KEY, page_id TEXT NOT NULL,
                    total_tokens INTEGER, cost_nano_yuan INTEGER,
                    pricing_version TEXT, model TEXT,
                    prompt_cache_hit_tokens INTEGER,
                    prompt_cache_miss_tokens INTEGER,
                    completion_tokens INTEGER,
                    recorded_at INTEGER NOT NULL)""")
                # Migrate the 1.0.3 ledger without guessing money from total_tokens.
                old_events = "cost_nano_yuan" not in self._columns(db, "token_events")
                for table in ("token_history", "token_pages"):
                    self._add_column(db, table, "total_cost_nano_yuan INTEGER NOT NULL DEFAULT 0")
                    self._add_column(db, table, "missing_cost INTEGER NOT NULL DEFAULT 0")
                for declaration in (
                    "cost_nano_yuan INTEGER", "pricing_version TEXT", "model TEXT",
                    "prompt_cache_hit_tokens INTEGER",
                    "prompt_cache_miss_tokens INTEGER", "completion_tokens INTEGER",
                ):
                    self._add_column(db, "token_events", declaration)
                db.execute(
                    "INSERT OR IGNORE INTO token_history(singleton, store_id, started_at) VALUES (1, ?, ?)",
                    (uuid.uuid4().hex, int(time.time())),
                )
                if old_events:
                    db.execute(
                        "UPDATE token_history SET missing_cost = "
                        "(SELECT COUNT(*) FROM token_events)"
                    )
                    db.execute(
                        "UPDATE token_pages SET missing_cost = "
                        "(SELECT COUNT(*) FROM token_events WHERE token_events.page_id = token_pages.page_id)"
                    )
                self._backfill_known_costs(db)
            _INITIALIZED_DATABASES.add(database_key)
            self._initialized = True

    def _backfill_known_costs(self, db: sqlite3.Connection) -> None:
        """Repair saved 1.0.4 events whose API response used a known model alias."""
        started_row = db.execute(
            "SELECT started_at FROM token_history WHERE singleton = 1"
        ).fetchone()
        started_at = int(started_row[0]) if started_row else 0
        rows = db.execute(
            """SELECT event_id, page_id, total_tokens, model,
                prompt_cache_hit_tokens, prompt_cache_miss_tokens,
                completion_tokens, recorded_at
                FROM token_events
                WHERE cost_nano_yuan IS NULL
                  AND prompt_cache_hit_tokens IS NOT NULL
                  AND prompt_cache_miss_tokens IS NOT NULL
                  AND completion_tokens IS NOT NULL"""
        ).fetchall()
        for event_id, page_id, total, model, hit, miss, completion, recorded_at in rows:
            usage = {
                "prompt_tokens": hit + miss,
                "completion_tokens": completion,
                "total_tokens": total,
                "prompt_cache_hit_tokens": hit,
                "prompt_cache_miss_tokens": miss,
            }
            cost, pricing_version = calculate_cost_nano_yuan(
                usage, model, recorded_at
            )
            if cost is None:
                continue
            changed = db.execute(
                """UPDATE token_events
                    SET cost_nano_yuan = ?, pricing_version = ?
                    WHERE event_id = ? AND cost_nano_yuan IS NULL""",
                (cost, pricing_version, event_id),
            ).rowcount
            if not changed:
                continue
            db.execute(
                """UPDATE token_pages SET
                    total_cost_nano_yuan = total_cost_nano_yuan + ?,
                    missing_cost = CASE WHEN missing_cost > 0 THEN missing_cost - 1 ELSE 0 END
                    WHERE page_id = ?""",
                (cost, page_id),
            )
            if recorded_at >= started_at:
                db.execute(
                    """UPDATE token_history SET
                        total_cost_nano_yuan = total_cost_nano_yuan + ?,
                        missing_cost = CASE WHEN missing_cost > 0 THEN missing_cost - 1 ELSE 0 END,
                        revision = revision + 1
                        WHERE singleton = 1""",
                    (cost,),
                )

    def record(
        self, event_id: str, page_id: str, usage_value: Any,
        model_value: Any = "", created_value: Any = None,
    ) -> bool:
        """One event per successful HTTP response, including unusable translations."""
        self.initialize()
        usage = clean_usage(usage_value)
        total = usage.get("total_tokens")
        missing_usage = int(total is None)
        cost, pricing_version = calculate_cost_nano_yuan(
            usage_value, model_value, created_value
        )
        missing_cost = int(cost is None)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            added = db.execute(
                """INSERT OR IGNORE INTO token_events(
                    event_id, page_id, total_tokens, cost_nano_yuan,
                    pricing_version, model, prompt_cache_hit_tokens,
                    prompt_cache_miss_tokens, completion_tokens, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id, page_id, total, cost, pricing_version,
                    str(model_value or "")[:100],
                    usage.get("prompt_cache_hit_tokens"),
                    usage.get("prompt_cache_miss_tokens"),
                    usage.get("completion_tokens"), int(time.time()),
                ),
            ).rowcount
            if not added:
                return False
            db.execute(
                """UPDATE token_history SET
                    total_tokens = total_tokens + ?,
                    missing_usage = missing_usage + ?,
                    total_cost_nano_yuan = total_cost_nano_yuan + ?,
                    missing_cost = missing_cost + ?,
                    revision = revision + 1 WHERE singleton = 1""",
                (total or 0, missing_usage, cost or 0, missing_cost),
            )
            if page_id:
                db.execute(
                    """INSERT INTO token_pages(
                        page_id, total_tokens, missing_usage,
                        total_cost_nano_yuan, missing_cost
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(page_id) DO UPDATE SET
                        total_tokens = total_tokens + excluded.total_tokens,
                        missing_usage = missing_usage + excluded.missing_usage,
                        total_cost_nano_yuan = total_cost_nano_yuan + excluded.total_cost_nano_yuan,
                        missing_cost = missing_cost + excluded.missing_cost""",
                    (page_id, total or 0, missing_usage, cost or 0, missing_cost),
                )
        return True

    def snapshot(self, page_id: str = "") -> dict[str, Any]:
        self.initialize()
        with self._connection() as db:
            db.execute("BEGIN")
            history = db.execute(
                """SELECT store_id, revision, total_tokens, missing_usage,
                    total_cost_nano_yuan, missing_cost, started_at
                    FROM token_history WHERE singleton = 1"""
            ).fetchone()
            page = db.execute(
                """SELECT total_tokens, missing_usage,
                    total_cost_nano_yuan, missing_cost
                    FROM token_pages WHERE page_id = ?""",
                (page_id,),
            ).fetchone()
        return {
            "store_id": history[0], "revision": history[1],
            "history_total_tokens": history[2], "history_missing_usage": history[3],
            "history_total_cost_nano_yuan": history[4],
            "history_missing_cost": history[5],
            "started_at": history[6], "page_id": page_id,
            "page_total_tokens": page[0] if page else 0,
            "page_missing_usage": page[1] if page else 0,
            "page_total_cost_nano_yuan": page[2] if page else 0,
            "page_missing_cost": page[3] if page else 0,
            "currency": "CNY", "pricing_version": PRICING_VERSION,
        }

    def clear_history(self) -> None:
        self.initialize()
        with self._connection() as db:
            db.execute(
                """UPDATE token_history SET
                    total_tokens = 0, missing_usage = 0,
                    total_cost_nano_yuan = 0, missing_cost = 0,
                    revision = revision + 1, started_at = ?
                    WHERE singleton = 1""",
                (int(time.time()),),
            )
        # Current-document totals and deduplication receipts intentionally remain.
