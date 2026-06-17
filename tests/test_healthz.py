import json
import time
from pathlib import Path

from wxm.bridge import write_probs
from wxm.calibrate.eligibility import evaluate_phase0
from wxm.calibrate.pipeline import calibrate_board
from wxm.db import connect, init_db
from wxm.healthz import healthz
from wxm.spec import load_spec

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO_ROOT / "migrations"
SPEC_DIR = REPO_ROOT / "spec"


def _names(checks):
    return {c.name: c.ok for c in checks}


def test_healthz_kill_file_fails_first(tmp_path):
    db_path = tmp_path / "wxm.db"
    init_db(db_path, MIGRATIONS)
    kill = tmp_path / "KILL"
    kill.write_text("halt")
    spec = load_spec(SPEC_DIR)
    checks = healthz(spec, db_path=db_path, bridge_dir=tmp_path / "bridge", kill_file=kill)
    assert _names(checks)["kill"] is False


def test_healthz_db_ok_when_initialized(tmp_path):
    db_path = tmp_path / "wxm.db"
    init_db(db_path, MIGRATIONS)
    spec = load_spec(SPEC_DIR)
    checks = healthz(spec, db_path=db_path, bridge_dir=tmp_path / "bridge",
                     kill_file=tmp_path / "KILL")
    assert _names(checks)["db"] is True


def test_healthz_probs_stale_flagged(tmp_path):
    db_path = tmp_path / "wxm.db"
    init_db(db_path, MIGRATIONS)
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    spec = load_spec(SPEC_DIR)
    old_ts_ms = int((time.time() - spec.trading.risk.stale_probs_max_age_s - 3600) * 1000)
    (bridge / "probs.json").write_text(json.dumps({"schema": 4, "stations": {}}))
    (bridge / ".version").write_text(json.dumps({"run_id": "old", "written_ts": old_ts_ms}))
    checks = healthz(spec, db_path=db_path, bridge_dir=bridge, kill_file=tmp_path / "KILL")
    assert _names(checks)["probs"] is False


def test_healthz_books_missing_flagged(tmp_path):
    db_path = tmp_path / "wxm.db"
    init_db(db_path, MIGRATIONS)
    spec = load_spec(SPEC_DIR)
    checks = healthz(spec, db_path=db_path, bridge_dir=tmp_path / "bridge",
                     kill_file=tmp_path / "KILL")
    assert _names(checks)["books"] is False


def test_healthz_full_green_path(tmp_path):
    db_path = tmp_path / "wxm.db"
    init_db(db_path, MIGRATIONS)
    spec = load_spec(SPEC_DIR)
    now = int(time.time())
    now_ms = now * 1000
    conn = connect(db_path)
    try:
        # Fresh observation, ensemble, book snapshot
        conn.execute(
            "INSERT INTO observations(station, date, source, value, units, fetched_ts, is_settlement_source) "
            "VALUES (?,?,?,?,?,?,?)",
            ("new_york", "2026-06-14", "wunderground", 86.0, "fahrenheit", now, 1),
        )
        conn.execute(
            "INSERT INTO ensemble_forecasts(station, target_date, model, run_label, fetch_ts, "
            " lead_hours, source, members_json, mean_c, std_c) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("new_york", "2026-06-15", "ecmwf_ifs025", "0z", now, 0.0, "live", "[25.0]", 25.0, 0.0),
        )
        conn.execute(
            "INSERT INTO markets(market_id, station, date, bucket_label, bucket_kind,"
            " token_id_yes, token_id_no, discovered_ts, closed) VALUES (?,?,?,?,?,?,?,?,?)",
            ("m-1", "new_york", "2026-06-15", 85.0, "band", "ty", "tn", 0, 0),
        )
        conn.execute(
            "INSERT INTO book_snapshots(market_id, ts, bid1, bid1_sz, ask1, ask1_sz) "
            "VALUES (?,?,?,?,?,?)", ("m-1", now_ms, 0.20, 100, 0.22, 100),
        )
        conn.commit()
    finally:
        conn.close()
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    (bridge / "probs.json").write_text(json.dumps({"schema": 4, "stations": {}}))
    (bridge / ".version").write_text(json.dumps({"run_id": "r1", "written_ts": now_ms}))
    checks = healthz(spec, db_path=db_path, bridge_dir=bridge, kill_file=tmp_path / "KILL")
    statuses = _names(checks)
    assert all(statuses.values()), [
        (c.name, c.detail) for c in checks if not c.ok
    ]
