import json
from pathlib import Path

import pytest

from wxm.db import connect, init_db
from wxm.ingest.station_truth import (
    DailyMaxObservation,
    parse_hko_monthly_xml,
    parse_nws_cli_text,
    parse_open_meteo_archive,
    upsert_observation,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "truth"
MIGRATIONS = REPO_ROOT / "migrations"


def test_parse_hko_monthly_xml():
    xml_text = (FIXTURES / "hko_2026_06.xml").read_text()
    val = parse_hko_monthly_xml(xml_text, "2026-06-15")
    assert val == pytest.approx(31.2)


def test_parse_hko_monthly_xml_missing_date_returns_none():
    xml_text = (FIXTURES / "hko_2026_06.xml").read_text()
    assert parse_hko_monthly_xml(xml_text, "2026-06-30") is None


def test_parse_hko_monthly_xml_invalid_xml():
    assert parse_hko_monthly_xml("not xml", "2026-06-15") is None


def test_parse_open_meteo_archive():
    payload = json.loads((FIXTURES / "open_meteo_archive_eglc_2026_06_15.json").read_text())
    val = parse_open_meteo_archive(payload, "2026-06-15")
    assert val == pytest.approx(22.4)


def test_parse_open_meteo_archive_wrong_date_returns_none():
    payload = json.loads((FIXTURES / "open_meteo_archive_eglc_2026_06_15.json").read_text())
    assert parse_open_meteo_archive(payload, "2026-06-14") is None


def test_parse_open_meteo_archive_empty():
    assert parse_open_meteo_archive({}, "2026-06-15") is None
    assert parse_open_meteo_archive({"daily": {"time": [], "temperature_2m_max": []}}, "2026-06-15") is None


def test_parse_open_meteo_archive_null_value():
    payload = {"daily": {"time": ["2026-06-15"], "temperature_2m_max": [None]}}
    assert parse_open_meteo_archive(payload, "2026-06-15") is None


def test_parse_nws_cli_text():
    text = (FIXTURES / "nws_cli_knyc_2026_06_15.txt").read_text()
    val = parse_nws_cli_text(text, "2026-06-15")
    assert val == 87


def test_upsert_observation_round_trip(tmp_path: Path):
    db_path = tmp_path / "wxm.db"
    init_db(db_path, MIGRATIONS)
    obs = DailyMaxObservation(
        station="hong_kong",
        date="2026-06-15",
        source="hko_extract",
        value=31.2,
        units="celsius",
        is_settlement_source=True,
    )
    upsert_observation(obs, db_path)
    upsert_observation(obs, db_path)  # idempotent under same key
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM observations WHERE station='hong_kong' AND date='2026-06-15'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["value"] == 31.2
    assert rows[0]["units"] == "celsius"
    assert rows[0]["is_settlement_source"] == 1


def test_upsert_observation_allows_multiple_sources_same_date(tmp_path: Path):
    db_path = tmp_path / "wxm.db"
    init_db(db_path, MIGRATIONS)
    upsert_observation(
        DailyMaxObservation("london", "2026-06-15", "open_meteo_archive", 22.4, "celsius", True),
        db_path,
    )
    upsert_observation(
        DailyMaxObservation("new_york", "2026-06-15", "nws_cli", 87.0, "fahrenheit", True),
        db_path,
    )
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT station, source, is_settlement_source FROM observations "
            "WHERE date='2026-06-15' ORDER BY station"
        ).fetchall()
    finally:
        conn.close()
    assert [r["source"] for r in rows] == ["open_meteo_archive", "nws_cli"]
    assert all(r["is_settlement_source"] == 1 for r in rows)
