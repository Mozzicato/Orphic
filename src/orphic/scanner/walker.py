import os
import stat
from collections.abc import Iterator
from concurrent.futures import (
    FIRST_COMPLETED,
    Executor,
    Future,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    wait,
)
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4


class SkipReason(StrEnum):
    PERMISSION_DENIED = "permission_denied"
    VANISHED = "vanished"
    OS_ERROR = "os_error"


@dataclass(frozen=True, slots=True)
class FileRecord:
    path: str
    name: str
    extension: str
    size: int
    mtime: float
    ctime: float
    is_directory: bool
    is_symlink: bool
    scan_id: str


@dataclass(frozen=True, slots=True)
class ScanSkip:
    path: str
    reason: SkipReason
    message: str
    scan_id: str


ScanItem = FileRecord | ScanSkip

# Directories sent to a process at once: moving data between processes is the
# expensive part, so fewer and larger transfers are cheaper.
PROCESS_BATCH_SIZE = 128


class DirectoryCache(Protocol):
    def subdirectories_if_unchanged(self, directory: FileRecord) -> list[str] | None:
        """Return known subdirectories if directory needs no listing, else None."""


def scan(
    root: str | Path,
    scan_id: str | None = None,
    cache: DirectoryCache | None = None,
    workers: int = 1,
    processes: bool = False,
) -> Iterator[ScanItem]:
    """Yield every entry under root, and a ScanSkip for anything unreadable."""
    if workers < 1:
        raise ValueError("workers must be at least 1")
    current_scan = scan_id or uuid4().hex
    # Explicit stacks, because recursion would be capped by tree depth.
    to_list = [str(root)]
    to_check: list[str] = []
    in_flight: set[Future[Any]] = set()
    pool: Executor | None = None
    if workers > 1:
        executor = ProcessPoolExecutor if processes else ThreadPoolExecutor
        pool = executor(max_workers=workers)
    batch_size = PROCESS_BATCH_SIZE if processes else 1
    read_batch = _read_batch_compact if processes else _read_batch

    def route(record: FileRecord) -> None:
        if not record.is_directory or record.is_symlink:
            return
        known = cache.subdirectories_if_unchanged(record) if cache else None
        if known is None:
            to_list.append(record.path)
        else:
            to_check.extend(known)

    try:
        while to_list or to_check or in_flight:
            if to_check:
                path = to_check.pop()
                try:
                    record = _record_from_path(path, current_scan)
                except OSError as error:
                    yield _skip(path, error, current_scan)
                    continue
                # Route before yielding: the consumer may overwrite what the cache reads.
                route(record)
                yield record
                continue

            batches: list[list[ScanItem]]
            if pool is None:
                batches = [_read_directory(to_list.pop(), current_scan)]
            else:
                # Bounded, so a huge tree cannot queue unlimited work.
                while to_list and len(in_flight) < workers * 2:
                    batch = to_list[-batch_size:]
                    del to_list[-batch_size:]
                    in_flight.add(pool.submit(read_batch, batch, current_scan))
                done, in_flight = wait(in_flight, return_when=FIRST_COMPLETED)
                results = [future.result() for future in done]
                batches = (
                    [_expand(rows, current_scan) for rows in results]
                    if processes
                    else results
                )

            # Only this thread touches the cache: its SQLite connection is not shared.
            for items in batches:
                for item in items:
                    if isinstance(item, FileRecord):
                        route(item)
                    yield item
    finally:
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)


def _read_batch(directories: list[str], scan_id: str) -> list[ScanItem]:
    return [item for directory in directories for item in _read_directory(directory, scan_id)]


def _read_batch_compact(directories: list[str], scan_id: str) -> list[tuple[Any, ...]]:
    # Plain tuples pickle far faster than dataclass instances.
    return [_compact(item) for item in _read_batch(directories, scan_id)]


def _compact(item: ScanItem) -> tuple[Any, ...]:
    if isinstance(item, ScanSkip):
        return (item.path, item.reason.value, item.message)
    return (
        item.path,
        item.name,
        item.extension,
        item.size,
        item.mtime,
        item.ctime,
        item.is_directory,
        item.is_symlink,
    )


def _expand(rows: list[tuple[Any, ...]], scan_id: str) -> list[ScanItem]:
    return [
        ScanSkip(row[0], SkipReason(row[1]), row[2], scan_id)
        if len(row) == 3
        else FileRecord(
            row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], scan_id
        )
        for row in rows
    ]


def _read_directory(directory: str, scan_id: str) -> list[ScanItem]:
    entries: list[os.DirEntry[str]] = []
    items: list[ScanItem] = []
    try:
        with os.scandir(directory) as iterator:
            for entry in iterator:
                entries.append(entry)
    except OSError as error:
        items.append(_skip(directory, error, scan_id))

    for entry in entries:
        try:
            items.append(_record(entry, scan_id))
        except OSError as error:
            items.append(_skip(entry.path, error, scan_id))
    return items


def _record(entry: os.DirEntry[str], scan_id: str) -> FileRecord:
    info = entry.stat(follow_symlinks=False)
    return _build(
        entry.path,
        entry.name,
        info,
        is_directory=entry.is_dir(follow_symlinks=False),
        # Junctions are Windows directory links that is_symlink() reports as False.
        is_symlink=entry.is_symlink() or entry.is_junction(),
        scan_id=scan_id,
    )


def _record_from_path(path: str, scan_id: str) -> FileRecord:
    info = os.lstat(path)
    return _build(
        path,
        os.path.basename(path),
        info,
        is_directory=stat.S_ISDIR(info.st_mode),
        is_symlink=stat.S_ISLNK(info.st_mode) or os.path.isjunction(path),
        scan_id=scan_id,
    )


def _build(
    path: str,
    name: str,
    info: os.stat_result,
    *,
    is_directory: bool,
    is_symlink: bool,
    scan_id: str,
) -> FileRecord:
    return FileRecord(
        path=path,
        name=name,
        extension=os.path.splitext(name)[1].lower(),
        size=info.st_size,
        mtime=info.st_mtime,
        ctime=info.st_ctime,
        is_directory=is_directory,
        is_symlink=is_symlink,
        scan_id=scan_id,
    )


def _skip(path: str, error: OSError, scan_id: str) -> ScanSkip:
    if isinstance(error, PermissionError):
        reason = SkipReason.PERMISSION_DENIED
    elif isinstance(error, FileNotFoundError):
        reason = SkipReason.VANISHED
    else:
        reason = SkipReason.OS_ERROR
    return ScanSkip(path=path, reason=reason, message=str(error), scan_id=scan_id)
