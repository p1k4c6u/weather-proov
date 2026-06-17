import math
import statistics
from pathlib import Path

import pytest

from wxm.calibrate.buckets import (
    Bucket,
    GeometryConfig,
    bucket_probs,
    enumerate_buckets,
    winning_bucket,
)
from wxm.db import connect, init_db

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO_ROOT / "migrations"


# ------------------------------------------------------------ Geometry edges


def test_geometry_rounds_to_centered_band():
    geom = GeometryConfig(hypothesis="rounds_to", width=2.0, verified=True)
    bs, tiled = enumerate_buckets("new_york", "2026-06-15", _seed_db(
        markets=[(85, "band"), (87, "band"), (89, "band"),
                 (91, "open_high"), (83, "open_low")],
        station="new_york",
        date="2026-06-15",
    ), geom)
    # All bands width=2, centered: [84,86),[86,88),[88,90)
    by_label = {b.label: b for b in bs}
    assert by_label[85].lo == 84 and by_label[85].hi == 86
    assert by_label[87].lo == 86 and by_label[87].hi == 88
    assert by_label[89].lo == 88 and by_label[89].hi == 90
    # Tails: open_high lo = label - width/2 in rounds_to to abut the last band
    assert by_label[91].lo == 90 and math.isinf(by_label[91].hi)
    assert math.isinf(by_label[83].lo) and by_label[83].hi == 84
    assert tiled is True


def test_geometry_floor_to_lower_edge_band():
    geom = GeometryConfig(hypothesis="floor_to", width=2.0, verified=True)
    bs, tiled = enumerate_buckets("new_york", "2026-06-15", _seed_db(
        markets=[(81, "band"), (83, "band"), (85, "band"),
                 (87, "open_high"), (79, "open_low")],
        station="new_york",
        date="2026-06-15",
    ), geom)
    by_label = {b.label: b for b in bs}
    assert by_label[81].lo == 81 and by_label[81].hi == 83
    assert by_label[83].lo == 83 and by_label[83].hi == 85
    assert by_label[85].lo == 85 and by_label[85].hi == 87
    assert by_label[87].lo == 87 and math.isinf(by_label[87].hi)
    assert math.isinf(by_label[79].lo) and by_label[79].hi == 81
    assert tiled is True


def test_tiling_gap_logs_when_provisional(caplog):
    geom = GeometryConfig(hypothesis="floor_to", width=2.0, verified=False)
    with caplog.at_level("WARNING"):
        _, tiled = enumerate_buckets("nyc", "2026-06-15", _seed_db(
            markets=[(81, "band"), (85, "band")],  # gap between [81,83) and [85,87)
            station="nyc",
            date="2026-06-15",
        ), geom)
    assert tiled is False
    assert any("gap" in r.message or "uncovered" in r.message for r in caplog.records)


def test_tiling_gap_raises_when_verified():
    geom = GeometryConfig(hypothesis="floor_to", width=2.0, verified=True)
    with pytest.raises(ValueError):
        enumerate_buckets("nyc", "2026-06-15", _seed_db(
            markets=[(81, "band"), (85, "band")],
            station="nyc",
            date="2026-06-15",
        ), geom)


# ---------------------------------------------------------------- bucket_probs


class _Normal:
    def __init__(self, mu, sigma):
        self._d = statistics.NormalDist(mu, sigma)
    def cdf(self, x):
        if math.isinf(x):
            return 1.0 if x > 0 else 0.0
        return self._d.cdf(x)


def test_bucket_probs_sum_to_one_and_renormalized():
    geom = GeometryConfig(hypothesis="floor_to", width=2.0, verified=True)
    bs, _ = enumerate_buckets("new_york", "2026-06-15", _seed_db(
        markets=[(81, "band"), (83, "band"), (85, "band"), (87, "band"),
                 (89, "open_high"), (79, "open_low")],
        station="new_york",
        date="2026-06-15",
    ), geom)
    probs = bucket_probs(_Normal(85, 3.0), bs)
    assert sum(probs.values()) == pytest.approx(1.0)
    # Closest band to the mean (85) should be the largest band probability
    band_labels = [b.label for b in bs if b.kind == "band"]
    band_probs = {l: probs[l] for l in band_labels}
    assert band_probs[85] == max(band_probs.values())


