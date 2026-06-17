"""The literals 9/5, 5/9, and 1.8 (as °C↔°F conversion factors) may appear only
in py/wxm/units.py. This test greps the source tree to enforce the rule.

Future Rust port (rs/) is exempt and gets its own mirror test in Rust.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

ALLOWED = {
    REPO_ROOT / "py" / "wxm" / "units.py",
}

EXCLUDED_DIRS = {".venv", "venv", "__pycache__", ".pytest_cache", "dist", "build", "data", "rs"}

PATTERNS = [
    re.compile(r"\b9\s*/\s*5\b"),
    re.compile(r"\b5\s*/\s*9\b"),
    re.compile(r"\*\s*1\.8\b"),
]


def _iter_py_files():
    for path in REPO_ROOT.rglob("*.py"):
        parts = set(path.relative_to(REPO_ROOT).parts)
        if parts & EXCLUDED_DIRS:
            continue
        # tests/ files are allowed to reference the literals — they compare against them
        if path.relative_to(REPO_ROOT).parts[0] == "tests":
            continue
        yield path


def test_no_unit_literals_outside_units():
    offenders = []
    for path in _iter_py_files():
        if path in ALLOWED:
            continue
        text = path.read_text()
        for pat in PATTERNS:
            if pat.search(text):
                offenders.append((path.relative_to(REPO_ROOT), pat.pattern))
    assert not offenders, (
        "unit-conversion literals found outside units.py: "
        + ", ".join(f"{p} matches /{pat}/" for p, pat in offenders)
    )


def test_units_py_itself_contains_the_literals():
    """Sanity check: if units.py stops containing the literals, the grep test
    becomes vacuous — better to fail loudly here."""
    text = (REPO_ROOT / "py" / "wxm" / "units.py").read_text()
    assert any(p.search(text) for p in PATTERNS), \
        "units.py is expected to contain at least one of 9/5, 5/9, *1.8"
