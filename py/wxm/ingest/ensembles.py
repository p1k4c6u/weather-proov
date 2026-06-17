"""Open-Meteo ensemble fetcher.

Per spec PART IV.3:

    GET https://ensemble-api.open-meteo.com/v1/ensemble
        ?latitude={station_lat}&longitude={station_lon}
        &daily=temperature_2m_max&hourly=temperature_2m
        &models={m}&forecast_days=4&past_days=1
        m ∈ { ecmwf_ifs025, gfs_seamless, icon_seamless }

Writes one ensemble_forecasts row per (station, target_date, model, run_label) and
mirrors each into forecast_pairs as the live Clock-B training-pair ledger. Station
coordinates come from spec/resolution.yaml — never hardcoded.

Open-Meteo Historical Forecast API (for backfill, source='historical_forecast')
is out of scope for Phase 0 and lives in a later module.
"""

from __future__ import annotations

import json
import re
import statistics
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from ..archive import archive_raw
from ..db import DEFAULT_DB_PATH, connect
from ..spec import Spec

OPEN_METEO_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
MODELS = ("ecmwf_ifs025", "gfs_seamless", "icon_seamless")

MODEL_RUN_HOURS: dict[str, tuple[int, ...]] = {
    "ecmwf_ifs025": (0, 12),
    "gfs_seamless": (0, 6, 12, 18),
    "icon_seamless": (0, 6, 12, 18),
}

_DAILY_MEMBER_RE = re.compile(r"^temperature_2m_max_member(\d+)$")
_HOURLY_MEMBER_RE = re.compile(r"^temperature_2m_member(\d+)$")


@dataclass(frozen=True)
class ParsedDaily:
    target_date: str
    mean_c: float
    std_c: float
    members: list[float]
    hourly_members: dict[str, list[float]] | None


def infer_run_label(model: str, now_utc: datetime) -> str:
    """Most recent past model-run hour, formatted as 'YYYY-MM-DDTHHZ'."""
    runs = sorted(MODEL_RUN_HOURS[model], reverse=True)
    for h in runs:
        if h <= now_utc.hour:
            return f"{now_utc:%Y-%m-%d}T{h:02d}Z"
    prev = now_utc - timedelta(days=1)
    return f"{prev:%Y-%m-%d}T{runs[0]:02d}Z"


def lead_bucket_for(target_date: str, fetch_local: datetime) -> str:
    today = fetch_local.date()
    tgt = date.fromisoformat(target_date)
    offset = (tgt - today).days
    if offset >= 3:
        return "d3"
    if offset == 2:
        return "d2"
    if offset == 1:
        return "d1"
    if offset == 0:
        return "d0_early" if fetch_local.hour < 9 else "d0_late"
    return "past"


def parse_ensemble_payload(payload: dict) -> list[ParsedDaily]:
    daily = payload.get("daily", {})
    times: list[str] = daily.get("time", [])
    if not times:
        return []

    member_cols: list[list[float | None]] = [
        v for k, v in daily.items() if _DAILY_MEMBER_RE.match(k)
    ]
    if not member_cols and "temperature_2m_max" in daily:
        member_cols = [daily["temperature_2m_max"]]

    hourly = payload.get("hourly", {})
    hourly_times: list[str] = hourly.get("time", [])
    hourly_member_cols: list[list[float | None]] = [
        v for k, v in hourly.items() if _HOURLY_MEMBER_RE.match(k)
    ]

    out: list[ParsedDaily] = []
    for i, target_date in enumerate(times):
        vals = [c[i] for c in member_cols if i < len(c) and c[i] is not None]
        if not vals:
            continue
        mean_c = sum(vals) / len(vals)
        std_c = statistics.pstdev(vals) if len(vals) > 1 else 0.0

        hourly_for_date: dict[str, list[float]] | None = None
        if hourly_times and hourly_member_cols:
            indices = [j for j, ht in enumerate(hourly_times) if ht.startswith(target_date)]
            if indices:
                hourly_for_date = {}
                for m_idx, col in enumerate(hourly_member_cols):
                    hourly_for_date[f"m{m_idx:02d}"] = [
                        col[j] for j in indices if j < len(col)
                    ]

        out.append(
            ParsedDaily(
                target_date=target_date,
                mean_c=mean_c,
                std_c=std_c,
                members=list(vals),
                hourly_members=hourly_for_date,
            )
        )
    return out


