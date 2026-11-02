"""SQLite persistence. All state lives here so every agent decision can be audited."""
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    first_name TEXT NOT NULL DEFAULT '',
    last_name TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    company TEXT NOT NULL DEFAULT '',
    domain TEXT NOT NULL,
    segment TEXT NOT NULL DEFAULT 'default',
    source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    unsubscribe_token TEXT NOT NULL UNIQUE,
    hold_until TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    text TEXT NOT NULL,
    source_url TEXT NOT NULL,
    page_sha256 TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    touch INTEGER NOT NULL,
    variant TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    verification TEXT NOT NULL,
    settings_version INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_review',
    scheduled_for TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reviewer TEXT,
    reviewed_at TEXT,
    approved_sha256 TEXT,
    human_edited INTEGER NOT NULL DEFAULT 0,
    review_feedback TEXT,
    provider TEXT,
    provider_id TEXT,
    sent_at TEXT,
    delivered_at TEXT,
    opened_at TEXT,
    bounced_at TEXT,
    replied_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status);
CREATE INDEX IF NOT EXISTS idx_messages_lead ON messages(lead_id);
CREATE TABLE IF NOT EXISTS replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER REFERENCES leads(id),
    from_email TEXT NOT NULL,
    body TEXT NOT NULL,
    received_at TEXT NOT NULL,
    intent TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    needs_human INTEGER NOT NULL,
    resolved_by TEXT,
    resolved_at TEXT,
    resolution TEXT
);
CREATE TABLE IF NOT EXISTS suppression (
    value TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    rationale TEXT NOT NULL,
    evidence TEXT NOT NULL,
    change TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    details TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""


# Columns added after the first version. Existing databases get them on open.
MIGRATIONS = (
    ("leads", "timezone", "TEXT"),
    ("leads", "booking_ref", "TEXT"),
    ("messages", "inbox", "TEXT"),
    ("messages", "message_id_header", "TEXT"),
    ("messages", "in_reply_to", "TEXT"),
    ("messages", "test_recipient", "TEXT"),
    ("replies", "source_message_id", "TEXT"),
)


class DB:
    def __init__(self, path):
        # The review UI, the web endpoints and the scheduler may each hold a connection:
        # WAL lets them read while one writes, and the timeout makes a writer wait its turn.
        self.conn = sqlite3.connect(str(path), isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        for table, column, kind in MIGRATIONS:
            if column not in {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_mid ON messages(message_id_header)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_booking ON leads(booking_ref)")
        self._depth = 0

    @contextmanager
    def tx(self):
        """Nestable write transaction; BEGIN IMMEDIATE makes cap checks race-free."""
        outer = self._depth == 0
        if outer:
            self.conn.execute("BEGIN IMMEDIATE")
        self._depth += 1
        try:
            yield
        except Exception:
            self._depth -= 1
            if outer:
                self.conn.execute("ROLLBACK")
            raise
        else:
            self._depth -= 1
            if outer:
                self.conn.execute("COMMIT")

    def execute(self, sql, params=()):
        return self.conn.execute(sql, params)

    def one(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()

    def all(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def scalar(self, sql, params=()):
        row = self.conn.execute(sql, params).fetchone()
        return row[0] if row else None

    def close(self):
        self.conn.close()
