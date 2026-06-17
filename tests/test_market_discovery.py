import json
from pathlib import Path

import pytest

from wxm.db import connect, init_db
from wxm.ingest.market_discovery import (
    ParsedMarket,
    parse_event,
    parse_market_record,
    upsert_markets,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "gamma" / "event_nyc_sample.json"
MIGRATIONS = REPO_ROOT / "migrations"


def _load_fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def test_parse_band_market():
    event = _load_fixture()
    sub = event["markets"][0]  # be-83
    parsed = parse_market_record(sub, station="new_york")
    assert parsed is not None
    assert parsed.bucket_label == 83.0
    assert parsed.bucket_kind == "band"
    assert parsed.target_date == "2026-06-15"
    assert parsed.token_id_yes == "tok-yes-nyc-83"
    assert parsed.token_id_no == "tok-no-nyc-83"


def test_parse_open_high_tail():
    event = _load_fixture()
    sub = next(m for m in event["markets"] if "89-or-higher" in m["slug"])
    parsed = parse_market_record(sub, station="new_york")
    assert parsed is not None
    assert parsed.bucket_label == 89.0
    assert parsed.bucket_kind == "open_high"


def test_parse_open_low_tail():
    event = _load_fixture()
    sub = next(m for m in event["markets"] if "81-or-lower" in m["slug"])
    parsed = parse_market_record(sub, station="new_york")
    assert parsed is not None
    assert parsed.bucket_label == 81.0
    assert parsed.bucket_kind == "open_low"


def test_token_ids_handles_json_string_or_list():
    event = _load_fixture()
    string_form = event["markets"][0]
    list_form = next(m for m in event["markets"] if "81-or-lower" in m["slug"])
    assert isinstance(string_form["clobTokenIds"], str)
    assert isinstance(list_form["clobTokenIds"], list)
    p1 = parse_market_record(string_form, station="new_york")
    p2 = parse_market_record(list_form, station="new_york")
    assert p1 is not None and p2 is not None
    assert p1.token_id_yes == "tok-yes-nyc-83"
    assert p2.token_id_yes == "tok-yes-nyc-81-lo"


def test_parse_event_returns_only_matching_markets():
    event = _load_fixture()
    parsed = parse_event(event, station="new_york", expected_target_date="2026-06-15")
    # Fixture has 5 weather markets + 1 unrelated.
    assert len(parsed) == 5
    assert {p.bucket_kind for p in parsed} == {"band", "open_high", "open_low"}


def test_parse_event_rejects_wrong_date():
    event = _load_fixture()
    parsed = parse_event(event, station="new_york", expected_target_date="2026-07-01")
    assert parsed == []


def test_band_buckets_tile_the_line():
    """The band labels in the fixture must be contiguous in their geometry.

    For 2°F bands centered on labels {83, 85, 87}, the implied edges are
    [82,84],[84,86],[86,88] — no gaps, no overlaps. The audit will set the
    actual width; this test enforces the property at the fixture level.
    """
    event = _load_fixture()
    parsed = parse_event(event, station="new_york", expected_target_date="2026-06-15")
    bands = sorted(p.bucket_label for p in parsed if p.bucket_kind == "band")
    diffs = [bands[i + 1] - bands[i] for i in range(len(bands) - 1)]
    assert all(d == diffs[0] for d in diffs), "band spacing must be uniform"


def test_upsert_markets_inserts_and_updates(tmp_path: Path):
    db_path = tmp_path / "wxm.db"
    init_db(db_path, MIGRATIONS)
    parsed = [
        ParsedMarket(
            market_id="m-1",
            condition_id="c-1",
            station="new_york",
            target_date="2026-06-15",
            bucket_label=85.0,
            bucket_kind="band",
            token_id_yes="ty",
            token_id_no="tn",
            closed=False,
        )
    ]
    assert upsert_markets(parsed, db_path) == 1
    # Update via second upsert (same market_id, different closed flag)
    parsed[0] = ParsedMarket(
        market_id="m-1",
        condition_id="c-1",
        station="new_york",
        target_date="2026-06-15",
        bucket_label=85.0,
        bucket_kind="band",
        token_id_yes="ty",
        token_id_no="tn",
        closed=True,
    )
    assert upsert_markets(parsed, db_path) == 1
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT closed FROM markets WHERE market_id='m-1'").fetchone()
    finally:
        conn.close()
    assert row["closed"] == 1
