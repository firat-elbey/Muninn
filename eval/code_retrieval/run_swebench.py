#!/usr/bin/env python3
"""Evaluate deterministic source retrieval on SWE-bench Lite.

The issue statement is the query.  Files modified by the accepted patch are
the relevance labels.  Each condition sees the same checkout and returns the
same number of files.  The primary label excludes test files because coding
agents must first locate the implementation; the report also retains all
patched files for audit.

The script downloads the official dataset metadata, selects instances by a
fixed hash before retrieval, checks out each recorded base commit, and writes
the complete top-20 rankings.  It does not call an LLM.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from muninn.code_search import CodeSearchIndex  # noqa: E402


DATASET = "princeton-nlp/SWE-bench_Lite"
DATASET_ROWS = 300
MODES = ("overlap", "bm25f", "lexical-hybrid", "hybrid", "graph-fusion")
DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.MULTILINE)


def _json_url(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "muninn-eval/1"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.load(response)


def dataset_rows() -> list[dict]:
    """Download all SWE-bench Lite rows from the official dataset server."""
    rows: list[dict] = []
    encoded = urllib.parse.quote(DATASET, safe="")
    for offset in range(0, DATASET_ROWS, 100):
        url = ("https://datasets-server.huggingface.co/rows?dataset="
               f"{encoded}&config=default&split=test&offset={offset}&length=100")
        payload = _json_url(url)
        rows.extend(item["row"] for item in payload.get("rows", []))
    if len(rows) != DATASET_ROWS:
        raise RuntimeError(f"expected {DATASET_ROWS} rows, received {len(rows)}")
    return rows


def select_rows(rows: list[dict], per_repo: int, seed: str,
                repositories: set[str] | None = None) -> list[dict]:
    """Select a fixed, stratified sample without inspecting retrieval results."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if repositories is None or row["repo"] in repositories:
            grouped[row["repo"]].append(row)
    selected: list[dict] = []
    for repository in sorted(grouped):
        ranked = sorted(grouped[repository], key=lambda row: hashlib.sha256(
            f"{seed}:{row['instance_id']}".encode()).hexdigest())
        selected.extend(ranked[:per_repo])
    return selected


def patch_files(patch: str) -> list[str]:
    """Return normalized destination paths from git diff headers."""
    paths = []
    for _before, after in DIFF_HEADER.findall(patch):
        path = after.strip()
        if path != "/dev/null" and path not in paths:
            paths.append(path)
    return paths


def is_test_path(path: str) -> bool:
    lower = "/" + path.lower().replace("\\", "/")
    name = lower.rsplit("/", 1)[-1]
    return ("/test/" in lower or "/tests/" in lower or name.startswith("test_")
            or name.endswith("_test.py") or name == "conftest.py")


def implementation_files(patch: str) -> list[str]:
    files = patch_files(patch)
    implementation = [path for path in files if not is_test_path(path)]
    return implementation or files


def _run(command: list[str], *, cwd: Path | None = None,
         timeout: int = 600, stdout=None) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=cwd, check=True, timeout=timeout,
                          stdout=stdout, stderr=subprocess.PIPE, text=False)


def ensure_repository(cache: Path, repository: str) -> Path:
    target = cache / repository.replace("/", "__")
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", "--bare", "--filter=blob:none",
              f"https://github.com/{repository}.git", str(target)], timeout=1800)
    return target


def fetch_commit(repository: Path, commit: str) -> None:
    present = subprocess.run(["git", "cat-file", "-e", f"{commit}^{{commit}}"],
                             cwd=repository, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL).returncode == 0
    if not present:
        _run(["git", "fetch", "--depth=1", "origin", commit],
             cwd=repository, timeout=1800)


