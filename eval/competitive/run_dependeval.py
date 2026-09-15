#!/usr/bin/env python3
"""Evaluate directed source graphs on preregistered DependEval folds."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from muninn.code_search import (  # noqa: E402
    source_dependency_details,
    source_record,
    structural_source_record,
)
from muninn.code_dependency import build_dependency_graph_records  # noqa: E402


DATASET_REVISION = "7c5f15bbd7ba5e032082bfe4595327fbc5d126b9"
GRAPHIFY_REVISION = "00efd6e7969837ae4a9f11d8d504dcd3b20b09df"
SPLIT_SEED = "muninn-dependeval-series5-1"
LANGUAGES = ("c", "c++", "c#", "typescript", "javascript", "java", "php",
             "python")


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def case_fold(language: str, index: int) -> int:
    value = f"{SPLIT_SEED}:{language}:{index}".encode()
    return int(hashlib.sha256(value).hexdigest()[:8], 16) % 5


def normalized_path(value: str) -> str:
    path = value.strip()
    if len(path) >= 2 and path[0] == path[-1] and path[0] in "'\"":
        path = path[1:-1]
    path = path.replace("\\", "/").lstrip("/")
    parts = [part for part in path.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"unsafe or empty source path: {value!r}")
    return "/".join(parts)


def split_source_files(raw_paths: list[str], content: str) -> dict[str, str]:
    markers = []
    files = {normalized_path(path): "" for path in raw_paths}
    for raw_path in raw_paths:
        marker = raw_path + "\n:"
        location = content.find(marker)
        if location < 0:
            continue
        if content.find(marker, location + 1) >= 0:
            raise ValueError(f"source marker is repeated: {raw_path}")
        markers.append((location, location + len(marker),
                        normalized_path(raw_path)))
    markers.sort()
    for position, (_start, body_start, path) in enumerate(markers):
        body_end = markers[position + 1][0] if position + 1 < len(markers) \
            else len(content)
        files[path] = content[body_start:body_end].rstrip()
    return files


def source_file(data_root: Path, language: str) -> Path:
    return data_root / language / f"task2_{language}_final.json"


def load_cases(data_root: Path, folds: set[int]) -> tuple[list[dict],
                                                          dict[str, str]]:
    cases = []
    digests = {}
    for language in LANGUAGES:
        path = source_file(data_root, language)
        digests[language] = digest(path)
        rows = json.loads(path.read_text())
        for index, row in enumerate(rows):
            fold = case_fold(language, index)
            if fold not in folds:
                continue
            files = split_source_files(row["files"], row["content"])
            gold_chain = [normalized_path(value) for value in row["gt"]]
            cases.append({
                "case_id": f"{language}/{index}",
                "language": language,
                "index": index,
                "fold": fold,
                "files": files,
                "gold_chain": gold_chain,
            })
    return cases, digests


def chain_edges(chain: list[str]) -> set[tuple[str, str]]:
    return {(chain[index], chain[index - 1]) for index in range(1, len(chain))}


def score_edges(predicted: set[tuple[str, str]],
                gold: set[tuple[str, str]]) -> dict[str, float | int]:
    matched = len(predicted & gold)
    precision = matched / len(predicted) if predicted else 0.0
    recall = matched / len(gold) if gold else float(not predicted)
    edge_f1 = (2 * precision * recall / (precision + recall)
               if precision + recall else 0.0)
    predicted_nodes = {value for edge in predicted for value in edge}
    gold_nodes = {value for edge in gold for value in edge}
    node_match = len(predicted_nodes & gold_nodes)
    node_precision = node_match / len(predicted_nodes) if predicted_nodes else 0.0
    node_recall = node_match / len(gold_nodes) if gold_nodes else float(
        not predicted_nodes)
    node_f1 = (2 * node_precision * node_recall /
               (node_precision + node_recall)
               if node_precision + node_recall else 0.0)
    return {
        "edge_precision": precision,
        "edge_recall": recall,
        "edge_f1": edge_f1,
        "node_f1": node_f1,
        "exact_chain": int(predicted == gold),
    }


def muninn_edges(files: dict[str, str], *, structural: bool,
                 imports_only: bool) -> tuple[set[tuple[str, str]], dict]:
    with tempfile.TemporaryDirectory(prefix="muninn-dependeval-") as directory:
        root = Path(directory)
        records = []
        for path, text in sorted(files.items()):
            physical = root / path
            physical.parent.mkdir(parents=True, exist_ok=True)
            physical.write_text(text)
            records.append(structural_source_record(path, text, str(physical))
                           if structural else source_record(path, text))
        relations, _evidence = source_dependency_details(
            records, imports_only=imports_only)
    edges = set(relations)
    return edges, {
        "records": len(records),
        "edges": len(edges),
        "records_with_structure": sum(
            bool(record.imports or record.relation_references)
            for record in records),
    }


def resolve_graphify_path(value: str, available: set[str]) -> str | None:
    raw = value.replace("\\", "/").lstrip("/")
    candidates = [path for path in available
                  if path == raw or path.endswith("/" + raw)]
    return candidates[0] if len(candidates) == 1 else None


def graphify_edges(files: dict[str, str]) -> tuple[
        set[tuple[str, str]], dict]:
    from graphify.extract import extract

    with tempfile.TemporaryDirectory(prefix="graphify-dependeval-") as directory:
        root = Path(directory) / "source"
        cache = Path(directory) / "cache"
        paths = []
        for path, text in sorted(files.items()):
            physical = root / path
            physical.parent.mkdir(parents=True, exist_ok=True)
            physical.write_text(text)
            paths.append(physical)
        extracted = extract(paths, cache_root=cache, root=root, parallel=False)
    available = set(files)
    source_by_node = {}
    for node in extracted.get("nodes", []):
        if not node.get("id") or not node.get("source_file"):
            continue
        path = resolve_graphify_path(str(node.get("source_file")), available)
        if path:
            source_by_node[str(node.get("id"))] = path
    edges = set()
    for edge in extracted.get("edges", []):
        source = source_by_node.get(str(edge.get("source")))
        target = source_by_node.get(str(edge.get("target")))
        if source and target and source != target:
            edges.add((source, target))
    return edges, {
        "records": len(files),
        "nodes": len(extracted.get("nodes", [])),
        "native_edges": len(extracted.get("edges", [])),
        "edges": len(edges),
    }


def hybrid_edges(files: dict[str, str]) -> tuple[set[tuple[str, str]], dict]:
    with tempfile.TemporaryDirectory(prefix="hybrid-dependeval-") as directory:
        root = Path(directory) / "source"
        records = []
        for path, text in sorted(files.items()):
            physical = root / path
            physical.parent.mkdir(parents=True, exist_ok=True)
            physical.write_text(text)
            records.append(structural_source_record(
                path, text, str(physical)))
        graph = build_dependency_graph_records(
            root, records, cache_root=Path(directory) / "cache")
    return set(graph.relations), {
        "records": len(files),
        "edges": len(graph.relations),
        "providers": [list(value) for value in graph.providers],
        "graphify_used": graph.graphify_used,
        "graphify_error": graph.graphify_error,
    }


def aggregate(rows: list[dict]) -> dict:
    metrics = ("edge_precision", "edge_recall", "edge_f1", "node_f1",
               "exact_chain")

    def means(values: list[dict]) -> dict[str, float]:
        return {name: statistics.fmean(row["metrics"][name] for row in values)
                for name in metrics}

    return {
        "all": means(rows),
        "by_language": {
            language: means([row for row in rows if row["language"] == language])
            for language in LANGUAGES
        },
        "by_chain_length": {
            str(length): means([row for row in rows
                                if row["gold_chain_length"] == length])
            for length in sorted({row["gold_chain_length"] for row in rows})
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--folds", type=int, nargs="+", required=True)
    parser.add_argument("--condition", choices=(
        "graphify", "muninn-hybrid", "muninn-light", "muninn-imports",
        "muninn-tree"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    folds = set(args.folds)
    if not folds or not folds <= {0, 1, 2, 3, 4}:
        raise ValueError("folds must be integers from zero through four")
    if 4 in folds and folds != {4}:
        raise ValueError("the confirmation fold must run alone")
    repository_root = args.data_root.parent
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository_root, text=True).strip()
    if revision != DATASET_REVISION:
        raise RuntimeError(f"DependEval revision is {revision}, not {DATASET_REVISION}")
    cases, source_digests = load_cases(args.data_root, folds)
    rows = []
    latencies = []
    peaks = []
    started = time.perf_counter()
    structural = args.condition in {"muninn-imports", "muninn-tree"}
    imports_only = args.condition == "muninn-imports"
    for case in cases:
        tracemalloc.start()
        case_started = time.perf_counter()
        if args.condition == "graphify":
            predicted, operation = graphify_edges(case["files"])
        elif args.condition == "muninn-hybrid":
            predicted, operation = hybrid_edges(case["files"])
        else:
            predicted, operation = muninn_edges(
                case["files"], structural=structural,
                imports_only=imports_only)
        latency = (time.perf_counter() - case_started) * 1000.0
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        gold = chain_edges(case["gold_chain"])
        latencies.append(latency)
        peaks.append(peak)
        rows.append({
            "case_id": case["case_id"],
            "language": case["language"],
            "fold": case["fold"],
            "gold_chain_length": len(case["gold_chain"]),
            "source_paths_sha256": hashlib.sha256(
                "\n".join(sorted(case["files"])).encode()).hexdigest(),
            "predicted_edges": sorted([list(edge) for edge in predicted]),
            "gold_edges": sorted([list(edge) for edge in gold]),
            "metrics": score_edges(predicted, gold),
            "operation": operation,
            "latency_ms": round(latency, 6),
            "peak_allocation_bytes": peak,
        })
    latencies.sort()
    peaks.sort()
    percentile_index = max(0, int(0.95 * len(rows)) - 1)
    run = {
        "schema": 1,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "muninn_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "dataset_revision": revision,
        "dataset_sha256": source_digests,
        "split_seed": SPLIT_SEED,
        "folds": sorted(folds),
        "condition": args.condition,
        "graphify_revision": (GRAPHIFY_REVISION if args.condition in {
            "graphify", "muninn-hybrid"} else None),
        "case_count": len(rows),
        "network_calls": 0,
        "model_calls": 0,
        "elapsed_seconds": round(time.perf_counter() - started, 6),
        "latency_ms": {
            "p50": statistics.median(latencies),
            "p95": latencies[percentile_index],
        },
        "peak_allocation_bytes": {
            "p50": statistics.median(peaks),
            "p95": peaks[percentile_index],
        },
        "summary": aggregate(rows),
        "results": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(run, indent=2, sort_keys=True) + "\n")
    print(args.output)
    print(json.dumps(run["summary"]["all"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
