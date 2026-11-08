import hashlib
import os
import shutil
import stat
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from orphic.cleanup.planner import CleanupPlan, Move
from orphic.dedup.engine import HASH_ALGORITHM
from orphic.index.store import Index, OperationState


@dataclass(slots=True)
class ExecutionResult:
    batch: str
    moved: int = 0
    bytes_moved: int = 0
    skipped: Counter[str] = field(default_factory=Counter)


@dataclass(slots=True)
class RestoreResult:
    restored: int = 0
    conflicts: int = 0
    failed: int = 0


def execute(plan: CleanupPlan, index: Index, recovery_root: str | Path) -> ExecutionResult:
    """Move duplicates to the recovery area. Nothing is ever deleted."""
    result = ExecutionResult(batch=uuid4().hex)
    destination_root = Path(recovery_root) / result.batch
    destination_root.mkdir(parents=True, exist_ok=True)

    for number, move in enumerate(plan.moves):
        problem = _problem_with(move)
        if problem is not None:
            result.skipped[problem] += 1
            continue

        destination = destination_root / f"{number}-{Path(move.file.path).name}"
        # Logged durably first: a crash after this line can always be reconciled.
        operation = index.log_operation(
            result.batch, move.file.path, str(destination), move.hash, move.file.size
        )
        try:
            shutil.move(move.file.path, destination)
        except OSError as error:
            index.set_operation_state(operation, OperationState.FAILED)
            result.skipped[type(error).__name__] += 1
            continue
        index.set_operation_state(operation, OperationState.MOVED)
        index.forget_file(move.file.path)
        result.moved += 1
        result.bytes_moved += move.file.size
    return result


def restore(index: Index, batch: str) -> RestoreResult:
    result = RestoreResult()
    for operation in index.operations(batch=batch, state=OperationState.MOVED):
        source = Path(operation["source"])
        if source.exists():
            index.set_operation_state(operation["id"], OperationState.CONFLICT)
            result.conflicts += 1
            continue
        try:
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(operation["destination"], source)
        except OSError:
            result.failed += 1
            continue
        index.set_operation_state(operation["id"], OperationState.RESTORED)
        result.restored += 1
    return result


def reconcile(index: Index) -> int:
    """Settle operations a crash left between the database and the filesystem."""
    settled = 0
    for operation in index.operations(state=OperationState.PLANNED):
        source_exists = os.path.lexists(operation["source"])
        destination_exists = os.path.lexists(operation["destination"])
        if source_exists:
            # A move across drives copies first, so this copy may be partial.
            if destination_exists:
                os.remove(operation["destination"])
            index.set_operation_state(operation["id"], OperationState.ABANDONED)
        elif destination_exists:
            index.set_operation_state(operation["id"], OperationState.MOVED)
            index.forget_file(operation["source"])
        else:
            index.set_operation_state(operation["id"], OperationState.FAILED)
        settled += 1

    for operation in index.operations(state=OperationState.MOVED):
        destination_exists = os.path.lexists(operation["destination"])
        if not destination_exists and os.path.lexists(operation["source"]):
            index.set_operation_state(operation["id"], OperationState.RESTORED)
            settled += 1
    return settled


def _problem_with(move: Move) -> str | None:
    """Re-check against the disk, because the plan may be out of date."""
    try:
        file_info = os.stat(move.file.path, follow_symlinks=False)
        original_info = os.stat(move.original.path, follow_symlinks=False)
        if not stat.S_ISREG(file_info.st_mode) or not stat.S_ISREG(original_info.st_mode):
            return "not_a_regular_file"
        if file_info.st_size != move.file.size or file_info.st_mtime != move.file.mtime:
            return "changed_since_scan"
        if (file_info.st_dev, file_info.st_ino) == (original_info.st_dev, original_info.st_ino):
            return "same_file_as_original"
        if _digest(move.file.path) != move.hash or _digest(move.original.path) != move.hash:
            return "content_mismatch"
    except OSError as error:
        return type(error).__name__
    return None


def _digest(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.file_digest(handle, HASH_ALGORITHM).hexdigest()
