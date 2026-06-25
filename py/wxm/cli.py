"""wxm CLI — Phase 0 commands."""

from pathlib import Path

import click

from .db import DEFAULT_DB_PATH, DEFAULT_MIGRATIONS_DIR, init_db
from .spec import load_spec


@click.group()
def main() -> None:
    """Weather Markets CLI."""


@main.group("fetch")
def fetch() -> None:
    """Pull external data into the wxm DB."""


@main.command("settle")
@click.option("--date", "target_date", type=str, required=False,
              help="Specific YYYY-MM-DD to settle; default = each station's local yesterday.")
@click.option("--station", type=str, required=False,
              help="Settle this station only (default: all stations).")
@click.option("--spec-dir", type=click.Path(path_type=Path, exists=True, file_okay=False),
              default=Path("spec"), show_default=True)
@click.option("--db", "db_path", type=click.Path(path_type=Path),
              default=DEFAULT_DB_PATH, show_default=True)
def settle_cmd(target_date: str | None, station: str | None, spec_dir: Path, db_path: Path) -> None:
    """Run the OPEN→PROVISIONAL→FINAL settlement state machine."""
    from .settle.settlement import settle_date, settle_pending_for_yesterday

    spec = load_spec(spec_dir)
    if target_date:
        stations = [station] if station else list(spec.resolution.cities)
        for sid in stations:
            out = settle_date(spec, sid, target_date, db_path)
            click.echo(f"  {sid} {target_date}: {out.state} winner={out.winning_label} pairs={out.pairs_completed}")
    else:
        results = settle_pending_for_yesterday(spec, db_path)
        for sid, out in results.items():
            click.echo(f"  {sid} {out.target_date}: {out.state} winner={out.winning_label} pairs={out.pairs_completed}")


@main.group("report")
def report() -> None:
    """Reports."""


@report.command("daily")
@click.option("--spec-dir", type=click.Path(path_type=Path, exists=True, file_okay=False),
              default=Path("spec"), show_default=True)
@click.option("--db", "db_path", type=click.Path(path_type=Path),
              default=DEFAULT_DB_PATH, show_default=True)
@click.option("--reports-dir", type=click.Path(path_type=Path),
              default=Path("reports"), show_default=True)
def report_daily_cmd(spec_dir: Path, db_path: Path, reports_dir: Path) -> None:
    """Write today's daily Markdown report."""
    from .report.daily import write_daily_report

    spec = load_spec(spec_dir)
    out = write_daily_report(spec, db_path, reports_dir=reports_dir)
    click.echo(f"report: {out}")


@main.group("cycle")
def cycle() -> None:
    """Multi-step orchestrations."""


@cycle.command("nightly")
@click.option("--spec-dir", type=click.Path(path_type=Path, exists=True, file_okay=False),
              default=Path("spec"), show_default=True)
@click.option("--db", "db_path", type=click.Path(path_type=Path),
              default=DEFAULT_DB_PATH, show_default=True)
@click.option("--bridge-dir", type=click.Path(path_type=Path),
              default=Path("data/bridge"), show_default=True)
@click.option("--reports-dir", type=click.Path(path_type=Path),
              default=Path("reports"), show_default=True)
