"""Phase 0 paper executor.

Reads ``data/bridge/probs.json``, joins it with the latest ``book_snapshots`` per
market, evaluates the calibrated-edge indicator (I1), and writes a row to
``signals`` for every threshold-crossing evaluation — acted or not. When
``acted=1`` it simulates a fill at touch + ``slippage.alpha * spread`` and writes
``orders``/``fills``/``positions`` with ``mode='paper'``.

Phase 0 invariants (structural, not optional):
  - mode='paper' is hardcoded; there is no live-order code path here yet
  - the executor consults eligibility.live_eligible before acting; if false (which
    it always is in Phase 0) acted=0 is forced and no fill is simulated
  - if probs.json is older than trading.risk.stale_probs_max_age_s, no new
    signals are generated
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..bridge import read_probs
from ..db import DEFAULT_DB_PATH, connect
from ..spec import Spec

log = logging.getLogger(__name__)

PAPER_MODE = "paper"


@dataclass
class CandidateSignal:
    station: str
    target_date: str
    lead: str
    market_id: str
    bucket_label: float
    bucket_kind: str
    side: str  # "buy_yes" | "buy_no"
    model_prob: float
    touch_price: float
    spread: float
    edge_after_fees: float
    composite_score: float
    indicator_json: dict[str, Any]
    eligibility_live: bool
    triggered: bool
    trigger: str
    probs_version: str
    geometry_provisional: bool
    failed_reason: str | None = None


def _fee_rate(spec: Spec) -> float:
    if spec.trading.fees.taker_bps is None:
        return 0.0
    return spec.trading.fees.taker_bps / 10_000


def _lead_short(lead: str) -> str:
    if lead.startswith("d0"):
        return "d0"
    return lead


def _latest_book_snapshot(
    db_path: Path, market_id: str, max_age_s: int | None = None
) -> dict | None:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM book_snapshots WHERE market_id=? ORDER BY ts DESC LIMIT 1",
            (market_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    if max_age_s is not None:
        if (time.time() * 1000 - row["ts"]) > max_age_s * 1000:
            return None
    return dict(row)


def evaluate_bucket(
    spec: Spec,
    station: str,
    target_date: str,
    lead: str,
    bucket: dict,
    book: dict | None,
    eligibility_live: bool,
    geometry_provisional: bool,
    probs_version: str,
    trigger: str,
) -> CandidateSignal | None:
    """Evaluate one (station, date, bucket); return CandidateSignal if a side has
    *any* positive edge worth logging, else None.
    """
    market_id = bucket.get("market_id")
    if not market_id:
        return None
    p_yes = float(bucket.get("p") or 0.0)
    if book is None or book.get("ask1") is None or book.get("bid1") is None:
        return None
    ask_yes = float(book["ask1"])
    bid_yes = float(book["bid1"])
    ask_no = 1.0 - bid_yes
    spread_yes = max(0.0, ask_yes - bid_yes)
    spread_no = spread_yes  # mirrored 1-x

    fee = _fee_rate(spec)
    haircut = spec.trading.oracle_haircut

    edge_yes = p_yes - ask_yes - fee * ask_yes - haircut
    edge_no = (1.0 - p_yes) - ask_no - fee * ask_no - haircut

    min_edge = spec.trading.edges.min_edge_after_fees.get(_lead_short(lead), 1.0)
    min_p_yes = spec.trading.edges.min_model_prob_for_yes

    # Side selection: bigger edge wins. YES requires p_yes >= min_model_prob_for_yes
    # (tail-humility); NO has no such floor (p_no = 1-p_yes is typically high).
    side = None
    chosen_edge = -float("inf")
    chosen_price = 0.0
    chosen_spread = 0.0
    chosen_p = 0.0
    if p_yes >= min_p_yes and edge_yes > chosen_edge:
        side, chosen_edge, chosen_price, chosen_spread, chosen_p = (
            "buy_yes", edge_yes, ask_yes, spread_yes, p_yes,
        )
    if edge_no > chosen_edge:
        side, chosen_edge, chosen_price, chosen_spread, chosen_p = (
            "buy_no", edge_no, ask_no, spread_no, 1.0 - p_yes,
        )
    if side is None:
        return None

    triggered = chosen_edge >= min_edge
    failed_reason: str | None = None
    if not triggered:
        failed_reason = f"edge<{min_edge}"

    indicator_json = {
        "I1": {
            "edge": chosen_edge,
            "p_model": chosen_p,
            "touch": chosen_price,
            "spread": chosen_spread,
            "min_edge": min_edge,
            "min_p_yes": min_p_yes,
        }
    }
    return CandidateSignal(
        station=station,
        target_date=target_date,
        lead=lead,
        market_id=market_id,
        bucket_label=float(bucket.get("label", 0.0)),
        bucket_kind=str(bucket.get("kind", "band")),
        side=side,
        model_prob=chosen_p,
        touch_price=chosen_price,
        spread=chosen_spread,
        edge_after_fees=chosen_edge,
        composite_score=chosen_edge,  # Phase 0: composite == I1
        indicator_json=indicator_json,
        eligibility_live=eligibility_live,
        triggered=triggered,
        trigger=trigger,
        probs_version=probs_version,
        geometry_provisional=geometry_provisional,
        failed_reason=failed_reason,
    )


def persist_signal(db_path: Path, sig: CandidateSignal, acted: bool) -> int:
    conn = connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO signals("
            "  ts, station, date, market_id, side, composite_score, i1_edge,"
            "  indicator_json, touch_price, edge_after_fees, trigger, probs_version, acted) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                int(time.time()),
                sig.station,
                sig.target_date,
                sig.market_id,
                sig.side,
                sig.composite_score,
                sig.indicator_json["I1"]["edge"],
                json.dumps(sig.indicator_json),
                sig.touch_price,
                sig.edge_after_fees,
                sig.trigger,
                sig.probs_version,
                int(acted),
            ),
        )
        conn.commit()
        return cur.lastrowid or 0
    finally:
        conn.close()


def kelly_stake(p: float, q: float, kelly_fraction: float, bankroll_usd: float) -> float:
    """Binary Kelly: f* = (p - q) / (1 - q). Negative or zero stake is allowed
    only when p > q; returns 0 otherwise."""
    if q >= 1.0 or p <= q:
        return 0.0
    f = (p - q) / (1.0 - q)
    return max(0.0, kelly_fraction * f * bankroll_usd)


def simulate_fill(
    spec: Spec,
    db_path: Path,
    signal_id: int,
    sig: CandidateSignal,
) -> str | None:
    """Cap Kelly stake by per-market exposure, write order/fill/position."""
    if sig.touch_price <= 0 or sig.touch_price >= 1.0:
        return None
    stake = kelly_stake(
        sig.model_prob,
        sig.touch_price,
        spec.trading.sizing.kelly_fraction,
        spec.trading.sizing.bankroll_allocated_usd,
    )
    stake = min(stake, spec.trading.sizing.exposure.max_stake_per_market_usd)
    if stake <= 0:
        return None

    fill_price = sig.touch_price + spec.trading.slippage.alpha * sig.spread
    fill_price = min(max(fill_price, 0.0), 1.0)
    order_id = f"paper-{uuid.uuid4().hex[:12]}"
    fill_id = f"f-{order_id}"
    now = int(time.time())

    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO orders(order_id, signal_id, ts, market_id, side, price, size_usd, status, mode) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (order_id, signal_id, now, sig.market_id, sig.side, fill_price, stake, "filled", PAPER_MODE),
        )
        conn.execute(
            "INSERT INTO fills(fill_id, order_id, ts, price, size_usd) VALUES (?,?,?,?,?)",
            (fill_id, order_id, now, fill_price, stake),
        )
        direction = "warm" if sig.side == "buy_yes" else "cold"
        conn.execute(
            "INSERT INTO positions(market_id, station, date, side, direction, avg_price, size_usd, settled, pnl) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(market_id) DO UPDATE SET "
            "  avg_price=(positions.avg_price*positions.size_usd + excluded.avg_price*excluded.size_usd)"
            "    /(positions.size_usd + excluded.size_usd),"
            "  size_usd=positions.size_usd + excluded.size_usd",
            (sig.market_id, sig.station, sig.target_date, sig.side, direction, fill_price, stake, 0, None),
        )
        conn.commit()
    finally:
        conn.close()
    return order_id


def run_once(
    spec: Spec,
    db_path: Path = DEFAULT_DB_PATH,
    bridge_dir: Path = Path("data/bridge"),
    trigger: str = "probs_reload",
) -> dict[str, int]:
    """One pass: evaluate every (station, date, bucket), persist all candidates, act
    on those that pass thresholds AND live-eligibility.

    Returns {"signals": n_logged, "fills": n_acted}.
    """
    payload = read_probs(bridge_dir)
    if payload is None:
        log.info("probs.json missing; skipping evaluation")
        return {"signals": 0, "fills": 0}
    written_ts_ms = payload.get("written_ts", 0)
    probs_age_s = time.time() - written_ts_ms / 1000
    stale = probs_age_s > spec.trading.risk.stale_probs_max_age_s
    if stale:
        log.warning("probs.json stale (%.1fs); no new signals", probs_age_s)
        return {"signals": 0, "fills": 0}

    run_id = payload.get("run_id", "")
    n_signals = 0
    n_fills = 0
    for station, sblock in (payload.get("stations") or {}).items():
        for target_date, dblock in (sblock.get("dates") or {}).items():
            lead = dblock.get("lead", "d0_late")
            eligibility = dblock.get("eligibility") or {}
            live_ok = bool(eligibility.get("live_eligible"))
            geom_provisional = bool(dblock.get("geometry_provisional", True))
            for bucket in dblock.get("buckets") or []:
                book = _latest_book_snapshot(db_path, bucket.get("market_id") or "")
                cand = evaluate_bucket(
                    spec, station, target_date, lead, bucket, book,
                    live_ok, geom_provisional, run_id, trigger,
                )
                if cand is None:
                    continue
                acted = cand.triggered and cand.eligibility_live
                signal_id = persist_signal(db_path, cand, acted)
                n_signals += 1
                if acted and simulate_fill(spec, db_path, signal_id, cand):
                    n_fills += 1
    return {"signals": n_signals, "fills": n_fills}


def run_loop(
    spec: Spec,
    db_path: Path = DEFAULT_DB_PATH,
    bridge_dir: Path = Path("data/bridge"),
    poll_s: int = 30,
    kill_file: Path = Path("data/KILL"),
) -> None:
    while not kill_file.exists():
        try:
            stats = run_once(spec, db_path=db_path, bridge_dir=bridge_dir)
            log.info("paper run pass", extra=stats)
        except Exception:
            log.exception("paper run pass failed")
        time.sleep(poll_s)
