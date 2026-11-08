from importlib.metadata import version
from pathlib import Path

from typer.testing import CliRunner

from orphic.cli.main import app

runner = CliRunner()


def test_version_prints_installed_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == f"orphic {version('orphic')}"


def test_scan_accepts_directory(sample_tree: Path) -> None:
    print(sample_tree)
    result = runner.invoke(app, ["scan", str(sample_tree)])

    assert result.exit_code == 0
    assert str(sample_tree) in result.stdout


def test_scan_rejects_missing_path(tmp_path: Path) -> None:
    result = runner.invoke(app, ["scan", str(tmp_path / "missing")])

    assert result.exit_code == 2


def test_scan_rejects_file(sample_tree: Path) -> None:
    print(sample_tree / "empty.txt")
    result = runner.invoke(app, ["scan", str(sample_tree / "empty.txt")])

    assert result.exit_code == 2
