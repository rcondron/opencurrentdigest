-- OCD — Open Current Digest
-- SQLite schema for articles.db

CREATE TABLE IF NOT EXISTS articles (
    id TEXT PRIMARY KEY,
    source TEXT,
    title TEXT,
    link TEXT,
    published TEXT,
    fetched_at TEXT,
    summary TEXT,
    raw_description TEXT
);

CREATE INDEX IF NOT EXISTS idx_fetched ON articles(fetched_at DESC);

CREATE TABLE IF NOT EXISTS visits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ip TEXT NOT NULL,
    path TEXT NOT NULL,
    user_agent TEXT,
    started_at TEXT NOT NULL,
    duration_seconds REAL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_visits_started ON visits(started_at);
CREATE INDEX IF NOT EXISTS idx_visits_ip ON visits(ip);
CREATE INDEX IF NOT EXISTS idx_visits_session ON visits(session_id);
