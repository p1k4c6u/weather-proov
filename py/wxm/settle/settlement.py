"""Settlement state machine: OPEN → PROVISIONAL → FINAL.

Per spec §6.7:
  - PROVISIONAL on first settlement-source read
  - FINAL immediately for ``first_publication_only`` stations (HKO, KNYC)
  - For ``revisable_until_next_day`` (London) the value is re-read on the next
    local day to capture revisions, then finalized
  - On FINAL: compute winner via buckets.winning_bucket, settle positions,
    complete every forecast_pairs row for that (station, target_date), compare
    Polymarket resolved outcome where available and flag mismatch
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as _date
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from ..calibrate.buckets import GeometryConfig, enumerate_buckets, winning_bucket
from ..calibrate.pipeline import PROVISIONAL_GEOMETRY
from ..db import DEFAULT_DB_PATH, connect
from ..spec import CitySpec, Spec
from ..units import native_to_c

log = logging.getLogger(__name__)


SettlementState = Literal["OPEN", "PROVISIONAL", "FINAL"]


@dataclass(frozen=True)
class SettlementOutcome:
    station: str
    target_date: str
    state: SettlementState
    settlement_source: str | None
    observed_value: float | None  # native units of the settlement source
    observed_units: str | None
    winning_label: float | None
    pairs_completed: int


def _today_local(city: CitySpec) -> _date:
    return datetime.now(ZoneInfo(city.timezone)).date()


def _settlement_source_obs(db_path: Path, station: str, target_date: str) -> dict | None:
    """Return the settlement-source observation for (station, target_date), or None."""
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM observations WHERE station=? AND date=? AND is_settlement_source=1 "
            "ORDER BY fetched_ts DESC LIMIT 1",
            (station, target_date),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def _existing_settlement(db_path: Path, station: str, target_date: str) -> dict | None:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM settlements WHERE station=? AND date=?", (station, target_date)
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def _should_finalize_now(city: CitySpec, target_date: str) -> bool:
    if city.settlement.revision_policy == "first_publication_only":
        return True
    # revisable_until_next_day: finalize when local today > target_date
    target = _date.fromisoformat(target_date)
    return _today_local(city) > target


def settle_date(
    spec: Spec, station: str, target_date: str, db_path: Path = DEFAULT_DB_PATH
) -> SettlementOutcome:
    """Run the settlement state machine for one (station, target_date)."""
    city = spec.resolution.cities[station]
    obs = _settlement_source_obs(db_path, station, target_date)
    if obs is None:
        return SettlementOutcome(station, target_date, "OPEN", None, None, None, None, 0)

    finalize = _should_finalize_now(city, target_date)
    if not finalize:
        return SettlementOutcome(
            station, target_date, "PROVISIONAL", obs["source"],
            obs["value"], obs["units"], None, 0,
        )

    # FINAL: compute winning bucket, settle positions, complete forecast_pairs.
    geom = GeometryConfig.from_city(
        hypothesis=city.buckets.hypothesis,
        width=city.buckets.width,
        verified=city.buckets.rounding_verified,
        provisional_default=PROVISIONAL_GEOMETRY[station],
    )
    buckets, _ = enumerate_buckets(station, target_date, db_path, geom)
    observed_native = float(obs["value"])
    win = winning_bucket(observed_native, buckets) if buckets else None
    winning_label = win.label if win else None
    if win is None and buckets:
        log.warning(
            "winning_bucket not found for %s %s observed=%s (audit alarm)",
            station, target_date, observed_native,
        )

    observed_c = native_to_c(observed_native, obs["units"])
    pairs_completed = _complete_forecast_pairs(
        db_path, station, target_date, observed_c, obs["source"], int(obs["fetched_ts"]),
    )
    _write_settlement_row(
        db_path, station, target_date, winning_label, our_predicted_winner=None,
    )
    _settle_positions(db_path, station, target_date, winning_label)
    return SettlementOutcome(
        station, target_date, "FINAL", obs["source"],
        observed_native, obs["units"], winning_label, pairs_completed,
    )


def _complete_forecast_pairs(
    db_path: Path, station: str, target_date: str,
    observed_c: float, source: str, settlement_ts: int,
) -> int:
    conn = connect(db_path)
    try:
        cur = conn.execute(
            "UPDATE forecast_pairs SET y_c=?, settlement_source=?, settlement_ts=? "
            "WHERE station=? AND target_date=? AND y_c IS NULL",
            (observed_c, source, settlement_ts, station, target_date),
        )
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


def _write_settlement_row(
    db_path: Path, station: str, target_date: str,
    winning_label: float | None, our_predicted_winner: float | None,
) -> None:
    import time as _time
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO settlements(station, date, bucket_label, market_id, resolved_ts,"
            " our_predicted_winner, mismatch) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(station, date) DO UPDATE SET "
            "  bucket_label=excluded.bucket_label, resolved_ts=excluded.resolved_ts,"
            "  our_predicted_winner=excluded.our_predicted_winner",
            (
                station, target_date, winning_label or 0.0, None, int(_time.time()),
                our_predicted_winner,
                int(our_predicted_winner is not None and winning_label is not None
                    and our_predicted_winner != winning_label),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _settle_positions(
    db_path: Path, station: str, target_date: str, winning_label: float | None,
) -> int:
    """Compute realized PnL for every paper position on (station, target_date).

    Binary payout convention: each share of the winning outcome pays $1.
      shares = size_usd / avg_price
      pnl_correct = shares * 1 - size_usd = size_usd * (1 - avg_price) / avg_price
      pnl_wrong = -size_usd
    """
    if winning_label is None:
        return 0
    conn = connect(db_path)
    try:
        # Need to look up each position's bucket via the markets table
        rows = conn.execute(
            "SELECT p.market_id, p.side, p.avg_price, p.size_usd, m.bucket_label "
            "FROM positions p JOIN markets m ON p.market_id = m.market_id "
            "WHERE p.station=? AND p.date=? AND p.settled=0",
            (station, target_date),
        ).fetchall()
        n = 0
        for r in rows:
            yes_wins = float(r["bucket_label"]) == float(winning_label)
            correct = (r["side"] == "buy_yes" and yes_wins) or (
                r["side"] == "buy_no" and not yes_wins
            )
            if r["avg_price"] is None or r["avg_price"] <= 0:
                pnl = 0.0
            elif correct:
                pnl = r["size_usd"] * (1.0 - r["avg_price"]) / r["avg_price"]
            else:
                pnl = -r["size_usd"]
            conn.execute(
                "UPDATE positions SET settled=1, pnl=? WHERE market_id=?",
                (pnl, r["market_id"]),
            )
            n += 1
        conn.commit()
        return n
    finally:
        conn.close()


def settle_pending_for_yesterday(
    spec: Spec, db_path: Path = DEFAULT_DB_PATH
) -> dict[str, SettlementOutcome]:
    """Convenience: settle each station's previous local day."""
    out: dict[str, SettlementOutcome] = {}
    for station_id, city in spec.resolution.cities.items():
        yesterday = (_today_local(city) - timedelta(days=1)).isoformat()
        out[station_id] = settle_date(spec, station_id, yesterday, db_path)
    return out
