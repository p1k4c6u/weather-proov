# wxm runbook — Phase 0

This is a paper-only build. There are no real orders to halt, but the recorder
and the paper loop are long-lived; the runbook covers starting, stopping, and
inspecting state.

## Start the local soak

```sh
cd weather-markets
uv pip install -e ".[dev]"   # one-time
wxm init-db                  # one-time
bash ops/run-local.sh
tmux attach -t wxm
```

Six tmux windows run in parallel: book recorder, hourly market discovery,
half-hourly ensemble fetch, 15-minute calibrate, paper signal loop, and a
nightly orchestration trigger.

## Halt

```sh
touch data/KILL
```

Every long-lived process checks this file and exits clean.

After investigation, resume with:

```sh
rm data/KILL
bash ops/run-local.sh
```

## Health check

```sh
wxm healthz
```

Returns nonzero on first failed check. Checked items:

| name | meaning |
|---|---|
| kill | `data/KILL` is absent |
| db | `data/wxm.db` is readable |
| probs | `data/bridge/probs.json` is fresh (within `trading.risk.stale_probs_max_age_s`) |
| ensembles | a forecast was fetched in the last 24h |
| books | a book snapshot was written in the last `trading.risk.stale_obs_max_age_s` |
| observations | a settlement-source observation exists within the last 48h |

## Logs

Per-window logs in `data/logs/{books,markets,ensembles,calibrate,paper,nightly}.log`.

## Raw archive

Every external HTTP/WS payload is gzipped before parsing into
`data/raw/<category>/<YYYY-MM-DD>/<ts_ms>.json.gz`. Parsing bugs are replayable;
do not delete raw files older than the ingestion window you care about.

Categories: `markets`, `books`, `ensembles`, `truth/{hko,wunderground,nws_cli,nws_cli_listing}`.

## Interpreting `failed_gate`

`eligibility.failed_gate` names the first blocker preventing live trading on a
`(station, lead_bucket)`. Phase 0 expected values:

- `ROUNDING_UNVERIFIED`: `spec/resolution.yaml` still has `rounding_verified: false`
  for that station. Phase 1's resolution audit unsticks this.
- `S2_NOT_REACHED`: not enough settled live forecast pairs yet (`n_live < 30` or
  `n_total < 60`). Just wait — the loop accumulates from settled days.
- `PIT_FAIL` / others: Phase 2 placeholders — should not appear until the EMOS
  pipeline lands.

A `(station, lead_bucket)` with `live_eligible = 0` is paper-only by definition,
and the paper executor enforces this before sizing.

## Missing daily report

If `reports/{YYYY-MM-DD}.md` is missing for yesterday:

1. Check `data/logs/nightly.log` for an exception trace.
2. Run the orchestration manually: `wxm cycle nightly`.
3. If individual steps fail, run them one at a time: `wxm fetch truth --date YYYY-MM-DD`, then `wxm settle --date YYYY-MM-DD`, then `wxm calibrate`, then `wxm report daily`.

## Backups

```sh
bash ops/backup.sh
```

Writes a date-stamped `wxm.db` backup and rsyncs the raw archive into
`$WXM_BACKUP_DIR` (default: `../wxm-backup`).