def _run_label_to_ts(run_label: str) -> int:
    dt = datetime.strptime(run_label, "%Y-%m-%dT%HZ").replace(tzinfo=ZoneInfo("UTC"))
    return int(dt.timestamp())


def fetch_ensemble(
    latitude: float, longitude: float, model: str, client: httpx.Client
) -> dict:
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "daily": "temperature_2m_max",
        "hourly": "temperature_2m",
        "models": model,
        "forecast_days": 4,
        "past_days": 1,
    }
    r = client.get(OPEN_METEO_URL, params=params, timeout=30)
    r.raise_for_status()
    archive_raw("ensembles", r.content)
    return r.json()


def persist_forecast(
    station: str,
    model: str,
    parsed: list[ParsedDaily],
    run_label: str,
    fetch_ts: int,
    station_local: datetime,
    db_path: Path = DEFAULT_DB_PATH,
) -> int:
    """Write ensemble_forecasts rows AND mirror into forecast_pairs."""
    if not parsed:
        return 0
    conn = connect(db_path)
    n = 0
    try:
        for p in parsed:
            offset_days = (date.fromisoformat(p.target_date) - station_local.date()).days
            lead_hours = max(0.0, offset_days * 24.0 + (12 - station_local.hour))
            lead_bucket = lead_bucket_for(p.target_date, station_local)
            if lead_bucket == "past":
                continue

            conn.execute(
                "INSERT INTO ensemble_forecasts("
                "  station, target_date, model, run_label, fetch_ts, lead_hours, source,"
                "  members_json, hourly_members_json, mean_c, std_c) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(station, target_date, model, run_label) DO UPDATE SET "
                "  fetch_ts=excluded.fetch_ts, lead_hours=excluded.lead_hours,"
                "  source=excluded.source, members_json=excluded.members_json,"
                "  hourly_members_json=excluded.hourly_members_json,"
                "  mean_c=excluded.mean_c, std_c=excluded.std_c",
                (
                    station,
                    p.target_date,
                    model,
                    run_label,
                    fetch_ts,
                    lead_hours,
                    "live",
                    json.dumps(p.members),
                    json.dumps(p.hourly_members) if p.hourly_members else None,
                    p.mean_c,
                    p.std_c,
                ),
            )

            forecast_run_ts = _run_label_to_ts(run_label)
            usable_after = (
                date.fromisoformat(p.target_date) + timedelta(days=1)
            ).isoformat()
            conn.execute(
                "INSERT INTO forecast_pairs("
                "  station, target_date, lead_bucket, model, run_label,"
                "  forecast_run_ts, forecast_fetch_ts, lead_hours, forecast_source, source_weight,"
                "  usable_for_train_after_date, created_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(station, target_date, lead_bucket, model, run_label, forecast_source) "
                "DO UPDATE SET forecast_fetch_ts=excluded.forecast_fetch_ts,"
                "  lead_hours=excluded.lead_hours",
                (
                    station,
                    p.target_date,
                    lead_bucket,
                    model,
                    run_label,
                    forecast_run_ts,
                    fetch_ts,
                    lead_hours,
                    "live",
                    1.0,
                    usable_after,
                    fetch_ts,
                ),
            )
            n += 1
        conn.commit()
    finally:
        conn.close()
    return n


def fetch_ensembles(
    spec: Spec,
    db_path: Path = DEFAULT_DB_PATH,
    client: httpx.Client | None = None,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    owned = client is None
    if owned:
        client = httpx.Client()
    try:
        now_utc = datetime.now(ZoneInfo("UTC"))
        for station_id, city in spec.resolution.cities.items():
            station_local = datetime.now(ZoneInfo(city.timezone))
            station_total = 0
            for model in MODELS:
                try:
                    payload = fetch_ensemble(city.latitude, city.longitude, model, client)
                except httpx.HTTPError:
                    continue
                parsed = parse_ensemble_payload(payload)
                run_label = infer_run_label(model, now_utc)
                station_total += persist_forecast(
                    station_id,
                    model,
                    parsed,
                    run_label,
                    int(time.time()),
                    station_local,
                    db_path,
                )
            counts[station_id] = station_total
    finally:
        if owned:
            client.close()
    return counts
