import os
import sqlite3
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self
from uuid import uuid4

from orphic.dedup.engine import HASH_ALGORITHM, Candidate, DuplicateGroup
from orphic.scanner import walker
from orphic.scanner.walker import FileRecord, ScanSkip

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id TEXT PRIMARY KEY,
    root_path TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    scan_id TEXT NOT NULL REFERENCES scans(id),
    path TEXT NOT NULL UNIQUE,
    parent TEXT NOT NULL,
    name TEXT NOT NULL,
    extension TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    ctime REAL NOT NULL,
    is_directory INTEGER NOT NULL,
    is_symlink INTEGER NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY,
    scan_id TEXT NOT NULL REFERENCES scans(id),
    path TEXT NOT NULL,
    error_type TEXT NOT NULL,
    message TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS duplicates (
    group_id INTEGER NOT NULL,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    hash TEXT NOT NULL,
    algorithm TEXT NOT NULL,
    position INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_duplicates_file ON duplicates(file_id);
CREATE INDEX IF NOT EXISTS idx_files_parent ON files(parent);
CREATE INDEX IF NOT EXISTS idx_files_scan ON files(scan_id);
CREATE INDEX IF NOT EXISTS idx_files_size ON files(size);
CREATE INDEX IF NOT EXISTS idx_files_extension ON files(extension);
"""

UPSERT_FILE = """
INSERT INTO files (scan_id, path, parent, name, extension, size, mtime, ctime,
                   is_directory, is_symlink, first_seen, last_seen)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(path) DO UPDATE SET
    scan_id = excluded.scan_id,
    parent = excluded.parent,
    name = excluded.name,
    extension = excluded.extension,
    size = excluded.size,
    mtime = excluded.mtime,
    ctime = excluded.ctime,
    is_directory = excluded.is_directory,
    is_symlink = excluded.is_symlink,
    last_seen = excluded.last_seen
"""

IS_REAL_DIRECTORY = "is_directory = 1 AND is_symlink = 0"

# A directory changed within this window of the last scan may have changed
# again without its mtime moving. FAT timestamps have 2 second resolution.
MTIME_RESOLUTION_SECONDS = 2.0

# Stored instead of a directory's mtime when listing it failed, so it never matches.
UNTRUSTED_MTIME = -1.0


@dataclass(frozen=True, slots=True)
class ScanSummary:
    scan_id: str
    files: int
    directories: int
    links: int
    total_size: int
    directories_reused: int
    skipped: Counter[str]


class _UnchangedDirectories:
    def __init__(
        self,
        connection: sqlite3.Connection,
        scan_id: str,
        now: float,
        trusted_before: float,
    ) -> None:
        self._connection = connection
        self._scan_id = scan_id
        self._now = now
        self._trusted_before = trusted_before
        self.reused = 0

    def subdirectories_if_unchanged(self, directory: FileRecord) -> list[str] | None:
        row = self._connection.execute(
            f"SELECT mtime FROM files WHERE path = ? AND {IS_REAL_DIRECTORY}",
            (directory.path,),
        ).fetchone()
        if (
            row is None
            or row["mtime"] != directory.mtime
            or directory.mtime >= self._trusted_before
        ):
            return None

        self.reused += 1
        self._connection.execute(
            f"UPDATE files SET scan_id = ?, last_seen = ? "
            f"WHERE parent = ? AND NOT ({IS_REAL_DIRECTORY})",
            (self._scan_id, self._now, directory.path),
        )
        rows = self._connection.execute(
            f"SELECT path FROM files WHERE parent = ? AND {IS_REAL_DIRECTORY}",
            (directory.path,),
        )
        return [subdirectory["path"] for subdirectory in rows]


class Index:
    def __init__(self, path: str | Path) -> None:
        database = str(Path(path).resolve())
        self._own_files = {database, f"{database}-wal", f"{database}-shm"}
        self._connection = sqlite3.connect(database, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.executescript(SCHEMA)
        # Only a crashed process can leave a scan running at open time.
        self._connection.execute(
            "UPDATE scans SET status = 'interrupted' WHERE status = 'running'"
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def record_scan(
        self,
        root: str | Path,
        *,
        full: bool = False,
        workers: int = 1,
        on_progress: Callable[[int], None] | None = None,
    ) -> ScanSummary:
        """Scan root and replace its indexed state atomically."""
        root_path = str(Path(root).resolve())
        scan_id = uuid4().hex
        now = time.time()
        trusted_before = self._trusted_before(root_path)
        self._connection.execute(
            "INSERT INTO scans (id, root_path, started_at, status) "
            "VALUES (?, ?, ?, 'running')",
            (scan_id, root_path, now),
        )

        cache = None
        if not full and trusted_before is not None:
            cache = _UnchangedDirectories(
                self._connection, scan_id, now, trusted_before
            )
        skipped: Counter[str] = Counter()

        # One transaction per scan: a crash leaves the previous index untouched.
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            items = walker.scan(root_path, scan_id, cache, workers)
            for count, item in enumerate(items, start=1):
                if isinstance(item, ScanSkip):
                    skipped[item.reason] += 1
                    self._store_skip(item, now)
                elif item.path in self._own_files:
                    continue
                elif _is_storable(item.path):
                    self._store_record(item, now)
                else:
                    skipped["unstorable_name"] += 1
                    self._store_unstorable(item, now)
                if on_progress and count % 1000 == 0:
                    on_progress(count)

            self._connection.execute(
                "DELETE FROM files WHERE scan_id != ? AND scan_id IN "
                "(SELECT id FROM scans WHERE root_path = ?)",
                (scan_id, root_path),
            )
            self._connection.execute(
                "UPDATE scans SET status = 'completed', finished_at = ? WHERE id = ?",
                (time.time(), scan_id),
            )
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            self._connection.execute(
                "UPDATE scans SET status = 'failed', finished_at = ? WHERE id = ?",
                (time.time(), scan_id),
            )
            raise

        return self._summary(scan_id, cache.reused if cache else 0, skipped)

    def largest(self, limit: int = 10) -> list[sqlite3.Row]:
        return self._files("ORDER BY size DESC, path", (limit,))

    def oldest(self, limit: int = 10) -> list[sqlite3.Row]:
        return self._files("ORDER BY mtime, path", (limit,))

    def newest(self, limit: int = 10) -> list[sqlite3.Row]:
        return self._files("ORDER BY mtime DESC, path", (limit,))

    def by_extension(self, extension: str, limit: int = 10) -> list[sqlite3.Row]:
        return self._files(
            "AND extension = ? ORDER BY size DESC, path",
            (extension.lower(), limit),
        )

    def candidates(self) -> list[Candidate]:
        rows = self._connection.execute(
            "SELECT id, path, size, mtime FROM files "
            "WHERE is_directory = 0 AND is_symlink = 0 AND size > 0"
        )
        return [Candidate(r["id"], r["path"], r["size"], r["mtime"]) for r in rows]

    def save_duplicates(self, groups: list[DuplicateGroup]) -> None:
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute("DELETE FROM duplicates")
            self._connection.executemany(
                "INSERT INTO duplicates (group_id, file_id, hash, algorithm, position) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    (group_id, file.file_id, group.hash, HASH_ALGORITHM, position)
                    for group_id, group in enumerate(groups, start=1)
                    for position, file in enumerate(group.files)
                ),
            )

    def saved_duplicates(self) -> list[DuplicateGroup]:
        rows = self._connection.execute(
            "SELECT d.group_id, d.hash, f.id, f.path, f.size, f.mtime "
            "FROM duplicates d JOIN files f ON f.id = d.file_id "
            "ORDER BY d.group_id, d.position"
        ).fetchall()
        members: dict[int, list[sqlite3.Row]] = {}
        for row in rows:
            members.setdefault(row["group_id"], []).append(row)
        return [
            DuplicateGroup(
                hash=group[0]["hash"],
                files=tuple(
                    Candidate(r["id"], r["path"], r["size"], r["mtime"]) for r in group
                ),
            )
            for group in members.values()
            if len(group) > 1
        ]

    def scans(self) -> list[sqlite3.Row]:
        return self._connection.execute(
            "SELECT * FROM scans ORDER BY started_at DESC"
        ).fetchall()

    def errors(self, scan_id: str) -> list[sqlite3.Row]:
        return self._connection.execute(
            "SELECT * FROM errors WHERE scan_id = ? ORDER BY path", (scan_id,)
        ).fetchall()

    def _files(self, clause: str, parameters: tuple[object, ...]) -> list[sqlite3.Row]:
        return self._connection.execute(
            f"SELECT * FROM files WHERE is_directory = 0 AND is_symlink = 0 "
            f"{clause} LIMIT ?",
            parameters,
        ).fetchall()

    def _trusted_before(self, root_path: str) -> float | None:
        row = self._connection.execute(
            "SELECT MAX(started_at) AS started FROM scans "
            "WHERE root_path = ? AND status = 'completed'",
            (root_path,),
        ).fetchone()
        if row["started"] is None:
            return None
        return float(row["started"]) - MTIME_RESOLUTION_SECONDS

    def _store_record(self, record: FileRecord, now: float) -> None:
        self._connection.execute(
            UPSERT_FILE,
            (
                record.scan_id,
                record.path,
                os.path.dirname(record.path),
                record.name,
                record.extension,
                record.size,
                record.mtime,
                record.ctime,
                record.is_directory,
                record.is_symlink,
                now,
                now,
            ),
        )

    def _store_skip(self, skip: ScanSkip, now: float) -> None:
        self._connection.execute(
            "UPDATE files SET mtime = ? WHERE path = ?",
            (UNTRUSTED_MTIME, skip.path),
        )
        self._insert_error(skip.scan_id, skip.path, skip.reason, skip.message, now)

    def _store_unstorable(self, record: FileRecord, now: float) -> None:
        self._insert_error(
            record.scan_id,
            record.path,
            "unstorable_name",
            "name is not valid Unicode",
            now,
        )

    def _insert_error(
        self, scan_id: str, path: str, error_type: str, message: str, now: float
    ) -> None:
        self._connection.execute(
            "INSERT INTO errors (scan_id, path, error_type, message, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (scan_id, _printable(path), error_type, _printable(message), now),
        )

    def _summary(
        self, scan_id: str, reused: int, skipped: Counter[str]
    ) -> ScanSummary:
        row = self._connection.execute(
            f"""
            SELECT
                COALESCE(SUM(is_symlink = 1), 0) AS links,
                COALESCE(SUM({IS_REAL_DIRECTORY}), 0) AS directories,
                COALESCE(SUM(is_directory = 0 AND is_symlink = 0), 0) AS files,
                COALESCE(SUM(CASE WHEN is_directory = 0 AND is_symlink = 0
                                  THEN size ELSE 0 END), 0) AS total_size
            FROM files WHERE scan_id = ?
            """,
            (scan_id,),
        ).fetchone()
        return ScanSummary(
            scan_id=scan_id,
            files=row["files"],
            directories=row["directories"],
            links=row["links"],
            total_size=row["total_size"],
            directories_reused=reused,
            skipped=skipped,
        )


def _is_storable(path: str) -> bool:
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _printable(text: str) -> str:
    return text.encode("utf-8", "backslashreplace").decode("utf-8")
