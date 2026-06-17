"""Bucket geometry — sole owner per spec §6.3.

The ONLY module that:
  (a) enumerates a market board's buckets from the markets table
  (b) converts a predictive distribution to per-bucket probabilities
  (c) maps an observed temperature to its winning bucket

Backtester, settlement, paper executor, and (via cross-language contract test)
the future Rust port all use this geometry. This structurally kills v1's
deadliest bug class — different parts of the code disagreeing on bucket width.

In Phase 0, spec.buckets.hypothesis is null. The caller supplies a provisional
guess (per-station defaults in calibrate/pipeline.py) and the geometry is
stamped ``geometry_provisional=True`` on every output row. Phase 1's resolution
audit overwrites the spec and flips ``rounding_verified=True``.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Protocol

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GeometryConfig:
    """Provisional or audited bucket geometry. Single owner of these literals."""

    hypothesis: str  # "rounds_to" | "floor_to" | "exact_band"
    width: float
    verified: bool

    @classmethod
    def from_city(cls, hypothesis: str | None, width: float | None, verified: bool,
                  provisional_default: tuple[str, float]) -> "GeometryConfig":
        if hypothesis is not None and width is not None:
            return cls(hypothesis=hypothesis, width=width, verified=verified)
        # Phase 0 provisional fallback
        h, w = provisional_default
        return cls(hypothesis=h, width=w, verified=False)


@dataclass(frozen=True)
class Bucket:
    label: float
    kind: str  # "band" | "open_low" | "open_high"
    lo: float
    hi: float
    market_id: str | None = None
    token_id_yes: str | None = None
    token_id_no: str | None = None


class Distribution(Protocol):
    def cdf(self, x: float) -> float: ...


# -------------------------------------------------------------------- Geometry


def _bucket_edges(label: float, kind: str, geom: GeometryConfig) -> tuple[float, float]:
    if kind == "open_high":
        if geom.hypothesis == "rounds_to":
            return (label - geom.width / 2, math.inf)
        return (label, math.inf)
    if kind == "open_low":
        if geom.hypothesis == "rounds_to":
            return (-math.inf, label + geom.width / 2)
        return (-math.inf, label + geom.width)
    if geom.hypothesis == "rounds_to":
        return (label - geom.width / 2, label + geom.width / 2)
    if geom.hypothesis == "floor_to":
        return (label, label + geom.width)
    if geom.hypothesis == "exact_band":
        return (label, label)
    raise ValueError(f"unknown hypothesis: {geom.hypothesis!r}")


def _check_tiling(buckets: list[Bucket], verified: bool) -> bool:
    """Return True if buckets tile the real line without gaps or overlaps.

    Buckets must include (a) zero or one open_low, (b) any number of bands, and
    (c) zero or one open_high. On gap or overlap, logs a warning. When
    ``verified=True`` (the audit has run and the spec says it tiled), raises;
    otherwise just reports.
    """
    bands = sorted([b for b in buckets if b.kind == "band"], key=lambda b: b.lo)
    open_low = next((b for b in buckets if b.kind == "open_low"), None)
    open_high = next((b for b in buckets if b.kind == "open_high"), None)
    issues: list[str] = []
    for i in range(len(bands) - 1):
        if not math.isclose(bands[i].hi, bands[i + 1].lo, rel_tol=1e-9, abs_tol=1e-9):
            issues.append(
                f"band gap/overlap at label {bands[i].label}->{bands[i+1].label}: "
                f"hi={bands[i].hi} lo={bands[i+1].lo}"
            )
    if bands:
        if open_low and not math.isclose(open_low.hi, bands[0].lo, rel_tol=1e-9, abs_tol=1e-9):
            issues.append(f"open_low.hi={open_low.hi} != first band.lo={bands[0].lo}")
        if open_high and not math.isclose(open_high.lo, bands[-1].hi, rel_tol=1e-9, abs_tol=1e-9):
            issues.append(f"open_high.lo={open_high.lo} != last band.hi={bands[-1].hi}")
        if open_low is None:
            issues.append(f"no open_low; (-inf, {bands[0].lo}) uncovered")
        if open_high is None:
            issues.append(f"no open_high; ({bands[-1].hi}, +inf) uncovered")
    if issues:
        msg = "; ".join(issues)
        if verified:
            raise ValueError(f"bucket tiling broken (verified geometry): {msg}")
        log.warning("bucket tiling not exhaustive (provisional geometry): %s", msg)
        return False
    return True


def enumerate_buckets(
    station: str,
    target_date: str,
    db_path: Path,
    geom: GeometryConfig,
) -> tuple[list[Bucket], bool]:
    """Read markets rows for (station, target_date) and apply geometry.

    Returns (buckets, tiled_ok). Buckets are sorted by lo.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT market_id, bucket_label, bucket_kind, token_id_yes, token_id_no "
            "FROM markets WHERE station=? AND date=? AND COALESCE(closed,0)=0",
            (station, target_date),
        ).fetchall()
    finally:
        conn.close()
    buckets: list[Bucket] = []
    for r in rows:
        kind = r["bucket_kind"] or "band"
        lo, hi = _bucket_edges(r["bucket_label"], kind, geom)
        buckets.append(
            Bucket(
                label=r["bucket_label"],
                kind=kind,
                lo=lo,
                hi=hi,
                market_id=r["market_id"],
                token_id_yes=r["token_id_yes"],
                token_id_no=r["token_id_no"],
            )
        )
    buckets.sort(key=lambda b: b.lo)
    tiled = _check_tiling(buckets, geom.verified)
    return buckets, tiled


# --------------------------------------------------------------------- Probs


def bucket_probs(dist: Distribution, buckets: Iterable[Bucket]) -> dict[float, float]:
    """Per-bucket probability via direct CDF integration on the predictive distribution.

    The result is renormalized over the enumerated board so probabilities sum to
    1 even if the tiling is not perfectly exhaustive (Phase 0 reality).
    """
    bs = list(buckets)
    raw: dict[float, float] = {}
    for b in bs:
        p = dist.cdf(b.hi) - dist.cdf(b.lo)
        raw[b.label] = max(0.0, p)
    total = sum(raw.values())
    if total <= 0:
        n = len(raw)
        return {label: (1.0 / n if n else 0.0) for label in raw}
    return {label: p / total for label, p in raw.items()}


# ------------------------------------------------------------ Winning bucket


def winning_bucket(
    observed_native: float,
    buckets: Iterable[Bucket],
) -> Bucket | None:
    """Find the bucket whose [lo, hi] contains observed_native.

    Inclusive on lo, exclusive on hi for bands; tails handle ±inf trivially.
    Returns None if no bucket matches (audit alarm — the board did not tile).
    """
    bs = list(buckets)
    # Tail check first (open intervals)
    for b in bs:
        if b.kind == "open_low" and observed_native < b.hi:
            return b
        if b.kind == "open_high" and observed_native >= b.lo:
            return b
    for b in bs:
        if b.kind != "band":
            continue
        if b.lo <= observed_native < b.hi:
            return b
    # Edge case: exact match on the upper edge of the topmost band
    bands_sorted = sorted([b for b in bs if b.kind == "band"], key=lambda b: b.hi, reverse=True)
    if bands_sorted and math.isclose(observed_native, bands_sorted[0].hi):
        return bands_sorted[0]
    return None