def export_commit(repository: Path, commit: str, destination: Path) -> None:
    """Export one immutable commit without modifying the cached repository."""
    archive = subprocess.Popen(["git", "archive", "--format=tar", commit],
                               cwd=repository, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    try:
        extracted = subprocess.run(["tar", "-xf", "-", "-C", str(destination)],
                                   stdin=archive.stdout, stderr=subprocess.PIPE,
                                   timeout=600)
        if archive.stdout is not None:
            archive.stdout.close()
        archive_error = archive.stderr.read() if archive.stderr is not None else b""
        archive_code = archive.wait(timeout=600)
    except Exception:
        archive.kill()
        archive.wait()
        raise
    if archive_code != 0 or extracted.returncode != 0:
        raise RuntimeError((archive_error + extracted.stderr).decode(
            "utf-8", "replace"))


def rank_metrics(ranking: list[str], gold: set[str]) -> dict[str, float]:
    positions = [rank for rank, path in enumerate(ranking, 1) if path in gold]
    return {
        "hit1": float(any(rank <= 1 for rank in positions)),
        "hit5": float(any(rank <= 5 for rank in positions)),
        "hit10": float(any(rank <= 10 for rank in positions)),
        "recall5": sum(rank <= 5 for rank in positions) / max(1, len(gold)),
        "recall10": sum(rank <= 10 for rank in positions) / max(1, len(gold)),
        "mrr": 0.0 if not positions else 1.0 / min(positions),
    }


def evaluate_instance(row: dict, cache: Path) -> dict:
    repository = ensure_repository(cache, row["repo"])
    fetch_commit(repository, row["base_commit"])
    with tempfile.TemporaryDirectory(prefix="muninn-swebench-") as directory:
        root = Path(directory)
        export_commit(repository, row["base_commit"], root)
        started = time.perf_counter()
        index = CodeSearchIndex.from_path(str(root), extensions={".py"})
        build_seconds = time.perf_counter() - started
        conditions = {}
        for mode in MODES:
            query_started = time.perf_counter()
            hits = index.search(row["problem_statement"], mode=mode, limit=20)
            query_ms = (time.perf_counter() - query_started) * 1000.0
            conditions[mode] = {
                "ranking": [hit.path for hit in hits],
                "hits": [{"path": hit.path, "score": hit.score,
                          "arms": dict(hit.arms)} for hit in hits],
                "query_ms": round(query_ms, 3),
            }

    gold_all = patch_files(row["patch"])
    gold = implementation_files(row["patch"])
    indexed = set(index.records)
    result = {
        "instance_id": row["instance_id"],
        "repository": row["repo"],
        "base_commit": row["base_commit"],
        "problem_sha256": hashlib.sha256(
            row["problem_statement"].encode()).hexdigest(),
        "gold": gold,
        "gold_all": gold_all,
        "gold_not_indexed": sorted(set(gold) - indexed),
        "documents": len(index.records),
        "graph_edges": sum(len(v) for v in index.graph.values()) // 2,
        "build_seconds": round(build_seconds, 3),
        "conditions": conditions,
    }
    for condition in conditions.values():
        condition["metrics"] = rank_metrics(condition["ranking"], set(gold))
    return result


def aggregate(results: list[dict]) -> dict:
    summary = {}
    for mode in MODES:
        rows = [result["conditions"][mode] for result in results]
        summary[mode] = {
            metric: statistics.fmean(row["metrics"][metric] for row in rows)
            for metric in ("hit1", "hit5", "hit10", "recall5", "recall10", "mrr")
        }
        summary[mode]["query_ms_p50"] = statistics.median(
            row["query_ms"] for row in rows)
    summary["build_seconds_p50"] = statistics.median(
        result["build_seconds"] for result in results)
    return summary


def paired_exact_p(gains: int, losses: int) -> float:
    """Return the two-sided exact sign-test probability for paired changes."""
    from math import comb
    n = gains + losses
    if n == 0:
        return 1.0
    tail = sum(comb(n, value) for value in range(min(gains, losses) + 1))
    return min(1.0, 2.0 * tail / (2 ** n))


def report_markdown(run: dict) -> str:
    results = run["results"]
    summary = run["summary"]
    lines = [
        "# SWE-bench Lite code-retrieval evaluation",
        "",
        (f"The fixed sample contains {len(results)} issues from "
         f"{len(set(r['repository'] for r in results))} repositories. "
         "The issue statement is the query, and non-test files modified by "
         "the accepted patch are the primary relevance labels."),
        (f"The median source-index build took "
         f"{summary['build_seconds_p50']:.2f} seconds."),
        "",
        "| condition | hit@1 | hit@5 | hit@10 | recall@5 | recall@10 | MRR | p50 query |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for mode in MODES:
        values = summary[mode]
        lines.append(
            f"| {mode} | {values['hit1']:.3f} | {values['hit5']:.3f} | "
            f"{values['hit10']:.3f} | {values['recall5']:.3f} | "
            f"{values['recall10']:.3f} | {values['mrr']:.3f} | "
            f"{values['query_ms_p50']:.1f} ms |")

    baseline = "overlap"
    lines += ["", "## Paired changes at hit@10", ""]
    for mode in MODES[1:]:
        wins = losses = ties = 0
        for result in results:
            before = result["conditions"][baseline]["metrics"]["hit10"]
            after = result["conditions"][mode]["metrics"]["hit10"]
            wins += after > before
            losses += after < before
            ties += after == before
        probability = paired_exact_p(wins, losses)
        probability_text = ("<0.0001" if probability < 0.00005
                            else f"{probability:.4f}")
        loss_label = "loss" if losses == 1 else "losses"
        lines.append(f"- {mode}: {wins} gains, {losses} {loss_label}, and "
                     f"{ties} ties; two-sided exact p={probability_text}.")

    missing = [(result["instance_id"], result["gold_not_indexed"])
               for result in results if result["gold_not_indexed"]]
    lines += ["", "## Scope and exclusions", "",
              ("The evaluation tests file localization only. It does not measure "
               "patch correctness, answer generation, learned usage, or "
               "non-Python structural extraction. Every Lite case has one "
               "accepted patch file, so this evaluation does not measure "
               "multi-file recall.")]
    if missing:
        lines.append("The bounded Python index excluded these gold files:")
        for instance, paths in missing:
            lines.append(f"- {instance}: {', '.join(paths)}")
    else:
        lines.append("Every primary gold file was present in the bounded Python index.")
    lines += ["", "## Reproduction", "", "```bash",
              ("python3 eval/code_retrieval/run_swebench.py "
               f"--instances-per-repo {run['parameters']['instances_per_repo']} "
               f"--seed {run['parameters']['seed']}"), "```", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instances-per-repo", type=int, default=2)
    parser.add_argument("--seed", default="muninn-code-retrieval-v1")
    parser.add_argument("--repos", help="comma-separated owner/repository names")
    parser.add_argument("--cache", type=Path, default=HERE / "cache")
    parser.add_argument("--output", type=Path, default=HERE / "runs")
    args = parser.parse_args()
    if args.instances_per_repo <= 0:
        parser.error("--instances-per-repo must be positive")

    repositories = set(args.repos.split(",")) if args.repos else None
    rows = select_rows(dataset_rows(), args.instances_per_repo, args.seed,
                       repositories)
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    for number, row in enumerate(rows, 1):
        print(f"[{number}/{len(rows)}] {row['instance_id']}", flush=True)
        try:
            results.append(evaluate_instance(row, args.cache))
        except Exception as error:
            results.append({
                "instance_id": row["instance_id"], "repository": row["repo"],
                "base_commit": row["base_commit"], "error": str(error),
            })
    failures = [result for result in results if "error" in result]
    completed = [result for result in results if "error" not in result]
    if not completed:
        raise RuntimeError("every evaluation instance failed")

    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=HERE,
                            capture_output=True, text=True, check=True).stdout.strip()
    dataset_revision = _json_url(
        "https://huggingface.co/api/datasets/" + DATASET)["sha"]
    run = {
        "dataset": DATASET,
        "dataset_revision": dataset_revision,
        "dataset_rows": DATASET_ROWS,
        "muninn_commit": commit,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "python": sys.version,
        "parameters": {"instances_per_repo": args.instances_per_repo,
                       "seed": args.seed, "repositories": sorted(repositories or ())},
        "failures": failures,
        "summary": aggregate(completed),
        "results": completed,
    }
    stem = f"swebench-lite-{len(completed)}"
    json_path = args.output / f"{stem}.json"
    markdown_path = args.output / f"{stem}.md"
    json_path.write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
    markdown = report_markdown(run)
    markdown_path.write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"Raw results: {json_path}")
    if failures:
        print(f"Failed instances: {len(failures)}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
