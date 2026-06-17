"""PnL aggregation queries used by the daily report."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..db import DEFAULT_DB_PATH, connect


def total_realized_pnl_usd(db_path: Path = DEFAULT_DB_PATH) -> float:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0.0) AS total FROM positions WHERE settled=1"
        ).fetchone()
    finally:
        conn.close()
    return float(row["total"] or 0.0)


def realized_pnl_last_n_days_usd(n_days: int, db_path: Path = DEFAULT_DB_PATH) -> float:
    cutoff = (datetime.now(UTC) - timedelta(days=n_days)).strftime("%Y-%m-%d")
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0.0) AS total FROM positions WHERE settled=1 AND date>=?",
            (cutoff,),
        ).fetchone()
    finally:
        conn.close()
    return float(row["total"] or 0.0)


def open_paper_exposure_usd(db_path: Path = DEFAULT_DB_PATH) -> float:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(size_usd), 0.0) AS total FROM positions WHERE settled=0"
        ).fetchone()
    finally:
        conn.close()
    return float(row["total"] or 0.0)
