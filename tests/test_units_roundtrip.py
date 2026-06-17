import pytest

from wxm.units import (
    c_to_f,
    c_to_native,
    f_to_c,
    native_to_c,
    sigma_to_label_units,
    to_label_units,
)


@pytest.mark.parametrize("c", [-40.0, 0.0, 20.0, 37.0, 100.0])
def test_c_to_f_to_c_roundtrip(c):
    assert f_to_c(c_to_f(c)) == pytest.approx(c, abs=1e-9)


def test_c_to_f_known_anchors():
    assert c_to_f(0) == pytest.approx(32.0)
    assert c_to_f(100) == pytest.approx(212.0)
    assert c_to_f(-40) == pytest.approx(-40.0)


def test_native_to_c():
    assert native_to_c(20.0, "celsius") == 20.0
    assert native_to_c(68.0, "fahrenheit") == pytest.approx(20.0)


def test_c_to_native():
    assert c_to_native(20.0, "celsius") == 20.0
    assert c_to_native(20.0, "fahrenheit") == pytest.approx(68.0)


def test_to_label_units_alias():
    assert to_label_units(20.0, "celsius") == 20.0
    assert to_label_units(20.0, "fahrenheit") == pytest.approx(68.0)


def test_sigma_is_multiplicative_only_no_offset():
    """sigma_to_label_units must scale without adding the 32 offset."""
    assert sigma_to_label_units(0.0, "fahrenheit") == 0.0
    assert sigma_to_label_units(1.0, "fahrenheit") == pytest.approx(1.8)
    assert sigma_to_label_units(2.5, "fahrenheit") == pytest.approx(4.5)
    assert sigma_to_label_units(1.0, "celsius") == 1.0


def test_unknown_units_raises():
    with pytest.raises(ValueError):
        native_to_c(1.0, "kelvin")
    with pytest.raises(ValueError):
        c_to_native(1.0, "kelvin")
    with pytest.raises(ValueError):
        sigma_to_label_units(1.0, "rankine")
