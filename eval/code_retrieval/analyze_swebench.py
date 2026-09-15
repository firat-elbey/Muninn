"""Create the permanent analysis for a code-retrieval evaluation run."""

from __future__ import annotations

import argparse
import gzip
import json
import math
import shlex
import statistics
from collections import defaultdict
from pathlib import Path

METRICS = ("hit1", "hit5", "hit10")
MODES = ("overlap", "bm25f", "lexical-hybrid", "hybrid", "graph-fusion")


def load_json(path: Path) -> dict:
    """Read an evaluation run from JSON or its committed gzip form."""
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as source:
            return json.load(source)
    return json.loads(path.read_text(encoding="utf-8"))


def paired_change(results: list[dict], before: str, after: str,
                  metric: str) -> tuple[int, int, int, float]:
    """Return gains, losses, ties, and the two-sided exact sign-test value."""
    gains = losses = ties = 0
    for result in results:
        prior = result["conditions"][before]["metrics"][metric]
        current = result["conditions"][after]["metrics"][metric]
        gains += current > prior
        losses += current < prior
        ties += current == prior
    observations = gains + losses
    if observations == 0:
        probability = 1.0
    else:
        tail = sum(math.comb(observations, value)
                   for value in range(min(gains, losses) + 1))
        probability = min(1.0, 2.0 * tail / (2 ** observations))
    return gains, losses, ties, probability


def probability_text(value: float) -> str:
    return "<0.0001" if value < 0.00005 else f"{value:.4f}"


def change_text(gains: int, losses: int) -> str:
    gain_label = "gain" if gains == 1 else "gains"
    loss_label = "loss" if losses == 1 else "losses"
    return f"{gains} {gain_label} and {losses} {loss_label}"


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * fraction)]


def changed_instances(results: list[dict], before: str, after: str,
                      metric: str) -> tuple[list[str], list[str]]:
    gains: list[str] = []
    losses: list[str] = []
    for result in results:
        prior = result["conditions"][before]["metrics"][metric]
        current = result["conditions"][after]["metrics"][metric]
        if current > prior:
            gains.append(result["instance_id"])
        elif current < prior:
            losses.append(result["instance_id"])
    return gains, losses


