import json
from pathlib import Path

from wxm.db import connect, init_db
from wxm.ingest.book_recorder import (
    BookState,
    apply_book_event,
    apply_price_change,
    load_open_token_ids,
    persist_snapshot,
    process_message,
    should_snapshot,
    snapshot_row,
    top3_asks,
    top3_bids,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO_ROOT / "migrations"


def test_apply_book_event_populates_levels():
    s = BookState()
    apply_book_event(
        s,
        {
            "event_type": "book",
            "asset_id": "tok",
            "bids": [
                {"price": "0.45", "size": "100"},
                {"price": "0.44", "size": "200"},
                {"price": "0.43", "size": "50"},
            ],
            "asks": [
                {"price": "0.55", "size": "100"},
                {"price": "0.56", "size": "30"},
            ],
        },
        now_ms=1_700_000_000_000,
    )
    assert top3_bids(s) == [(0.45, 100.0), (0.44, 200.0), (0.43, 50.0)]
    assert top3_asks(s) == [(0.55, 100.0), (0.56, 30.0)]


def test_apply_book_event_drops_zero_size_levels():
    s = BookState()
    apply_book_event(
        s,
        {"bids": [{"price": "0.45", "size": "0"}, {"price": "0.44", "size": "100"}], "asks": []},
        now_ms=1,
    )
    assert top3_bids(s) == [(0.44, 100.0)]


def test_price_change_updates_and_removes_levels():
    s = BookState()
    apply_book_event(
        s,
        {
            "bids": [{"price": "0.45", "size": "100"}, {"price": "0.44", "size": "200"}],
            "asks": [{"price": "0.55", "size": "100"}],
        },
        now_ms=1,
    )
    apply_price_change(
        s,
        {
            "changes": [
                {"price": "0.45", "side": "BUY", "size": "0"},   # remove
                {"price": "0.46", "side": "BUY", "size": "150"}, # add
                {"price": "0.54", "side": "SELL", "size": "50"}, # add new best ask
            ]
        },
        now_ms=2,
    )
    assert top3_bids(s) == [(0.46, 150.0), (0.44, 200.0)]
    assert top3_asks(s) == [(0.54, 50.0), (0.55, 100.0)]


def test_snapshot_row_pads_missing_levels_with_none():
    s = BookState()
    apply_book_event(
        s,
        {"bids": [{"price": "0.45", "size": "100"}], "asks": [{"price": "0.55", "size": "100"}]},
        now_ms=1,
    )
    row = snapshot_row(s, "mkt-1", ts_ms=1_700_000_000_000)
    assert row[0] == "mkt-1"
    assert row[1] == 1_700_000_000_000
    assert row[2] == 0.45 and row[3] == 100.0
    assert row[4] is None and row[5] is None
    assert row[8] == 0.55 and row[9] == 100.0


def test_should_snapshot_respects_interval():
    s = BookState()
    s.last_event_ts = 1_000_000
    s.last_snapshot_ts = 1_000_000
    assert should_snapshot(s, now_ms=1_000_000 + 15_000, interval_s=15) is True
    assert should_snapshot(s, now_ms=1_000_000 + 14_000, interval_s=15) is False


def test_should_snapshot_false_until_first_event():
    s = BookState()
    assert should_snapshot(s, now_ms=999_999, interval_s=15) is False


def test_process_message_handles_batch_and_triggers_snapshot_on_trade():
    state_by_token: dict[str, BookState] = {}
    raw = json.dumps(
        [
            {
                "event_type": "book",
                "asset_id": "tok",
                "bids": [{"price": "0.45", "size": "100"}],
                "asks": [{"price": "0.55", "size": "100"}],
            },
            {"event_type": "last_trade_price", "asset_id": "tok", "price": "0.50", "size": "1"},
        ]
    )
    to_snap = process_message(state_by_token, raw, now_ms=1)
    assert "tok" in to_snap
    assert "tok" in state_by_token
    assert top3_bids(state_by_token["tok"]) == [(0.45, 100.0)]


def test_process_message_ignores_unknown_event_type():
    state_by_token: dict[str, BookState] = {}
    raw = json.dumps({"event_type": "weird", "asset_id": "tok"})
    to_snap = process_message(state_by_token, raw, now_ms=1)
    assert to_snap == []


def test_process_message_ignores_invalid_json():
    state_by_token: dict[str, BookState] = {}
    to_snap = process_message(state_by_token, b"not json", now_ms=1)
    assert to_snap == []
    assert state_by_token == {}


def test_persist_snapshot_round_trip(tmp_path: Path):
    db_path = tmp_path / "wxm.db"
    init_db(db_path, MIGRATIONS)
    s = BookState()
    apply_book_event(
        s,
        {"bids": [{"price": "0.45", "size": "100"}], "asks": [{"price": "0.55", "size": "50"}]},
        now_ms=1,
    )
    conn = connect(db_path)
    try:
        persist_snapshot(conn, snapshot_row(s, "mkt-1", 1_700_000_000_000))
        conn.commit()
        rows = conn.execute("SELECT * FROM book_snapshots WHERE market_id='mkt-1'").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["bid1"] == 0.45
    assert rows[0]["ask1"] == 0.55


def test_load_open_token_ids_excludes_closed(tmp_path: Path):
    db_path = tmp_path / "wxm.db"
    init_db(db_path, MIGRATIONS)
    conn = connect(db_path)
    try:
        conn.executemany(
            "INSERT INTO markets("
            "  market_id, station, date, bucket_label, bucket_kind,"
            "  token_id_yes, token_id_no, discovered_ts, closed) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            [
                ("m-open", "new_york", "2026-06-15", 85.0, "band", "tok-yo", "tok-no", 0, 0),
                ("m-closed", "new_york", "2026-06-14", 85.0, "band", "tok-yc", "tok-nc", 0, 1),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    tokens = load_open_token_ids(db_path)
    # Phase 0: YES side only; NO is derived at execution time.
    assert "tok-yo" in tokens
    assert "tok-no" not in tokens
    assert "tok-yc" not in tokens and "tok-nc" not in tokens
