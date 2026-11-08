from collections import Counter
from importlib.metadata import version
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from orphic.scanner import walker
from orphic.scanner.walker import ScanSkip, SkipReason

app = typer.Typer(no_args_is_help=True)


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


@app.command()
def scan(
    path: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, help="Directory to scan."),
    ],
) -> None:
    """Scan a directory and report what was found and skipped."""
    files = directories = links = total_size = 0
    skipped: Counter[SkipReason] = Counter()

    with Console(stderr=True).status("Scanning...") as status:
        for count, item in enumerate(walker.scan(path), start=1):
            if isinstance(item, ScanSkip):
                skipped[item.reason] += 1
            elif item.is_symlink:
                links += 1
            elif item.is_directory:
                directories += 1
            else:
                files += 1
                total_size += item.size
            if count % 1000 == 0:
                status.update(f"Scanned {count:,} entries...")

    typer.echo(f"Scanned {path}")
    typer.echo(f"Files: {files:,}  Directories: {directories:,}  Links: {links:,}")
    typer.echo(f"Total size: {total_size:,} bytes")
    typer.echo(f"Skipped: {skipped.total():,}")
    for reason, count in sorted(skipped.items()):
        typer.echo(f"  {reason}: {count:,}")
