import os
from collections.abc import Iterable
from dataclasses import dataclass, field

from orphic.dedup.engine import Candidate, DuplicateGroup

# Identical files inside these are needed where they are, by the tool that owns them.
PROTECTED_DIRECTORY_NAMES = frozenset(
    {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "site-packages", "appdata"}
)


def default_protected_directories() -> tuple[str, ...]:
    if os.name == "nt":
        variables = ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")
        return tuple(os.environ[name] for name in variables if name in os.environ)
    return ("/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/sbin", "/sys", "/usr")


def _normalized(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _is_within(path: str, directory: str) -> bool:
    return path == directory or path.startswith(directory.rstrip(os.sep) + os.sep)


@dataclass(frozen=True, slots=True)
class ProtectionPolicy:
    directories: tuple[str, ...]
    names: frozenset[str] = PROTECTED_DIRECTORY_NAMES

    @classmethod
    def with_defaults(cls, extra: Iterable[str] = ()) -> "ProtectionPolicy":
        return cls(directories=(*default_protected_directories(), *extra))

    def is_protected(self, path: str) -> bool:
        normalized = _normalized(path)
        if any(_is_within(normalized, _normalized(d)) for d in self.directories):
            return True
        return any(part.casefold() in self.names for part in normalized.split(os.sep))


@dataclass(frozen=True, slots=True)
class Move:
    file: Candidate
    original: Candidate
    hash: str


@dataclass(slots=True)
class CleanupPlan:
    moves: list[Move] = field(default_factory=list)
    kept: list[Candidate] = field(default_factory=list)
    protected: list[Candidate] = field(default_factory=list)

    @property
    def bytes_to_move(self) -> int:
        return sum(move.file.size for move in self.moves)


def build_plan(
    groups: Iterable[DuplicateGroup], policy: ProtectionPolicy, recovery_root: str
) -> CleanupPlan:
    """Decide what could move. Reads no file and changes nothing."""
    plan = CleanupPlan()
    recovery = _normalized(recovery_root)
    for group in groups:
        # Copies already in recovery are not live files, so they cannot be the kept copy.
        files = [f for f in group.files if not _is_within(_normalized(f.path), recovery)]
        if len(files) < 2:
            continue
        original, *duplicates = files
        plan.kept.append(original)
        for file in duplicates:
            if policy.is_protected(file.path):
                plan.protected.append(file)
            else:
                plan.moves.append(Move(file=file, original=original, hash=group.hash))
    return plan
