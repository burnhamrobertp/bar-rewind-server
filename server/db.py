# The server's state: one SQLite file. Each thread keeps its own connection; writes that read first
# take the write lock up front (tx), so two requests can't both claim the same job.
import contextlib, json, sqlite3, threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT '',
    pct INTEGER NOT NULL DEFAULT 0,
    created INTEGER NOT NULL,
    expires INTEGER NOT NULL,
    started INTEGER,
    finished INTEGER,
    heartbeat INTEGER,
    progress_at INTEGER,
    uid TEXT NOT NULL,
    user TEXT NOT NULL,
    map TEXT NOT NULL DEFAULT '',
    file_name TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER NOT NULL DEFAULT 0,
    players TEXT NOT NULL DEFAULT '[]',
    start_time TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    worker TEXT,
    size INTEGER,
    packed_size INTEGER,
    curves_version INTEGER,
    curves_key TEXT,
    log_key TEXT,
    event_count INTEGER,
    events TEXT,
    peak_mem_mb INTEGER
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status, created);

CREATE TABLE IF NOT EXISTS users (
    uid TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    refresh_token TEXT,
    checked INTEGER NOT NULL DEFAULT 0
);
"""

_local = threading.local()
_path = None


def init(path):
    global _path
    _path = path
    conn().executescript(SCHEMA)


def conn():
    c = getattr(_local, "conn", None)
    if c is None or getattr(_local, "path", None) != _path:
        c = sqlite3.connect(_path, isolation_level=None, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode = WAL")
        _local.conn, _local.path = c, _path

    return c


@contextlib.contextmanager
def tx():
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
    except BaseException:
        c.execute("ROLLBACK")
        raise
    c.execute("COMMIT")


def one(sql, *args):
    row = conn().execute(sql, args).fetchone()

    return dict(row) if row else None


def rows(sql, *args):
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def insert(c, table, row):
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    c.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", [json.dumps(v) if isinstance(v, (list, dict)) else v for v in row.values()])


def update(c, table, key, value, fields):
    sets = ", ".join(f"{k} = ?" for k in fields)
    values = [json.dumps(v) if isinstance(v, (list, dict)) else v for v in fields.values()]

    return c.execute(f"UPDATE {table} SET {sets} WHERE {key} = ?", values + [value]).rowcount
