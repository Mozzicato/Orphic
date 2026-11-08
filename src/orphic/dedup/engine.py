import hashlib
import os
from collections import Counter, defaultdict
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum

PARTIAL_BYTES = 4096
HASH_ALGORITHM = "sha256"


class Strategy(StrEnum):
    HASH_EVERYTHING = "hash-everything"
    SIZE = "size"
    SIZE_FIRST = "size-first"
    SIZE_FIRST_LAST = "size-first-last"


@dataclass(frozen=True, slots=True)
class Candidate:
    file_id: int
    path: str
    size: int
    mtime: float


@dataclass(frozen=True, slots=True)
class DuplicateGroup:
    hash: str
    files: tuple[Candidate, ...]

    @property
    def original(self) -> Candidate:
        return self.files[0]

    @property
    def duplicates(self) -> tuple[Candidate, ...]:
        return self.files[1:]

    @property
    def recoverable_bytes(self) -> int:
        return self.original.size * len(self.duplicates)


@dataclass(slots=True)
class DedupStats:
    files_considered: int = 0
    size_candidates: int = 0
    partial_hashed: int = 0
    full_hashed: int = 0
    files_opened: int = 0
    bytes_read: int = 0
    hard_links: int = 0
    skipped: Counter[str] = field(default_factory=Counter)

    @property
    def full_hash_skip_ratio(self) -> float:
        if self.files_considered == 0:
            return 0.0
        return 1 - self.full_hashed / self.files_considered


def find_duplicates(
    files: Iterable[Candidate], strategy: Strategy = Strategy.SIZE_FIRST
) -> tuple[list[DuplicateGroup], DedupStats]:
    """Group files with identical content, reading as little as possible."""
    stats = DedupStats()
    nonempty = [file for file in files if file.size > 0]
    stats.files_considered = len(nonempty)

    if strategy is Strategy.HASH_EVERYTHING:
        groups = [nonempty] if nonempty else []
    else:
        groups = _split([nonempty], lambda file: file.size)
    stats.size_candidates = sum(len(group) for group in groups)

    groups = [_current_distinct_files(group, stats) for group in groups]
    if strategy in (Strategy.SIZE_FIRST, Strategy.SIZE_FIRST_LAST):
        groups = _partial_stage(groups, strategy, stats)

    results: list[DuplicateGroup] = []
    digests: dict[Hashable, list[Candidate]] = defaultdict(list)
    for group in groups:
        digests.clear()
        for file in group:
            digest = _full_hash(file, stats)
            if digest is not None:
                digests[digest].append(file)
        for digest, members in digests.items():
            if len(members) > 1:
                results.append(_duplicate_group(str(digest), members))

    results.sort(key=lambda group: (-group.recoverable_bytes, group.hash))
    return results, stats


def _split(
    groups: list[list[Candidate]],
    key: Callable[[Candidate], Hashable | None],
) -> list[list[Candidate]]:
    """Regroup by key, dropping unique files and files whose key is None."""
    result: list[list[Candidate]] = []
    for group in groups:
        buckets: dict[Hashable, list[Candidate]] = defaultdict(list)
        for file in group:
            value = key(file)
            if value is not None:
                buckets[value].append(file)
        result.extend(bucket for bucket in buckets.values() if len(bucket) > 1)
    return result


def _current_distinct_files(
    group: list[Candidate], stats: DedupStats
) -> list[Candidate]:
    """Drop files the index is stale about, and extra hard links to one file."""
    distinct: list[Candidate] = []
    identities: set[tuple[int, int]] = set()
    for file in group:
        try:
            info = os.stat(file.path, follow_symlinks=False)
        except OSError as error:
            stats.skipped[type(error).__name__] += 1
            continue
        if info.st_size != file.size:
            stats.skipped["changed"] += 1
            continue
        identity = (info.st_dev, info.st_ino)
        # Hard links share their bytes, so removing one frees nothing.
        if info.st_ino != 0 and identity in identities:
            stats.hard_links += 1
            continue
        identities.add(identity)
        distinct.append(file)
    return distinct


def _partial_stage(
    groups: list[list[Candidate]], strategy: Strategy, stats: DedupStats
) -> list[list[Candidate]]:
    read_tail = strategy is Strategy.SIZE_FIRST_LAST
    # A file this small is read in full by the partial hash, so skip straight on.
    threshold = PARTIAL_BYTES * 2 if read_tail else PARTIAL_BYTES

    def partial_key(file: Candidate) -> Hashable | None:
        if file.size <= threshold:
            return "whole-file"
        return _partial_hash(file, read_tail, stats)

    return _split(groups, partial_key)


def _partial_hash(
    file: Candidate, read_tail: bool, stats: DedupStats
) -> str | None:
    try:
        with open(file.path, "rb") as handle:
            stats.files_opened += 1
            digest = hashlib.sha256(_counted(handle.read(PARTIAL_BYTES), stats))
            if read_tail:
                handle.seek(-PARTIAL_BYTES, os.SEEK_END)
                digest.update(_counted(handle.read(PARTIAL_BYTES), stats))
    except OSError as error:
        stats.skipped[type(error).__name__] += 1
        return None
    stats.partial_hashed += 1
    return digest.hexdigest()


def _counted(data: bytes, stats: DedupStats) -> bytes:
    stats.bytes_read += len(data)
    return data


def _full_hash(file: Candidate, stats: DedupStats) -> str | None:
    try:
        with open(file.path, "rb") as handle:
            stats.files_opened += 1
            digest = hashlib.file_digest(handle, HASH_ALGORITHM)
            read = handle.tell()
    except OSError as error:
        stats.skipped[type(error).__name__] += 1
        return None
    stats.full_hashed += 1
    stats.bytes_read += read
    if read != file.size:
        stats.skipped["changed"] += 1
        return None
    return digest.hexdigest()


def _duplicate_group(digest: str, members: list[Candidate]) -> DuplicateGroup:
    ordered = sorted(members, key=lambda f: (f.mtime, len(f.path), f.path))
    return DuplicateGroup(hash=digest, files=tuple(ordered))
