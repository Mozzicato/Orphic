# Orphic

A local-first command-line tool that indexes a disk into SQLite, finds files with identical content while reading as little data as possible, and cleans them up safely: dry run by default, nothing is ever deleted, and every move can be undone.

Everything runs on your machine. No network, no service, one SQLite file.

```
filesystem ──► scanner ──► SQLite index ──► dedup ──► cleanup plan ──► move to recovery area
               (walker)    (incremental)    (staged)   (dry run)        (logged, restorable)
```

## Install

Requires Python 3.12+.

```bash
git clone https://github.com/Mozzicato/Orphic.git
cd Orphic
uv venv && uv pip install -e ".[dev]"     # or: python -m venv .venv && pip install -e ".[dev]"
```

## Usage

```bash
orphic scan ~/Downloads                   # index a directory (incremental after the first scan)
orphic scan ~/Downloads --full            # relist every directory
orphic scan ~ --workers 8 --processes     # read directories in parallel processes
orphic largest --limit 20                 # biggest indexed files
orphic dedup                              # groups of identical files
orphic clean                              # dry run: show what would move, change nothing
orphic clean --execute                    # move duplicates to the recovery area
orphic restore <batch-id>                 # put a cleanup batch back where it was
```

The index lives at `~/.orphic/index.db` and moved files go to `~/.orphic/recovery/`. Override them with `--db` / `ORPHIC_DB` and `--recovery-dir` / `ORPHIC_RECOVERY`.

## How it works

**Scanning.** `os.scandir` with an explicit stack (no recursion limit) streams one record per entry. Symlinks and Windows junctions are recorded but never followed, so the scan cannot loop or leave the root. Permission errors, vanished paths and unreadable names become structured skips instead of crashes.

**Index.** One SQLite database in WAL mode. Each scan is a single transaction: a crash mid-scan leaves the previous index intact.

**Incremental scans.** A directory's mtime changes when entries are added, removed or renamed in it. If a directory's mtime matches the index, and it is older than a 2-second window around the last scan (to catch changes during that scan), its listing is reused instead of read again. The trade-off is that an in-place edit to a file does not change its directory's mtime, so `--full` exists to catch those.

**Duplicate detection** narrows candidates before reading whole files:

1. Group by size. A file with a unique size has no duplicate and is never opened.
2. Hash the first 4 KB of each remaining candidate. Different partial hashes rule a file out.
3. SHA-256 the full contents of what remains.

Hard links (same device and inode) count as one file. Files changed since the scan are dropped rather than trusted.

**Cleanup.** In each group the oldest copy is kept. Protected locations (`.git`, `.venv`, `node_modules`, `site-packages`, application data, and anything passed with `--protect`) are never touched. Before each move, Orphic re-checks size, mtime and full hash against the disk, logs the operation durably, and only then moves the file into a recovery batch. Whenever the CLI opens the index, operations a crash left half-done are reconciled first.

## Measured results

Measured on a Windows 11 laptop (Intel i5-8365U, 4 cores / 8 threads, 8 GB RAM, SSD). The scripts are in [`benchmarks/bench.py`](benchmarks/bench.py) and the raw numbers are in [`benchmarks/results/`](benchmarks/results/).

| What | Result |
|---|---|
| Parallel scanning, 863,709 entries, warm cache, median of 5 | **2.69×** with 8 processes (1.33× at 2, 2.26× at 4, 2.47× at 16). Threads were *slower* than serial (0.45–0.64×) because of the GIL. |
| Incremental vs full rescan, 753,394 files / 96,777 directories, median of 5 | **68% less time** (52 s vs 162 s) |
| Duplicate detection, 4.18 GB / 2,643 files, warm cache, single run | Size-first pipeline **18× faster** than hashing everything (1.1 s vs 20.3 s), reading 56 MB instead of 4.18 GB; same 136 duplicate groups |

What the numbers depend on:

- **Dedup speedup depends on the data.** On a folder of 12.9 GB / 327,772 mostly small source files, size grouping ruled out far fewer files and data read fell only about 3×. The partial-hash stage only helps when there are large files of the same size that differ.
- **On Windows the first open of a file cost about 40 ms**, against 0.67 ms for a reopen, so dedup time is governed by how many files are opened. That is the reason to filter by size first.
- **Parallel scanning uses processes, not threads,** because per-entry work is Python code holding the GIL. Directories are sent to workers in batches of 128 as plain tuples to keep pickling cheap.
- **Incremental profiling** showed most of the remaining time is SQLite writes rather than filesystem reads. Rows under unchanged directories are now left untouched instead of rewritten; the cost is that their `last_seen` is not refreshed.

## Development

```bash
pytest --cov       # 118 tests pass, 5 skip on Windows (symlink privilege, path length); ~97% coverage
ruff check .
mypy               # strict
```

CI runs on Ubuntu and Windows with Python 3.12, 3.13 and 3.14.

```
src/orphic/
  scanner/walker.py     directory walk, sequential or parallel
  index/store.py        SQLite index, incremental scans
  dedup/engine.py       staged duplicate detection
  cleanup/planner.py    what to move, protection rules
  cleanup/executor.py   logged moves, restore, crash reconciliation
  cli/main.py           command-line interface
```

## Limitations

- Exact duplicates only, no near-duplicate detection.
- Incremental scans miss in-place edits until a `--full` scan.
- Single machine, single writer.

## License

MIT
