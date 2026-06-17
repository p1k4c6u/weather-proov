import json
import time
from pathlib import Path

import pytest

from wxm.db import connect, init_db
from wxm.execute.paper import (
    CandidateSignal,
    evaluate_bucket,
    kelly_stake,
    persist_signal,
    run_once,
    simulate_fill,
)
from wxm.spec import load_spec

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_DIR = REPO_ROOT / "spec"
MIGRATIONS = REPO_ROOT / "migrations"


def _spec():
    return load_spec(SPEC_DIR)


def _seed_market(db_path: Path, market_id: str, station: str = "new_york", target_date: str = "2026-06-15"):
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO markets(market_id, station, date, bucket_label, bucket_kind,"
            " token_id_yes, token_id_no, discovered_ts, closed) VALUES (?,?,?,?,?,?,?,?,?)",
            (market_id, station, target_date, 85.0, "band", "ty", "tn", 0, 0),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_book(db_path: Path, market_id: str, bid: float, ask: float, ts_ms: int | None = None):
    ts_ms = ts_ms or int(time.time() * 1000)
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO book_snapshots(market_id, ts, bid1, bid1_sz, ask1, ask1_sz) "
            "VALUES (?,?,?,?,?,?)",
            (market_id, ts_ms, bid, 100.0, ask, 100.0),
        )
        conn.commit()
    finally:
        conn.close()


def _set_eligibility(db_path: Path, station: str, lead: str, live: bool):
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO eligibility(station, lead_bucket, stage, n_live, n_total, effective_n,"
            " near_mean_eligible, tail_eligible, live_eligible, failed_gate, evaluated_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(station, lead_bucket) DO UPDATE SET live_eligible=excluded.live_eligible,"
            " failed_gate=excluded.failed_gate",
            (station, lead, "S2", 100, 100, 100.0, 1, 1, int(live), None if live else "ROUNDING_UNVERIFIED", 0),
        )
        conn.commit()
    finally:
        conn.close()


def _bucket(p: float = 0.30, label: float = 85.0, market_id: str = "m-1") -> dict:
    return {
        "label": label,
        "kind": "band",
        "lo": 84.0,
        "hi": 86.0,
        "p": p,
        "market_id": market_id,
        "token_yes": "ty",
        "token_no": "tn",
    }


# ----------------------------------------------------------- evaluate_bucket


def test_evaluate_bucket_picks_yes_when_yes_edge_positive():
    spec = _spec()
    book = {"bid1": 0.18, "ask1": 0.20}
    cand = evaluate_bucket(
        spec, "new_york", "2026-06-15", "d0_late", _bucket(p=0.30), book,
        eligibility_live=False, geometry_provisional=True,
        probs_version="r1", trigger="t",
    )
    assert cand is not None
    assert cand.side == "buy_yes"
    # edge = 0.30 - 0.20 - 0 - 0 = 0.10
    assert cand.edge_after_fees == pytest.approx(0.10)
    assert cand.triggered is True  # 0.10 > min_edge_after_fees[d0]=0.04


def test_evaluate_bucket_picks_no_when_no_edge_better():
    spec = _spec()
    # p_yes=0.10, bid_yes=0.04 → ask_no = 0.96, p_no=0.90, edge_no = 0.90 - 0.96 = -0.06
    # Or with deeper market: bid_yes=0.02 → ask_no=0.98, p_no=0.90, edge_no = -0.08
    # Make it positive: p_yes=0.10, bid_yes=0.20 → ask_no=0.80, p_no=0.90, edge_no=0.10
    book = {"bid1": 0.20, "ask1": 0.30}
    cand = evaluate_bucket(
        spec, "new_york", "2026-06-15", "d0_late", _bucket(p=0.10), book,
        eligibility_live=False, geometry_provisional=True,
        probs_version="r1", trigger="t",
    )
    assert cand is not None
    assert cand.side == "buy_no"
    assert cand.edge_after_fees == pytest.approx(0.10)
    assert cand.triggered is True


