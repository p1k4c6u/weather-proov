-- Phase 0 schema (13 tables). Verbatim from v2.1 spec PART V, restricted to
-- the Phase 0 subset listed in PART XIII Phase 0. Phase 1+ tables (chain_params,
-- exceedance_table, live_obs) are deferred to future migration files.

CREATE TABLE ensemble_forecasts (
  id INTEGER PRIMARY KEY,
  station TEXT NOT NULL,
  target_date TEXT NOT NULL,
  model TEXT NOT NULL,
  run_label TEXT NOT NULL,
  fetch_ts INTEGER NOT NULL,
  lead_hours REAL NOT NULL,
  source TEXT NOT NULL DEFAULT 'live',
  members_json TEXT NOT NULL,
  hourly_members_json TEXT,
  mean_c REAL NOT NULL,
  std_c REAL NOT NULL,
  UNIQUE(station, target_date, model, run_label)
);

CREATE TABLE forecast_pairs (
  station TEXT NOT NULL,
  target_date TEXT NOT NULL,
  lead_bucket TEXT NOT NULL,
  model TEXT NOT NULL,
  run_label TEXT NOT NULL,
  forecast_run_ts INTEGER NOT NULL,
  forecast_fetch_ts INTEGER NOT NULL,
  lead_hours REAL NOT NULL,
  forecast_source TEXT NOT NULL,
  source_weight REAL NOT NULL DEFAULT 1.0,
  settlement_source TEXT,
  settlement_ts INTEGER,
  y_c REAL,
  usable_for_train_after_date TEXT NOT NULL,
  created_ts INTEGER NOT NULL,
  PRIMARY KEY (station, target_date, lead_bucket, model, run_label, forecast_source),
  CHECK (forecast_source IN ('live','historical_forecast')),
  CHECK (source_weight > 0 AND source_weight <= 1.0)
);

CREATE TABLE observations (
  station TEXT NOT NULL,
  date TEXT NOT NULL,
  source TEXT NOT NULL,
  value REAL NOT NULL,
  units TEXT NOT NULL,
  fetched_ts INTEGER NOT NULL,
  is_settlement_source INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (station, date, source)
);

CREATE TABLE settlements (
  station TEXT NOT NULL,
  date TEXT NOT NULL,
  bucket_label REAL NOT NULL,
  market_id TEXT,
  resolved_ts INTEGER,
  our_predicted_winner REAL,
  mismatch INTEGER DEFAULT 0,
  PRIMARY KEY (station, date)
);

CREATE TABLE emos_params (
  station TEXT NOT NULL,
  model TEXT NOT NULL,
  lead_bucket TEXT NOT NULL,
  fitted_ts INTEGER NOT NULL,
  n_total INTEGER NOT NULL,
  n_live INTEGER NOT NULL DEFAULT 0,
  n_backfill INTEGER NOT NULL DEFAULT 0,
  effective_n REAL,
  stage TEXT NOT NULL DEFAULT 'S0',
  complexity_level TEXT NOT NULL DEFAULT 'climatology',
  param_drift REAL,
  param_drift_rel REAL,
  param_drift_abs REAL,
  prob_surface_drift REAL,
  a REAL, b REAL, c REAL, d REAL,
  e1 REAL, e2 REAL,
  train_crps REAL,
  holdout_crps REAL,
  PRIMARY KEY (station, model, lead_bucket)
);

CREATE TABLE blend_weights (
  station TEXT NOT NULL,
  lead_bucket TEXT NOT NULL,
  model TEXT NOT NULL,
  weight REAL NOT NULL,
  fitted_ts INTEGER NOT NULL,
  PRIMARY KEY (station, lead_bucket, model)
);

CREATE TABLE eligibility (
  station TEXT NOT NULL,
  lead_bucket TEXT NOT NULL,
  stage TEXT,
  n_live INTEGER,
  n_total INTEGER,
  effective_n REAL,
  pit_p REAL,
  pit_method TEXT,
  brier_model REAL,
  brier_market REAL,
  brier_delta REAL,
  brier_delta_ci_low REAL,
  market_price_policy TEXT,
  param_drift REAL,
  prob_surface_drift REAL,
  bucket_coverage REAL,
  near_mean_eligible INTEGER NOT NULL DEFAULT 0,
  tail_eligible INTEGER NOT NULL DEFAULT 0,
  live_eligible INTEGER NOT NULL DEFAULT 0,
  failed_gate TEXT,
  evaluated_ts INTEGER,
  PRIMARY KEY (station, lead_bucket)
);

CREATE TABLE markets (
  market_id TEXT PRIMARY KEY,
  station TEXT,
  date TEXT,
  bucket_label REAL,
  bucket_kind TEXT,
  token_id_yes TEXT,
  token_id_no TEXT,
  discovered_ts INTEGER,
  closed INTEGER DEFAULT 0
);

CREATE TABLE book_snapshots (
  market_id TEXT,
  ts INTEGER,
  bid1 REAL, bid1_sz REAL, bid2 REAL, bid2_sz REAL, bid3 REAL, bid3_sz REAL,
  ask1 REAL, ask1_sz REAL, ask2 REAL, ask2_sz REAL, ask3 REAL, ask3_sz REAL,
  PRIMARY KEY (market_id, ts)
);

CREATE TABLE signals (
  id INTEGER PRIMARY KEY,
  ts INTEGER,
  station TEXT,
  date TEXT,
  market_id TEXT,
  side TEXT,
  composite_score REAL,
  i1_edge REAL,
  indicator_json TEXT NOT NULL,
  touch_price REAL,
  edge_after_fees REAL,
  trigger TEXT,
  probs_version TEXT,
  acted INTEGER DEFAULT 0
);

CREATE TABLE orders (
  order_id TEXT PRIMARY KEY,
  signal_id INTEGER,
  ts INTEGER,
  market_id TEXT,
  side TEXT,
  price REAL,
  size_usd REAL,
  status TEXT,
  mode TEXT NOT NULL
);

CREATE TABLE fills (
  fill_id TEXT PRIMARY KEY,
  order_id TEXT,
  ts INTEGER,
  price REAL,
  size_usd REAL
);

CREATE TABLE positions (
  market_id TEXT PRIMARY KEY,
  station TEXT,
  date TEXT,
  side TEXT,
  direction TEXT,
  avg_price REAL,
  size_usd REAL,
  settled INTEGER DEFAULT 0,
  pnl REAL
);
