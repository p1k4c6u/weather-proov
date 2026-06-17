import json
import time
from pathlib import Path

import pytest

from wxm.calibrate.eligibility import evaluate_phase0
from wxm.db import connect, init_db
from wxm.ingest.station_truth import DailyMaxObservation, upsert_observation
from wxm.report.daily import collect_metrics, render_markdown, write_daily_report
from wxm.settle.pnl import (
    open_paper_exposure_usd,
    realized_pnl_last_n_days_usd,
    total_realized_pnl_usd,
)
from wxm.settle.settlement import settle_date
from wxm.spec import load_spec

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_DIR = REPO_ROOT / "spec"
MIGRATIONS = REPO_ROOT / "migrations"


def _setup_db(tmp_path: Path):
    db_path = tmp_path / "wxm.db"
    init_db(db_path, MIGRATIONS)
    return db_path


def _seed_nyc_board(db_path: Path, target_date: str = "2026-06-14"):
    """Seed a 2°F floor-to board around the mid-80s for NYC."""
    conn = connect(db_path)
    try:
        # bands: 81,83,85,87 (each covers [label, label+2))
        for label, kind in [(79, "open_low"), (81, "band"), (83, "band"),
                            (85, "band"), (87, "band"), (89, "open_high")]:
            conn.execute(
                "INSERT INTO markets(market_id, station, date, bucket_label, bucket_kind,"
                " token_id_yes, token_id_no, discovered_ts, closed) VALUES (?,?,?,?,?,?,?,?,?)",
                (f"m-ny-{label}", "new_york", target_date, float(label),
                 kind, f"ty-{label}", f"tn-{label}", 0, 0),
            )
        conn.commit()
    finally:
        conn.close()


def _seed_paper_position(db_path: Path, market_id: str, side: str, avg_price: float,
                        size_usd: float, target_date: str = "2026-06-14"):
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO positions(market_id, station, date, side, direction, "
            " avg_price, size_usd, settled, pnl) VALUES (?,?,?,?,?,?,?,?,?)",
            (market_id, "new_york", target_date, side,
             "warm" if side == "buy_yes" else "cold", avg_price, size_usd, 0, None),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_forecast_pair(db_path: Path, target_date: str = "2026-06-14"):
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO forecast_pairs(station, target_date, lead_bucket, model, run_label,"
            " forecast_run_ts, forecast_fetch_ts, lead_hours, forecast_source, source_weight,"
            " usable_for_train_after_date, created_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("new_york", target_date, "d0_late", "ecmwf_ifs025", "2026-06-14T00Z",
             0, 0, 24.0, "live", 1.0, "2026-06-15", 0),
        )
        conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------------------- settlement


def test_settle_open_when_no_observation(tmp_path):
    db_path = _setup_db(tmp_path)
    spec = load_spec(SPEC_DIR)
    out = settle_date(spec, "new_york", "2026-06-14", db_path)
    assert out.state == "OPEN"


