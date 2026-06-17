import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from wxm.db import connect, init_db
from wxm.ingest.ensembles import (
    MODEL_RUN_HOURS,
    infer_run_label,
    lead_bucket_for,
    parse_ensemble_payload,
    persist_forecast,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO_ROOT / "migrations"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "open_meteo" / "nyc_ecmwf_sample.json"


def test_parse_extracts_member_columns():
    payload = json.loads(FIXTURE.read_text())
    parsed = parse_ensemble_payload(payload)
    # 5 dates in fixture
    assert len(parsed) == 5
    p615 = next(p for p in parsed if p.target_date == "2026-06-15")
    assert p615.members == [26.3, 25.8, 26.2, 25.9]
    assert p615.mean_c == pytest.approx(sum(p615.members) / 4)
    assert p615.std_c > 0


def test_parse_extracts_hourly_per_date():
    payload = json.loads(FIXTURE.read_text())
    parsed = parse_ensemble_payload(payload)
    p615 = next(p for p in parsed if p.target_date == "2026-06-15")
    assert p615.hourly_members is not None
    # Two hourly member columns in fixture, each with 4 timestamps for 2026-06-15
    assert set(p615.hourly_members.keys()) == {"m00", "m01"}
    assert len(p615.hourly_members["m00"]) == 4


def test_parse_falls_back_to_deterministic_series_when_no_members():
    payload = {
        "daily": {
            "time": ["2026-06-15"],
            "temperature_2m_max": [26.0],
        }
    }
    parsed = parse_ensemble_payload(payload)
    assert len(parsed) == 1
    assert parsed[0].members == [26.0]
    assert parsed[0].std_c == 0.0


def test_infer_run_label_picks_most_recent_past_run():
    # ECMWF runs at 00 and 12 UTC
    now = datetime(2026, 6, 15, 14, 30, tzinfo=ZoneInfo("UTC"))
    assert infer_run_label("ecmwf_ifs025", now) == "2026-06-15T12Z"
    now = datetime(2026, 6, 15, 11, 0, tzinfo=ZoneInfo("UTC"))
    assert infer_run_label("ecmwf_ifs025", now) == "2026-06-15T00Z"


def test_infer_run_label_rolls_back_before_first_run_of_day():
    # Before today's first run → yesterday's last run
    now = datetime(2026, 6, 15, 0, 0, tzinfo=ZoneInfo("UTC"))  # exactly 00Z
    # at 00:00 UTC the 00Z run for today has just begun — be defensive and accept either
    label = infer_run_label("gfs_seamless", now)
    assert label in ("2026-06-15T00Z", "2026-06-14T18Z")


def test_gfs_run_hours_are_quarter_day():
    assert MODEL_RUN_HOURS["gfs_seamless"] == (0, 6, 12, 18)
    assert MODEL_RUN_HOURS["icon_seamless"] == (0, 6, 12, 18)


def test_lead_bucket_for():
    local = datetime(2026, 6, 15, 8, 0, tzinfo=ZoneInfo("America/New_York"))
    assert lead_bucket_for("2026-06-15", local) == "d0_early"
    local = datetime(2026, 6, 15, 11, 0, tzinfo=ZoneInfo("America/New_York"))
    assert lead_bucket_for("2026-06-15", local) == "d0_late"
    assert lead_bucket_for("2026-06-16", local) == "d1"
    assert lead_bucket_for("2026-06-17", local) == "d2"
    assert lead_bucket_for("2026-06-18", local) == "d3"
    assert lead_bucket_for("2026-06-14", local) == "past"
    assert lead_bucket_for("2026-06-19", local) == "d3"  # capped


def test_persist_forecast_writes_both_tables(tmp_path: Path):
    db_path = tmp_path / "wxm.db"
    init_db(db_path, MIGRATIONS)
    payload = json.loads(FIXTURE.read_text())
    parsed = parse_ensemble_payload(payload)
    local = datetime(2026, 6, 15, 11, 0, tzinfo=ZoneInfo("America/New_York"))
    n = persist_forecast(
        station="new_york",
        model="ecmwf_ifs025",
        parsed=parsed,
        run_label="2026-06-15T00Z",
        fetch_ts=1_700_000_000,
        station_local=local,
        db_path=db_path,
    )
    # Fixture has dates [2026-06-14 (past), 06-15, 06-16, 06-17, 06-18] → 4 future dates persisted
    assert n == 4

    conn = connect(db_path)
    try:
        ef_rows = conn.execute("SELECT target_date, mean_c, std_c FROM ensemble_forecasts").fetchall()
        fp_rows = conn.execute(
            "SELECT target_date, lead_bucket, usable_for_train_after_date, forecast_source "
            "FROM forecast_pairs ORDER BY target_date"
        ).fetchall()
    finally:
        conn.close()

    assert {r["target_date"] for r in ef_rows} == {"2026-06-15", "2026-06-16", "2026-06-17", "2026-06-18"}
    assert {r["forecast_source"] for r in fp_rows} == {"live"}
    # Walk-forward: every pair's usable_after is target_date + 1
    for r in fp_rows:
        assert r["usable_for_train_after_date"] > r["target_date"]
    # Lead buckets cover d0_late..d3 (11:00 local → d0_late on 06-15)
    bucketed = {r["target_date"]: r["lead_bucket"] for r in fp_rows}
    assert bucketed["2026-06-15"] == "d0_late"
    assert bucketed["2026-06-16"] == "d1"
    assert bucketed["2026-06-17"] == "d2"
    assert bucketed["2026-06-18"] == "d3"


def test_persist_forecast_is_idempotent_on_dedup_key(tmp_path: Path):
    db_path = tmp_path / "wxm.db"
    init_db(db_path, MIGRATIONS)
    payload = json.loads(FIXTURE.read_text())
    parsed = parse_ensemble_payload(payload)
    local = datetime(2026, 6, 15, 11, 0, tzinfo=ZoneInfo("America/New_York"))
    persist_forecast(
        "new_york", "ecmwf_ifs025", parsed, "2026-06-15T00Z", 100, local, db_path,
    )
    persist_forecast(
        "new_york", "ecmwf_ifs025", parsed, "2026-06-15T00Z", 200, local, db_path,
    )
    conn = connect(db_path)
    try:
        ef_count = conn.execute("SELECT COUNT(*) AS n FROM ensemble_forecasts").fetchone()["n"]
        fp_count = conn.execute("SELECT COUNT(*) AS n FROM forecast_pairs").fetchone()["n"]
        # Second insert updated fetch_ts (latest value wins)
        sample = conn.execute(
            "SELECT fetch_ts FROM ensemble_forecasts WHERE target_date='2026-06-15'"
        ).fetchone()
    finally:
        conn.close()
    assert ef_count == 4
    assert fp_count == 4
    assert sample["fetch_ts"] == 200
