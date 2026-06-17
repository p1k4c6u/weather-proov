"""Frozen pydantic models for spec/resolution.yaml and spec/trading.yaml."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, model_validator


_FROZEN = ConfigDict(frozen=True, extra="forbid")


class BucketsSpec(BaseModel):
    model_config = _FROZEN

    kind: Literal["binary_per_label"]
    label_units: Literal["celsius", "fahrenheit"]
    hypothesis: Literal["rounds_to", "floor_to", "exact_band"] | None = None
    width: float | None = None
    rounding_verified: bool = False

    @model_validator(mode="after")
    def _check_geometry_when_verified(self) -> "BucketsSpec":
        if self.rounding_verified and (self.hypothesis is None or self.width is None):
            raise ValueError(
                "rounding_verified=true requires both hypothesis and width to be set"
            )
        return self


class SettlementSpec(BaseModel):
    model_config = _FROZEN

    provider: str
    station_id: str
    source_kind: str
    url: str
    units: Literal["celsius", "fahrenheit"]
    reported_precision: int
    revision_policy: Literal["first_publication_only", "revisable_until_next_day"]
    typical_delay_hours: int


class LiveObsSpec(BaseModel):
    model_config = _FROZEN

    source_kind: str
    cadence_s: int
    poll_s: int


class ChainSpec(BaseModel):
    model_config = _FROZEN

    rho: float | None = None
    peak_local_hour: float


class CitySpec(BaseModel):
    model_config = _FROZEN

    name: str
    polymarket_slug_pattern: str
    latitude: float
    longitude: float
    timezone: str
    variable: Literal["daily_high"]
    settlement: SettlementSpec
    buckets: BucketsSpec
    live_obs: LiveObsSpec
    chain: ChainSpec


class ResolutionSpec(BaseModel):
    model_config = _FROZEN

    schema_version: int
    cities: dict[str, CitySpec]


class FeesSpec(BaseModel):
    model_config = _FROZEN

    taker_bps: float | None = None
    maker_bps: float | None = None
    winnings_fee_bps: float | None = None
    gas_fixed_usd: float = 0.0


class SlippageSpec(BaseModel):
    model_config = _FROZEN

    model: Literal["half_spread_plus"]
    alpha: float


class EdgesSpec(BaseModel):
    model_config = _FROZEN

    min_edge_after_fees: dict[str, float]
    min_model_prob_for_yes: float


class IndicatorsSpec(BaseModel):
    model_config = _FROZEN

    weights: dict[str, float]
    i1_veto: bool
    downside_eps_trust_hour_offset: float


class ExposureSpec(BaseModel):
    model_config = _FROZEN

    max_stake_per_market_usd: float
    max_stake_per_station_target_date_usd: float
    max_station_chain_risk_usd: float
    opposite_direction_offset: float
    max_open_risk_usd: float


class ChainBrakesSpec(BaseModel):
    model_config = _FROZEN

    consecutive_loss_days_halve: int
    consecutive_loss_days_halt: int
    regime_error_freeze_c: float


class SizingSpec(BaseModel):
    model_config = _FROZEN

    kelly_fraction: float
    bankroll_allocated_usd: float
    exposure: ExposureSpec
    chain_brakes: ChainBrakesSpec


class RiskSpec(BaseModel):
    model_config = _FROZEN

    daily_loss_kill_usd: float
    stale_probs_max_age_s: int
    stale_obs_max_age_s: int
    kill_file: str


class LeadsEnabledSpec(BaseModel):
    model_config = _FROZEN

    d0: bool
    d1: bool
    d2: bool
    d3: bool


class FlagsSpec(BaseModel):
    model_config = _FROZEN

    leads_enabled: LeadsEnabledSpec
    maker_mode_planned: bool


class TradingSpec(BaseModel):
    model_config = _FROZEN

    fees: FeesSpec
    slippage: SlippageSpec
    oracle_haircut: float
    edges: EdgesSpec
    indicators: IndicatorsSpec
    sizing: SizingSpec
    risk: RiskSpec
    flags: FlagsSpec


class Spec(BaseModel):
    model_config = _FROZEN

    resolution: ResolutionSpec
    trading: TradingSpec


def load_spec(spec_dir: Path) -> Spec:
    res_path = spec_dir / "resolution.yaml"
    tr_path = spec_dir / "trading.yaml"
    res = yaml.safe_load(res_path.read_text())
    tr = yaml.safe_load(tr_path.read_text())
    return Spec(
        resolution=ResolutionSpec(**res),
        trading=TradingSpec(**tr),
    )
