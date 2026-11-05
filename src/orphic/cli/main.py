from importlib.metadata import version
from pathlib import Path
from typing import Annotated

import typer

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
    """Scan a directory. Not implemented until Phase 1."""
    typer.echo(f"Would scan: {path}")
