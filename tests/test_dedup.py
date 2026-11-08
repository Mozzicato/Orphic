import builtins
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orphic.dedup import engine
from orphic.dedup.engine import PARTIAL_BYTES, Candidate, Strategy, find_duplicates
from orphic.index.store import Index

ALL_STRATEGIES = list(Strategy)


def candidate(path: Path, file_id: int = 0) -> Candidate:
    info = path.stat()
    return Candidate(file_id, str(path), info.st_size, info.st_mtime)


def write(path: Path, data: bytes) -> Candidate:
    path.write_bytes(data)
    return candidate(path)


def names(groups: list[engine.DuplicateGroup]) -> list[set[str]]:
    return [{Path(file.path).name for file in group.files} for group in groups]


@pytest.mark.parametrize("strategy", ALL_STRATEGIES)
def test_identical_files_are_grouped(tmp_path: Path, strategy: Strategy) -> None:
    files = [
        write(tmp_path / "a", b"same"),
        write(tmp_path / "b", b"same"),
        write(tmp_path / "c", b"diff"),
    ]

    groups, _ = find_duplicates(files, strategy)

    assert names(groups) == [{"a", "b"}]


@pytest.mark.parametrize("strategy", ALL_STRATEGIES)
def test_same_first_and_last_blocks_but_different_middle(
    tmp_path: Path, strategy: Strategy
) -> None:
    edge = b"e" * PARTIAL_BYTES
    files = [
        write(tmp_path / "a", edge + b"AAAA" + edge),
        write(tmp_path / "b", edge + b"BBBB" + edge),
    ]

    groups, _ = find_duplicates(files, strategy)

    assert groups == []


@pytest.mark.parametrize("strategy", ALL_STRATEGIES)
def test_strategies_agree_around_the_partial_boundary(
    tmp_path: Path, strategy: Strategy
) -> None:
    files = []
    for size in (PARTIAL_BYTES - 1, PARTIAL_BYTES, PARTIAL_BYTES + 1, 2 * PARTIAL_BYTES + 1):
        files.append(write(tmp_path / f"{size}-a", b"x" * size))
        files.append(write(tmp_path / f"{size}-b", b"x" * size))

    groups, _ = find_duplicates(files, strategy)

    assert len(groups) == 4


def test_empty_files_are_never_duplicates(tmp_path: Path) -> None:
    files = [write(tmp_path / "a", b""), write(tmp_path / "b", b"")]

    groups, stats = find_duplicates(files)

    assert groups == []
    assert stats.files_considered == 0


def test_unique_sizes_cost_no_reads(tmp_path: Path) -> None:
    files = [write(tmp_path / str(n), b"x" * n) for n in range(1, 6)]

    _, stats = find_duplicates(files)

    assert stats.size_candidates == 0
    assert stats.files_opened == 0
    assert stats.full_hash_skip_ratio == 1.0


def test_hash_everything_reads_every_file(tmp_path: Path) -> None:
    files = [write(tmp_path / str(n), b"x" * n) for n in range(1, 6)]

    _, stats = find_duplicates(files, Strategy.HASH_EVERYTHING)

    assert stats.full_hashed == 5
    assert stats.bytes_read == 1 + 2 + 3 + 4 + 5


def test_partial_hash_rejects_different_start_without_full_read(tmp_path: Path) -> None:
    size = PARTIAL_BYTES * 3
    files = [
        write(tmp_path / "a", b"a" * size),
        write(tmp_path / "b", b"b" * size),
    ]

    _, stats = find_duplicates(files, Strategy.SIZE_FIRST)

    assert stats.partial_hashed == 2
    assert stats.full_hashed == 0
    assert stats.bytes_read == PARTIAL_BYTES * 2


def test_reading_the_tail_rejects_files_with_a_shared_header(tmp_path: Path) -> None:
    header = b"h" * PARTIAL_BYTES
    tail_a = b"a" * PARTIAL_BYTES * 2
    tail_b = b"b" * PARTIAL_BYTES * 2
    files = [write(tmp_path / "a", header + tail_a), write(tmp_path / "b", header + tail_b)]

    _, first_only = find_duplicates(files, Strategy.SIZE_FIRST)
    _, first_last = find_duplicates(files, Strategy.SIZE_FIRST_LAST)

    assert first_only.full_hashed == 2
    assert first_last.full_hashed == 0


def test_small_files_are_read_once_not_twice(tmp_path: Path) -> None:
    files = [write(tmp_path / "a", b"small"), write(tmp_path / "b", b"small")]

    _, stats = find_duplicates(files, Strategy.SIZE_FIRST)

    assert stats.partial_hashed == 0
    assert stats.files_opened == 2


def test_hard_links_are_not_duplicates(tmp_path: Path) -> None:
    first = write(tmp_path / "a", b"content")
    os.link(tmp_path / "a", tmp_path / "linked")

    groups, stats = find_duplicates([first, candidate(tmp_path / "linked")])

    assert groups == []
    assert stats.hard_links == 1


