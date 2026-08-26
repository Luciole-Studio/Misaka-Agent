"""LCM SQLite schema and small, idempotent migrations."""

import time

LATEST_SCHEMA_VERSION = 4


def _columns(con, table):
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def migrate(con):
    """Upgrade one LCM connection to the current schema."""
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
    )

    con.executescript("""
    CREATE TABLE IF NOT EXISTS messages (
        store_id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        source TEXT DEFAULT '',
        role TEXT NOT NULL,
        content TEXT,
        tool_call_id TEXT,
        tool_calls TEXT,
        tool_name TEXT,
        timestamp REAL NOT NULL,
        token_estimate INTEGER DEFAULT 0,
        pinned INTEGER DEFAULT 0,
        ingested_at REAL,
        observed_at REAL,
        observed_at_source TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id, store_id);
    CREATE INDEX IF NOT EXISTS idx_msg_session_ts ON messages(session_id, timestamp);
    CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
        content, content='messages', content_rowid='store_id');
    CREATE TRIGGER IF NOT EXISTS msg_fts_insert AFTER INSERT ON messages BEGIN
        INSERT INTO messages_fts(rowid, content) VALUES (new.store_id, new.content);
    END;
    CREATE TRIGGER IF NOT EXISTS msg_fts_delete AFTER DELETE ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, content)
            VALUES('delete', old.store_id, old.content);
    END;
    CREATE TRIGGER IF NOT EXISTS msg_fts_update AFTER UPDATE OF content ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, content)
            VALUES('delete', old.store_id, old.content);
        INSERT INTO messages_fts(rowid, content) VALUES (new.store_id, new.content);
    END;
    CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
    """)
    con.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(1, ?)",
        (time.time(),),
    )

    if "host_entry_id" not in _columns(con, "messages"):
        con.execute("ALTER TABLE messages ADD COLUMN host_entry_id TEXT")
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_msg_host_entry "
        "ON messages(session_id, host_entry_id) WHERE host_entry_id IS NOT NULL"
    )
    con.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(2, ?)",
        (time.time(),),
    )

    con.executescript("""
    CREATE TABLE IF NOT EXISTS summary_nodes (
        node_id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        depth INTEGER NOT NULL DEFAULT 0,
        summary TEXT NOT NULL,
        token_count INTEGER DEFAULT 0,
        source_token_count INTEGER DEFAULT 0,
        source_ids TEXT NOT NULL DEFAULT '[]',
        source_type TEXT NOT NULL DEFAULT 'messages',
        created_at REAL NOT NULL,
        earliest_at REAL,
        latest_at REAL,
        expand_hint TEXT DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_nodes_session_depth
        ON summary_nodes(session_id, depth, created_at);
    CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
        summary, content='summary_nodes', content_rowid='node_id');
    CREATE TRIGGER IF NOT EXISTS nodes_fts_insert AFTER INSERT ON summary_nodes BEGIN
        INSERT INTO nodes_fts(rowid, summary) VALUES (new.node_id, new.summary);
    END;
    CREATE TRIGGER IF NOT EXISTS nodes_fts_delete AFTER DELETE ON summary_nodes BEGIN
        INSERT INTO nodes_fts(nodes_fts, rowid, summary)
            VALUES('delete', old.node_id, old.summary);
    END;
    """)
    node_columns = _columns(con, "summary_nodes")
    if "attempt_id" not in node_columns:
        con.execute("ALTER TABLE summary_nodes ADD COLUMN attempt_id TEXT")
    if "committed" not in node_columns:
        con.execute("ALTER TABLE summary_nodes ADD COLUMN committed INTEGER NOT NULL DEFAULT 1")
    if "host_compaction_id" not in node_columns:
        con.execute("ALTER TABLE summary_nodes ADD COLUMN host_compaction_id TEXT")
    con.executescript("""
    CREATE INDEX IF NOT EXISTS idx_nodes_attempt ON summary_nodes(attempt_id, committed);
    CREATE TABLE IF NOT EXISTS compaction_attempts (
        attempt_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        result_summary TEXT NOT NULL,
        first_kept_entry_id TEXT,
        created_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_attempt_session ON compaction_attempts(session_id);
    """)
    con.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(3, ?)",
        (time.time(),),
    )

    con.executescript("""
    CREATE TABLE IF NOT EXISTS summary_embeddings (
        node_id INTEGER NOT NULL,
        model TEXT NOT NULL,
        dims INTEGER NOT NULL,
        vector BLOB NOT NULL,
        norm REAL NOT NULL,
        created_at REAL NOT NULL,
        PRIMARY KEY(node_id, model),
        FOREIGN KEY(node_id) REFERENCES summary_nodes(node_id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_summary_embeddings_model
        ON summary_embeddings(model, node_id);
    """)
    con.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(4, ?)",
        (time.time(),),
    )
    con.commit()