def test_settle_final_for_first_publication_only(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed_nyc_board(db_path)
    spec = load_spec(SPEC_DIR)
    upsert_observation(
        DailyMaxObservation(
            "new_york", "2026-06-14", "wunderground", 86.0, "fahrenheit", True,
        ),
        db_path,
    )
    out = settle_date(spec, "new_york", "2026-06-14", db_path)
    assert out.state == "FINAL"
    # 86°F falls in band 85 = [85, 87) under floor_to width=2 provisional geometry
    assert out.winning_label == 85.0
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT * FROM settlements WHERE station='new_york'").fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["bucket_label"] == 85.0


def test_settle_completes_forecast_pairs(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed_nyc_board(db_path)
    _seed_forecast_pair(db_path)
    spec = load_spec(SPEC_DIR)
    upsert_observation(
        DailyMaxObservation(
            "new_york", "2026-06-14", "wunderground", 86.0, "fahrenheit", True,
        ),
        db_path,
    )
    out = settle_date(spec, "new_york", "2026-06-14", db_path)
    assert out.state == "FINAL"
    assert out.pairs_completed == 1
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT y_c, settlement_source, settlement_ts FROM forecast_pairs"
        ).fetchone()
    finally:
        conn.close()
    # 86°F → 30°C in observed_c
    assert row["y_c"] == pytest.approx(30.0, abs=0.01)
    assert row["settlement_source"] == "wunderground"
    assert row["settlement_ts"] is not None


def test_settle_provisional_for_london_revisable(tmp_path):
    """London is revisable_until_next_day. With target_date=today (in fixture),
    settlement stays PROVISIONAL until tomorrow.
    """
    db_path = _setup_db(tmp_path)
    spec = load_spec(SPEC_DIR)
    # use a far-future date so 'today' < target_date
    upsert_observation(
        DailyMaxObservation("london", "2099-12-31", "wunderground", 12.0, "celsius", True),
        db_path,
    )
    out = settle_date(spec, "london", "2099-12-31", db_path)
    assert out.state == "PROVISIONAL"


def test_settle_pnl_for_buy_yes_winner(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed_nyc_board(db_path)
    # Position: buy YES on bucket 85 at avg_price=0.20, size=$10
    _seed_paper_position(db_path, "m-ny-85", "buy_yes", avg_price=0.20, size_usd=10.0)
    # Position: buy YES on losing bucket 83 at avg_price=0.30, size=$10
    _seed_paper_position(db_path, "m-ny-83", "buy_yes", avg_price=0.30, size_usd=10.0)
    upsert_observation(
        DailyMaxObservation("new_york", "2026-06-14", "wunderground", 86.0, "fahrenheit", True),
        db_path,
    )
    spec = load_spec(SPEC_DIR)
    settle_date(spec, "new_york", "2026-06-14", db_path)

    conn = connect(db_path)
    try:
        rows = {r["market_id"]: dict(r) for r in
                conn.execute("SELECT market_id, settled, pnl FROM positions").fetchall()}
    finally:
        conn.close()
    # Winner: bucket 85, buy_yes → pnl = 10 * (1 - 0.20)/0.20 = 40
    assert rows["m-ny-85"]["settled"] == 1
    assert rows["m-ny-85"]["pnl"] == pytest.approx(40.0)
    # Loser: bucket 83, buy_yes → pnl = -10
    assert rows["m-ny-83"]["settled"] == 1
    assert rows["m-ny-83"]["pnl"] == pytest.approx(-10.0)


def test_settle_pnl_for_buy_no_loser_outcome_is_correct(tmp_path):
    """buy_no on a bucket that LOSES should be a winning bet (pays out)."""
    db_path = _setup_db(tmp_path)
    _seed_nyc_board(db_path)
    # buy NO on bucket 83 at avg_price=0.70, size=$10. Observed=86 → bucket 83 loses → NO wins.
    _seed_paper_position(db_path, "m-ny-83", "buy_no", avg_price=0.70, size_usd=10.0)
    upsert_observation(
        DailyMaxObservation("new_york", "2026-06-14", "wunderground", 86.0, "fahrenheit", True),
        db_path,
    )
    spec = load_spec(SPEC_DIR)
    settle_date(spec, "new_york", "2026-06-14", db_path)
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT pnl FROM positions WHERE market_id='m-ny-83'").fetchone()
    finally:
        conn.close()
    # pnl = 10 * (1 - 0.70)/0.70 = 4.2857
    assert row["pnl"] == pytest.approx(10 * 0.3 / 0.7, abs=1e-6)


# ----------------------------------------------------------------- pnl agg


def test_pnl_aggregates(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed_nyc_board(db_path)
    _seed_paper_position(db_path, "m-ny-85", "buy_yes", 0.20, 10.0)
    _seed_paper_position(db_path, "m-ny-83", "buy_yes", 0.30, 10.0)
    upsert_observation(
        DailyMaxObservation("new_york", "2026-06-14", "wunderground", 86.0, "fahrenheit", True),
        db_path,
    )
    spec = load_spec(SPEC_DIR)
    settle_date(spec, "new_york", "2026-06-14", db_path)
    assert total_realized_pnl_usd(db_path) == pytest.approx(40.0 + -10.0)
    # Open exposure: there are no unsettled positions left after settlement
    assert open_paper_exposure_usd(db_path) == 0.0


# ----------------------------------------------------------------- report


def test_daily_report_renders_all_sections(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed_nyc_board(db_path)
    spec = load_spec(SPEC_DIR)
    evaluate_phase0(spec, db_path)
    out = write_daily_report(spec, db_path, reports_dir=tmp_path / "reports", today_iso="2026-06-14")
    text = out.read_text()
    for section in [
        "Forecasts ingested",
        "Markets open",
        "Book coverage",
        "Signals",
        "Fills",
        "Forecast pairs by lead",
        "Eligibility",
        "Recent settlements",
        "Data quality gaps",
        "Paper PnL",
        "Brier-vs-market",
    ]:
        assert section in text, f"missing section: {section}"
    # Eligibility table shows live_eligible=0 for every row
    assert "ROUNDING_UNVERIFIED" in text