def report(run: dict, run_path: Path | None = None) -> str:
    results = run["results"]
    if not results:
        raise ValueError("the run contains no completed instances")
    summary = {}
    for mode in MODES:
        conditions = [result["conditions"][mode] for result in results]
        summary[mode] = {
            metric: statistics.fmean(row["metrics"][metric] for row in conditions)
            for metric in (*METRICS, "mrr")
        }
        summary[mode]["query_ms_p50"] = statistics.median(
            row["query_ms"] for row in conditions)
    by_repository: dict[str, list[dict]] = defaultdict(list)
    for result in results:
        by_repository[result["repository"]].append(result)
    gains, losses, _ties, probability = paired_change(
        results, "overlap", "hybrid", "hit10")
    bm25_gains, bm25_losses, _ties, bm25_probability = paired_change(
        results, "bm25f", "hybrid", "hit10")
    graph_gains1, graph_losses1, _ties, _probability = paired_change(
        results, "hybrid", "graph-fusion", "hit1")
    missing = sum(bool(result.get("gold_not_indexed")) for result in results)
    coverage = (
        f"The count of cases with primary relevance labels outside the bounded "
        f"index was {missing}. The run records {len(run.get('failures', []))} "
        "failed evaluation cases.")
    metric_changes = ", ".join(
        f"{metric.replace('hit', 'hit@')} from "
        f"{summary['overlap'][metric]:.1%} to {summary['hybrid'][metric]:.1%}"
        for metric in METRICS)
    parameters = run.get("parameters", {})
    command = ["python3", "eval/code_retrieval/run_swebench.py",
               "--instances-per-repo", str(parameters.get("instances_per_repo", 2)),
               "--seed", parameters.get("seed", "muninn-code-retrieval-v1")]
    if parameters.get("repositories"):
        command += ["--repos", ",".join(parameters["repositories"])]
    archive = str(run_path) if run_path is not None else "run.json"
    case_label = "case" if len(results) == 1 else "cases"

    lines = [
        "# SWE-bench Lite file-localization evaluation",
        "",
        "## Result",
        "",
        (f"Across {len(results)} completed {case_label}, replacing overlap with "
         f"the selected hybrid changed {metric_changes}."),
        "",
        (f"At hit@10, the hybrid recorded {change_text(gains, losses)} "
         f"against overlap, with two-sided exact p-value {probability_text(probability)}. "
         f"Against BM25F, it recorded {change_text(bm25_gains, bm25_losses)}, "
         f"with two-sided exact p-value {probability_text(bm25_probability)}."),
        "",
        "## Method",
        "",
        (f"The evaluation used {len(results)} issues from SWE-bench Lite "
         f"across {len(by_repository)} Python repositories. The issue statement "
         "was the query, and implementation files modified by the accepted "
         "patch provided relevance labels. No language model generated candidates or graded "
         "results."),
        "",
        (f"The dataset revision was `{run['dataset_revision']}`. The evaluated "
         f"Muninn revision was `{run['muninn_commit']}`. {coverage}"),
        "",
        "## Aggregate results",
        "",
        "| Condition | hit@1 | hit@5 | hit@10 | MRR | Median query |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for mode in MODES:
        values = summary[mode]
        lines.append(
            f"| {mode} | {values['hit1']:.3f} | {values['hit5']:.3f} | "
            f"{values['hit10']:.3f} | {values['mrr']:.3f} | "
            f"{values['query_ms_p50']:.1f} ms |")

    lines += [
        "",
        ("The lexical hybrid fused BM25F and exact rankings at every position. "
         "The selected hybrid preserved the first BM25F result and fused the "
         "remaining ranks. Graph fusion added the two-step Python graph directly "
         "to that competition."),
        "",
        "## Paired analysis",
        "",
        "| Comparison | Metric | Gains | Losses | Ties | Exact p |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for before, after in (("overlap", "hybrid"),
                          ("bm25f", "hybrid"),
                          ("hybrid", "graph-fusion")):
        for metric in METRICS:
            gains, losses, ties, probability = paired_change(
                results, before, after, metric)
            label = metric.replace("hit", "hit@")
            lines.append(
                f"| {after} vs. {before} | {label} | {gains} | {losses} | "
                f"{ties} | {probability_text(probability)} |")

    lines += [
        "",
        (f"Direct graph fusion recorded {change_text(graph_gains1, graph_losses1)} "
         "against the hybrid at hit@1. The paired "
         "results measure file localization and do not establish downstream "
         "task completion."),
        "",
        "## Repository consistency",
        "",
        "| Repository | Cases | overlap hit@10 | BM25F hit@10 | hybrid hit@10 | graph hit@10 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    repository_hybrid_rates = []
    for repository, rows in sorted(by_repository.items()):
        rates = {}
        for mode in ("overlap", "bm25f", "hybrid", "graph-fusion"):
            rates[mode] = statistics.fmean(
                row["conditions"][mode]["metrics"]["hit10"] for row in rows)
        repository_hybrid_rates.append(rates["hybrid"])
        lines.append(
            f"| {repository} | {len(rows)} | {rates['overlap']:.3f} | "
            f"{rates['bm25f']:.3f} | {rates['hybrid']:.3f} | "
            f"{rates['graph-fusion']:.3f} |")
    macro_overlap = statistics.fmean(
        statistics.fmean(row["conditions"]["overlap"]["metrics"]["hit10"]
                         for row in rows)
        for rows in by_repository.values())
    macro_hybrid = statistics.fmean(repository_hybrid_rates)
    improved_repositories = sum(
        statistics.fmean(
            row["conditions"]["hybrid"]["metrics"]["hit10"] for row in rows)
        > statistics.fmean(
            row["conditions"]["overlap"]["metrics"]["hit10"] for row in rows)
        for rows in by_repository.values())
    worsened_repositories = sum(
        statistics.fmean(
            row["conditions"]["hybrid"]["metrics"]["hit10"] for row in rows)
        < statistics.fmean(
            row["conditions"]["overlap"]["metrics"]["hit10"] for row in rows)
        for rows in by_repository.values())
    tied_repositories = (len(by_repository) - improved_repositories
                         - worsened_repositories)
    lines += [
        "",
        (f"The repository-macro hit@10 changed from {macro_overlap:.3f} to "
         f"{macro_hybrid:.3f}. Against overlap, the repository counts were "
         f"{improved_repositories} improved, {worsened_repositories} worsened, "
         f"and {tied_repositories} tied."),
        "",
        "## Operational profile",
        "",
    ]
    documents = [result["documents"] for result in results]
    edges = [result["graph_edges"] for result in results]
    builds = [result["build_seconds"] for result in results]
    hybrid_queries = [result["conditions"]["hybrid"]["query_ms"]
                      for result in results]
    lines += [
        (f"The median checkout contained {statistics.median(documents):,.0f} "
         f"indexed Python files and {statistics.median(edges):,.0f} undirected "
         "graph edges. The in-memory index took "
         f"{statistics.median(builds):.2f} seconds at the median and "
         f"{percentile(builds, 0.95):.2f} seconds at the 95th percentile. "
         f"Hybrid queries took {statistics.median(hybrid_queries):.1f} ms at "
         f"the median and {percentile(hybrid_queries, 0.95):.1f} ms at the "
         "95th percentile."),
        "",
        ("These build times measure repeated in-memory construction. They do "
         "not establish persistent-index storage or update cost."),
        "",
        "## Failure analysis",
        "",
    ]
    _hybrid_gains, hybrid_losses = changed_instances(
        results, "overlap", "hybrid", "hit10")
    graph_gains, graph_losses = changed_instances(
        results, "hybrid", "graph-fusion", "hit10")
    for label, instances in (("Hybrid hit@10 losses against overlap", hybrid_losses),
                             ("Graph fusion hit@10 gains", graph_gains),
                             ("Graph fusion hit@10 losses", graph_losses)):
        if instances:
            lines += [label + " occurred in " + ", ".join(f"`{name}`" for name in instances)
                      + ".", ""]
        else:
            lines += [f"No {label[0].lower() + label[1:]} were observed.", ""]
    lines += [
        "## Limits and next evaluations",
        "",
        ("This evaluation measures file localization in Python. It does "
         "not measure dependency reconstruction, symbol or excerpt localization, "
         "patch correctness, answer quality, personal learning, persistent "
         "index updates, or another language. Accepted patch files are useful "
         "but imperfect relevance labels."),
        "",
        ("The next retrieval evaluation should test relational and multi-file "
         "queries so that typed graph edges can be assessed directly. A "
         "longitudinal evaluation should train usage only on earlier sessions "
         "and test later paraphrased tasks. An agent-level evaluation should "
         "hold the model, repository revision, context budget, and tool budget "
         "constant while comparing overlap, BM25F, and the selected hybrid."),
        "",
        "## Reproduction",
        "",
        "```bash",
        shlex.join(command),
        shlex.join(["python3", "eval/code_retrieval/analyze_swebench.py", archive,
                    "--output", "report.md"]),
        "```",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    run = load_json(args.run)
    if run.get("failures"):
        parser.error("the run contains failed instances")
    if not run.get("results"):
        parser.error("the run contains no completed instances")
    rendered = report(run, args.run)
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
