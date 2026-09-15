#!/usr/bin/env python3
"""Report persistent-index parity and operating measurements."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import statistics
from pathlib import Path


MODES = ("overlap", "bm25f", "lexical-hybrid", "hybrid", "graph-fusion")


def load_json(path: Path) -> dict:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as source:
            return json.load(source)
    return json.loads(path.read_text())


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def report(run: dict, source: dict, paths: dict[str, Path]) -> str:
    source_by_id = {row["instance_id"]: row for row in source["results"]}
    if set(source_by_id) != {row["instance_id"] for row in run["results"]}:
        if not run["limited_run"]:
            raise RuntimeError("persistent and source runs contain different cases")
    summary = run["summary"]
    rows = len(run["results"])
    all_persistent_parity = all(
        row["conditions"][mode]["matches_in_memory"]
        for row in run["results"] for mode in MODES)
    lexical_source_parity = all(
        row["conditions"][mode]["matches_source_run"]
        for row in run["results"]
        for mode in ("overlap", "bm25f", "lexical-hybrid", "hybrid"))

    graph_current = [row["conditions"]["graph-fusion"]["metrics"]
                     for row in run["results"]]
    graph_source = [source_by_id[row["instance_id"]]["conditions"]
                    ["graph-fusion"]["metrics"] for row in run["results"]]
    graph_changes = {
        metric: statistics.fmean(value[metric] for value in graph_current)
        - statistics.fmean(value[metric] for value in graph_source)
        for metric in ("hit1", "hit10", "mrr")
    }
    storage_ratio = (summary["index_bytes_total"]
                     / max(1, summary["source_bytes_total"]))
    lines = [
        "# Persistent source-index evaluation",
        "",
        (f"The persistent index "
         f"{'reproduced' if all_persistent_parity else 'did not reproduce'} "
         f"all in-memory rankings across {rows} SWE-bench Lite cases. "
         f"The four lexical conditions "
         f"{'matched' if lexical_source_parity else 'did not match'} the "
         "earlier source artifact exactly."),
        "",
        "## Setup",
        "",
        ("Each case was exported at its recorded base commit and scanned once. "
         "The in-memory and SQLite indexes received the same source records. "
         "The SQLite index stored compressed postings and typed edges but not "
         "source bodies."),
        "",
        "## Ranking and latency",
        "",
        "| condition | source parity | hit@1 | hit@10 | MRR | memory p50 | persistent p50 | persistent p95 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for mode in MODES:
        values = summary[mode]
        lines.append(
            f"| {mode} | {values['source_run_parity']}/{rows} | "
            f"{values['hit1']:.3f} | {values['hit10']:.3f} | "
            f"{values['mrr']:.3f} | {values['memory_query_ms_p50']:.1f} ms | "
            f"{values['persistent_query_ms_p50']:.1f} ms | "
            f"{values['persistent_query_ms_p95']:.1f} ms |")
    lines += [
        "",
        "## Build, open, and storage",
        "",
        "| measure | result |",
        "|---|---:|",
        f"| source scan p50 | {summary['scan_seconds_p50']:.3f} s |",
        f"| in-memory build p50 | {summary['memory_build_seconds_p50']:.3f} s |",
        f"| persistent build p50 | {summary['persistent_build_seconds_p50']:.3f} s |",
        f"| persistent reopen p50 | {summary['persistent_open_ms_p50']:.3f} ms |",
        f"| persistent reopen p95 | {summary['persistent_open_ms_p95']:.3f} ms |",
        f"| source bytes | {summary['source_bytes_total']:,} |",
        f"| index bytes | {summary['index_bytes_total']:,} |",
        f"| index-to-source ratio | {storage_ratio:.2f} |",
        "",
        "## Graph change",
        "",
        ("The graph condition is not a parity target because the candidate "
         "adds cross-language module aliases and relation labels. Relative to "
         f"the earlier graph, hit@1 changed by {graph_changes['hit1']:+.3f}, "
         f"hit@10 by {graph_changes['hit10']:+.3f}, and MRR by "
         f"{graph_changes['mrr']:+.3f}. These values are diagnostic; the graph "
         "cannot enter primary ranking without the separate relational test."),
        "",
        "## Decision",
        "",
        (("Retain the persistent index as the operational candidate. Do not "
          "change the public default until the complete case set and the "
          "relational, longitudinal, and agent-level gates pass.")
         if all_persistent_parity and lexical_source_parity else
         ("Do not retain the persistent candidate. Resolve the ranking "
          "difference before further evaluation.")),
        "",
        "## Artifacts",
        "",
    ]
    for name, path in paths.items():
        lines.append(f"- {name}: `{path.name}`, SHA-256 `{digest(path)}`.")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--artifact", type=Path)
    args = parser.parse_args()
    paths = {"persistent source run": args.run,
             "original source run": args.source}
    if args.artifact:
        paths["compressed persistent run"] = args.artifact
    args.output.write_text(report(
        load_json(args.run), load_json(args.source), paths))
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
