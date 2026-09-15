#!/usr/bin/env python3
"""Verify published LongMemEval tables against their raw result records."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path


HERE = Path(__file__).resolve().parent
RUNS = {
    "pilot": (
        "sample.json",
        "RESULTS-pilot.md",
        {"none": "No memory", "flat": "Flat retrieval", "muninn": "Muninn"},
    ),
    "walk": (
        "walk-sample.json",
        "RESULTS-walk.md",
        {
            "flat": "Flat retrieval",
            "muninn": "Single-cue Muninn walk",
            "muninn-walk": "Multi-cue Muninn walk",
        },
    ),
    "walk-typed": (
        "walk-sample.json",
        "RESULTS-walk-typed.md",
        {"flat": "Flat retrieval", "muninn-walk": "Typed multi-cue Muninn walk"},
    ),
    "embed": (
        "pref-sample.json",
        "RESULTS-embed.md",
        {
            "flat": "Flat retrieval",
            "muninn-walk": "Lexical Muninn walk",
            "muninn-walk-embed": "Muninn walk with semantic seeds",
        },
    ),
    "embed-full": (
        "walk-sample.json",
        "RESULTS-embed-full.md",
        {
            "muninn-walk": "Lexical Muninn walk",
            "muninn-walk-embed": "Muninn walk with semantic seeds",
        },
    ),
}


def question_type(question: dict) -> str:
    return "abstention" if question["abstention"] else question["question_type"]


def verify_run(tag: str, sample_name: str, report_name: str,
               labels: dict[str, str]) -> None:
    sample = json.loads((HERE / sample_name).read_text(encoding="utf-8"))
    questions = {question["question_id"]: question for question in sample}
    results = json.loads(
        (HERE / f"results-{tag}.json").read_text(encoding="utf-8")
    )
    report = " ".join((HERE / report_name).read_text(encoding="utf-8").split())
    by_condition: dict[str, list[dict]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for result in results:
        key = (result["qid"], result["cond"])
        if result["qid"] not in questions or key in seen:
            raise ValueError(f"{tag} has an unknown or duplicate result: {key}")
        seen.add(key)
        by_condition[result["cond"]].append(result)
    if set(by_condition) != set(labels):
        raise ValueError(f"{tag} conditions differ from the report specification")

    for condition, label in labels.items():
        rows = by_condition[condition]
        if {row["qid"] for row in rows} != set(questions):
            raise ValueError(f"{tag}/{condition} does not cover every sample question")
        correct = sum(bool(row["correct"]) for row in rows)
        mean_tokens = round(sum(row["pack_tokens"] for row in rows) / len(rows))
        errors = sum(bool(row.get("error")) for row in rows)
        table_row = (
            f"| {label} | {correct}/{len(rows)} | {mean_tokens:,} | {errors} |"
        )
        if table_row not in (HERE / report_name).read_text(encoding="utf-8"):
            raise ValueError(f"{tag} report omits or changes this result: {table_row}")
        for kind in {question_type(question) for question in sample}:
            ids = {
                qid for qid, question in questions.items()
                if question_type(question) == kind
            }
            selected = [row for row in rows if row["qid"] in ids]
            fraction = f"{sum(bool(row['correct']) for row in selected)}/{len(selected)}"
            if fraction not in report:
                raise ValueError(f"{tag} report omits the {condition}/{kind} result")

    projected_calls = round(len(results) * 2 * 500 / len(sample))
    if f"{projected_calls:,} model calls" not in report:
        raise ValueError(f"{tag} report has an incorrect full-run call projection")


def main() -> int:
    try:
        for tag, specification in RUNS.items():
            verify_run(tag, *specification)
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}")
        return 1
    print("LongMemEval verification passed: five reports match their raw records.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