def cycle_nightly_cmd(spec_dir: Path, db_path: Path, bridge_dir: Path,
                     reports_dir: Path) -> None:
    """Nightly: fetch truth → settle yesterday → recalibrate → report."""
    from datetime import datetime as _dt, timedelta as _td
    from zoneinfo import ZoneInfo as _ZI

    from .bridge import write_probs
    from .calibrate.eligibility import evaluate_phase0
    from .calibrate.pipeline import (
        _lead_bucket_for_offset,
        calibrate_board,
        count_forecast_pairs,
        write_emos_params_row,
    )
    from .ingest.station_truth import fetch_truth
    from .report.daily import write_daily_report
    from .settle.settlement import settle_date

    spec = load_spec(spec_dir)

    # 1) Truth + settle yesterday per station's local calendar
    for sid, city in spec.resolution.cities.items():
        yesterday = (_dt.now(_ZI(city.timezone)).date() - _td(days=1)).isoformat()
        fetch_truth(spec, yesterday, db_path=db_path)
        out = settle_date(spec, sid, yesterday, db_path)
        click.echo(f"  settle {sid} {yesterday}: {out.state} pairs={out.pairs_completed}")

    # 2) Recalibrate every (station, future target_date) we have forecasts for
    evaluate_phase0(spec, db_path)
    predictions_by_station: dict[str, list] = {}
    for sid, city in spec.resolution.cities.items():
        station_local = _dt.now(_ZI(city.timezone))
        today_iso = station_local.date().isoformat()
        conn = connect(db_path)
        try:
            target_rows = conn.execute(
                "SELECT DISTINCT target_date FROM ensemble_forecasts WHERE station=?", (sid,),
            ).fetchall()
        finally:
            conn.close()
        boards = []
        for r in target_rows:
            lead = _lead_bucket_for_offset(r["target_date"], today_iso, station_local.hour)
            if lead == "past":
                continue
            pred = calibrate_board(spec, sid, r["target_date"], lead, db_path)
            if pred is None:
                continue
            n_live, n_backfill = count_forecast_pairs(db_path, sid, lead)
            write_emos_params_row(db_path, sid, r["target_date"], lead, n_live, n_backfill)
            boards.append(pred)
        predictions_by_station[sid] = boards
    write_probs(spec, predictions_by_station, db_path, bridge_dir=bridge_dir)

    # 3) Daily report
    out_path = write_daily_report(spec, db_path, reports_dir=reports_dir)
    click.echo(f"nightly complete; report: {out_path}")


@main.command("healthz")
@click.option("--spec-dir", type=click.Path(path_type=Path, exists=True, file_okay=False),
              default=Path("spec"), show_default=True)
@click.option("--db", "db_path", type=click.Path(path_type=Path),
              default=DEFAULT_DB_PATH, show_default=True)
@click.option("--bridge-dir", type=click.Path(path_type=Path),
              default=Path("data/bridge"), show_default=True)
@click.option("--kill-file", type=click.Path(path_type=Path),
              default=Path("data/KILL"), show_default=True)
def healthz_cmd(spec_dir: Path, db_path: Path, bridge_dir: Path, kill_file: Path) -> None:
    """Print one line per health check; exit nonzero on first failure."""
    import sys as _sys

    from .healthz import healthz

    spec = load_spec(spec_dir)
    checks = healthz(spec, db_path=db_path, bridge_dir=bridge_dir, kill_file=kill_file)
    all_ok = True
    for c in checks:
        mark = "ok " if c.ok else "FAIL"
        click.echo(f"[{mark}] {c.name}: {c.detail}")
        if not c.ok:
            all_ok = False
    _sys.exit(0 if all_ok else 1)


@main.group("paper")
def paper() -> None:
    """Phase 0 paper executor."""


@paper.command("run")
@click.option("--spec-dir", type=click.Path(path_type=Path, exists=True, file_okay=False),
              default=Path("spec"), show_default=True)
@click.option("--db", "db_path", type=click.Path(path_type=Path),
              default=DEFAULT_DB_PATH, show_default=True)
@click.option("--bridge-dir", type=click.Path(path_type=Path),
              default=Path("data/bridge"), show_default=True)
@click.option("--once/--loop", default=False,
              help="Run a single pass and exit, or loop until KILL file appears.")
