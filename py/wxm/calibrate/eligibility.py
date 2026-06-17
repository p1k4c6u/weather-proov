"""Live-capital eligibility evaluator.

In Phase 0 this writes paper-locked rows: every (station, lead_bucket) is
``live_eligible=0`` with ``failed_gate`` pinned to whichever blocker is
checked first. Phase 2 fills in PIT diagnostics, Brier-vs-market, drift
diagnostics, and tail eligibility; the executor reads this table on every
order forever after.
"""

from __future__ import annotations

import time
from pathlib import Path

from ..db import DEFAULT_DB_PATH, connect
from ..spec import Spec
from .pipeline import count_forecast_pairs

LEAD_BUCKETS = ("d0_early", "d0_late", "d1", "d2", "d3")


def evaluate_phase0(
    spec: Spec, db_path: Path = DEFAULT_DB_PATH
) -> dict[tuple[str, str], str]:
    """Phase 0 eligibility: paper-only with first-blocker reasoning.

    Returns {(station, lead_bucket): failed_gate}.
    """
    out: dict[tuple[str, str], str] = {}
    evaluated_ts = int(time.time())
    conn = connect(db_path)
    try:
        for station_id, city in spec.resolution.cities.items():
            for lead_bucket in LEAD_BUCKETS:
                n_live, n_backfill = count_forecast_pairs(db_path, station_id, lead_bucket)
                n_total = n_live + n_backfill
                effective_n = float(n_live)
                if not city.buckets.rounding_verified:
                    failed_gate = "ROUNDING_UNVERIFIED"
                elif n_total < 60 or n_live < 30:
                    failed_gate = "S2_NOT_REACHED"
                else:
                    failed_gate = "PIT_FAIL"  # placeholder; Phase 2 evaluates this
                conn.execute(
                    "INSERT INTO eligibility("
                    "  station, lead_bucket, stage, n_live, n_total, effective_n,"
                    "  near_mean_eligible, tail_eligible, live_eligible, failed_gate, evaluated_ts) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(station, lead_bucket) DO UPDATE SET "
                    "  stage=excluded.stage, n_live=excluded.n_live, n_total=excluded.n_total,"
                    "  effective_n=excluded.effective_n,"
                    "  near_mean_eligible=excluded.near_mean_eligible,"
                    "  tail_eligible=excluded.tail_eligible,"
                    "  live_eligible=excluded.live_eligible,"
                    "  failed_gate=excluded.failed_gate, evaluated_ts=excluded.evaluated_ts",
                    (
                        station_id,
                        lead_bucket,
                        "S0",
                        n_live,
                        n_total,
                        effective_n,
                        0,  # near_mean_eligible — Phase 0 always 0
                        0,  # tail_eligible — Phase 0 always 0
                        0,  # live_eligible — Phase 0 always 0
                        failed_gate,
                        evaluated_ts,
                    ),
                )
                out[(station_id, lead_bucket)] = failed_gate
        conn.commit()
    finally:
        conn.close()
    return out


def load_eligibility_for_lead(
    db_path: Path, station: str, lead_bucket: str
) -> dict | None:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT stage, live_eligible, near_mean_eligible, tail_eligible, failed_gate "
            "FROM eligibility WHERE station=? AND lead_bucket=?",
            (station, lead_bucket),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None
