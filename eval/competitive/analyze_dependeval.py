#!/usr/bin/env python3
"""Analyze the preregistered DependEval comparison."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import random
import statistics
from pathlib import Path


BOOTSTRAP_REPETITIONS = 10_000
BOOTSTRAP_SEED = 5_105


def load_json(path: Path) -> dict:
    opener = gzip.open if path.suffix == ".gz" else path.open
    with opener(path, "rt") if path.suffix == ".gz" else opener("r") as source:
        return json.load(source)


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def exact_sign_probability(gains: int, losses: int) -> float:
    total = gains + losses
    if total == 0:
        return 1.0
    smaller = min(gains, losses)
    tail = sum(math.comb(total, value) for value in range(smaller + 1))
    return min(1.0, 2 * tail / (2 ** total))


def paired(candidate: list[dict], baseline: list[dict]) -> dict:
    differences = [
        left["metrics"]["edge_f1"] - right["metrics"]["edge_f1"]
        for left, right in zip(candidate, baseline)
    ]
    gains = sum(value > 1e-12 for value in differences)
    losses = sum(value < -1e-12 for value in differences)
    generator = random.Random(BOOTSTRAP_SEED)
    estimates = sorted(statistics.fmean(
        differences[generator.randrange(len(differences))]
        for _item in differences) for _repeat in range(BOOTSTRAP_REPETITIONS))
    return {
        "difference": statistics.fmean(differences),
        "gains": gains,
        "losses": losses,
        "ties": len(differences) - gains - losses,
        "p": exact_sign_probability(gains, losses),
        "low": estimates[int(0.025 * len(estimates))],
        "high": estimates[int(0.975 * len(estimates)) - 1],
    }


def verify_alignment(runs: list[dict]) -> None:
    reference = runs[0]
    keys = ("dataset_revision", "dataset_sha256", "split_seed", "folds",
            "case_count")
    for run in runs[1:]:
        for key in keys:
            if run[key] != reference[key]:
                raise ValueError(f"run mismatch in {key}")
    identities = [row["case_id"] for row in reference["results"]]
    gold = [row["gold_edges"] for row in reference["results"]]
    for run in runs[1:]:
        if [row["case_id"] for row in run["results"]] != identities:
            raise ValueError("case identities do not align")
        if [row["gold_edges"] for row in run["results"]] != gold:
            raise ValueError("gold dependency edges do not align")


def verify_repeat(candidate: dict, repeat: dict) -> None:
    verify_alignment([candidate, repeat])
    keys = ("case_id", "language", "fold", "gold_chain_length",
            "source_paths_sha256", "predicted_edges", "gold_edges", "metrics",
            "operation")
    for left, right in zip(candidate["results"], repeat["results"]):
        for key in keys:
            if left[key] != right[key]:
                raise ValueError(
                    f"repeat differs for {left['case_id']} in {key}")
    if candidate["summary"] != repeat["summary"]:
        raise ValueError("repeat summary differs")


def report(candidate: dict, muninn: dict, graphify: dict,
           paths: dict[str, Path], phase: str,
           repeat: dict | None = None) -> str:
    verify_alignment([candidate, muninn, graphify])
    if repeat is not None:
        verify_repeat(candidate, repeat)
    candidate_rows = candidate["results"]
    comparisons = {
        "Muninn tree": paired(candidate_rows, muninn["results"]),
        "Graphify": paired(candidate_rows, graphify["results"]),
    }
    languages = candidate["summary"]["by_language"]
    regressions = {
        language: (values["edge_f1"]
                   - muninn["summary"]["by_language"][language]["edge_f1"])
        for language, values in languages.items()
    }
    passes_comparisons = all(
        values["difference"] > 0
        and (values["p"] < 0.05 or values["low"] > 0)
        for values in comparisons.values())
    passes_languages = min(regressions.values()) >= -0.02
    passed = passes_comparisons and passes_languages
    title_phase = "development" if phase == "development" else "confirmation"
    decision = (
        "The candidate passed development and may be frozen for one confirmation run."
        if phase == "development" and passed else
        "The candidate passed confirmation on this named dependency task."
        if phase == "confirmation" and passed else
        "The candidate failed the preregistered gate and must not replace the current method."
    )
    lines = [
        f"# DependEval dependency recognition, series 5 {title_phase}",
        "",
        (f"The hybrid candidate reached "
         f"{candidate['summary']['all']['edge_f1']:.3f} macro edge F1, "
         f"compared with {muninn['summary']['all']['edge_f1']:.3f} for "
         "Muninn tree extraction and "
         f"{graphify['summary']['all']['edge_f1']:.3f} for Graphify. "
         f"{decision}"),
        "",
        "## Primary result",
        "",
        "| method | edge F1 | precision | recall | exact graph | node F1 | p50 ms | p95 ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, run in (("Muninn hybrid", candidate), ("Muninn tree", muninn),
                      ("Graphify", graphify)):
        values = run["summary"]["all"]
        latency = run["latency_ms"]
        lines.append(
            f"| {name} | {values['edge_f1']:.3f} | "
            f"{values['edge_precision']:.3f} | {values['edge_recall']:.3f} | "
            f"{values['exact_chain']:.3f} | {values['node_f1']:.3f} | "
            f"{latency['p50']:.1f} | {latency['p95']:.1f} |")
    lines += [
        "",
        "## Paired comparisons",
        "",
        "| candidate minus | difference | gains | losses | ties | exact p | 95% paired bootstrap interval |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, values in comparisons.items():
        lines.append(
            f"| {name} | {values['difference']:+.3f} | {values['gains']} | "
            f"{values['losses']} | {values['ties']} | {values['p']:.4g} | "
            f"[{values['low']:+.3f}, {values['high']:+.3f}] |")
    lines += [
        "",
        "## Language results",
        "",
        "| language | hybrid | Muninn tree | Graphify | hybrid minus Muninn |",
        "|---|---:|---:|---:|---:|",
    ]
    for language in sorted(languages):
        lines.append(
            f"| {language} | {languages[language]['edge_f1']:.3f} | "
            f"{muninn['summary']['by_language'][language]['edge_f1']:.3f} | "
            f"{graphify['summary']['by_language'][language]['edge_f1']:.3f} | "
            f"{regressions[language]:+.3f} |")
    lines += [
        "",
        "## Scope and decision",
        "",
        (f"The comparison contains {candidate['case_count']} cases from folds "
         f"{candidate['folds']} and eight languages. Every system received "
         "the same named files and source text. Each run recorded zero network "
         "calls and zero model calls."),
        "",
        ("A second candidate run reproduced every predicted edge, metric, and "
         "provider decision." if repeat is not None else
         "Confirmation ran once, as preregistered." if phase == "confirmation"
         else "No repeat run was supplied to this analysis."),
        "",
        ("The hybrid uses Graphify for C, C++, C#, PHP, and TypeScript source "
         "files. It uses Muninn for Java, JavaScript, and Python. Graphify is "
         "optional; an unavailable provider returns the internal graph and "
         "records the error."),
        "",
        decision,
        "",
        "This result measures directed dependency reconstruction. It does not "
        "establish semantic retrieval, longitudinal learning, or agent task "
        "success.",
        "",
        "## Reproduction record",
        "",
        f"- DependEval revision: `{candidate['dataset_revision']}`.",
        f"- Muninn revision: `{candidate['muninn_revision']}`.",
    ]
    for name, path in paths.items():
        lines.append(
            f"- {name.replace('_', ' ').title()}: `{path.name}`, SHA-256 "
            f"`{digest(path)}`.")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--muninn", type=Path, required=True)
    parser.add_argument("--graphify", type=Path, required=True)
    parser.add_argument("--phase", choices=("development", "confirmation"),
                        required=True)
    parser.add_argument("--repeat", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    paths = {
        "candidate": args.candidate,
        "muninn": args.muninn,
        "graphify": args.graphify,
    }
    repeat = load_json(args.repeat) if args.repeat else None
    if args.repeat:
        paths["repeat"] = args.repeat
    args.output.write_text(report(
        load_json(args.candidate), load_json(args.muninn),
        load_json(args.graphify), paths, args.phase, repeat))
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
