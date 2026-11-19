"""SQLite storage. One connection per thread, WAL, every commit synced to disk."""
import sqlite3
import threading
from contextlib import contextmanager

from .config import DB_PATH

_local = threading.local()

MIGRATIONS = [
    """
    CREATE TABLE video (
        id            INTEGER PRIMARY KEY,
        path          TEXT NOT NULL UNIQUE,
        size_bytes    INTEGER NOT NULL,
        mtime         REAL NOT NULL,
        title         TEXT,
        duration_s    REAL,
        youtube_id    TEXT,            -- given by the caller, read from .info.json, or found by name search
        youtube_how   TEXT,            -- given | info.json | search
        -- queued -> fetching -> done | whisper -> transcribing -> done | failed
        status        TEXT NOT NULL DEFAULT 'queued',
        priority      INTEGER NOT NULL DEFAULT 0,
        progress      REAL,            -- 0..1 while transcribing
        source        TEXT,            -- file | youtube | whisper
        source_detail TEXT,            -- e.g. file name, 'manual en', 'auto hi-orig', 'small'
        language      TEXT,
        error         TEXT,
        next_try_at   TEXT,            -- set while waiting for YouTube to accept requests again
        created_at    TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX video_status ON video(status, priority DESC, id);

    CREATE TABLE segment (
        video_id  INTEGER NOT NULL REFERENCES video(id) ON DELETE CASCADE,
        idx       INTEGER NOT NULL,
        start_s   REAL NOT NULL,
        end_s     REAL NOT NULL,
        text      TEXT NOT NULL,
        PRIMARY KEY (video_id, idx)
    );

    CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """,
    # v2: things made from a transcript with an LLM. Deleted whenever the transcript changes.
    """
    CREATE TABLE artifact (
        video_id       INTEGER NOT NULL REFERENCES video(id) ON DELETE CASCADE,
        kind           TEXT NOT NULL,            -- summary | notes | translation
        status         TEXT NOT NULL DEFAULT 'queued',   -- queued | working | done | failed
        content        TEXT,                     -- Markdown, or JSON segments for a translation
        model          TEXT,
        input_tokens   INTEGER,
        output_tokens  INTEGER,
        error          TEXT,
        requested_at   TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (video_id, kind)
    );
    CREATE INDEX artifact_status ON artifact(status, requested_at);
    """,
]


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = _local.conn = _connect()
    return c


@contextmanager
def tx():
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


def migrate() -> None:
    c = conn()
    v = c.execute("PRAGMA user_version").fetchone()[0]
    for i, sql in enumerate(MIGRATIONS[v:], start=v + 1):
        c.executescript("BEGIN;" + sql + f"; PRAGMA user_version={i}; COMMIT;")
    # a restart interrupts work in progress: put it back in the queue
    c.execute("UPDATE video SET status='queued' WHERE status='fetching'")
    c.execute("UPDATE video SET status='whisper', progress=NULL WHERE status='transcribing'")
    c.execute("UPDATE artifact SET status='queued' WHERE status='working'")


def one(sql: str, params=()) -> dict | None:
    r = conn().execute(sql, params).fetchone()
    return dict(r) if r else None


def all_(sql: str, params=()) -> list[dict]:
    return [dict(r) for r in conn().execute(sql, params).fetchall()]


def kv_get(key: str) -> str | None:
    r = conn().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return r[0] if r else None


def kv_set(key: str, value: str) -> None:
    conn().execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, value))
