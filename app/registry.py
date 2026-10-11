"""SQLite store of learned formats."""
import json
import sqlite3
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from app.fingerprint import (
    DRIFT_THRESHOLD, MATCH_THRESHOLD, Fingerprint, similarity,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS formats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    fingerprint_hash TEXT NOT NULL,
    labels_json TEXT NOT NULL,
    config_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class MatchKind(str, Enum):
    MATCH = "match"      # same format
    DRIFT = "drift"      # looks like a known format but the layout changed
    UNKNOWN = "unknown"  # never seen


@dataclass
class MatchResult:
    kind: MatchKind
    format_id: Optional[int] = None
    format_name: Optional[str] = None
    score: float = 0.0
    config: Optional[dict] = None


class Registry:
    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def add_format(self, name: str, fp: Fingerprint, config: Optional[dict] = None) -> int:
        cur = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM formats WHERE name = ?", (name,))
        version = cur.fetchone()[0] + 1
        cur = self.conn.execute(
            "INSERT INTO formats (name, version, fingerprint_hash, labels_json, config_json)"
            " VALUES (?, ?, ?, ?, ?)",
            (name, version, fp.digest, json.dumps(sorted(fp.labels)),
             json.dumps(config) if config is not None else None))
        self.conn.commit()
        return cur.lastrowid

    def all_formats(self) -> list:
        return self.conn.execute("SELECT * FROM formats ORDER BY id").fetchall()

    def match(self, fp: Fingerprint) -> MatchResult:
        best = None
        best_score = 0.0
        for row in self.all_formats():
            if row["fingerprint_hash"] == fp.digest:
                return self._result(MatchKind.MATCH, row, 1.0)
            score = similarity(fp.labels, frozenset(json.loads(row["labels_json"])))
            if score > best_score:
                best, best_score = row, score

        if best is None or best_score < DRIFT_THRESHOLD:
            return MatchResult(MatchKind.UNKNOWN, score=best_score)
        kind = MatchKind.MATCH if best_score >= MATCH_THRESHOLD else MatchKind.DRIFT
        return self._result(kind, best, best_score)

    @staticmethod
    def _result(kind, row, score) -> MatchResult:
        cfg = json.loads(row["config_json"]) if row["config_json"] else None
        return MatchResult(kind, row["id"], row["name"], score, cfg)