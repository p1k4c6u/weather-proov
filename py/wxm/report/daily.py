"""Daily report — emits every Gate G0 field per spec PART XIII Phase 0.

Each evening (after settlement + refit), this writes a Markdown report under
``reports/{YYYY-MM-DD}.md`` with the loop's health snapshot. Phase 0 leaves
Brier-vs-market / PIT empty until settled forecast pairs accumulate.
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..db import DEFAULT_DB_PATH, connect
from ..settle.pnl import (
    open_paper_exposure_usd,
    realized_pnl_last_n_days_usd,
    total_realized_pnl_usd,
)
from ..spec import Spec

REPORTS_DIR = Path("reports")


def _q(conn, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def collect_metrics(db_path: Path, spec: Spec) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        ensembles_ingested = _q(
            conn,
            "SELECT station, COUNT(*) AS n FROM ensemble_forecasts GROUP BY station ORDER BY station",
        )
        markets_open = _q(
            conn,
            "SELECT station, COUNT(*) AS n FROM markets WHERE COALESCE(closed,0)=0 "
            "GROUP BY station ORDER BY station",
        )
        book_coverage = _q(
            conn,
            "SELECT m.station, COUNT(b.market_id) AS snapshots, "
            "  COUNT(DISTINCT b.market_id) AS markets_with_books "
            "FROM book_snapshots b JOIN markets m ON b.market_id=m.market_id "
            "GROUP BY m.station ORDER BY m.station",
        )
        signals_count = _q(
            conn,
            "SELECT station, COUNT(*) AS total, SUM(acted) AS acted "
            "FROM signals GROUP BY station ORDER BY station",
        )
        fills_count = _q(
            conn,
            "SELECT m.station, COUNT(f.fill_id) AS n "
            "FROM fills f JOIN orders o ON f.order_id=o.order_id "
            "JOIN markets m ON o.market_id=m.market_id "
            "GROUP BY m.station ORDER BY m.station",
        )
        forecast_pairs_by_lead = _q(
            conn,
            "SELECT station, lead_bucket, forecast_source, "
            "  SUM(CASE WHEN y_c IS NOT NULL THEN 1 ELSE 0 END) AS settled, "
            "  COUNT(*) AS total "
            "FROM forecast_pairs GROUP BY station, lead_bucket, forecast_source "
            "ORDER BY station, lead_bucket, forecast_source",
        )
        eligibility_rows = _q(
            conn,
            "SELECT station, lead_bucket, stage, live_eligible, near_mean_eligible, "
            "  tail_eligible, failed_gate FROM eligibility ORDER BY station, lead_bucket",
        )
        settlements_recent = _q(
            conn,
            "SELECT station, date, bucket_label, mismatch FROM settlements "
            "WHERE date >= ? ORDER BY date DESC, station",
            ((date.today() - timedelta(days=7)).isoformat(),),
        )
        data_quality_gaps = _collect_data_quality_gaps(conn, spec)
    finally:
        conn.close()
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "ensembles_ingested": ensembles_ingested,
        "markets_open": markets_open,
        "book_coverage": book_coverage,
        "signals_count": signals_count,
        "fills_count": fills_count,
        "forecast_pairs_by_lead": forecast_pairs_by_lead,
        "eligibility": eligibility_rows,
        "settlements_recent": settlements_recent,
        "data_quality_gaps": data_quality_gaps,
        "pnl": {
            "total_usd": total_realized_pnl_usd(db_path),
            "last_1_day_usd": realized_pnl_last_n_days_usd(1, db_path),
            "open_exposure_usd": open_paper_exposure_usd(db_path),
        },
    }


def _collect_data_quality_gaps(conn, spec: Spec) -> list[dict[str, Any]]:
    """Find feeds whose most recent rows are older than the spec's staleness thresholds."""
    gaps: list[dict[str, Any]] = []
    now_ts = int(time.time())

    # Ensemble freshness: should be at most 12 hours (worst case between model runs)
    ens_rows = _q(
        conn,
        "SELECT station, model, MAX(fetch_ts) AS last_ts FROM ensemble_forecasts "
        "GROUP BY station, model",
    )
    for r in ens_rows:
        age_s = now_ts - int(r["last_ts"] or 0)
        if age_s > 24 * 3600:
            gaps.append({"feed": "ensemble_forecasts", "station": r["station"],
                         "model": r["model"], "age_s": age_s})

    # Book snapshot freshness: should be ≤ stale_obs_max_age_s
    book_rows = _q(
        conn,
        "SELECT m.station, MAX(b.ts)/1000 AS last_ts FROM book_snapshots b "
        "JOIN markets m ON b.market_id=m.market_id GROUP BY m.station",
    )
    threshold = spec.trading.risk.stale_obs_max_age_s
    for r in book_rows:
        age_s = now_ts - int(r["last_ts"] or 0)
        if age_s > threshold:
            gaps.append({"feed": "book_snapshots", "station": r["station"], "age_s": age_s})

    # Observations: at least one settlement-source per station within last 48h
    for sid in spec.resolution.cities:
        row = conn.execute(
            "SELECT MAX(fetched_ts) AS last_ts FROM observations "
            "WHERE station=? AND is_settlement_source=1", (sid,),
        ).fetchone()
        last_ts = int((row["last_ts"] or 0))
        age_s = now_ts - last_ts
        if last_ts == 0 or age_s > 48 * 3600:
            gaps.append({"feed": "observations", "station": sid, "age_s": age_s})
    return gaps


