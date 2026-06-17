from pathlib import Path

import pytest
from pydantic import ValidationError

from wxm.spec import BucketsSpec, load_spec

SPEC_DIR = Path(__file__).resolve().parents[1] / "spec"


def test_resolution_loads_all_three_cities():
    spec = load_spec(SPEC_DIR)
    assert set(spec.resolution.cities) == {"hong_kong", "london", "new_york"}


def test_station_coordinates_are_station_not_city_centre():
    spec = load_spec(SPEC_DIR)
    # The v1 bug used city-centre coords; v2 fixes this. Verify the corrected values.
    hk = spec.resolution.cities["hong_kong"]
    assert (hk.latitude, hk.longitude) == (22.3019, 114.1742)
    london = spec.resolution.cities["london"]
    assert (london.latitude, london.longitude) == (51.5048, 0.0495)
    ny = spec.resolution.cities["new_york"]
    assert (ny.latitude, ny.longitude) == (40.7789, -73.9692)


def test_all_stations_start_unverified():
    spec = load_spec(SPEC_DIR)
    for city in spec.resolution.cities.values():
        assert city.buckets.rounding_verified is False
        assert city.buckets.hypothesis is None
        assert city.buckets.width is None


def test_rounding_verified_requires_geometry():
    with pytest.raises(ValidationError):
        BucketsSpec(
            kind="binary_per_label",
            label_units="celsius",
            hypothesis=None,
            width=None,
            rounding_verified=True,
        )


def test_rounding_verified_accepts_complete_geometry():
    b = BucketsSpec(
        kind="binary_per_label",
        label_units="celsius",
        hypothesis="rounds_to",
        width=1.0,
        rounding_verified=True,
    )
    assert b.rounding_verified is True
    assert b.width == 1.0


def test_trading_caps_loaded():
    spec = load_spec(SPEC_DIR)
    t = spec.trading
    assert t.sizing.kelly_fraction == 0.25
    assert t.sizing.bankroll_allocated_usd == 1000
    assert t.sizing.exposure.max_open_risk_usd == 300
    assert t.flags.leads_enabled.d0 is True
    assert t.flags.maker_mode_planned is False
    assert t.edges.min_edge_after_fees["d0"] == 0.04


def test_spec_is_frozen():
    spec = load_spec(SPEC_DIR)
    with pytest.raises(ValidationError):
        spec.trading.sizing.__class__(
            kelly_fraction=0.5,
            bankroll_allocated_usd=spec.trading.sizing.bankroll_allocated_usd,
            exposure=spec.trading.sizing.exposure,
            chain_brakes=spec.trading.sizing.chain_brakes,
            unknown_field=1,
        )
