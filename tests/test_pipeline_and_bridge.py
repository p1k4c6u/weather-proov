import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from wxm.bridge import build_payload, probs_age_seconds, read_probs, write_probs
from wxm.calibrate.eligibility import evaluate_phase0, load_eligibility_for_lead
from wxm.calibrate.pipeline import (
    PROVISIONAL_GEOMETRY,
    blend_models,
    calibrate_board,
    count_forecast_pairs,
    write_emos_params_row,
)
from wxm.db import connect, init_db
from wxm.ingest.ensembles import parse_ensemble_payload, persist_forecast
from wxm.spec import load_spec

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO_ROOT / "migrations"
SPEC_DIR = REPO_ROOT / "spec"
ENSEMBLE_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "open_meteo" / "nyc_ecmwf_sample.json"


def _seed_full_db(tmp_path, target_date="2026-06-15"):
    db_path = tmp_path / "wxm.db"
    init_db(db_path, MIGRATIONS)
    # Seed markets for new_york
    conn = connect(db_path)
    try:
        for label, kind in [(81, "open_low"), (83, "band"), (85, "band"), (87, "band"),
                            (89, "band"), (91, "open_high")]:
            conn.execute(
                "INSERT INTO markets(market_id, station, date, bucket_label, bucket_kind,"
                " token_id_yes, token_id_no, discovered_ts, closed) VALUES (?,?,?,?,?,?,?,?,?)",
                (f"m-ny-{target_date}-{label}", "new_york", target_date, float(label),
                 kind, f"ty-{label}", f"tn-{label}", 0, 0),
            )
        conn.commit()
    finally:
        conn.close()

    # Seed ensemble forecasts for new_york via the real ingest path
    payload = json.loads(ENSEMBLE_FIXTURE.read_text())
    parsed = parse_ensemble_payload(payload)
    local = datetime(2026, 6, 15, 11, 0, tzinfo=ZoneInfo("America/New_York"))
    persist_forecast(
        "new_york", "ecmwf_ifs025", parsed, "2026-06-15T00Z", int(time.time()),
        local, db_path,
    )
    return db_path


def test_blend_models_equal_weight():
    rows = [
        {"mean_c": 25.0, "std_c": 1.0},
        {"mean_c": 27.0, "std_c": 1.5},
        {"mean_c": 26.0, "std_c": 2.0},
    ]
    mu, sigma2, n = blend_models(rows)
    assert n == 3
    assert mu == pytest.approx((25 + 27 + 26) / 3)
    assert sigma2 > 0


def test_calibrate_board_end_to_end(tmp_path):
    db_path = _seed_full_db(tmp_path)
    spec = load_spec(SPEC_DIR)
    pred = calibrate_board(spec, "new_york", "2026-06-15", "d0_late", db_path)
    assert pred is not None
    # Bucket probs sum to 1
    assert sum(pred.bucket_probs.values()) == pytest.approx(1.0)
    # Phase 0: geometry is provisional because rounding_verified=false in fixture spec
    assert pred.geometry_provisional is True
    # Forecast mean should be near fixture ensemble mean
    assert 25.5 <= pred.forecast.mu_c <= 26.5


def test_calibrate_board_uses_provisional_geometry_for_unverified_station(tmp_path):
    db_path = _seed_full_db(tmp_path)
    spec = load_spec(SPEC_DIR)
    pred = calibrate_board(spec, "new_york", "2026-06-15", "d0_late", db_path)
    # Provisional geometry for new_york is (floor_to, 2.0) per pipeline.PROVISIONAL_GEOMETRY
    assert PROVISIONAL_GEOMETRY["new_york"] == ("floor_to", 2.0)
    # Bands should respect floor_to width=2
    bands = [b for b in pred.buckets if b.kind == "band"]
    bands.sort(key=lambda b: b.label)
    for b in bands:
        assert b.hi - b.lo == pytest.approx(2.0)


