"""Health check: DB writable, probs fresh, no KILL, feeds within staleness budget.

CLI command exposes this as ``wxm healthz``. Exit code 0 if all checks pass, 1
on first failure. Print a one-line summary per check so a failed soak is
diagnosable from the log.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from .bridge import probs_age_seconds
from .db import DEFAULT_DB_PATH, connect
from .spec import Spec


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def _check_kill(kill_file: Path) -> Check:
    if kill_file.exists():
        return Check("kill", False, f"KILL file present at {kill_file}")
    return Check("kill", True, "no KILL")


def _check_db(db_path: Path) -> Check:
    if not db_path.exists():
        return Check("db", False, f"missing: {db_path}")
    try:
        conn = connect(db_path)
        conn.execute("SELECT 1").fetchone()
        conn.close()
        return Check("db", True, f"writable: {db_path}")
    except Exception as e:
        return Check("db", False, f"unhealthy: {e}")


def _check_probs(bridge_dir: Path, max_age_s: int) -> Check:
    age = probs_age_seconds(bridge_dir)
    if age is None:
        return Check("probs", False, f"missing or unreadable in {bridge_dir}")
    if age > max_age_s:
        return Check("probs", False, f"stale {age:.0f}s > {max_age_s}s")
    return Check("probs", True, f"age={age:.0f}s")


def _check_books(db_path: Path, max_age_s: int) -> Check:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT MAX(ts) AS last_ts FROM book_snapshots"
        ).fetchone()
    finally:
        conn.close()
    last_ts_ms = (row["last_ts"] if row else None) or 0
    if last_ts_ms == 0:
        return Check("books", False, "no book snapshots recorded yet")
    age_s = time.time() - last_ts_ms / 1000
    if age_s > max_age_s:
        return Check("books", False, f"stale {age_s:.0f}s > {max_age_s}s")
    return Check("books", True, f"age={age_s:.0f}s")


def _check_observations(db_path: Path) -> Check:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT MAX(fetched_ts) AS last_ts FROM observations WHERE is_settlement_source=1"
        ).fetchone()
    finally:
        conn.close()
    last_ts = (row["last_ts"] if row else None) or 0
    if last_ts == 0:
        return Check("observations", False, "no settlement-source observations recorded yet")
    age_s = time.time() - last_ts
    if age_s > 48 * 3600:
        return Check("observations", False, f"newest is {age_s/3600:.1f}h old")
    return Check("observations", True, f"newest age={age_s/3600:.1f}h")


def _check_ensembles(db_path: Path) -> Check:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT MAX(fetch_ts) AS last_ts FROM ensemble_forecasts"
        ).fetchone()
    finally:
        conn.close()
    last_ts = (row["last_ts"] if row else None) or 0
    if last_ts == 0:
        return Check("ensembles", False, "no forecasts ingested yet")
    age_s = time.time() - last_ts
    if age_s > 24 * 3600:
        return Check("ensembles", False, f"newest is {age_s/3600:.1f}h old")
    return Check("ensembles", True, f"newest age={age_s/3600:.1f}h")


def healthz(
    spec: Spec,
    db_path: Path = DEFAULT_DB_PATH,
    bridge_dir: Path = Path("data/bridge"),
    kill_file: Path = Path("data/KILL"),
) -> list[Check]:
    return [
        _check_kill(kill_file),
        _check_db(db_path),
        _check_probs(bridge_dir, spec.trading.risk.stale_probs_max_age_s),
        _check_ensembles(db_path),
        _check_books(db_path, spec.trading.risk.stale_obs_max_age_s),
        _check_observations(db_path),
    ]
