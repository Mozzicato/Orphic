import os
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from orphic.index.store import Index
from orphic.scanner import walker
from orphic.scanner.walker import FileRecord, ScanItem


@pytest.fixture
def index(tmp_path: Path) -> Iterator[Index]:
    with Index(tmp_path / "index.db") as opened:
        yield opened


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    (root / "a" / "deep").mkdir(parents=True)
    (root / "b").mkdir()
    (root / "top.txt").write_text("top")
    (root / "a" / "one.txt").write_text("one!")
    (root / "a" / "deep" / "two.log").write_text("twotwo")
    (root / "b" / "three.txt").write_text("x")
    return root


def set_directory_mtimes(root: Path, mtime: float) -> None:
    for directory in [root, *(p for p in root.rglob("*") if p.is_dir())]:
        os.utime(directory, (mtime, mtime))


def age_directories(root: Path) -> None:
    set_directory_mtimes(root, 1_000_000_000.0)


@pytest.fixture
def listings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    listed: list[str] = []
    real_scandir = os.scandir

    def counting_scandir(path: str) -> Any:
        listed.append(path)
        return real_scandir(path)

    monkeypatch.setattr(walker.os, "scandir", counting_scandir)
    return listed


def paths(rows: list[Any]) -> list[str]:
    return [row["name"] for row in rows]


def test_index_survives_close_and_reopen(tmp_path: Path, tree: Path) -> None:
    database = tmp_path / "index.db"
    with Index(database) as first:
        first.record_scan(tree)

    with Index(database) as second:
        assert paths(second.largest(1)) == ["two.log"]


def test_summary_counts_files_directories_and_size(index: Index, tree: Path) -> None:
    summary = index.record_scan(tree)

    assert (summary.files, summary.directories, summary.links) == (4, 3, 0)
    assert summary.total_size == len("top" + "one!" + "twotwo" + "x")
    assert summary.directories_reused == 0


def test_rescan_keeps_one_row_per_path_and_first_seen(
    index: Index, tree: Path
) -> None:
    index.record_scan(tree)
    before = index.largest(10)[0]["first_seen"]
    time.sleep(0.01)
    index.record_scan(tree, full=True)

    rows = index.largest(10)
    assert len(rows) == 4
    assert rows[0]["first_seen"] == before
    assert rows[0]["last_seen"] > before


def test_deleted_file_leaves_the_index(index: Index, tree: Path) -> None:
    index.record_scan(tree)
    (tree / "top.txt").unlink()

    index.record_scan(tree)

    assert "top.txt" not in paths(index.largest(10))