def test_evaluate_bucket_skips_yes_below_min_model_prob():
    """min_model_prob_for_yes=0.05 (tail humility). YES with p_yes=0.04 forbidden."""
    spec = _spec()
    book = {"bid1": 0.01, "ask1": 0.02}
    cand = evaluate_bucket(
        spec, "new_york", "2026-06-15", "d0_late", _bucket(p=0.04), book,
        eligibility_live=True, geometry_provisional=False,
        probs_version="r1", trigger="t",
    )
    assert cand is not None
    # Edge YES would be 0.04-0.02=0.02 but p_yes < min_p_yes → side falls to NO
    assert cand.side == "buy_no"


def test_evaluate_bucket_returns_none_when_book_missing():
    spec = _spec()
    cand = evaluate_bucket(
        spec, "new_york", "2026-06-15", "d0_late", _bucket(), None,
        eligibility_live=False, geometry_provisional=True,
        probs_version="r1", trigger="t",
    )
    assert cand is None


def test_evaluate_bucket_not_triggered_below_threshold():
    spec = _spec()
    # p=0.21, ask=0.20 → edge=0.01 < min_edge_after_fees[d0]=0.04
    book = {"bid1": 0.18, "ask1": 0.20}
    cand = evaluate_bucket(
        spec, "new_york", "2026-06-15", "d0_late", _bucket(p=0.21), book,
        eligibility_live=True, geometry_provisional=False,
        probs_version="r1", trigger="t",
    )
    assert cand is not None
    assert cand.triggered is False
    assert cand.failed_reason is not None


# ------------------------------------------------------------------- Kelly


def test_kelly_stake_zero_when_no_edge():
    assert kelly_stake(p=0.3, q=0.30, kelly_fraction=0.25, bankroll_usd=1000) == 0


def test_kelly_stake_proportional_to_edge():
    s_small = kelly_stake(p=0.31, q=0.30, kelly_fraction=0.25, bankroll_usd=1000)
    s_large = kelly_stake(p=0.40, q=0.30, kelly_fraction=0.25, bankroll_usd=1000)
    assert 0 < s_small < s_large


# ------------------------------------------------------------- persistence


def test_persist_signal_writes_indicator_json(tmp_path: Path):
    db_path = tmp_path / "wxm.db"
    init_db(db_path, MIGRATIONS)
    _seed_market(db_path, "m-1")
    spec = _spec()
    cand = evaluate_bucket(
        spec, "new_york", "2026-06-15", "d0_late",
        _bucket(p=0.30, market_id="m-1"), {"bid1": 0.18, "ask1": 0.20},
        eligibility_live=False, geometry_provisional=True,
        probs_version="r1", trigger="probs_reload",
    )
    persist_signal(db_path, cand, acted=False)
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT * FROM signals").fetchone()
    finally:
        conn.close()
    assert row is not None
    body = json.loads(row["indicator_json"])
    assert "I1" in body
    assert body["I1"]["p_model"] == pytest.approx(0.30)
    assert row["acted"] == 0


def test_eligibility_interlock_paper_locks_acted(tmp_path: Path):
    """Eligibility live_eligible=0 must force acted=0 even when threshold passes."""
    db_path = tmp_path / "wxm.db"
    init_db(db_path, MIGRATIONS)
    _seed_market(db_path, "m-1")
    _seed_book(db_path, "m-1", bid=0.18, ask=0.20)
    _set_eligibility(db_path, "new_york", "d0_late", live=False)

    spec = _spec()
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    payload = {
        "schema": 4,
        "run_id": "r-test",
        "written_ts": int(time.time() * 1000),
        "stations": {
            "new_york": {
                "label_units": "fahrenheit",
                "peak_local_hour": 15.5,
                "dates": {
                    "2026-06-15": {
                        "lead": "d0_late",
                        "geometry_provisional": True,
                        "eligibility": {"live_eligible": False, "near_mean_eligible": False,
                                        "tail_eligible": False, "failed_gate": "ROUNDING_UNVERIFIED",
                                        "stage": "S0"},
                        "mixture": [{"w": 1.0, "mu_c": 30.0, "sigma_c": 2.0}],
                        "buckets": [_bucket(p=0.30, market_id="m-1")],
                        "exceedance": {},
                    }
                }
            }
        }
    }
    (bridge_dir / "probs.json").write_text(json.dumps(payload))
    (bridge_dir / ".version").write_text(json.dumps({"run_id": "r-test", "written_ts": payload["written_ts"]}))

    stats = run_once(spec, db_path=db_path, bridge_dir=bridge_dir)
    assert stats["signals"] == 1
    assert stats["fills"] == 0  # paper-locked

    conn = connect(db_path)
    try:
        signals = conn.execute("SELECT * FROM signals").fetchall()
        orders = conn.execute("SELECT * FROM orders").fetchall()
        fills = conn.execute("SELECT * FROM fills").fetchall()
    finally:
        conn.close()
    assert len(signals) == 1 and signals[0]["acted"] == 0
    assert orders == [] and fills == []


