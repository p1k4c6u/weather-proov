"""Unit conversion. Sole owner of all temperature unit literals in the codebase.

Enforced by tests/test_units_literal_grep.py: the literals 9/5, 5/9, and 1.8
may not appear in any other Python file outside tests/ and the eventual Rust
mirror.
"""


def c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def f_to_c(f: float) -> float:
    return (f - 32) * 5 / 9


def native_to_c(value: float, units: str) -> float:
    if units == "celsius":
        return value
    if units == "fahrenheit":
        return f_to_c(value)
    raise ValueError(f"unknown units: {units!r}")


def c_to_native(value_c: float, units: str) -> float:
    if units == "celsius":
        return value_c
    if units == "fahrenheit":
        return c_to_f(value_c)
    raise ValueError(f"unknown units: {units!r}")


def to_label_units(value_c: float, label_units: str) -> float:
    return c_to_native(value_c, label_units)


def sigma_to_label_units(sigma_c: float, label_units: str) -> float:
    """Convert a standard deviation from °C to label units. Multiplicative only."""
    if label_units == "celsius":
        return sigma_c
    if label_units == "fahrenheit":
        return sigma_c * 9 / 5
    raise ValueError(f"unknown label_units: {label_units!r}")
