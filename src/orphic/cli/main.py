from importlib.metadata import version
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from orphic.index.store import Index

app = typer.Typer(no_args_is_help=True)

DatabaseOption = Annotated[
    Path,
    typer.Option("--db", envvar="ORPHIC_DB", help="Index database location."),
]
DEFAULT_DATABASE = Path.home() / ".orphic" / "index.db"


def _show_version(requested: bool) -> None:
    if requested:
        typer.echo(f"orphic {version('orphic')}")
        raise typer.Exit()


@app.callback()
def main(
    show_version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_show_version,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Orphic: a local-first filesystem intelligence engine."""


def _open_index(database: Path) -> Index:
    database.parent.mkdir(parents=True, exist_ok=True)
    return Index(database)


@app.command()
def scan(
    path: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, help="Directory to scan."),
    ],
    full: Annotated[
        bool, typer.Option("--full", help="Relist every directory.")
    ] = False,
    database: DatabaseOption = DEFAULT_DATABASE,
) -> None:
    """Scan a directory into the index and report what was found and skipped."""
    with _open_index(database) as index:
        with Console(stderr=True).status("Scanning...") as status:
            summary = index.record_scan(
                path,
                full=full,
                on_progress=lambda n: status.update(f"Scanned {n:,} entries..."),
            )

    typer.echo(f"Scanned {path}")
    typer.echo(
        f"Files: {summary.files:,}  Directories: {summary.directories:,}  "
        f"Links: {summary.links:,}"
    )
    typer.echo(f"Total size: {summary.total_size:,} bytes")
    typer.echo(f"Directories reused: {summary.directories_reused:,}")
    typer.echo(f"Skipped: {summary.skipped.total():,}")
    for reason, count in sorted(summary.skipped.items()):
        typer.echo(f"  {reason}: {count:,}")


@app.command()
def largest(
    limit: Annotated[int, typer.Option(help="How many files to show.")] = 10,
    database: DatabaseOption = DEFAULT_DATABASE,
) -> None:
    """Show the largest indexed files."""
    with _open_index(database) as index:
        for row in index.largest(limit):
            typer.echo(f"{row['size']:>15,}  {row['path']}")
