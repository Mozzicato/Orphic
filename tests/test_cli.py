import shutil
import subprocess
from importlib.metadata import version
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orphic.cli.main import app

runner = CliRunner()


def test_version_prints_installed_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == f"orphic {version('orphic')}"


def test_scan_accepts_directory(sample_tree: Path) -> None:
    result = runner.invoke(app, ["scan", str(sample_tree)])

    assert result.exit_code == 0
    assert str(sample_tree) in result.stdout


def test_scan_rejects_missing_path(tmp_path: Path) -> None:
    result = runner.invoke(app, ["scan", str(tmp_path / "missing")])

    assert result.exit_code == 2


def test_scan_rejects_file(sample_tree: Path) -> None:
    result = runner.invoke(app, ["scan", str(sample_tree / "empty.txt")])

    assert result.exit_code == 2


def test_scan_reports_counts(sample_tree: Path) -> None:
    result = runner.invoke(app, ["scan", str(sample_tree)])

    assert "Files: 2  Directories: 1  Links: 0" in result.stdout
    assert "Total size: 5 bytes" in result.stdout
    assert "Skipped: 0" in result.stdout


def test_largest_reads_from_the_index_without_rescanning(sample_tree: Path) -> None:
    runner.invoke(app, ["scan", str(sample_tree)])

    result = runner.invoke(app, ["largest", "--limit", "1"])

    assert result.exit_code == 0
    assert "notes.txt" in result.stdout


def test_dedup_reports_groups_and_saves_them(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("same")
    (root / "b.txt").write_text("same")
    runner.invoke(app, ["scan", str(root)])

    result = runner.invoke(app, ["dedup"])

    assert result.exit_code == 0
    assert "Duplicate groups: 1" in result.stdout
    assert "Recoverable: 4 bytes" in result.stdout


def test_installed_command_can_scan_with_processes(sample_tree: Path) -> None:
    command = shutil.which("orphic")
    if command is None:
        pytest.skip("orphic is not on PATH")

    completed = subprocess.run(
        [command, "scan", str(sample_tree), "--workers", "2", "--processes"],
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Directories: 1" in completed.stdout