def test_hard_link_does_not_hide_a_real_copy(tmp_path: Path) -> None:
    first = write(tmp_path / "a", b"content")
    os.link(tmp_path / "a", tmp_path / "linked")
    copy = write(tmp_path / "copy", b"content")

    groups, _ = find_duplicates([first, candidate(tmp_path / "linked"), copy])

    assert len(groups) == 1
    assert len(groups[0].files) == 2


def test_vanished_file_is_skipped(tmp_path: Path) -> None:
    files = [
        write(tmp_path / "a", b"same"),
        write(tmp_path / "b", b"same"),
        write(tmp_path / "c", b"same"),
    ]
    (tmp_path / "c").unlink()

    groups, stats = find_duplicates(files)

    assert names(groups) == [{"a", "b"}]
    assert stats.skipped["FileNotFoundError"] == 1


def test_file_resized_since_indexing_is_skipped(tmp_path: Path) -> None:
    files = [write(tmp_path / "a", b"same"), write(tmp_path / "b", b"same")]
    (tmp_path / "b").write_bytes(b"much longer now")

    groups, stats = find_duplicates(files)

    assert groups == []
    assert stats.skipped["changed"] == 1


def test_unreadable_file_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = [
        write(tmp_path / "a", b"same"),
        write(tmp_path / "b", b"same"),
        write(tmp_path / "locked", b"same"),
    ]
    real_open = builtins.open

    def guarded_open(path: str, *args: Any, **kwargs: Any) -> Any:
        if Path(path).name == "locked":
            raise PermissionError(13, "denied", path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(engine, "open", guarded_open, raising=False)

    groups, stats = find_duplicates(files)

    assert names(groups) == [{"a", "b"}]
    assert stats.skipped["PermissionError"] == 1


def test_file_that_changes_length_while_hashing_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path / "a", b"same")
    write(tmp_path / "b", b"different length")
    files = [
        Candidate(0, str(tmp_path / "a"), 4, 0.0),
        Candidate(0, str(tmp_path / "b"), 4, 0.0),
    ]
    identity = iter(range(1, 100))

    def lying_stat(path: str, **kwargs: Any) -> Any:
        return SimpleNamespace(st_size=4, st_dev=1, st_ino=next(identity))

    monkeypatch.setattr(engine.os, "stat", lying_stat)

    groups, stats = find_duplicates(files)

    assert groups == []
    assert stats.skipped["changed"] == 1


def test_oldest_file_is_the_original_and_recoverable_counts_the_rest(tmp_path: Path) -> None:
    for name, when in (("new", 3000), ("old", 1000), ("mid", 2000)):
        write(tmp_path / name, b"12345")
        os.utime(tmp_path / name, (when, when))
    files = [candidate(tmp_path / name) for name in ("new", "old", "mid")]

    group = find_duplicates(files)[0][0]

    assert Path(group.original.path).name == "old"
    assert [Path(f.path).name for f in group.duplicates] == ["mid", "new"]
    assert group.recoverable_bytes == 10


def test_largest_saving_comes_first(tmp_path: Path) -> None:
    files = [
        write(tmp_path / "s1", b"a"),
        write(tmp_path / "s2", b"a"),
        write(tmp_path / "b1", b"b" * 100),
        write(tmp_path / "b2", b"b" * 100),
    ]

    groups, _ = find_duplicates(files)

    assert names(groups) == [{"b1", "b2"}, {"s1", "s2"}]


def scanned_copies(tmp_path: Path) -> tuple[Index, Path]:
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_bytes(b"same")
    (root / "b.txt").write_bytes(b"same")
    (root / "c.txt").write_bytes(b"same")
    index = Index(tmp_path / "index.db")
    index.record_scan(root)
    return index, root


def test_groups_round_trip_through_the_index(tmp_path: Path) -> None:
    index, _ = scanned_copies(tmp_path)
    groups, _ = find_duplicates(index.candidates())

    index.save_duplicates(groups)

    assert index.saved_duplicates() == groups
    index.close()


def test_saving_replaces_the_previous_result(tmp_path: Path) -> None:
    index, _ = scanned_copies(tmp_path)
    groups, _ = find_duplicates(index.candidates())
    index.save_duplicates(groups)

    index.save_duplicates([])

    assert index.saved_duplicates() == []
    index.close()


def test_rescan_removing_a_copy_updates_saved_groups(tmp_path: Path) -> None:
    index, root = scanned_copies(tmp_path)
    index.save_duplicates(find_duplicates(index.candidates())[0])
    (root / "c.txt").unlink()
    index.record_scan(root, full=True)
    (root / "b.txt").unlink()
    index.record_scan(root, full=True)

    assert index.saved_duplicates() == []
    index.close()
