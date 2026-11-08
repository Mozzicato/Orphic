import os
import shutil
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orphic.cleanup.executor import execute, reconcile, restore
from orphic.cleanup.planner import CleanupPlan, ProtectionPolicy, build_plan
from orphic.cli.main import app
from orphic.dedup.engine import Candidate, DuplicateGroup, find_duplicates
from orphic.index.store import Index, OperationState

NO_PROTECTION = ProtectionPolicy(directories=(), names=frozenset())
runner = CliRunner()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    folder = tmp_path / "root"
    folder.mkdir()
    (folder / "a.txt").write_bytes(b"same content")
    os.utime(folder / "a.txt", (1000, 1000))
    (folder / "b.txt").write_bytes(b"same content")
    os.utime(folder / "b.txt", (2000, 2000))
    (folder / "unique.txt").write_bytes(b"different content")
    return folder


@pytest.fixture
def index(tmp_path: Path, root: Path) -> Iterator[Index]:
    with Index(tmp_path / "index.db") as opened:
        opened.record_scan(root)
        opened.save_duplicates(find_duplicates(opened.candidates())[0])
        yield opened


@pytest.fixture
def recovery(tmp_path: Path) -> Path:
    return tmp_path / "recovery"


def plan_for(index: Index, recovery: Path) -> CleanupPlan:
    return build_plan(index.saved_duplicates(), NO_PROTECTION, str(recovery))


def candidate(path: str) -> Candidate:
    return Candidate(0, path, 1, 0.0)


def test_plan_keeps_the_original_and_moves_the_copy(index: Index, recovery: Path) -> None:
    plan = plan_for(index, recovery)

    assert [Path(f.path).name for f in plan.kept] == ["a.txt"]
    assert [Path(m.file.path).name for m in plan.moves] == ["b.txt"]
    assert plan.bytes_to_move == len(b"same content")


def test_planning_changes_nothing_on_disk(index: Index, root: Path, recovery: Path) -> None:
    plan_for(index, recovery)

    assert (root / "b.txt").exists()
    assert not recovery.exists()


def test_protected_directory_copy_is_not_moved(tmp_path: Path) -> None:
    protected = str(tmp_path / "school")
    group = DuplicateGroup(
        "h", (candidate(str(tmp_path / "keep")), candidate(os.path.join(protected, "copy")))
    )

    plan = build_plan([group], ProtectionPolicy((protected,), frozenset()), str(tmp_path / "r"))

    assert plan.moves == []
    assert len(plan.protected) == 1


def test_sibling_with_a_shared_prefix_is_not_protected(tmp_path: Path) -> None:
    policy = ProtectionPolicy((str(tmp_path / "school"),), frozenset())

    assert not policy.is_protected(str(tmp_path / "school-old" / "file"))
    assert policy.is_protected(str(tmp_path / "school" / "file"))


@pytest.mark.parametrize("folder", [".git", "node_modules", ".venv", "site-packages"])
def test_tool_directories_are_protected_by_default(tmp_path: Path, folder: str) -> None:
    policy = ProtectionPolicy.with_defaults()

    assert policy.is_protected(str(tmp_path / "project" / folder / "file"))


def test_ordinary_folders_are_not_protected_by_default() -> None:
    policy = ProtectionPolicy.with_defaults()

    assert not policy.is_protected(os.path.join(os.sep, "home", "me", "Photos", "trip.jpg"))


def test_copies_already_in_recovery_are_never_the_kept_file(tmp_path: Path) -> None:
    recovery_root = tmp_path / "recovery"
    group = DuplicateGroup(
        "h",
        (candidate(str(recovery_root / "b1" / "old")), candidate(str(tmp_path / "live"))),
    )

    plan = build_plan([group], NO_PROTECTION, str(recovery_root))

    assert plan.moves == []
    assert plan.kept == []


def test_execute_moves_the_copy_and_keeps_its_bytes(
    index: Index, root: Path, recovery: Path
) -> None:
    result = execute(plan_for(index, recovery), index, recovery)

    assert result.moved == 1
    assert not (root / "b.txt").exists()
    assert (root / "a.txt").exists()
    moved = index.operations(batch=result.batch)[0]
    assert Path(moved["destination"]).read_bytes() == b"same content"
    assert moved["state"] == OperationState.MOVED


