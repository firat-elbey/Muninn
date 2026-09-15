#!/usr/bin/env python3
"""Compare the in-memory and persistent indexes on fixed SWE-bench cases."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import gzip
import hashlib
import json
import statistics
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "src"))

import run_swebench as shared  # noqa: E402
from muninn.code_search import CodeSearchIndex, source_records  # noqa: E402
from muninn.persistent_code_search import PersistentCodeSearchIndex  # noqa: E402


def load_json(path: Path) -> dict:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as source:
            return json.load(source)
    return json.loads(path.read_text())


def git_revision() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


def evaluate(reference: dict, row: dict, cache: Path) -> dict:
    problem_sha256 = hashlib.sha256(row["problem_statement"].encode()).hexdigest()
    if problem_sha256 != reference["problem_sha256"]:
        raise RuntimeError(
            f"dataset query differs for {reference['instance_id']}")
    repository = shared.ensure_repository(cache, row["repo"])
    shared.fetch_commit(repository, row["base_commit"])
    with tempfile.TemporaryDirectory(prefix="muninn-persistent-eval-") as directory:
        root = Path(directory) / "source"
        root.mkdir()
        shared.export_commit(repository, row["base_commit"], root)

        started = time.perf_counter()
        records = source_records(str(root), extensions={".py"})
        scan_seconds = time.perf_counter() - started
        source_bytes = sum(len(record.text.encode()) for record in records)

        started = time.perf_counter()
        memory = CodeSearchIndex(records)
        memory_build_seconds = time.perf_counter() - started

        database = Path(directory) / "code-index.sqlite"
        started = time.perf_counter()
        persistent = PersistentCodeSearchIndex.build_records(
            records, database, source_root=str(root))
        persistent_build_seconds = time.perf_counter() - started
        index_bytes = persistent.size_bytes
        fingerprint = persistent.metadata["source_fingerprint"]
        persistent.close()

        started = time.perf_counter()
        persistent = PersistentCodeSearchIndex(database)
        persistent_open_ms = (time.perf_counter() - started) * 1000.0
        conditions = {}
        for mode in shared.MODES:
            started = time.perf_counter()
            memory_hits = memory.search(
                row["problem_statement"], mode=mode, limit=20)
            memory_query_ms = (time.perf_counter() - started) * 1000.0
            started = time.perf_counter()
            persistent_hits = persistent.search(
                row["problem_statement"], mode=mode, limit=20)
            persistent_query_ms = (time.perf_counter() - started) * 1000.0
            memory_ranking = [hit.path for hit in memory_hits]
            persistent_ranking = [hit.path for hit in persistent_hits]
            source_ranking = reference["conditions"][mode]["ranking"]
            if memory_ranking != persistent_ranking:
                raise RuntimeError(
                    f"persistent ranking differs for {reference['instance_id']} "
                    f"under {mode}")
            conditions[mode] = {
                "ranking": persistent_ranking,
                "matches_in_memory": True,
                "matches_source_run": persistent_ranking == source_ranking,
                "memory_query_ms": round(memory_query_ms, 3),
                "persistent_query_ms": round(persistent_query_ms, 3),
                "metrics": shared.rank_metrics(
                    persistent_ranking, set(reference["gold"])),
            }
        persistent.close()

    return {
        "instance_id": reference["instance_id"],
        "repository": reference["repository"],
        "base_commit": reference["base_commit"],
        "documents": len(records),
        "source_bytes": source_bytes,
        "source_fingerprint": fingerprint,
        "scan_seconds": round(scan_seconds, 3),
        "memory_build_seconds": round(memory_build_seconds, 3),
        "persistent_build_seconds": round(persistent_build_seconds, 3),
        "persistent_open_ms": round(persistent_open_ms, 3),
        "index_bytes": index_bytes,
        "conditions": conditions,
    }


def evaluate_task(task: tuple[dict, dict, str]) -> dict:
    reference, row, cache = task
    return evaluate(reference, row, Path(cache))


def sample_per_repository(references: list[dict], count: int,
                          seed: str) -> list[dict]:
    grouped = defaultdict(list)
    for reference in references:
        grouped[reference["repository"]].append(reference)
    selected = []
    for repository in sorted(grouped):
        ranked = sorted(grouped[repository], key=lambda row: hashlib.sha256(
            f"{seed}:{row['instance_id']}".encode()).hexdigest())
        selected.extend(ranked[:count])
    return selected


def aggregate(results: list[dict]) -> dict:
    summary = {
        "scan_seconds_p50": statistics.median(
            row["scan_seconds"] for row in results),
        "memory_build_seconds_p50": statistics.median(
            row["memory_build_seconds"] for row in results),
        "persistent_build_seconds_p50": statistics.median(
            row["persistent_build_seconds"] for row in results),
        "persistent_open_ms_p50": statistics.median(
            row["persistent_open_ms"] for row in results),
        "persistent_open_ms_p95": percentile(
            [row["persistent_open_ms"] for row in results], 0.95),
        "source_bytes_total": sum(row["source_bytes"] for row in results),
        "index_bytes_total": sum(row["index_bytes"] for row in results),
    }
    for mode in shared.MODES:
        entries = [row["conditions"][mode] for row in results]
        memory_times = [entry["memory_query_ms"] for entry in entries]
        persistent_times = [entry["persistent_query_ms"] for entry in entries]
        summary[mode] = {
            "source_run_parity": sum(entry["matches_source_run"]
                                     for entry in entries),
            "hit1": statistics.fmean(entry["metrics"]["hit1"]
                                      for entry in entries),
            "hit10": statistics.fmean(entry["metrics"]["hit10"]
                                       for entry in entries),
            "mrr": statistics.fmean(entry["metrics"]["mrr"]
                                     for entry in entries),
            "memory_query_ms_p50": statistics.median(memory_times),
            "memory_query_ms_p95": percentile(memory_times, 0.95),
            "persistent_query_ms_p50": statistics.median(persistent_times),
            "persistent_query_ms_p95": percentile(persistent_times, 0.95),
        }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_run", type=Path)
    parser.add_argument("--cache", type=Path, default=HERE / "cache")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--rows-per-repository", type=int)
    parser.add_argument("--seed", default="muninn-persistent-index-1")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.limit is not None and args.limit <= 0:
        parser.error("limit must be positive")
    if args.rows_per_repository is not None and args.rows_per_repository <= 0:
        parser.error("rows per repository must be positive")
    if args.limit is not None and args.rows_per_repository is not None:
        parser.error("limit and rows per repository cannot be combined")
    if args.workers <= 0:
        parser.error("workers must be positive")

    source = load_json(args.source_run)
    references = list(source["results"])
    if args.rows_per_repository is not None:
        references = sample_per_repository(
            references, args.rows_per_repository, args.seed)
    elif args.limit:
        references = references[:args.limit]
    dataset = {row["instance_id"]: row for row in shared.dataset_rows()}
    for reference in references:
        row = dataset[reference["instance_id"]]
        repository = shared.ensure_repository(args.cache, row["repo"])
        shared.fetch_commit(repository, row["base_commit"])
    tasks = [(reference, dataset[reference["instance_id"]], str(args.cache))
             for reference in references]
    results = []
    started = time.perf_counter()
    if args.workers == 1:
        completed = map(evaluate_task, tasks)
        pool = None
    else:
        pool = concurrent.futures.ProcessPoolExecutor(
            max_workers=args.workers)
        completed = pool.map(evaluate_task, tasks)
    try:
        for position, result in enumerate(completed, 1):
            results.append(result)
            print(f"persistent parity: {position}/{len(references)}",
                  file=sys.stderr, flush=True)
    finally:
        if pool is not None:
            pool.shutdown()
    run = {
        "schema": 1,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "muninn_revision": git_revision(),
        "source_run": str(args.source_run),
        "source_run_sha256": hashlib.sha256(
            args.source_run.read_bytes()).hexdigest(),
        "dataset": source["dataset"],
        "dataset_revision": source["dataset_revision"],
        "limited_run": (args.limit is not None
                        or args.rows_per_repository is not None),
        "parameters": {
            "limit": args.limit,
            "rows_per_repository": args.rows_per_repository,
            "seed": args.seed,
            "workers": args.workers,
        },
        "rows": len(results),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "summary": aggregate(results),
        "results": results,
    }
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = args.output or HERE / "runs" / f"persistent-parity-{stamp}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(run, indent=2, sort_keys=True) + "\n")
    print(destination)
    for mode in shared.MODES:
        values = run["summary"][mode]
        print(f"{mode:15s} source parity={values['source_run_parity']}/"
              f"{len(results)} persistent p50="
              f"{values['persistent_query_ms_p50']:.3f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
