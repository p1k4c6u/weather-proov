"""Polymarket CLOB book recorder.

Subscribes by token_id to ``wss://ws-subscriptions-clob.polymarket.com/ws/market``,
maintains in-memory L2 books, and snapshots top-3 levels into ``book_snapshots``
every ``snapshot_interval_s`` seconds plus on every trade.

Polymarket CLOB WS event shapes recognised here (canonical form; small variations
are tolerated):

    {"event_type":"book","asset_id":"<token>","bids":[{"price":"0.45","size":"100"}, ...],
                                              "asks":[{"price":"0.55","size":"100"}, ...]}
    {"event_type":"price_change","asset_id":"<token>",
     "changes":[{"price":"0.45","side":"BUY"|"SELL","size":"0"}]}
    {"event_type":"last_trade_price","asset_id":"<token>","price":"0.50", ...}

Connection management is a thin wrapper; the parser/state-update functions are
pure so they can be unit-tested without a real WS server.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..archive import archive_raw
from ..db import DEFAULT_DB_PATH, connect

log = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
DEFAULT_SNAPSHOT_INTERVAL_S = 15
RECONNECT_BACKOFF_CAP_S = 60
KILL_FILE = Path("data/KILL")


@dataclass
class BookState:
    """In-memory L2 book for a single token. Maps price -> size, both as floats."""

    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    last_event_ts: int = 0
    last_snapshot_ts: int = 0


def apply_book_event(state: BookState, event: dict, now_ms: int) -> None:
    """Full replace of the book state from a 'book' event."""
    state.bids = {
        float(level["price"]): float(level["size"])
        for level in event.get("bids", [])
        if float(level.get("size", 0)) > 0
    }
    state.asks = {
        float(level["price"]): float(level["size"])
        for level in event.get("asks", [])
        if float(level.get("size", 0)) > 0
    }
    state.last_event_ts = now_ms


def apply_price_change(state: BookState, event: dict, now_ms: int) -> None:
    """Incremental update from a 'price_change' event. size=0 removes a level."""
    for ch in event.get("changes", []):
        price = float(ch["price"])
        size = float(ch["size"])
        side = ch.get("side", "").upper()
        book = state.bids if side == "BUY" else state.asks if side == "SELL" else None
        if book is None:
            continue
        if size == 0:
            book.pop(price, None)
        else:
            book[price] = size
    state.last_event_ts = now_ms


def top3_bids(state: BookState) -> list[tuple[float, float]]:
    """Highest 3 bids, descending."""
    return sorted(state.bids.items(), key=lambda kv: kv[0], reverse=True)[:3]


def top3_asks(state: BookState) -> list[tuple[float, float]]:
    """Lowest 3 asks, ascending."""
    return sorted(state.asks.items(), key=lambda kv: kv[0])[:3]


def snapshot_row(state: BookState, market_id: str, ts_ms: int) -> tuple:
    """Produce a tuple matching the book_snapshots column order. Missing levels = None."""
    bids = top3_bids(state) + [(None, None)] * 3
    asks = top3_asks(state) + [(None, None)] * 3
    return (
        market_id,
        ts_ms,
        bids[0][0], bids[0][1], bids[1][0], bids[1][1], bids[2][0], bids[2][1],
        asks[0][0], asks[0][1], asks[1][0], asks[1][1], asks[2][0], asks[2][1],
    )


def should_snapshot(
    state: BookState, now_ms: int, interval_s: int = DEFAULT_SNAPSHOT_INTERVAL_S
) -> bool:
    if state.last_event_ts == 0:
        return False
    return (now_ms - state.last_snapshot_ts) >= interval_s * 1000


def persist_snapshot(conn, row: tuple) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO book_snapshots("
        "  market_id, ts,"
        "  bid1, bid1_sz, bid2, bid2_sz, bid3, bid3_sz,"
        "  ask1, ask1_sz, ask2, ask2_sz, ask3, ask3_sz)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        row,
    )


def process_message(
    state_by_token: dict[str, BookState],
    raw_msg: bytes | str,
    now_ms: int,
) -> list[str]:
    """Update internal state from a single raw WS frame.

    Returns the list of token_ids that need a snapshot (either because of trade
    event or because the snapshot-interval elapsed).
    """
    text = raw_msg.decode("utf-8") if isinstance(raw_msg, bytes) else raw_msg
    archive_raw("books", text.encode("utf-8"))
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        log.warning("non-json book frame ignored")
        return []
    events = payload if isinstance(payload, list) else [payload]
    to_snapshot: set[str] = set()
    for ev in events:
        if not isinstance(ev, dict):
            continue
        token = ev.get("asset_id") or ev.get("market") or ""
        if not token:
            continue
        state = state_by_token.setdefault(token, BookState())
        kind = ev.get("event_type", "")
        if kind == "book":
            apply_book_event(state, ev, now_ms)
        elif kind == "price_change":
            apply_price_change(state, ev, now_ms)
        elif kind == "last_trade_price":
            state.last_event_ts = now_ms
            to_snapshot.add(token)
            continue
        else:
            continue
        if should_snapshot(state, now_ms):
            to_snapshot.add(token)
    return list(to_snapshot)


def load_open_token_ids(db_path: Path = DEFAULT_DB_PATH) -> dict[str, str]:
    """Return {token_id -> market_id} for all open markets, YES side only.

    Phase 0 simplification: book_snapshots is keyed by market_id (spec PART V),
    but Polymarket runs separate L2 books per YES/NO token. We subscribe to YES
    only and derive the NO side at execution time (ask_no ≈ 1 - bid_yes). Phase 4
    will widen this to per-token books with a schema migration.
    """
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT market_id, token_id_yes FROM markets WHERE COALESCE(closed,0)=0"
        ).fetchall()
    finally:
        conn.close()
    return {r["token_id_yes"]: r["market_id"] for r in rows if r["token_id_yes"]}


def kill_file_present() -> bool:
    return KILL_FILE.exists()


def run_recorder(
    db_path: Path = DEFAULT_DB_PATH,
    snapshot_interval_s: int = DEFAULT_SNAPSHOT_INTERVAL_S,
    refresh_tokens_every_s: int = 300,
) -> None:
    """Long-lived WS subscription + persistence loop. Exits clean when KILL file appears."""
    from websockets.sync.client import connect as ws_connect
    from websockets.exceptions import WebSocketException

    state_by_token: dict[str, BookState] = {}
    backoff = 1.0
    last_token_refresh = 0
    token_to_market: dict[str, str] = {}
    db_conn = connect(db_path)
    try:
        while not kill_file_present():
            try:
                now = time.time()
                if now - last_token_refresh > refresh_tokens_every_s:
                    token_to_market = load_open_token_ids(db_path)
                    last_token_refresh = now
                if not token_to_market:
                    log.info("no open markets to subscribe; sleeping")
                    time.sleep(min(refresh_tokens_every_s, 30))
                    continue
                tokens = list(token_to_market.keys())
                log.info("connecting to CLOB WS", extra={"n_tokens": len(tokens)})
                with ws_connect(WS_URL, open_timeout=15) as ws:
                    sub_msg = json.dumps({"type": "Market", "assets_ids": tokens})
                    ws.send(sub_msg)
                    backoff = 1.0
                    for raw in ws:
                        if kill_file_present():
                            log.info("kill file detected; closing WS")
                            break
                        now_ms = int(time.time() * 1000)
                        to_snap = process_message(state_by_token, raw, now_ms)
                        for token in to_snap:
                            market_id = token_to_market.get(token)
                            if not market_id:
                                continue
                            row = snapshot_row(state_by_token[token], market_id, now_ms)
                            persist_snapshot(db_conn, row)
                            state_by_token[token].last_snapshot_ts = now_ms
                        db_conn.commit()
            except WebSocketException as e:
                log.warning("ws disconnected; backing off", extra={"error": str(e), "backoff_s": backoff})
                time.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_BACKOFF_CAP_S)
            except OSError as e:
                log.warning("ws connect failed; backing off", extra={"error": str(e), "backoff_s": backoff})
                time.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_BACKOFF_CAP_S)
    finally:
        db_conn.close()
