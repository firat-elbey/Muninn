"""Render the November public benchmark report from its recorded measurements."""

import argparse
import gzip
import json
from pathlib import Path
from statistics import median

HERE = Path(__file__).resolve().parent
DATA = HERE / "public-benchmarks-2026-11.json.gz"
REPORT = HERE / "PUBLIC-BENCHMARKS-2026-11.md"
ARMS = ("none", "flat", "muninn", "muninn-walk", "muninn-walk-embed")


def report():
    with gzip.open(DATA, "rt", encoding="utf-8") as source:
        run = json.load(source)
    model = run["longmemeval"]
    answers = {}
    for row in model["rows"]:
        for arm in row["conditions"]:
            key = (row["qid"], arm)
            if key in answers or arm not in ARMS:
                raise ValueError("Duplicate or unknown scoring condition")
            answers[key] = row["correct"]

    lines = [
        "# November 2026 public benchmark results", "",
        "## LongMemEval", "",
        f"Each question uses a {model['budget']}-token estimated budget and up to {model['k']} notes. "
        f"{model['model']} answered and judged with tools disabled and a structured yes/no verdict. "
        f"The run recorded {len(answers)} question-condition scores and {model['provider_errors']} provider errors. "
        "Overlapping manifests reuse the same judgment for an identical question and pack. "
        "The preference manifest is a subset of the multi-session and preference manifest.", "",
        "| Manifest | Questions | No memory | Flat | Muninn | Walk | Walk with embeddings |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    manifests = [
        ("Pilot", "sample", ARMS[:3]),
        ("Multi-session and preferences", "walk-sample", ARMS[1:]),
        ("Preferences", "pref-sample", ("flat", "muninn-walk", "muninn-walk-embed")),
    ]
    for label, stem, measured in manifests:
        manifest = json.loads((HERE / "longmemeval" / (stem + ".json")).read_text(encoding="utf-8"))
        cells = [str(sum(answers[(q["question_id"], arm)] for q in manifest))
                 if arm in measured else "" for arm in ARMS]
        lines.append(f"| {label} | {len(manifest)} | " + " | ".join(cells) + " |")

    loader = run["longmemeval_loading"]
    rows = loader["rows"]
    before = median(row["before_ms"] for row in rows) / 1000
    after = median(row["after_ms"] for row in rows) / 1000
    packs = sum(len(row["pack_sha256"]) for row in rows)
    swe = run["swebench_lite"]
    hits = swe["hybrid_hits"]
    dep = run["dependeval"]
    f1 = dep["edge_f1"]
    pair = dep["hybrid_vs_graphify"]
    lines.extend([
        "", f"The embedding condition used {model['embedding']} with an {model['embedding_context']:,}-token window.",
        "", "### Bundle opening", "",
        f"Across the {len(rows)} public bundles and {sum(row['notes'] for row in rows):,} notes, "
        f"median opening time changed from {before:.3f} s to {after:.3f} s with {loader['workers']} workers. "
        f"All {len(rows)} link graphs and {packs} rendered packs matched the control. "
        "These timings exclude answering and judging.", "",
        "## SWE-bench Lite", "",
        f"The hybrid returned an accepted file at rank one on {hits['hit1']}/{swe['cases']} cases, "
        f"in the top five on {hits['hit5']}/{swe['cases']}, and in the top ten on {hits['hit10']}/{swe['cases']}. "
        "The labels measure source-file localization.", "",
        "## DependEval", "",
        f"Across {dep['cases']} cases, native extraction reached {f1['muninn-tree']:.4f} macro edge F1, "
        f"the hybrid reached {f1['muninn-hybrid']:.4f}, and standalone Graphify reached {f1['graphify']:.4f}. "
        f"The hybrid gained on {pair['gains']} cases, lost on {pair['losses']} and tied on {pair['ties']} "
        f"(paired sign-test p={pair['p']:.3f}). The historical confirmation criterion failed on this replay. "
        "The [registered report](competitive/RESULTS-dependeval-series5-confirmation.md) specifies the fold "
        "and explains the upstream redistribution limit.", "",
        "The [measurement file](public-benchmarks-2026-11.json.gz) records dataset fingerprints, "
        "LongMemEval scoring decisions and pack digests, loader measurements and the two code benchmark aggregates. "
        "Rebuild this report with `python eval/public_benchmarks.py`.", "",
    ])
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=REPORT)
    args = parser.parse_args()
    args.output.write_text(report(), encoding="utf-8")