def test_rescan_of_one_root_leaves_other_roots_alone(
    index: Index, tree: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    (other / "kept.txt").touch()
    index.record_scan(other)

    index.record_scan(tree)

    assert "kept.txt" in paths(index.largest(10))


def test_unchanged_tree_lists_only_the_root(
    index: Index, tree: Path, listings: list[str]
) -> None:
    age_directories(tree)
    index.record_scan(tree)
    listings.clear()

    summary = index.record_scan(tree)

    assert listings == [str(tree.resolve())]
    assert summary.directories_reused == 3
    assert (summary.files, summary.directories) == (4, 3)


def test_only_the_changed_directory_is_relisted(
    index: Index, tree: Path, listings: list[str]
) -> None:
    age_directories(tree)
    index.record_scan(tree)
    (tree / "a" / "deep" / "new.txt").touch()
    listings.clear()

    summary = index.record_scan(tree)

    assert sorted(listings) == sorted(
        [str(tree.resolve()), str((tree / "a" / "deep").resolve())]
    )
    assert summary.files == 5


def test_recently_changed_directories_are_not_trusted(
    index: Index, tree: Path, listings: list[str]
) -> None:
    set_directory_mtimes(tree, time.time())
    index.record_scan(tree)
    listings.clear()

    summary = index.record_scan(tree)

    assert summary.directories_reused == 0
    assert len(listings) == 4


def test_full_scan_ignores_the_cache(
    index: Index, tree: Path, listings: list[str]
) -> None:
    age_directories(tree)
    index.record_scan(tree)
    listings.clear()

    index.record_scan(tree, full=True)

    assert len(listings) == 4


def test_incremental_misses_in_place_edits_until_a_full_scan(
    index: Index, tree: Path
) -> None:
    age_directories(tree)
    index.record_scan(tree)
    (tree / "b" / "three.txt").write_text("now much longer than before")
    age_directories(tree)

    def size() -> int:
        rows = index.by_extension(".txt", 10)
        return next(r["size"] for r in rows if r["name"] == "three.txt")

    index.record_scan(tree)
    assert size() == len("x")

    index.record_scan(tree, full=True)
    assert size() == len("now much longer than before")


def test_index_does_not_record_its_own_database(tmp_path: Path) -> None:
    (tmp_path / "real.txt").touch()
    with Index(tmp_path / "index.db") as opened:
        opened.record_scan(tmp_path)

        assert paths(opened.largest(10)) == ["real.txt"]


def test_directory_that_failed_listing_is_relisted_next_time(
    index: Index, tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    age_directories(tree)
    blocked = str((tree / "b").resolve())
    real_scandir = os.scandir

    def failing_scandir(path: str) -> Any:
        if path == blocked:
            raise PermissionError(13, "denied", path)
        return real_scandir(path)

    monkeypatch.setattr(walker.os, "scandir", failing_scandir)
    first = index.record_scan(tree)
    assert first.skipped["permission_denied"] == 1
    assert index.errors(first.scan_id)[0]["path"] == blocked

    monkeypatch.setattr(walker.os, "scandir", real_scandir)
    index.record_scan(tree)

    assert "three.txt" in paths(index.largest(10))


def test_failed_scan_rolls_back_to_the_previous_index(
    index: Index, tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index.record_scan(tree)
    real_scan = walker.scan

    def exploding_scan(*args: Any) -> Iterator[ScanItem]:
        yield next(real_scan(*args))
        raise RuntimeError("disk fell off")

    monkeypatch.setattr(walker, "scan", exploding_scan)
    with pytest.raises(RuntimeError):
        index.record_scan(tree)

    assert len(index.largest(10)) == 4
    assert index.scans()[0]["status"] == "failed"


def test_killed_process_leaves_index_consistent(tmp_path: Path, tree: Path) -> None:
    database = tmp_path / "index.db"
    with Index(database) as first:
        first.record_scan(tree)

    script = textwrap.dedent(
        """
        import os, sys
        from orphic.index.store import Index
        from orphic.scanner import walker

        real_scan = walker.scan

        def dying_scan(*args):
            yield next(real_scan(*args))
            os._exit(9)

        walker.scan = dying_scan
        Index(sys.argv[1]).record_scan(sys.argv[2], full=True)
        """
    )
    result = subprocess.run([sys.executable, "-c", script, str(database), str(tree)])
    assert result.returncode == 9

    with Index(database) as reopened:
        assert len(reopened.largest(10)) == 4
        assert reopened.scans()[0]["status"] == "interrupted"


def test_interrupted_scan_does_not_count_as_a_trusted_baseline(
    tmp_path: Path, tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Index(tmp_path / "index.db") as opened:
        real_scan = walker.scan

        def exploding_scan(*args: Any) -> Iterator[ScanItem]:
            yield next(real_scan(*args))
            raise RuntimeError

        monkeypatch.setattr(walker, "scan", exploding_scan)
        with pytest.raises(RuntimeError):
            opened.record_scan(tree)
        monkeypatch.setattr(walker, "scan", real_scan)

        summary = opened.record_scan(tree)

    assert summary.directories_reused == 0
    assert summary.files == 4


def test_undecodable_name_is_recorded_as_an_error_not_a_crash(
    index: Index, tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def scan_with_bad_name(
        root: str, scan_id: str, cache: Any, workers: int
    ) -> Iterator[ScanItem]:
        yield FileRecord(
            f"{root}/bad\udc80.txt", "bad\udc80.txt", ".txt", 1, 1.0, 1.0, False, False, scan_id
        )

    monkeypatch.setattr(walker, "scan", scan_with_bad_name)

    summary = index.record_scan(tree)

    assert summary.skipped["unstorable_name"] == 1
    assert "\\udc80" in index.errors(summary.scan_id)[0]["path"]


def test_queries(index: Index, tree: Path) -> None:
    os.utime(tree / "top.txt", (100, 100))
    os.utime(tree / "b" / "three.txt", (200_000, 200_000))
    index.record_scan(tree)

    assert paths(index.largest(2)) == ["two.log", "one.txt"]
    assert paths(index.oldest(1)) == ["top.txt"]
    assert "three.txt" not in paths(index.newest(1))
    assert paths(index.by_extension(".LOG")) == ["two.log"]


def test_incremental_scan_works_with_worker_threads(index: Index, tree: Path) -> None:
    age_directories(tree)
    index.record_scan(tree, workers=4)

    summary = index.record_scan(tree, workers=4)

    assert summary.directories_reused == 3
    assert summary.files == 4
