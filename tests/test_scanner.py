import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from orphic.scanner import walker
from orphic.scanner.walker import FileRecord, ScanItem, ScanSkip, SkipReason, scan


def records(items: list[ScanItem]) -> dict[str, FileRecord]:
    return {item.name: item for item in items if isinstance(item, FileRecord)}


def skips(items: list[ScanItem]) -> list[ScanSkip]:
    return [item for item in items if isinstance(item, ScanSkip)]


def symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks needs extra privileges here")


class FakeEntry:
    def __init__(self, entry: os.DirEntry[str], error: OSError) -> None:
        self._entry = entry
        self._error = error

    def __getattr__(self, name: str) -> Any:
        return getattr(self._entry, name)

    def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
        raise self._error


class FakeScandir:
    def __init__(self, entries: list[Any]) -> None:
        self._entries = entries

    def __enter__(self) -> Iterator[Any]:
        return iter(self._entries)

    def __exit__(self, *exc_info: object) -> None:
        return None


def test_finds_every_entry_with_metadata(sample_tree: Path) -> None:
    found = records(list(scan(sample_tree)))

    assert set(found) == {"docs", "notes.txt", "empty.txt"}
    assert found["docs"].is_directory
    assert found["notes.txt"].size == len("notes")
    assert found["notes.txt"].extension == ".txt"
    assert found["empty.txt"].size == 0


def test_all_items_share_one_scan_id(sample_tree: Path) -> None:
    ids = {item.scan_id for item in scan(sample_tree)}

    assert len(ids) == 1


def test_extension_is_lowercased_and_dotfiles_have_none(tmp_path: Path) -> None:
    (tmp_path / "PHOTO.JPG").touch()
    (tmp_path / ".bashrc").touch()

    found = records(list(scan(tmp_path)))

    assert found["PHOTO.JPG"].extension == ".jpg"
    assert found[".bashrc"].extension == ""


def test_empty_directory_yields_nothing(tmp_path: Path) -> None:
    assert list(scan(tmp_path)) == []


def test_deep_nesting(tmp_path: Path) -> None:
    current = tmp_path
    for _ in range(40):
        current = current / "d"
        current.mkdir()

    directories = [item for item in scan(tmp_path) if isinstance(item, FileRecord)]

    assert len(directories) == 40


def test_ten_thousand_files(tmp_path: Path) -> None:
    for index in range(10_000):
        (tmp_path / f"{index}.bin").touch()

    assert len(list(scan(tmp_path))) == 10_000


@pytest.mark.parametrize("name", ["résumé.txt", "日本語.txt", "emoji-😀.txt", "with space.txt"])
def test_unicode_names(tmp_path: Path, name: str) -> None:
    (tmp_path / name).touch()

    assert name in records(list(scan(tmp_path)))


@pytest.mark.skipif(sys.platform == "win32", reason="exceeds the Windows path limit")
def test_longest_allowed_name(tmp_path: Path) -> None:
    name = "a" * 255
    (tmp_path / name).touch()

    assert name in records(list(scan(tmp_path)))


def test_symlink_is_recorded_but_not_followed(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "inside.txt").touch()
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink_or_skip(outside / "link", target)

    items = list(scan(outside))

    assert [item.name for item in records(items).values()] == ["link"]
    assert records(items)["link"].is_symlink


def test_symlink_cycle_terminates(tmp_path: Path) -> None:
    child = tmp_path / "child"
    child.mkdir()
    symlink_or_skip(child / "back", tmp_path)

    assert set(records(list(scan(tmp_path)))) == {"child", "back"}


def test_broken_symlink_is_recorded(tmp_path: Path) -> None:
    symlink_or_skip(tmp_path / "broken", tmp_path / "missing")

    items = list(scan(tmp_path))

    assert records(items)["broken"].is_symlink
    assert skips(items) == []


@pytest.mark.skipif(sys.platform != "win32", reason="junctions only exist on Windows")
def test_junction_is_recorded_but_not_followed(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "inside.txt").touch()
    outside = tmp_path / "outside"
    outside.mkdir()
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(outside / "junction"), str(target)],
        check=True,
        capture_output=True,
    )

    found = records(list(scan(outside)))

    assert set(found) == {"junction"}
    assert found["junction"].is_symlink


def test_missing_root_is_a_skip_not_a_crash(tmp_path: Path) -> None:
    items = list(scan(tmp_path / "missing"))

    assert [item.reason for item in skips(items)] == [SkipReason.VANISHED]


def test_file_as_root_is_a_skip_not_a_crash(sample_tree: Path) -> None:
    items = list(scan(sample_tree / "empty.txt"))

    assert len(skips(items)) == 1


