from importlib.metadata import version
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from orphic.dedup.engine import Strategy, find_duplicates
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
    workers: Annotated[
        int, typer.Option(min=1, help="Threads used to read directories.")
    ] = 1,
    database: DatabaseOption = DEFAULT_DATABASE,
) -> None:
    """Scan a directory into the index and report what was found and skipped."""
    with _open_index(database) as index:
        with Console(stderr=True).status("Scanning...") as status:
            summary = index.record_scan(
                path,
                full=full,
                workers=workers,
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


@app.command()
def dedup(
    strategy: Annotated[
        Strategy, typer.Option(help="How candidates are narrowed before full hashing.")
    ] = Strategy.SIZE_FIRST,
    limit: Annotated[int, typer.Option(help="How many groups to show.")] = 10,
    database: DatabaseOption = DEFAULT_DATABASE,
) -> None:
    """Find files with identical content among the indexed files."""
    with _open_index(database) as index:
        with Console(stderr=True).status("Hashing candidates..."):
            groups, stats = find_duplicates(index.candidates(), strategy)
        index.save_duplicates(groups)

    for number, group in enumerate(groups[:limit], start=1):
        typer.echo(f"Duplicate Group #{number}")
        typer.echo(f"Original candidate: {group.original.path}")
        typer.echo(f"Duplicates: {', '.join(f.path for f in group.duplicates)}")
        typer.echo(f"Recoverable: {group.recoverable_bytes:,} bytes")
        typer.echo("")

    typer.echo(f"Duplicate groups: {len(groups):,}")
    typer.echo(f"Recoverable: {sum(g.recoverable_bytes for g in groups):,} bytes")
    typer.echo(
        f"Files considered: {stats.files_considered:,}  "
        f"Fully hashed: {stats.full_hashed:,} "
        f"({stats.full_hash_skip_ratio:.1%} skipped)"
    )
    typer.echo(f"Files opened: {stats.files_opened:,}  Bytes read: {stats.bytes_read:,}")
    typer.echo(f"Hard links ignored: {stats.hard_links:,}")
    typer.echo(f"Skipped: {stats.skipped.total():,}")
    for reason, count in sorted(stats.skipped.items()):
        typer.echo(f"  {reason}: {count:,}")
