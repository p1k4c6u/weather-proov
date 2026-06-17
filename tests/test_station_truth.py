import json
from pathlib import Path

import pytest

from wxm.db import connect, init_db
from wxm.ingest.station_truth import (
    DailyMaxObservation,
    extract_wu_api_key,
    parse_hko_monthly_xml,
    parse_nws_cli_text,
    parse_wu_historical_json,
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


def test_parse_wu_historical_json_celsius():
    payload = json.loads((FIXTURES / "wu_knyc_2026_06_15.json").read_text())
    result = parse_wu_historical_json(payload, units_hint="m")
    assert result is not None
    value, units = result
    assert value == 87
    assert units == "celsius"  # the units come from the units_hint, not the value


def test_parse_wu_historical_json_fahrenheit():
    payload = json.loads((FIXTURES / "wu_knyc_2026_06_15.json").read_text())
    result = parse_wu_historical_json(payload, units_hint="e")
    assert result is not None
    value, units = result
    assert value == 87
    assert units == "fahrenheit"


def test_parse_wu_historical_json_empty():
    assert parse_wu_historical_json({}, "m") is None
    assert parse_wu_historical_json({"observations": []}, "m") is None
    assert parse_wu_historical_json({"observations": [{"foo": "bar"}]}, "m") is None


def test_extract_wu_api_key():
    page_html = '<script>var config={apiKey: "abc123def456ghi789jkl0", region: "us"};</script>'
    assert extract_wu_api_key(page_html) == "abc123def456ghi789jkl0"
    assert extract_wu_api_key("no key here") is None


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
        DailyMaxObservation("new_york", "2026-06-15", "wunderground", 87.0, "fahrenheit", True),
        db_path,
    )
    upsert_observation(
        DailyMaxObservation("new_york", "2026-06-15", "nws_cli", 87.0, "fahrenheit", False),
        db_path,
    )
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT source, is_settlement_source FROM observations WHERE station='new_york' "
            "AND date='2026-06-15' ORDER BY source"
        ).fetchall()
    finally:
        conn.close()
    assert [r["source"] for r in rows] == ["nws_cli", "wunderground"]
    assert [r["is_settlement_source"] for r in rows] == [0, 1]