def test_eligibility_live_unlocks_fill(tmp_path: Path):
    """When eligibility is live, an acted signal produces a paper fill + position."""
    db_path = tmp_path / "wxm.db"
    init_db(db_path, MIGRATIONS)
    _seed_market(db_path, "m-1")
    _seed_book(db_path, "m-1", bid=0.18, ask=0.20)
    _set_eligibility(db_path, "new_york", "d0_late", live=True)

    spec = _spec()
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    payload = {
        "schema": 4,
        "run_id": "r-test",
        "written_ts": int(time.time() * 1000),
        "stations": {
            "new_york": {
                "label_units": "fahrenheit",
                "peak_local_hour": 15.5,
                "dates": {
                    "2026-06-15": {
                        "lead": "d0_late",
                        "geometry_provisional": False,
                        "eligibility": {"live_eligible": True, "near_mean_eligible": True,
                                        "tail_eligible": False, "failed_gate": None,
                                        "stage": "S2"},
                        "mixture": [{"w": 1.0, "mu_c": 30.0, "sigma_c": 2.0}],
                        "buckets": [_bucket(p=0.30, market_id="m-1")],
                        "exceedance": {},
                    }
                }
            }
        }
    }
    (bridge_dir / "probs.json").write_text(json.dumps(payload))
    (bridge_dir / ".version").write_text(json.dumps({"run_id": "r-test", "written_ts": payload["written_ts"]}))

    stats = run_once(spec, db_path=db_path, bridge_dir=bridge_dir)
    assert stats["signals"] == 1
    assert stats["fills"] == 1

    conn = connect(db_path)
    try:
        orders = conn.execute("SELECT * FROM orders").fetchall()
        positions = conn.execute("SELECT * FROM positions").fetchall()
    finally:
        conn.close()
    assert len(orders) == 1 and orders[0]["mode"] == "paper"
    assert orders[0]["status"] == "filled"
    assert len(positions) == 1
    assert positions[0]["direction"] == "warm"
    assert positions[0]["size_usd"] > 0


def test_stale_probs_disables_signal_generation(tmp_path: Path):
    db_path = tmp_path / "wxm.db"
    init_db(db_path, MIGRATIONS)
    _seed_market(db_path, "m-1")
    _seed_book(db_path, "m-1", bid=0.18, ask=0.20)
    spec = _spec()
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    # ts older than stale_probs_max_age_s
    old_ts_ms = int((time.time() - spec.trading.risk.stale_probs_max_age_s - 60) * 1000)
    payload = {
        "schema": 4, "run_id": "old", "written_ts": old_ts_ms,
        "stations": {"new_york": {"label_units": "fahrenheit", "peak_local_hour": 15.5,
            "dates": {"2026-06-15": {"lead": "d0_late", "geometry_provisional": True,
                "eligibility": {"live_eligible": False, "near_mean_eligible": False,
                                "tail_eligible": False, "failed_gate": "ROUNDING_UNVERIFIED", "stage": "S0"},
                "mixture": [{"w": 1.0, "mu_c": 30.0, "sigma_c": 2.0}],
                "buckets": [_bucket(p=0.30, market_id="m-1")], "exceedance": {}}}}}
    }
    (bridge_dir / "probs.json").write_text(json.dumps(payload))
    stats = run_once(spec, db_path=db_path, bridge_dir=bridge_dir)
    assert stats == {"signals": 0, "fills": 0}


def test_no_probs_file_returns_zeros(tmp_path: Path):
    spec = _spec()
    stats = run_once(spec, db_path=tmp_path / "wxm.db", bridge_dir=tmp_path / "bridge-missing")
    assert stats == {"signals": 0, "fills": 0}