def test_write_emos_params_row_round_trip(tmp_path):
    db_path = tmp_path / "wxm.db"
    init_db(db_path, MIGRATIONS)
    write_emos_params_row(db_path, "new_york", "2026-06-15", "d0_late", n_live=0, n_backfill=0)
    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM emos_params").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["stage"] == "S0"
    assert rows[0]["complexity_level"] == "climatology"
    assert rows[0]["model"] == "_climatology"


def test_count_forecast_pairs_only_settled(tmp_path):
    db_path = _seed_full_db(tmp_path)
    n_live, n_backfill = count_forecast_pairs(db_path, "new_york", "d0_late")
    # forecast_pairs rows exist but none are settled yet (y_c is NULL)
    assert n_live == 0 and n_backfill == 0


def test_eligibility_phase0_all_paper(tmp_path):
    db_path = _seed_full_db(tmp_path)
    spec = load_spec(SPEC_DIR)
    failed = evaluate_phase0(spec, db_path)
    # Every (station, lead_bucket) must be paper-locked
    assert all(v == "ROUNDING_UNVERIFIED" for v in failed.values())
    for station in spec.resolution.cities:
        for lead in ("d0_early", "d0_late", "d1", "d2", "d3"):
            elig = load_eligibility_for_lead(db_path, station, lead)
            assert elig is not None
            assert elig["live_eligible"] == 0
            assert elig["near_mean_eligible"] == 0
            assert elig["tail_eligible"] == 0
            assert elig["failed_gate"] == "ROUNDING_UNVERIFIED"


def test_bridge_writes_probs_and_version_atomically(tmp_path, monkeypatch):
    db_path = _seed_full_db(tmp_path)
    spec = load_spec(SPEC_DIR)
    evaluate_phase0(spec, db_path)
    pred = calibrate_board(spec, "new_york", "2026-06-15", "d0_late", db_path)
    assert pred is not None
    bridge_dir = tmp_path / "bridge"
    target = write_probs(spec, {"new_york": [pred]}, db_path, bridge_dir=bridge_dir)
    assert target.exists()
    # Version sidecar exists with both fields
    version = json.loads((bridge_dir / ".version").read_text())
    assert "run_id" in version and "written_ts" in version
    # Main file is a complete payload
    body = json.loads(target.read_text())
    assert body["schema"] == 4
    assert body["run_id"] == version["run_id"]
    # tmp file must not linger
    assert not (bridge_dir / "probs.json.tmp").exists()


def test_bridge_payload_marks_geometry_provisional_and_paper(tmp_path):
    db_path = _seed_full_db(tmp_path)
    spec = load_spec(SPEC_DIR)
    evaluate_phase0(spec, db_path)
    pred = calibrate_board(spec, "new_york", "2026-06-15", "d0_late", db_path)
    payload = build_payload(
        spec, {"new_york": [pred]}, run_id="r1", written_ts_ms=1000, db_path=db_path,
    )
    date_block = payload["stations"]["new_york"]["dates"]["2026-06-15"]
    assert date_block["geometry_provisional"] is True
    e = date_block["eligibility"]
    assert e["live_eligible"] is False
    assert e["near_mean_eligible"] is False
    assert e["tail_eligible"] is False
    assert e["failed_gate"] == "ROUNDING_UNVERIFIED"


def test_bridge_serializes_open_tails_with_null_bounds(tmp_path):
    db_path = _seed_full_db(tmp_path)
    spec = load_spec(SPEC_DIR)
    evaluate_phase0(spec, db_path)
    pred = calibrate_board(spec, "new_york", "2026-06-15", "d0_late", db_path)
    payload = build_payload(spec, {"new_york": [pred]}, "r1", 1000, db_path)
    buckets = payload["stations"]["new_york"]["dates"]["2026-06-15"]["buckets"]
    tails = [b for b in buckets if b["kind"] in ("open_low", "open_high")]
    assert tails  # the fixture seeds at least one tail
    for t in tails:
        if t["kind"] == "open_low":
            assert t["lo"] is None  # -inf serialized as null
        if t["kind"] == "open_high":
            assert t["hi"] is None
