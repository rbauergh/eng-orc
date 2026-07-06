"""SQLite-backed memory store: always available, zero external dependencies.

This is the durability layer. Every memory item lands here first (FTS5 for
search); the Letta backend adds semantic recall and curation on top. If
FTS5 is missing from the sqlite build, search degrades to LIKE scanning
rather than failing.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from ..fsio import ensure_dir
from .schema import MemoryHit, MemoryItem

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    project TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    importance INTEGER NOT NULL DEFAULT 3
);
CREATE TABLE IF NOT EXISTS blocks (
    label TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    updated TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS letta_outbox (
    item_id TEXT PRIMARY KEY
);
"""

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    title, body, tags, content='items', content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts(rowid, title, body, tags)
    VALUES (new.rowid, new.title, new.body, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, body, tags)
    VALUES ('delete', old.rowid, old.title, old.body, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, body, tags)
    VALUES ('delete', old.rowid, old.title, old.body, old.tags);
    INSERT INTO items_fts(rowid, title, body, tags)
    VALUES (new.rowid, new.title, new.body, new.tags);
END;
"""


class LocalMemoryStore:
    name = "local"

    def __init__(self, db_path: Path):
        ensure_dir(db_path.parent)
        self.db_path = db_path
        self._fts = True
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            try:
                conn.executescript(_FTS_SCHEMA)
            except sqlite3.OperationalError:
                self._fts = False

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def health(self) -> tuple[bool, str]:
        return True, f"sqlite at {self.db_path}" + ("" if self._fts else " (no FTS5, LIKE fallback)")

    # -- items ---------------------------------------------------------------
    def save(self, item: MemoryItem) -> str:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO items (id, ts, kind, project, title, body, tags, importance)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id, item.ts, item.kind, item.project,
                    item.title, item.body, json.dumps(item.tags), item.importance,
                ),
            )
        return item.id

    def get(self, item_id: str) -> MemoryItem | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return self._to_item(row) if row else None

    def search(
        self,
        query: str,
        k: int = 5,
        kinds: list[str] | None = None,
        project: str | None = None,
    ) -> list[MemoryHit]:
        rows = self._fts_search(query, k * 4) if self._fts else self._like_search(query, k * 4)
        hits: list[MemoryHit] = []
        for row, score in rows:
            item = self._to_item(row)
            if kinds and item.kind not in kinds:
                continue
            if project is not None and item.project not in (project, ""):
                continue
            hits.append(MemoryHit(item=item, score=score, backend=self.name))
            if len(hits) >= k:
                break
        return hits

    def _fts_search(self, query: str, limit: int) -> list[tuple[sqlite3.Row, float]]:
        terms = [t for t in re.findall(r"[A-Za-z0-9_]{3,}", query)][:12]
        if not terms:
            return []
        match = " OR ".join(f'"{t}"' for t in terms)
        sql = (
            "SELECT items.*, bm25(items_fts) AS rank FROM items_fts"
            " JOIN items ON items.rowid = items_fts.rowid"
            " WHERE items_fts MATCH ? ORDER BY rank LIMIT ?"
        )
        try:
            with self._connect() as conn:
                rows = conn.execute(sql, (match, limit)).fetchall()
        except sqlite3.OperationalError:
            return self._like_search(query, limit)
        # bm25 is a cost (lower = better); flip into a similarity-ish score
        return [(row, 1.0 / (1.0 + max(row["rank"], 0.0))) for row in rows]

    def _like_search(self, query: str, limit: int) -> list[tuple[sqlite3.Row, float]]:
        terms = [t.lower() for t in re.findall(r"[A-Za-z0-9_]{3,}", query)][:8]
        if not terms:
            return []
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM items ORDER BY ts DESC LIMIT 500").fetchall()
        scored = []
        for row in rows:
            haystack = f"{row['title']} {row['body']} {row['tags']}".lower()
            score = sum(haystack.count(t) for t in terms)
            if score > 0:
                scored.append((row, float(score)))
        scored.sort(key=lambda pair: -pair[1])
        return scored[:limit]

    def recent(self, k: int = 20, kinds: list[str] | None = None) -> list[MemoryItem]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM items ORDER BY ts DESC LIMIT ?", (k * 3,)).fetchall()
        items = [self._to_item(row) for row in rows]
        if kinds:
            items = [i for i in items if i.kind in kinds]
        return items[:k]

    @staticmethod
    def _to_item(row: sqlite3.Row) -> MemoryItem:
        return MemoryItem(
            id=row["id"], ts=row["ts"], kind=row["kind"], project=row["project"],
            title=row["title"], body=row["body"], tags=json.loads(row["tags"]),
            importance=row["importance"],
        )

    # -- blocks ---------------------------------------------------------------
    def get_block(self, label: str) -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM blocks WHERE label = ?", (label,)).fetchone()
        return row["value"] if row else ""

    def set_block(self, label: str, value: str) -> None:
        from ..util import iso_now

        with self._connect() as conn:
            conn.execute(
                "INSERT INTO blocks (label, value, updated) VALUES (?, ?, ?)"
                " ON CONFLICT(label) DO UPDATE SET value = excluded.value, updated = excluded.updated",
                (label, value, iso_now()),
            )

    # -- letta sync bookkeeping --------------------------------------------------
    def outbox_add(self, item_id: str) -> None:
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO letta_outbox (item_id) VALUES (?)", (item_id,))

    def outbox_remove(self, item_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM letta_outbox WHERE item_id = ?", (item_id,))

    def outbox_items(self) -> list[MemoryItem]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT items.* FROM letta_outbox JOIN items ON items.id = letta_outbox.item_id"
            ).fetchall()
        return [self._to_item(row) for row in rows]
