from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORPHIC_DB", str(tmp_path / "cli-index.db"))
    monkeypatch.setenv("ORPHIC_RECOVERY", str(tmp_path / "cli-recovery"))


@pytest.fixture
def sample_tree(tmp_path: Path) -> Path:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "notes.txt").write_text("notes")
    (tmp_path / "empty.txt").touch()
    return tmp_path
