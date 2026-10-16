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

SCHEMA += """
CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL UNIQUE,
    subject TEXT, sender TEXT, received TEXT,
    decision TEXT NOT NULL,
    attachments_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

SCHEMA += """
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected', 'reopened')),
    note TEXT,
    reviewed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_reviews_doc ON reviews(document_id);
"""

# A document joined with its LATEST review decision (if any).
_FROM = ("FROM documents d LEFT JOIN reviews r "
         "ON r.id = (SELECT MAX(id) FROM reviews WHERE document_id = d.id)")
_SELECT = ("SELECT d.*, r.decision AS review_decision, r.note AS review_note, "
           "r.reviewed_at AS reviewed_at")

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
    

    def email_seen(self, message_id: str) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM emails WHERE message_id = ?", (message_id,)).fetchone()
        return row is not None

    def record_email(self, message_id, subject, sender, received, decision, attachments_json):
        with self._lock:
            self.conn.execute(
                "INSERT INTO emails (message_id, subject, sender, received, decision, "
                "attachments_json) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(message_id) DO UPDATE SET decision=excluded.decision, "
                "attachments_json=excluded.attachments_json",
                (message_id, subject, sender, received, decision, attachments_json))
            self.conn.commit()

    def list_emails(self) -> list:
        with self._lock:
            return self.conn.execute("SELECT * FROM emails ORDER BY id DESC").fetchall()
    
    def get_email(self, message_id: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM emails WHERE message_id = ?", (message_id,)).fetchone()


    def query(self, *, status=None, attention=False, q=None, limit=25, offset=0):
        """One page of documents plus the total match count.
        attention = flagged AND not yet resolved by a person."""
        where, params = [], []
        if status:
            where.append("d.status = ?")
            params.append(status)
        if attention:
            where.append("d.status != 'parsed_ok' "
                         "AND (r.decision IS NULL OR r.decision = 'reopened')")
        if q and q.strip():
            like = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            cols = ["d.filename", "d.provider", "d.client_id", "d.strategy_name", "d.format_name"]
            where.append("(" + " OR ".join(f"{c} LIKE ? ESCAPE '\\'" for c in cols) + ")")
            params += [like] * len(cols)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            total = self.conn.execute(f"SELECT COUNT(*) {_FROM} {clause}", params).fetchone()[0]
            rows = self.conn.execute(
                f"{_SELECT} {_FROM} {clause} ORDER BY d.id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset]).fetchall()
        return rows, total

    def get_with_review(self, doc_id: int) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                f"{_SELECT} {_FROM} WHERE d.id = ?", (doc_id,)).fetchone()

    def add_review(self, doc_id: int, decision: str, note: str = ""):
        with self._lock:
            self.conn.execute(
                "INSERT INTO reviews (document_id, decision, note) VALUES (?, ?, ?)",
                (doc_id, decision, note or None))
            self.conn.commit()

    def review_history(self, doc_id: int) -> list:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM reviews WHERE document_id = ? ORDER BY id DESC",
                (doc_id,)).fetchall()

    def review_counts(self) -> dict:
        with self._lock:
            rows = self.conn.execute(
                "SELECT decision, COUNT(*) AS n FROM reviews "
                "WHERE id IN (SELECT MAX(id) FROM reviews GROUP BY document_id) "
                "GROUP BY decision").fetchall()
        got = {r["decision"]: r["n"] for r in rows}
        return {"approved": got.get("approved", 0), "rejected": got.get("rejected", 0)}
    
    def clear_all(self):
        """Delete every document, email and review (used only by the demo reset)."""
        with self._lock:
            for table in ("reviews", "emails", "documents"):
                self.conn.execute(f"DELETE FROM {table}")      # fixed names, never user input
            self.conn.commit()