@click.option("--poll-s", type=int, default=30, show_default=True)
def paper_run_cmd(spec_dir: Path, db_path: Path, bridge_dir: Path, once: bool, poll_s: int) -> None:
    """Read probs.json + book snapshots, log I1 signals, simulate fills (paper-only)."""
    import logging as _logging
    from .execute.paper import run_loop, run_once

    _logging.basicConfig(level=_logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    spec = load_spec(spec_dir)
    if once:
        stats = run_once(spec, db_path=db_path, bridge_dir=bridge_dir)
        click.echo(f"signals={stats['signals']} fills={stats['fills']}")
    else:
        run_loop(spec, db_path=db_path, bridge_dir=bridge_dir, poll_s=poll_s)


@main.command("calibrate")
@click.option(
    "--spec-dir",
    type=click.Path(path_type=Path, exists=True, file_okay=False),
    default=Path("spec"),
    show_default=True,
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(path_type=Path),
    default=DEFAULT_DB_PATH,
    show_default=True,
)
@click.option(
    "--bridge-dir",
    type=click.Path(path_type=Path),
    default=Path("data/bridge"),
    show_default=True,
)
def calibrate_cmd(spec_dir: Path, db_path: Path, bridge_dir: Path) -> None:
    """Phase 0 calibration: climatology + raw-ensemble Gaussian → probs.json."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI

    from .bridge import write_probs
    from .calibrate.eligibility import evaluate_phase0
    from .calibrate.pipeline import (
        calibrate_board,
        count_forecast_pairs,
        write_emos_params_row,
    )

    spec = load_spec(spec_dir)
    evaluate_phase0(spec, db_path)

    predictions_by_station: dict[str, list] = {}
    for station_id, city in spec.resolution.cities.items():
        station_local = _dt.now(_ZI(city.timezone))
        today_iso = station_local.date().isoformat()
        from .calibrate.pipeline import _lead_bucket_for_offset
        boards = []
        # Look at all target_dates for which forecasts have been ingested
        from .db import connect as _connect
        conn = _connect(db_path)
        try:
            tgt_rows = conn.execute(
                "SELECT DISTINCT target_date FROM ensemble_forecasts WHERE station=?",
                (station_id,),
            ).fetchall()
        finally:
            conn.close()
        for r in tgt_rows:
            target_date = r["target_date"]
            lead = _lead_bucket_for_offset(target_date, today_iso, station_local.hour)
            if lead == "past":
                continue
            pred = calibrate_board(spec, station_id, target_date, lead, db_path)
            if pred is None:
                continue
            n_live, n_backfill = count_forecast_pairs(db_path, station_id, lead)
            write_emos_params_row(db_path, station_id, target_date, lead, n_live, n_backfill)
            boards.append(pred)
        predictions_by_station[station_id] = boards
        click.echo(f"  {station_id}: {len(boards)} boards calibrated")

    target = write_probs(spec, predictions_by_station, db_path, bridge_dir=bridge_dir)
    click.echo(f"probs.json written: {target}")


@main.group("record")
def record() -> None:
    """Long-lived recorders (book snapshots, etc)."""


@record.command("books")
@click.option(
    "--db",
    "db_path",
    type=click.Path(path_type=Path),
    default=DEFAULT_DB_PATH,
    show_default=True,
)
@click.option(
    "--snapshot-interval-s",
    type=int,
    default=15,
    show_default=True,
)
def record_books_cmd(db_path: Path, snapshot_interval_s: int) -> None:
    """Subscribe to Polymarket CLOB WS for all open tokens and record top-3 book snapshots."""
    import logging as _logging
    from .ingest.book_recorder import run_recorder

    _logging.basicConfig(level=_logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run_recorder(db_path=db_path, snapshot_interval_s=snapshot_interval_s)


@fetch.command("markets")
@click.option(
    "--spec-dir",
    type=click.Path(path_type=Path, exists=True, file_okay=False),
    default=Path("spec"),
    show_default=True,
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(path_type=Path),
    default=DEFAULT_DB_PATH,
    show_default=True,
)
@click.option(
    "--days-ahead",
    type=int,
    default=4,
    show_default=True,
    help="Number of local target dates per station to probe (d0..d3 = 4).",
)
def fetch_markets_cmd(spec_dir: Path, db_path: Path, days_ahead: int) -> None:
    """Discover open Polymarket boards for each station via the Gamma API."""
    from .ingest.market_discovery import discover_markets

    spec = load_spec(spec_dir)
    counts = discover_markets(spec, db_path, days_ahead=days_ahead)
    total = sum(counts.values())
    for station, n in counts.items():
        click.echo(f"  {station}: {n} buckets")
    click.echo(f"total: {total} bucket markets upserted")


@fetch.command("truth")
@click.option(
    "--date",
    "target_date",
    type=str,
    required=True,
    help="Target local date in YYYY-MM-DD form.",
)
@click.option(
    "--spec-dir",
    type=click.Path(path_type=Path, exists=True, file_okay=False),
    default=Path("spec"),
    show_default=True,
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(path_type=Path),
    default=DEFAULT_DB_PATH,
    show_default=True,
)
def fetch_truth_cmd(target_date: str, spec_dir: Path, db_path: Path) -> None:
    """Pull settlement-grade observations (HKO XML, Open-Meteo archive, NWS CLI)."""
    from .ingest.station_truth import fetch_truth

    spec = load_spec(spec_dir)
    result = fetch_truth(spec, target_date, db_path=db_path)
    for station, sources in result.items():
        click.echo(f"  {station}: {sources or '(none)'}")


@fetch.command("ensembles")
@click.option(
    "--spec-dir",
    type=click.Path(path_type=Path, exists=True, file_okay=False),
    default=Path("spec"),
    show_default=True,
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(path_type=Path),
    default=DEFAULT_DB_PATH,
    show_default=True,
)
def fetch_ensembles_cmd(spec_dir: Path, db_path: Path) -> None:
    """Pull ECMWF/GFS/ICON ensemble forecasts from Open-Meteo for d0..d3."""
    from .ingest.ensembles import fetch_ensembles

    spec = load_spec(spec_dir)
    counts = fetch_ensembles(spec, db_path)
    total = sum(counts.values())
    for station, n in counts.items():
        click.echo(f"  {station}: {n} (station,target_date,model,run) rows")
    click.echo(f"total: {total} forecast pairs upserted")


@main.command("init-db")
@click.option(
    "--db",
    "db_path",
    type=click.Path(path_type=Path),
    default=DEFAULT_DB_PATH,
    show_default=True,
)
@click.option(
    "--migrations",
    "migrations_dir",
    type=click.Path(path_type=Path, exists=True, file_okay=False),
    default=DEFAULT_MIGRATIONS_DIR,
    show_default=True,
)
def init_db_cmd(db_path: Path, migrations_dir: Path) -> None:
    """Create the SQLite DB and apply any pending migrations."""
    applied = init_db(db_path, migrations_dir)
    if applied:
        click.echo(f"applied {len(applied)} migration(s): {', '.join(applied)}")
    else:
        click.echo("no pending migrations")
    click.echo(f"db ready at {db_path}")


@main.command("spec-check")
@click.option(
    "--spec-dir",
    type=click.Path(path_type=Path, exists=True, file_okay=False),
    default=Path("spec"),
    show_default=True,
)
def spec_check_cmd(spec_dir: Path) -> None:
    """Load and validate both YAML spec files; print a one-line summary per city."""
    spec = load_spec(spec_dir)
    click.echo(f"resolution.schema_version={spec.resolution.schema_version}")
    for sid, city in spec.resolution.cities.items():
        b = city.buckets
        click.echo(
            f"  {sid}: lat={city.latitude} lon={city.longitude} "
            f"label_units={b.label_units} rounding_verified={b.rounding_verified} "
            f"hypothesis={b.hypothesis} width={b.width}"
        )
    click.echo(
        f"trading.kelly_fraction={spec.trading.sizing.kelly_fraction} "
        f"bankroll={spec.trading.sizing.bankroll_allocated_usd} "
        f"oracle_haircut={spec.trading.oracle_haircut}"
    )


if __name__ == "__main__":
    main()
