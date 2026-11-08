"""Measure Orphic on a real directory. Results record counts, never file paths.

python benchmarks/bench.py scan ROOT
python benchmarks/bench.py incremental ROOT
python benchmarks/bench.py dedup ROOT
"""

import argparse
import csv
import platform
import random
import statistics
import tempfile
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path

from orphic.dedup.engine import Strategy, find_duplicates
from orphic.index.store import Index
from orphic.scanner import walker

RESULTS = Path(__file__).parent / "results"
WORKER_COUNTS = (1, 2, 4, 8, 16)
# Cheapest first and the baseline last, so warm-cache effects favour the baseline.
DEDUP_ORDER = (
    Strategy.SIZE_FIRST,
    Strategy.SIZE_FIRST_LAST,
    Strategy.SIZE,
    Strategy.HASH_EVERYTHING,
)
# Longer than the index's timestamp safety window, so directories can be reused.
SETTLE_SECONDS = 3


def timed[T](action: Callable[[], T]) -> tuple[float, T]:
    start = time.perf_counter()
    result = action()
    return time.perf_counter() - start, result


def save(kind: str, rows: list[dict[str, object]]) -> None:
    RESULTS.mkdir(exist_ok=True)
    environment = {
        "date": date.today().isoformat(),
        "os": platform.platform(),
        "python": platform.python_version(),
    }
    output = RESULTS / f"{kind}-{environment['date']}.csv"
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[*environment, *rows[0]])
        writer.writeheader()
        writer.writerows({**environment, **row} for row in rows)
    print(f"\nSaved {output}")


def bench_scan(root: Path, repeats: int) -> None:
    def count_entries(workers: int, processes: bool) -> int:
        return sum(1 for _ in walker.scan(root, workers=workers, processes=processes))

    print("Warming the filesystem cache...")
    entries = count_entries(8, False)

    configurations = [
        (1, False),
        *((workers, False) for workers in WORKER_COUNTS[1:]),
        *((workers, True) for workers in WORKER_COUNTS[1:]),
    ]
    # Every setup runs in every round, shuffled, so background load hits them alike.
    times: dict[tuple[int, bool], list[float]] = {setup: [] for setup in configurations}
    for _ in range(repeats):
        for setup in random.sample(configurations, len(configurations)):
            times[setup].append(timed(lambda chosen=setup: count_entries(*chosen))[0])

    baseline = statistics.median(times[(1, False)])
    rows: list[dict[str, object]] = []
    for setup in configurations:
        workers, processes = setup
        median = statistics.median(times[setup])
        rows.append(
            {
                "mode": "serial" if workers == 1 else "processes" if processes else "threads",
                "workers": workers,
                "entries": entries,
                "repeats": repeats,
                "median_seconds": round(median, 4),
                "min_seconds": round(min(times[setup]), 4),
                "max_seconds": round(max(times[setup]), 4),
                "entries_per_second": round(entries / median),
                "speedup_vs_serial": round(baseline / median, 2),
            }
        )
        print(rows[-1], flush=True)
    save("scan", rows)


def bench_incremental(root: Path, repeats: int) -> None:
    with tempfile.TemporaryDirectory() as folder, Index(Path(folder) / "i.db") as index:
        first, _ = timed(lambda: index.record_scan(root, full=True))
        time.sleep(SETTLE_SECONDS)

        full_times: list[float] = []
        incremental_times: list[float] = []
        for _ in range(repeats):
            full_times.append(timed(lambda: index.record_scan(root, full=True))[0])
            time.sleep(SETTLE_SECONDS)
            elapsed, summary = timed(lambda: index.record_scan(root))
            incremental_times.append(elapsed)
            time.sleep(SETTLE_SECONDS)

    full = statistics.median(full_times)
    incremental = statistics.median(incremental_times)
    row: dict[str, object] = {
        "files": summary.files,
        "directories": summary.directories,
        "directories_reused": summary.directories_reused,
        "repeats": repeats,
        "first_scan_seconds": round(first, 4),
        "full_rescan_median_seconds": round(full, 4),
        "incremental_median_seconds": round(incremental, 4),
        "reduction_percent": round((1 - incremental / full) * 100, 1),
    }
    print(row)
    save("incremental", [row])


def bench_dedup(root: Path, include_baseline: bool) -> None:
    rows: list[dict[str, object]] = []
    seconds: dict[Strategy, float] = {}
    with tempfile.TemporaryDirectory() as folder, Index(Path(folder) / "i.db") as index:
        summary = index.record_scan(root, full=True)
        candidates = index.candidates()
        for strategy in DEDUP_ORDER:
            if strategy is Strategy.HASH_EVERYTHING and not include_baseline:
                continue
            elapsed, (groups, stats) = timed(
                lambda chosen=strategy: find_duplicates(candidates, chosen)
            )
            seconds[strategy] = elapsed
            rows.append(
                {
                    "strategy": strategy.value,
                    "files_indexed": summary.files,
                    "total_gb": round(summary.total_size / 1e9, 2),
                    "seconds": round(elapsed, 3),
                    "duplicate_groups": len(groups),
                    "files_opened": stats.files_opened,
                    "fully_hashed": stats.full_hashed,
                    "skip_full_hash_percent": round(stats.full_hash_skip_ratio * 100, 1),
                    "gb_read": round(stats.bytes_read / 1e9, 3),
                    "skipped_files": stats.skipped.total(),
                }
            )
            print(rows[-1])

    if include_baseline:
        baseline = seconds[Strategy.HASH_EVERYTHING]
        for row, strategy in zip(rows, DEDUP_ORDER, strict=True):
            row["speedup_vs_baseline"] = round(baseline / seconds[strategy], 2)
            print(strategy.value, row["speedup_vs_baseline"])
    save("dedup", rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=["scan", "incremental", "dedup"])
    parser.add_argument("root", type=Path)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument(
        "--no-baseline", action="store_true", help="Skip the slow hash-everything run."
    )
    arguments = parser.parse_args()

    if arguments.kind == "scan":
        bench_scan(arguments.root, arguments.repeats)
    elif arguments.kind == "incremental":
        bench_incremental(arguments.root, arguments.repeats)
    else:
        bench_dedup(arguments.root, not arguments.no_baseline)


if __name__ == "__main__":
    main()
