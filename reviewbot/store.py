"""SQLite 持久层。

v1 的问题：每次调用都 ``sqlite3.connect`` 再关闭，全文代码无长度上限地存两遍，
且没有 WAL。这里改为线程本地连接复用 + WAL + 只存截断后的代码。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reviewbot.logging_setup import get_logger

logger = get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    task_id          TEXT PRIMARY KEY,
    language         TEXT NOT NULL,
    code_preview     TEXT NOT NULL,
    source_type      TEXT NOT NULL,
    state            TEXT NOT NULL,
    cache_hit        INTEGER NOT NULL DEFAULT 0,
    llm_degraded     INTEGER NOT NULL DEFAULT 0,
    result_json      TEXT,
    error            TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_reviews_created_at ON reviews (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reviews_state      ON reviews (state);
"""


@dataclass(slots=True)
class StoredReview:
    task_id: str
    state: str
    result: dict[str, Any] | None
    error: str | None


class ReviewStore:
    """线程安全的轻量存储。

    用 ``threading.local`` 给每个线程一个连接：SQLite 连接不可跨线程共享，
    而每次新建连接的开销在并发下会放大。
    """

    def __init__(self, db_path: str, max_code_chars: int = 20000) -> None:
        self.path = Path(db_path)
        self.max_code_chars = max_code_chars
        self._local = threading.local()
        if self.path.parent != Path(""):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ---------- 连接管理 ----------
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        self._conn().executescript(_SCHEMA)

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ---------- 写入 ----------
    def create(self, task_id: str, language: str, code: str, source_type: str) -> None:
        preview = code[: self.max_code_chars]
        self._conn().execute(
            "INSERT OR REPLACE INTO reviews (task_id, language, code_preview, source_type, state) "
            "VALUES (?, ?, ?, ?, 'PENDING')",
            (task_id, language, preview, source_type),
        )

    def finish(
        self,
        task_id: str,
        *,
        state: str,
        result: dict[str, Any] | None,
        cache_hit: bool = False,
        llm_degraded: bool = False,
        error: str | None = None,
    ) -> None:
        self._conn().execute(
            "UPDATE reviews SET state = ?, result_json = ?, cache_hit = ?, llm_degraded = ?, "
            "error = ?, updated_at = datetime('now') WHERE task_id = ?",
            (
                state,
                json.dumps(result, ensure_ascii=False) if result else None,
                1 if cache_hit else 0,
                1 if llm_degraded else 0,
                error,
                task_id,
            ),
        )

    # ---------- 读取 ----------
    def get(self, task_id: str) -> StoredReview | None:
        row = self._conn().execute("SELECT * FROM reviews WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        return StoredReview(
            task_id=row["task_id"],
            state=row["state"],
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=row["error"],
        )

    def history(self, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT task_id, language, code_preview, created_at, state, result_json "
            "FROM reviews ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            nesting = None
            if row["result_json"]:
                try:
                    nesting = json.loads(row["result_json"])["analysis"]["structure"]["max_loop_nesting"]
                except (json.JSONDecodeError, KeyError, TypeError):
                    nesting = None
            items.append(
                {
                    "task_id": row["task_id"],
                    "language": row["language"],
                    "max_loop_nesting": nesting,
                    "code_preview": row["code_preview"][:100],
                    "created_at": row["created_at"],
                }
            )
        return items

    def stats(self) -> dict[str, int]:
        row = self._conn().execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN state = 'FAILED' THEN 1 ELSE 0 END) AS failed, "
            "SUM(CASE WHEN cache_hit = 1 THEN 1 ELSE 0 END) AS hits, "
            "SUM(CASE WHEN llm_degraded = 1 THEN 1 ELSE 0 END) AS degraded "
            "FROM reviews"
        ).fetchone()
        return {
            "total_reviews": int(row["total"] or 0),
            "failed_reviews": int(row["failed"] or 0),
            "cache_hits": int(row["hits"] or 0),
            "llm_degraded": int(row["degraded"] or 0),
        }

    def ping(self) -> bool:
        try:
            self._conn().execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            logger.warning("sqlite_ping_failed", exc_info=True)
            return False
