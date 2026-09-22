"""A plain SQLite FTS5 memory behind the same Add/Search interface as
`service.MemoryService`, for calibration only.

AML's open-source board lists an "SQLite-FTS-Baseline" at 41.79. Running
the same kind of system through this repo's local reproduction says how
the stand-in reader and judge compare with AML's hidden ones, and, more
usefully, how far the bettermemory engine sits above a naive full-text
index under identical ingest and presentation. Only ranking differs:
the same rounds, the same "[date]" headers, the same top_k.

The query is the question's alphanumeric tokens OR'd together, ranked by
FTS5's built-in bm25 with the porter tokenizer, which is what a
straightforward FTS baseline does.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

from aml.service import _fmt_ts, rounds_of

_WORD = re.compile(r"[A-Za-z0-9]+")


class FtsService:
    def __init__(self, root: Path, serve: int = 100) -> None:
        self.root = root
        self.serve = serve
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _db(self, user_id: str) -> tuple[sqlite3.Connection, threading.Lock]:
        key = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:32]
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        conn = sqlite3.connect(self.root / f"{key}.db", check_same_thread=False)
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS rounds USING fts5("
            "body, session UNINDEXED, ts UNINDEXED, seq UNINDEXED, "
            "tokenize='porter unicode61')"
        )
        conn.execute("CREATE TABLE IF NOT EXISTS done (request_id TEXT PRIMARY KEY)")
        return conn, lock

    def add(
        self,
        request_id: str,
        user_id: str,
        messages: list[dict[str, Any]],
        session_id: str = "",
    ) -> int:
        conn, lock = self._db(user_id)
        with lock, conn:
            if conn.execute(
                "SELECT 1 FROM done WHERE request_id = ?", (request_id,)
            ).fetchone():
                return 0
            (seq,) = conn.execute("SELECT COUNT(*) FROM rounds").fetchone()
            n = 0
            for body, ts in rounds_of(messages):
                stamp = _fmt_ts(ts)
                content = f"[{stamp}]\n{body}" if stamp else body
                conn.execute(
                    "INSERT INTO rounds (body, session, ts, seq) VALUES (?, ?, ?, ?)",
                    (content, session_id, ts, seq + n),
                )
                n += 1
            conn.execute("INSERT INTO done VALUES (?)", (request_id,))
        conn.close()
        return n

    def search(self, user_id: str, query: str, top_k: int) -> list[dict[str, Any]]:
        conn, lock = self._db(user_id)
        top_k = min(top_k, self.serve)
        words = sorted({w.lower() for w in _WORD.findall(query)})
        if not words:
            return []
        match = " OR ".join(f'"{w}"' for w in words)
        with lock:
            rows = conn.execute(
                "SELECT rowid, body, bm25(rounds) FROM rounds WHERE rounds MATCH ? "
                "ORDER BY bm25(rounds) LIMIT ?",
                (match, top_k),
            ).fetchall()
        conn.close()
        return [
            {"id": f"fts-{rowid}", "content": body, "score": -float(score)}
            for rowid, body, score in rows
        ]

    def sessions_for(self, user_id: str, ids: list[str]) -> list[str]:
        conn, lock = self._db(user_id)
        out: list[str] = []
        with lock:
            for i in ids:
                row = conn.execute(
                    "SELECT session FROM rounds WHERE rowid = ?", (int(i[4:]),)
                ).fetchone()
                out.append(row[0] if row else "")
        conn.close()
        return out