def test_moved_file_leaves_the_index(index: Index, recovery: Path) -> None:
    execute(plan_for(index, recovery), index, recovery)

    assert index.saved_duplicates() == []


def test_restore_puts_the_file_back(index: Index, root: Path, recovery: Path) -> None:
    result = execute(plan_for(index, recovery), index, recovery)

    outcome = restore(index, result.batch)

    assert outcome.restored == 1
    assert (root / "b.txt").read_bytes() == b"same content"


def test_restore_never_overwrites_a_new_file(index: Index, root: Path, recovery: Path) -> None:
    result = execute(plan_for(index, recovery), index, recovery)
    (root / "b.txt").write_bytes(b"new work")

    outcome = restore(index, result.batch)

    assert outcome.conflicts == 1
    assert (root / "b.txt").read_bytes() == b"new work"
    assert Path(index.operations(batch=result.batch)[0]["destination"]).exists()


def test_file_edited_since_the_scan_is_left_alone(
    index: Index, root: Path, recovery: Path
) -> None:
    plan = plan_for(index, recovery)
    (root / "b.txt").write_bytes(b"edited, longer than before")

    result = execute(plan, index, recovery)

    assert result.moved == 0
    assert result.skipped["changed_since_scan"] == 1
    assert (root / "b.txt").exists()


def test_same_size_and_time_but_different_bytes_is_left_alone(
    index: Index, root: Path, recovery: Path
) -> None:
    plan = plan_for(index, recovery)
    (root / "b.txt").write_bytes(b"SAME CONTENT")
    os.utime(root / "b.txt", (2000, 2000))

    result = execute(plan, index, recovery)

    assert result.skipped["content_mismatch"] == 1
    assert (root / "b.txt").exists()


def test_copy_is_kept_if_the_original_has_gone(index: Index, root: Path, recovery: Path) -> None:
    plan = plan_for(index, recovery)
    (root / "a.txt").unlink()

    result = execute(plan, index, recovery)

    assert result.moved == 0
    assert (root / "b.txt").exists()


def test_original_edited_since_the_scan_keeps_the_copy(
    index: Index, root: Path, recovery: Path
) -> None:
    plan = plan_for(index, recovery)
    (root / "a.txt").write_bytes(b"SAME CONTENT")

    result = execute(plan, index, recovery)

    assert result.skipped["content_mismatch"] == 1
    assert (root / "b.txt").exists()


