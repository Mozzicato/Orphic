import os
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
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


def scan(root: str | Path, scan_id: str | None = None) -> Iterator[ScanItem]:
    """Yield every entry under root, and a ScanSkip for anything unreadable."""
    current_scan = scan_id or uuid4().hex
    pending = [str(root)]

    # An explicit stack, because recursion would be capped by tree depth.
    while pending:
        directory = pending.pop()
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
            yield record
            if record.is_directory and not record.is_symlink:
                pending.append(record.path)


def _record(entry: os.DirEntry[str], scan_id: str) -> FileRecord:
    info = entry.stat(follow_symlinks=False)
    return FileRecord(
        path=entry.path,
        name=entry.name,
        extension=os.path.splitext(entry.name)[1].lower(),
        size=info.st_size,
        mtime=info.st_mtime,
        ctime=info.st_ctime,
        is_directory=entry.is_dir(follow_symlinks=False),
        # Junctions are Windows directory links that is_symlink() reports as False.
        is_symlink=entry.is_symlink() or entry.is_junction(),
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
