"""Storage. Postgres when DATABASE_URL / POSTGRES_URL is set (Vercel), otherwise a local SQLite
file (for running on your own computer). SQL is written once with `?` placeholders."""
import json
import os
import sqlite3
import threading

PG_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
SQLITE_PATH = os.environ.get("SQLITE_PATH") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "local.db")

BLOB = "BYTEA" if PG_URL else "BLOB"
SCHEMA = [
    "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)",
    """CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, name TEXT, pw_hash TEXT,
         is_admin INTEGER DEFAULT 0, created TEXT, failed INTEGER DEFAULT 0, locked_until TEXT)""",
    "CREATE TABLE IF NOT EXISTS settings (username TEXT PRIMARY KEY, data TEXT)",
    "CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, owner TEXT, created TEXT, data TEXT)",
    "CREATE INDEX IF NOT EXISTS items_owner ON items (owner)",
    "CREATE TABLE IF NOT EXISTS suppliers (k TEXT PRIMARY KEY, data TEXT)",
    """CREATE TABLE IF NOT EXISTS history (id TEXT PRIMARY KEY, supplier TEXT, invoice TEXT,
         item_id TEXT, owner TEXT, created TEXT, data TEXT)""",
    "CREATE INDEX IF NOT EXISTS history_key ON history (supplier, invoice)",
    f"""CREATE TABLE IF NOT EXISTS files (id TEXT PRIMARY KEY, owner TEXT, batch TEXT, name TEXT,
         mime TEXT, data {BLOB}, created TEXT)""",
    "CREATE INDEX IF NOT EXISTS files_batch ON files (batch)",
    """CREATE TABLE IF NOT EXISTS batches (id TEXT PRIMARY KEY, owner TEXT, created TEXT,
         label TEXT, kind TEXT, total REAL, count INTEGER)""",
    # one row per requisition sent for signing; data holds the slots and the audit trail
    """CREATE TABLE IF NOT EXISTS signing (id TEXT PRIMARY KEY, file_id TEXT, owner TEXT,
         created TEXT, status TEXT, data TEXT)""",
    "CREATE TABLE IF NOT EXISTS signatures (username TEXT PRIMARY KEY, data TEXT)",
]

MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN position TEXT",  # job title, printed under their signature
]

_local = threading.local()
_ready = False


def _connect():
    if PG_URL:
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(PG_URL, autocommit=True, row_factory=dict_row, prepare_threshold=None)
    conn = sqlite3.connect(SQLITE_PATH, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _conn():
    c = getattr(_local, "conn", None)
    if c is None or (PG_URL and c.closed):
        c = _local.conn = _connect()
    return c


def _sql(sql):
    return sql.replace("?", "%s") if PG_URL else sql


def _run(sql, params=()):
    global _ready
    if not _ready:
        _ready = True
        for stmt in SCHEMA:
            _exec(stmt)
        for stmt in MIGRATIONS:  # columns added after the first release; fail = already there
            try:
                _exec(stmt)
            except Exception:
                pass
    return _exec(sql, params)


def _exec(sql, params=()):
    try:
        return _conn().execute(_sql(sql), params)
    except Exception as e:
        if PG_URL and "authentication" not in str(e).lower():  # stale serverless connection - reconnect once
            _local.conn = None
            return _conn().execute(_sql(sql), params)
        raise


def rows(sql, params=()):
    return [dict(r) for r in _run(sql, params).fetchall()]


def row(sql, params=()):
    r = _run(sql, params).fetchone()
    return dict(r) if r else None


def run(sql, params=()):
    _run(sql, params)


def blob(b):
    """Value to store in a BLOB/BYTEA column."""
    return b if PG_URL else sqlite3.Binary(b)


# ---------------------------------------------------------------- small helpers

def get_json(table, key_col, key, default=None):
    r = row(f"SELECT data FROM {table} WHERE {key_col} = ?", (key,))
    return json.loads(r["data"]) if r else default


def put_json(table, key_col, key, data):
    run(f"INSERT INTO {table} ({key_col}, data) VALUES (?, ?) "
        f"ON CONFLICT ({key_col}) DO UPDATE SET data = excluded.data", (key, json.dumps(data)))


def meta_get(k):
    r = row("SELECT v FROM meta WHERE k = ?", (k,))
    return r["v"] if r else None


def meta_set(k, v):
    run("INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT (k) DO UPDATE SET v = excluded.v", (k, v))
