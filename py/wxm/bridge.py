"""Python → Rust bridge.

Atomically writes ``data/bridge/probs.json`` + ``data/bridge/.version`` per spec
PART VII (schema v4). The Python paper executor in Phase 0 reads the same file
so the schema is exercised end-to-end from day one.

Atomicity: write to ``probs.json.tmp`` then ``os.rename`` to ``probs.json``. The
sidecar ``.version`` carries ``{run_id, written_ts}`` so consumers can stat-poll
without re-parsing the main file.
"""

from __future__ import annotations

import json
import math
import os
import time
import uuid
from pathlib import Path

from .calibrate.eligibility import load_eligibility_for_lead
from .calibrate.pipeline import BoardPrediction
from .spec import Spec

SCHEMA_VERSION = 4
BRIDGE_DIR = Path("data/bridge")


def _serialize_bucket(b, p: float) -> dict:
    return {
        "label": b.label,
        "kind": b.kind,
        "lo": None if math.isinf(b.lo) else b.lo,
        "hi": None if math.isinf(b.hi) else b.hi,
        "p": p,
        "market_id": b.market_id,
        "token_yes": b.token_id_yes,
        "token_no": b.token_id_no,
    }


def build_payload(
    spec: Spec,
    predictions_by_station: dict[str, list[BoardPrediction]],
    run_id: str,
    written_ts_ms: int,
    db_path: Path,
) -> dict:
    """Build the probs.json payload."""
    stations_block: dict[str, dict] = {}
    for station_id, city in spec.resolution.cities.items():
        boards = predictions_by_station.get(station_id, [])
        date_blocks: dict[str, dict] = {}
        forecast_d0_c: float | None = None
        for pred in boards:
            elig = (
                load_eligibility_for_lead(db_path, station_id, pred.forecast.lead_bucket)
                or {"live_eligible": 0, "near_mean_eligible": 0, "tail_eligible": 0,
                    "failed_gate": "UNINITIALIZED", "stage": "S0"}
            )
            if pred.forecast.lead_bucket.startswith("d0"):
                forecast_d0_c = pred.forecast.mu_c
            date_blocks[pred.forecast.target_date] = {
                "lead": pred.forecast.lead_bucket,
                "geometry_provisional": pred.geometry_provisional,
                "eligibility": {
                    "stage": elig.get("stage", "S0"),
                    "live_eligible": bool(elig.get("live_eligible", 0)),
                    "near_mean_eligible": bool(elig.get("near_mean_eligible", 0)),
                    "tail_eligible": bool(elig.get("tail_eligible", 0)),
                    "failed_gate": elig.get("failed_gate"),
                },
                "mixture": [
                    {"w": 1.0, "mu_c": pred.forecast.mu_c, "sigma_c": pred.forecast.sigma_c}
                ],
                "buckets": [
                    _serialize_bucket(b, pred.bucket_probs.get(b.label, 0.0))
                    for b in pred.buckets
                ],
                "exceedance": {},
            }
        stations_block[station_id] = {
            "label_units": city.buckets.label_units,
            "peak_local_hour": city.chain.peak_local_hour,
            "rho": city.chain.rho,
            "e1": None,
            "eps_minus1_c": None,
            "forecast_d0_c": forecast_d0_c,
            "dates": date_blocks,
        }
    return {
        "schema": SCHEMA_VERSION,
        "run_id": run_id,
        "written_ts": written_ts_ms,
        "stations": stations_block,
    }


def write_probs(
    spec: Spec,
    predictions_by_station: dict[str, list[BoardPrediction]],
    db_path: Path,
    bridge_dir: Path = BRIDGE_DIR,
    run_id: str | None = None,
) -> Path:
    bridge_dir.mkdir(parents=True, exist_ok=True)
    run_id = run_id or f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    written_ts_ms = int(time.time() * 1000)
    payload = build_payload(spec, predictions_by_station, run_id, written_ts_ms, db_path)
    target = bridge_dir / "probs.json"
    tmp = bridge_dir / "probs.json.tmp"
    tmp.write_text(json.dumps(payload, indent=2, allow_nan=False))
    os.replace(tmp, target)
    version_path = bridge_dir / ".version"
    version_path.write_text(json.dumps({"run_id": run_id, "written_ts": written_ts_ms}))
    return target


def read_probs(bridge_dir: Path = BRIDGE_DIR) -> dict | None:
    target = bridge_dir / "probs.json"
    if not target.exists():
        return None
    return json.loads(target.read_text())


def probs_age_seconds(bridge_dir: Path = BRIDGE_DIR) -> float | None:
    version_path = bridge_dir / ".version"
    if not version_path.exists():
        return None
    info = json.loads(version_path.read_text())
    return time.time() - info["written_ts"] / 1000
