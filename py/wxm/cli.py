"""wxm CLI — Phase 0 commands."""

from pathlib import Path

import click

from .db import DEFAULT_DB_PATH, DEFAULT_MIGRATIONS_DIR, init_db
from .spec import load_spec


@click.group()
def main() -> None:
    """Weather Markets CLI."""


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
