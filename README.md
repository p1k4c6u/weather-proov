# wxm — Weather Markets

Polymarket daily-high temperature market trader for Hong Kong (HKO), London (EGLC), and New York (KNYC). Python paper-trades from day one; live execution is gated by the self-calibration eligibility ladder.

This is **Phase 0** of the v2.1 build: a thin vertical slice running paper-only.

## Dev setup

```sh
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"

wxm init-db
wxm spec-check
pytest
```

## Spec

The system reads `spec/resolution.yaml` (per-city settlement geometry, station coords) and `spec/trading.yaml` (fees, edges, sizing, risk caps). Both are frozen pydantic models; loading is the sole entry point.

## Status flags

- `rounding_verified=false` on every station → trading is paper-only until the Phase 1 resolution audit.
- `eligibility.live_eligible=false` on every (station, lead_bucket) → no real orders in Phase 0.
