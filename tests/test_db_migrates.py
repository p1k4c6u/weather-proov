import sqlite3
from pathlib import Path

from wxm.db import connect, init_db

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO_ROOT / "migrations"

PHASE0_TABLES = {
    "markets",
    "book_snapshots",
    "ensemble_forecasts",
    "forecast_pairs",
    "observations",
    "settlements",
    "signals",
    "orders",
    "fills",
    "positions",
    "emos_params",
    "blend_weights",
    "eligibility",
}


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r["name"] for r in rows}


def test_init_db_creates_all_phase0_tables(tmp_path: Path):
    db_path = tmp_path / "wxm.db"
    applied = init_db(db_path, MIGRATIONS)
    assert applied == ["0001_phase0_schema.sql"]
    conn = connect(db_path)
    try:
        names = _table_names(conn)
    finally:
        conn.close()
    assert PHASE0_TABLES.issubset(names), f"missing: {PHASE0_TABLES - names}"


def test_init_db_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "wxm.db"
    init_db(db_path, MIGRATIONS)
    second = init_db(db_path, MIGRATIONS)
    assert second == []


def test_wal_mode_enabled(tmp_path: Path):
    db_path = tmp_path / "wxm.db"
    init_db(db_path, MIGRATIONS)
    conn = connect(db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode.lower() == "wal"


def test_forecast_pairs_check_constraint_rejects_bad_source(tmp_path: Path):
    db_path = tmp_path / "wxm.db"
    init_db(db_path, MIGRATIONS)
    conn = connect(db_path)
    try:
        try:
            conn.execute(
                "INSERT INTO forecast_pairs "
                "(station, target_date, lead_bucket, model, run_label, "
                " forecast_run_ts, forecast_fetch_ts, lead_hours, forecast_source, "
                " usable_for_train_after_date, created_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "new_york",
                    "2026-06-15",
                    "d0_late",
                    "gfs_seamless",
                    "00z",
                    0,
                    0,
                    0.0,
                    "made_up_source",
                    "2026-06-16",
                    0,
                ),
            )
            assert False, "CHECK constraint should reject unknown forecast_source"
        except sqlite3.IntegrityError:
            pass
    finally:
        conn.close()