def test_bucket_probs_point_mass_lands_on_one_bucket():
    """Degenerate Gaussian (sigma very small) at x=84.5 should put ~100% in the
    band containing 84.5."""
    geom = GeometryConfig(hypothesis="floor_to", width=2.0, verified=True)
    bs, _ = enumerate_buckets("new_york", "2026-06-15", _seed_db(
        markets=[(83, "band"), (85, "band"), (87, "band"),
                 (89, "open_high"), (81, "open_low")],
        station="new_york",
        date="2026-06-15",
    ), geom)
    # band 83 = [83, 85). 84.5 → bucket 83.
    probs = bucket_probs(_Normal(84.5, 0.01), bs)
    assert probs[83.0] == pytest.approx(1.0, abs=1e-6)


# ---------------------------------------------------------- winning_bucket


def test_winning_bucket_lands_in_correct_band():
    geom = GeometryConfig(hypothesis="floor_to", width=2.0, verified=True)
    bs, _ = enumerate_buckets("new_york", "2026-06-15", _seed_db(
        markets=[(83, "band"), (85, "band"), (87, "band"),
                 (89, "open_high"), (81, "open_low")],
        station="new_york",
        date="2026-06-15",
    ), geom)
    assert winning_bucket(84.5, bs).label == 83
    assert winning_bucket(85.0, bs).label == 85
    assert winning_bucket(86.999, bs).label == 85
    assert winning_bucket(87.0, bs).label == 87


def test_winning_bucket_tails():
    geom = GeometryConfig(hypothesis="floor_to", width=2.0, verified=True)
    bs, _ = enumerate_buckets("new_york", "2026-06-15", _seed_db(
        markets=[(83, "band"), (85, "band"),
                 (87, "open_high"), (81, "open_low")],
        station="new_york",
        date="2026-06-15",
    ), geom)
    assert winning_bucket(75.0, bs).label == 81  # open_low
    assert winning_bucket(95.0, bs).label == 87  # open_high


def test_winning_bucket_consistent_with_bucket_probs_point_mass():
    """SAME geometry must be applied to both: when bucket_probs concentrates ~100%
    on a label, winning_bucket on the same value must return that label.
    """
    geom = GeometryConfig(hypothesis="rounds_to", width=1.0, verified=True)
    bs, _ = enumerate_buckets("hong_kong", "2026-06-15", _seed_db(
        markets=[(30, "band"), (31, "band"), (32, "band"),
                 (33, "open_high"), (29, "open_low")],
        station="hong_kong",
        date="2026-06-15",
    ), geom)
    observed = 31.4
    probs = bucket_probs(_Normal(observed, 0.001), bs)
    top_label = max(probs, key=lambda l: probs[l])
    assert winning_bucket(observed, bs).label == top_label


# ---------------------------------------------------------------- helper


def _seed_db(markets, station, date):
    """Create a temp DB pre-seeded with the given markets rows and return its path."""
    import tempfile

    p = Path(tempfile.mkdtemp()) / "wxm.db"
    init_db(p, MIGRATIONS)
    conn = connect(p)
    try:
        for label, kind in markets:
            conn.execute(
                "INSERT INTO markets("
                "  market_id, station, date, bucket_label, bucket_kind,"
                "  token_id_yes, token_id_no, discovered_ts, closed) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    f"m-{station}-{date}-{label}-{kind}",
                    station,
                    date,
                    float(label),
                    kind,
                    f"ty-{label}",
                    f"tn-{label}",
                    0,
                    0,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return p
