import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol
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


class DirectoryCache(Protocol):
    def subdirectories_if_unchanged(self, directory: FileRecord) -> list[str] | None:
        """Return known subdirectories if directory needs no listing, else None."""


def scan(
    root: str | Path,
    scan_id: str | None = None,
    cache: DirectoryCache | None = None,
) -> Iterator[ScanItem]:
    """Yield every entry under root, and a ScanSkip for anything unreadable."""
    current_scan = scan_id or uuid4().hex
    # Explicit stacks, because recursion would be capped by tree depth.
    to_list = [str(root)]
    to_check: list[str] = []

    def route(record: FileRecord) -> None:
        if not record.is_directory or record.is_symlink:
            return
        known = cache.subdirectories_if_unchanged(record) if cache else None
        if known is None:
            to_list.append(record.path)
        else:
            to_check.extend(known)

    while to_list or to_check:
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

        directory = to_list.pop()
        entries: list[os.DirEntry[str]] = []
        try:
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    entries.append(entry)
        except OSError as error:
            yield _skip(directory, error, current_scan)

        for entry in entries:
            try:
                record = _record(entry, current_scan)
            except OSError as error:
                yield _skip(entry.path, error, current_scan)
                continue
            route(record)
            yield record


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
