"""SQLite connection helpers and migrations runner."""

import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path("data/wxm.db")
DEFAULT_MIGRATIONS_DIR = Path("migrations")


def connect(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(
    db_path: Path = DEFAULT_DB_PATH,
    migrations_dir: Path = DEFAULT_MIGRATIONS_DIR,
) -> list[str]:
    """Run pending migrations from migrations_dir against db_path.

    Returns the list of migration filenames applied this run.
    """
    conn = connect(db_path)
    applied_now: list[str] = []
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS _migrations ("
            "  filename TEXT PRIMARY KEY,"
            "  applied_ts INTEGER NOT NULL"
            ")"
        )
        already = {
            r["filename"]
            for r in conn.execute("SELECT filename FROM _migrations").fetchall()
        }
        for sql_file in sorted(migrations_dir.glob("*.sql")):
            if sql_file.name in already:
                continue
            conn.executescript(sql_file.read_text())
            conn.execute(
                "INSERT INTO _migrations(filename, applied_ts) "
                "VALUES (?, strftime('%s','now'))",
                (sql_file.name,),
            )
            applied_now.append(sql_file.name)
        conn.commit()
    finally:
        conn.close()
    return applied_now
