"""Phase 0 calibration pipeline.

Reads the latest ensemble_forecasts per (station, target_date, model), blends models
equally into a Gaussian, applies a climatological variance blend, and computes
per-bucket probabilities via buckets.bucket_probs (sole owner). Writes emos_params
rows stamped ``stage='S0', complexity_level='climatology'`` and hands probabilities to
bridge.write_probs.

No EMOS, no fitted blend weights, no exceedance conditioning, no persistence terms
— those are Phase 2+. This pipeline is intentionally honest about its own immaturity.
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

from ..db import DEFAULT_DB_PATH, connect
from ..spec import Spec
from ..units import sigma_to_label_units, to_label_units
from .buckets import Bucket, GeometryConfig, bucket_probs, enumerate_buckets

# Phase 0 climatological σ in °C per station (rough daily-max forecast spread).
# Replaced by EMOS-fitted variance in Phase 2.
CLIM_SIGMA_C: dict[str, float] = {
    "hong_kong": 3.0,
    "london": 3.5,
    "new_york": 3.5,
}

# Phase 0 provisional bucket geometry per station, per spec PART III commentary.
PROVISIONAL_GEOMETRY: dict[str, tuple[str, float]] = {
    "hong_kong": ("rounds_to", 1.0),
    "london": ("floor_to", 1.0),
    "new_york": ("floor_to", 2.0),
}

# Variance blend weight (Phase 0 fixed; grid-searched in Phase 2).
CLIM_BLEND_LAMBDA: float = 0.5


@dataclass(frozen=True)
class GaussianForecast:
    station: str
    target_date: str
    lead_bucket: str
    mu_c: float
    sigma_c: float
    n_models_blended: int


@dataclass(frozen=True)
class BoardPrediction:
    forecast: GaussianForecast
    buckets: list[Bucket]
    bucket_probs: dict[float, float]
    geometry_provisional: bool


class _NormalDist:
    """Minimal Gaussian CDF wrapper for `buckets.bucket_probs`."""

    def __init__(self, mu: float, sigma: float) -> None:
        self._d = statistics.NormalDist(mu=mu, sigma=max(sigma, 1e-6))

    def cdf(self, x: float) -> float:
        if x == float("inf"):
            return 1.0
        if x == float("-inf"):
            return 0.0
        return self._d.cdf(x)


def _latest_forecast_rows(db_path: Path, station: str) -> list[dict]:
    """Most recent ensemble_forecasts row per (target_date, model)."""
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT target_date, model, run_label, mean_c, std_c, lead_hours "
            "FROM ensemble_forecasts ef "
            "WHERE station=? AND fetch_ts = (SELECT MAX(fetch_ts) FROM ensemble_forecasts "
            "  WHERE station=ef.station AND target_date=ef.target_date AND model=ef.model)",
            (station,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _lead_bucket_for_offset(target_date: str, today_iso: str, local_hour: int) -> str:
    from datetime import date

    t = date.fromisoformat(target_date)
    today = date.fromisoformat(today_iso)
    offset = (t - today).days
    if offset >= 3:
        return "d3"
    if offset == 2:
        return "d2"
    if offset == 1:
        return "d1"
    if offset == 0:
        return "d0_early" if local_hour < 9 else "d0_late"
    return "past"


def blend_models(model_rows: list[dict]) -> tuple[float, float, int]:
    """Equal-weight blend of model (μ_c, σ_c) pairs to a single Gaussian.

    μ = mean of model means
    σ² = mean(σ_model²) + var(μ_model) + λ·σ²_clim
    """
    mus = [r["mean_c"] for r in model_rows]
    sigmas = [r["std_c"] for r in model_rows]
    if not mus:
        return 0.0, 0.0, 0
    mu = sum(mus) / len(mus)
    inter_model_var = statistics.pvariance(mus) if len(mus) > 1 else 0.0
    avg_intra_var = sum(s * s for s in sigmas) / len(sigmas)
    return mu, inter_model_var + avg_intra_var, len(mus)


def calibrate_board(
    spec: Spec,
    station_id: str,
    target_date: str,
    lead_bucket: str,
    db_path: Path,
) -> BoardPrediction | None:
    city = spec.resolution.cities[station_id]
    rows = [r for r in _latest_forecast_rows(db_path, station_id) if r["target_date"] == target_date]
    if not rows:
        return None
    mu_c, model_var, n_models = blend_models(rows)
    clim_sigma = CLIM_SIGMA_C.get(station_id, 3.0)
    sigma2 = model_var + CLIM_BLEND_LAMBDA * clim_sigma ** 2
    sigma_c = sigma2 ** 0.5

    geom = GeometryConfig.from_city(
        hypothesis=city.buckets.hypothesis,
        width=city.buckets.width,
        verified=city.buckets.rounding_verified,
        provisional_default=PROVISIONAL_GEOMETRY[station_id],
    )
    buckets, tiled = enumerate_buckets(station_id, target_date, db_path, geom)
    if not buckets:
        return None

    # Convert Gaussian into label units for bucket integration
    mu_lbl = to_label_units(mu_c, city.buckets.label_units)
    sigma_lbl = sigma_to_label_units(sigma_c, city.buckets.label_units)
    dist = _NormalDist(mu_lbl, sigma_lbl)
    probs = bucket_probs(dist, buckets)

    forecast = GaussianForecast(
        station=station_id,
        target_date=target_date,
        lead_bucket=lead_bucket,
        mu_c=mu_c,
        sigma_c=sigma_c,
        n_models_blended=n_models,
    )
    geometry_provisional = (not city.buckets.rounding_verified) or (not tiled)
    return BoardPrediction(
        forecast=forecast,
        buckets=buckets,
        bucket_probs=probs,
        geometry_provisional=geometry_provisional,
    )


def write_emos_params_row(
    db_path: Path,
    station: str,
    target_date: str,
    lead_bucket: str,
    n_live: int,
    n_backfill: int,
) -> None:
    n_total = n_live + n_backfill
    effective_n = float(n_live)
    fitted_ts = int(time.time())
    conn = connect(db_path)
    try:
        # Phase 0: one row per (station, model='_climatology', lead_bucket)
        conn.execute(
            "INSERT INTO emos_params("
            "  station, model, lead_bucket, fitted_ts, n_total, n_live, n_backfill, effective_n,"
            "  stage, complexity_level) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(station, model, lead_bucket) DO UPDATE SET "
            "  fitted_ts=excluded.fitted_ts, n_total=excluded.n_total, "
            "  n_live=excluded.n_live, n_backfill=excluded.n_backfill,"
            "  effective_n=excluded.effective_n, stage=excluded.stage,"
            "  complexity_level=excluded.complexity_level",
            (
                station,
                "_climatology",
                lead_bucket,
                fitted_ts,
                n_total,
                n_live,
                n_backfill,
                effective_n,
                "S0",
                "climatology",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def count_forecast_pairs(
    db_path: Path, station: str, lead_bucket: str
) -> tuple[int, int]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT forecast_source, COUNT(*) AS n FROM forecast_pairs "
            "WHERE station=? AND lead_bucket=? AND y_c IS NOT NULL "
            "GROUP BY forecast_source",
            (station, lead_bucket),
        ).fetchall()
    finally:
        conn.close()
    n_live = 0
    n_backfill = 0
    for r in rows:
        if r["forecast_source"] == "live":
            n_live = r["n"]
        elif r["forecast_source"] == "historical_forecast":
            n_backfill = r["n"]
    return n_live, n_backfill