def render_markdown(metrics: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"# wxm daily report — {metrics['generated_at']}")
    lines.append("")
    lines.append("Phase 0 paper-only. eligibility.live_eligible=0 for every (station, lead_bucket).")
    lines.append("")

    def _table(title: str, rows: list[dict], cols: list[str]) -> None:
        lines.append(f"## {title}")
        if not rows:
            lines.append("(no rows)")
            lines.append("")
            return
        lines.append("| " + " | ".join(cols) + " |")
        lines.append("| " + " | ".join("---" for _ in cols) + " |")
        for r in rows:
            lines.append("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |")
        lines.append("")

    _table("Forecasts ingested", metrics["ensembles_ingested"], ["station", "n"])
    _table("Markets open", metrics["markets_open"], ["station", "n"])
    _table("Book coverage", metrics["book_coverage"], ["station", "snapshots", "markets_with_books"])
    _table("Signals", metrics["signals_count"], ["station", "total", "acted"])
    _table("Fills", metrics["fills_count"], ["station", "n"])
    _table("Forecast pairs by lead", metrics["forecast_pairs_by_lead"],
           ["station", "lead_bucket", "forecast_source", "settled", "total"])
    _table("Eligibility", metrics["eligibility"],
           ["station", "lead_bucket", "stage", "live_eligible", "near_mean_eligible",
            "tail_eligible", "failed_gate"])
    _table("Recent settlements", metrics["settlements_recent"],
           ["date", "station", "bucket_label", "mismatch"])
    _table("Data quality gaps", metrics["data_quality_gaps"], ["feed", "station", "age_s"])

    pnl = metrics["pnl"]
    lines.append("## Paper PnL (USD)")
    lines.append(f"- total realized: {pnl['total_usd']:.2f}")
    lines.append(f"- last 1 day: {pnl['last_1_day_usd']:.2f}")
    lines.append(f"- open exposure (paper): {pnl['open_exposure_usd']:.2f}")
    lines.append("")
    lines.append("## Brier-vs-market, PIT")
    lines.append("(awaiting Phase 2 — requires accumulated settled forecast pairs)")
    return "\n".join(lines) + "\n"


def write_daily_report(
    spec: Spec, db_path: Path = DEFAULT_DB_PATH, reports_dir: Path = REPORTS_DIR,
    today_iso: str | None = None,
) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    metrics = collect_metrics(db_path, spec)
    md = render_markdown(metrics)
    today_iso = today_iso or date.today().isoformat()
    out = reports_dir / f"{today_iso}.md"
    out.write_text(md)
    return out
