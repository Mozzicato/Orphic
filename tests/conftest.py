from pathlib import Path

import pytest


@pytest.fixture
def sample_tree(tmp_path: Path) -> Path:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "notes.txt").write_text("notes")
    (tmp_path / "empty.txt").touch()
    return tmp_path