def test_move_failure_is_logged_and_the_run_continues(
    index: Index, root: Path, recovery: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = plan_for(index, recovery)

    def failing_move(source: str, destination: Path) -> None:
        raise PermissionError(13, "in use", source)

    monkeypatch.setattr(shutil, "move", failing_move)

    result = execute(plan, index, recovery)

    assert result.skipped["PermissionError"] == 1
    assert index.operations(batch=result.batch)[0]["state"] == OperationState.FAILED
    assert (root / "b.txt").exists()


def logged(index: Index, source: Path, destination: Path) -> int:
    return index.log_operation("batch", str(source), str(destination), "hash", 1)


def test_reconcile_crash_before_the_move(index: Index, tmp_path: Path, root: Path) -> None:
    operation = logged(index, root / "b.txt", tmp_path / "dest")

    assert reconcile(index) == 1

    assert index.operations(batch="batch")[0]["id"] == operation
    assert index.operations(batch="batch")[0]["state"] == OperationState.ABANDONED
    assert (root / "b.txt").exists()


def test_reconcile_crash_after_the_move(index: Index, tmp_path: Path, root: Path) -> None:
    logged(index, root / "b.txt", tmp_path / "dest")
    (root / "b.txt").rename(tmp_path / "dest")

    reconcile(index)

    assert index.operations(batch="batch")[0]["state"] == OperationState.MOVED


def test_reconcile_removes_a_partial_copy_and_keeps_the_source(
    index: Index, tmp_path: Path, root: Path
) -> None:
    logged(index, root / "b.txt", tmp_path / "dest")
    (tmp_path / "dest").write_bytes(b"sam")

    reconcile(index)

    assert not (tmp_path / "dest").exists()
    assert (root / "b.txt").read_bytes() == b"same content"


def test_reconcile_with_both_sides_missing_records_failure(index: Index, tmp_path: Path) -> None:
    logged(index, tmp_path / "gone", tmp_path / "also-gone")

    reconcile(index)

    assert index.operations(batch="batch")[0]["state"] == OperationState.FAILED


def test_reconcile_detects_a_restore_that_crashed(index: Index, root: Path, recovery: Path) -> None:
    result = execute(plan_for(index, recovery), index, recovery)
    operation = index.operations(batch=result.batch)[0]
    Path(operation["destination"]).rename(operation["source"])

    reconcile(index)

    assert index.operations(batch=result.batch)[0]["state"] == OperationState.RESTORED


def test_process_killed_mid_move_is_recovered(tmp_path: Path, root: Path, recovery: Path) -> None:
    database = tmp_path / "index.db"
    with Index(database) as opened:
        opened.record_scan(root)
        opened.save_duplicates(find_duplicates(opened.candidates())[0])

    script = textwrap.dedent(
        """
        import os, shutil, sys
        from orphic.cleanup.executor import execute
        from orphic.cleanup.planner import ProtectionPolicy, build_plan
        from orphic.index.store import Index

        real_move = shutil.move

        def move_then_die(source, destination):
            real_move(source, destination)
            os._exit(9)

        shutil.move = move_then_die
        index = Index(sys.argv[1])
        policy = ProtectionPolicy((), frozenset())
        execute(build_plan(index.saved_duplicates(), policy, sys.argv[2]), index, sys.argv[2])
        """
    )
    completed = subprocess.run([sys.executable, "-c", script, str(database), str(recovery)])
    assert completed.returncode == 9

    with Index(database) as reopened:
        assert reconcile(reopened) == 1
        operation = reopened.operations(state=OperationState.MOVED)[0]
        assert Path(operation["destination"]).read_bytes() == b"same content"
        assert not (root / "b.txt").exists()


@pytest.fixture
def cli_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # pytest's temp folder is inside AppData on Windows, which is protected by default.
    monkeypatch.setattr(
        ProtectionPolicy,
        "with_defaults",
        lambda extra=(): ProtectionPolicy(tuple(extra), frozenset()),
    )
    folder = tmp_path / "cli"
    folder.mkdir()
    (folder / "a.txt").write_text("same")
    (folder / "b.txt").write_text("same")
    runner.invoke(app, ["scan", str(folder)])
    runner.invoke(app, ["dedup"])
    return folder


def files_in(folder: Path) -> int:
    return len(list(folder.iterdir()))


def test_clean_is_a_dry_run_by_default(cli_root: Path) -> None:
    result = runner.invoke(app, ["clean"])

    assert result.exit_code == 0
    assert "Would move: 1 files" in result.stdout
    assert "Dry run" in result.stdout
    assert files_in(cli_root) == 2


def test_clean_execute_stops_when_not_confirmed(cli_root: Path) -> None:
    result = runner.invoke(app, ["clean", "--execute"], input="n\n")

    assert result.exit_code == 1
    assert files_in(cli_root) == 2


def test_clean_execute_moves_after_confirmation(cli_root: Path) -> None:
    result = runner.invoke(app, ["clean", "--execute"], input="y\n")

    assert result.exit_code == 0
    assert "Moved: 1 files" in result.stdout
    assert files_in(cli_root) == 1


def test_clean_respects_protect_option(cli_root: Path) -> None:
    result = runner.invoke(app, ["clean", "--execute", "--yes", "--protect", str(cli_root)])

    assert "Moved: 0 files" in result.stdout
    assert files_in(cli_root) == 2


def test_restore_command_undoes_a_clean(cli_root: Path) -> None:
    cleaned = runner.invoke(app, ["clean", "--execute", "--yes"])
    batch = next(
        line.split()[-1] for line in cleaned.stdout.splitlines() if line.startswith("Batch")
    )

    result = runner.invoke(app, ["restore", batch])

    assert "Restored: 1" in result.stdout
    assert files_in(cli_root) == 2
