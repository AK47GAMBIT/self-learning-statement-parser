"""SQLite store of processed documents: one row per distinct file (by hash)."""
import sqlite3
import threading
from typing import Optional

DATA_COLUMNS = [
    "filename", "source", "status", "statement_type", "provider", "strategy_name",
    "client_id", "period_start", "period_end", "format_name", "match_kind", "match_score",
    "used_llm", "issues_json", "validation_json", "statement_json", "error",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash TEXT NOT NULL UNIQUE,
    filename TEXT, source TEXT,
    status TEXT NOT NULL,
    statement_type TEXT, provider TEXT, strategy_name TEXT, client_id TEXT,
    period_start TEXT, period_end TEXT,
    format_name TEXT, match_kind TEXT, match_score REAL,
    used_llm INTEGER NOT NULL DEFAULT 0,
    issues_json TEXT, validation_json TEXT, statement_json TEXT, error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
"""


class Store:
    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self.conn.executescript(SCHEMA)

    def upsert(self, file_hash: str, **fields):
        """Insert or fully replace the row for this file. Every column must be given,
        so a re-processed file can never keep stale values from an earlier attempt."""
        missing, extra = set(DATA_COLUMNS) - set(fields), set(fields) - set(DATA_COLUMNS)
        if missing or extra:
            raise ValueError(f"missing columns {sorted(missing)}, unknown columns {sorted(extra)}")
        cols = ["file_hash", *DATA_COLUMNS]       # names come from our own whitelist
        sql = (f"INSERT INTO documents ({', '.join(cols)}) "
               f"VALUES ({', '.join('?' for _ in cols)}) "
               f"ON CONFLICT(file_hash) DO UPDATE SET "
               f"{', '.join(f'{c}=excluded.{c}' for c in DATA_COLUMNS)}, "
               f"updated_at=CURRENT_TIMESTAMP")
        with self._lock:
            self.conn.execute(sql, [file_hash, *(fields[c] for c in DATA_COLUMNS)])
            self.conn.commit()

    def get_by_hash(self, file_hash: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM documents WHERE file_hash = ?", (file_hash,)).fetchone()

    def get(self, doc_id: int) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()

    def list(self, status: Optional[str] = None) -> list:
        with self._lock:
            if status:
                return self.conn.execute(
                    "SELECT * FROM documents WHERE status = ? ORDER BY id DESC",
                    (status,)).fetchall()
            return self.conn.execute("SELECT * FROM documents ORDER BY id DESC").fetchall()

    def counts(self) -> dict:
        with self._lock:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM documents GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}