def test_permission_denied_directory_is_skipped_and_scan_continues(
    sample_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_scandir = os.scandir
    blocked = str(sample_tree / "docs")

    def fake_scandir(path: str) -> Any:
        if path == blocked:
            raise PermissionError(13, "denied", path)
        return real_scandir(path)

    monkeypatch.setattr(walker.os, "scandir", fake_scandir)

    items = list(scan(sample_tree))

    assert [(s.path, s.reason) for s in skips(items)] == [
        (blocked, SkipReason.PERMISSION_DENIED)
    ]
    assert {"docs", "empty.txt"} == set(records(items))


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (FileNotFoundError(2, "gone"), SkipReason.VANISHED),
        (PermissionError(13, "denied"), SkipReason.PERMISSION_DENIED),
        (OSError(5, "io error"), SkipReason.OS_ERROR),
    ],
)
def test_stat_failure_skips_only_that_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError, reason: SkipReason
) -> None:
    (tmp_path / "bad.txt").touch()
    (tmp_path / "good.txt").touch()
    real_scandir = os.scandir

    def fake_scandir(path: str) -> Any:
        with real_scandir(path) as iterator:
            entries = [FakeEntry(e, error) if e.name == "bad.txt" else e for e in iterator]
        return FakeScandir(entries)

    monkeypatch.setattr(walker.os, "scandir", fake_scandir)

    items = list(scan(tmp_path))

    assert [s.reason for s in skips(items)] == [reason]
    assert set(records(items)) == {"good.txt"}


def test_files_deleted_during_scan_never_crash_it(tmp_path: Path) -> None:
    files = [tmp_path / f"{index}.bin" for index in range(2_000)]
    for path in files:
        path.touch()

    deleter = threading.Thread(target=lambda: [p.unlink() for p in files])
    deleter.start()
    items = list(scan(tmp_path))
    deleter.join()

    assert len(items) <= len(files)


@pytest.mark.parametrize("workers", [1, 2, 4, 16])
def test_every_worker_count_finds_the_same_entries(tmp_path: Path, workers: int) -> None:
    for branch in range(6):
        directory = tmp_path / f"branch{branch}" / "leaf"
        directory.mkdir(parents=True)
        (directory / f"{branch}.txt").touch()

    found = sorted(item.path for item in scan(tmp_path, workers=workers))

    assert found == sorted(str(path) for path in tmp_path.rglob("*"))


@pytest.mark.parametrize("workers", [1, 4])
def test_failures_are_skips_at_any_worker_count(
    sample_tree: Path, monkeypatch: pytest.MonkeyPatch, workers: int
) -> None:
    real_scandir = os.scandir
    blocked = str(sample_tree / "docs")

    def fake_scandir(path: str) -> Any:
        if path == blocked:
            raise PermissionError(13, "denied", path)
        return real_scandir(path)

    monkeypatch.setattr(walker.os, "scandir", fake_scandir)

    items = list(scan(sample_tree, workers=workers))

    assert [s.reason for s in skips(items)] == [SkipReason.PERMISSION_DENIED]


def test_directories_really_are_read_in_parallel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    both_running = threading.Barrier(2, timeout=10)
    real_scandir = os.scandir

    def waiting_scandir(path: str) -> Any:
        if path != str(tmp_path):
            both_running.wait()
        return real_scandir(path)

    monkeypatch.setattr(walker.os, "scandir", waiting_scandir)

    assert len(list(scan(tmp_path, workers=2))) == 2


def test_abandoning_a_concurrent_scan_shuts_it_down(sample_tree: Path) -> None:
    iterator = scan(sample_tree, workers=4)
    next(iterator)

    iterator.close()

    assert list(iterator) == []


def test_zero_workers_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        list(scan(tmp_path, workers=0))


@pytest.mark.parametrize("workers", [2, 4])
def test_processes_find_the_same_entries_as_a_serial_scan(
    tmp_path: Path, workers: int
) -> None:
    for branch in range(40):
        directory = tmp_path / f"branch{branch}" / "leaf"
        directory.mkdir(parents=True)
        (directory / f"{branch}.txt").write_text("x" * branch)
    # Windows can report a just-changed directory's mtime late, so pin them.
    for path in tmp_path.rglob("*"):
        os.utime(path, (1000, 1000))

    serial = {item.path: item for item in scan(tmp_path, scan_id="s")}
    parallel = {item.path: item for item in scan(tmp_path, "s", None, workers, True)}

    assert parallel == serial


def test_processes_report_a_missing_root_as_a_skip(tmp_path: Path) -> None:
    items = list(scan(tmp_path / "missing", workers=2, processes=True))

    assert [item.reason for item in skips(items)] == [SkipReason.VANISHED]


def test_compact_round_trip_keeps_records_and_skips() -> None:
    items: list[ScanItem] = [
        FileRecord("/a/b.txt", "b.txt", ".txt", 7, 1.5, 2.5, False, True, "id"),
        ScanSkip("/a/c", SkipReason.PERMISSION_DENIED, "denied", "id"),
    ]

    assert walker._expand([walker._compact(item) for item in items], "id") == items
