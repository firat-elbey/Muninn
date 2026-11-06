"""Verify that evaluation analysis describes the supplied run."""

import gzip
import importlib.util
import json
import statistics
import sys
from pathlib import Path

import pytest


def load_analyzer():
    path = Path(__file__).resolve().parents[1] / "eval/code_retrieval/analyze_swebench.py"
    spec = importlib.util.spec_from_file_location("swebench_analysis", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODES = ("overlap", "bm25f", "lexical-hybrid", "hybrid", "graph-fusion")


def make_run(cases):
    results = []
    for number, (repository, overlap, hybrid, graph) in enumerate(cases):
        conditions = {}
        for mode, hit in zip(MODES, (overlap, hybrid, hybrid, hybrid, graph)):
            conditions[mode] = {
                "metrics": {"hit1": hit, "hit5": hit, "hit10": hit, "mrr": hit},
                "query_ms": 2.0,
                "ranking": ["answer.py"] if hit else ["other.py"],
            }
        results.append({
            "instance_id": f"case-{number}", "repository": repository,
            "gold": ["answer.py"], "gold_not_indexed": [],
            "documents": 10, "graph_edges": 8, "build_seconds": 0.1,
            "conditions": conditions,
        })
    summary = {
        mode: {
            **{metric: statistics.fmean(row["conditions"][mode]["metrics"][metric]
                                       for row in results)
               for metric in ("hit1", "hit5", "hit10", "mrr")},
            "query_ms_p50": 2.0,
        }
        for mode in MODES
    } if results else {}
    return {
        "results": results, "summary": summary, "failures": [],
        "dataset_revision": "dataset-test", "muninn_commit": "commit-test",
        "parameters": {"instances_per_repo": 2, "seed": "test-seed"},
    }


def test_report_accepts_zero_losses_and_uses_actual_case_count():
    analyzer = load_analyzer()
    text = analyzer.report(make_run([("repo/a", 0.0, 1.0, 1.0)]))
    assert "1 completed case" in text
    assert "0.0% to 100.0%" in text
    assert "No hybrid hit@10 losses" in text
    assert "300-case" not in text
    assert "Matplotlib" not in text


def test_report_counts_repository_losses_separately_from_ties():
    analyzer = load_analyzer()
    text = analyzer.report(make_run([
        ("repo/a", 1.0, 0.0, 0.0),
        ("repo/b", 1.0, 0.0, 1.0),
        ("repo/c", 0.0, 1.0, 0.0),
        ("repo/d", 1.0, 1.0, 1.0),
    ]))
    assert "1 improved, 2 worsened, and 1 tied" in text
    assert "changed from 0.750 to 0.500" in text
    assert "Hybrid hit@10 losses against overlap occurred in `case-0`, `case-1`." in text
    assert "Graph fusion hit@10 gains occurred in `case-1`." in text
    assert "Graph fusion hit@10 losses occurred in `case-2`." in text
    assert "76 cases" not in text
    assert "outside the first 20" not in text


def test_report_describes_missing_labels_and_run_reproduction():
    analyzer = load_analyzer()
    run = make_run([("repo/a", 1.0, 0.0, 0.0)])
    run["results"][0]["gold_not_indexed"] = ["answer.py"]
    run["parameters"]["repositories"] = ["repo/a"]
    text = analyzer.report(run)
    assert "primary relevance labels outside the bounded index was 1" in text
    assert "--instances-per-repo 2 --seed test-seed --repos repo/a" in text
    assert "swebench-lite-300.json" not in text
    assert "Every relevance label was present" not in text


def test_report_rejects_empty_results():
    with pytest.raises(ValueError, match="no completed instances"):
        load_analyzer().report(make_run([]))


def test_report_recomputes_summary_from_case_results():
    run = make_run([("repo/a", 0.0, 1.0, 1.0)])
    run["summary"]["hybrid"].update(hit1=0.0, hit10=0.0, query_ms_p50=999.0)
    text = load_analyzer().report(run)
    assert "hit@1 from 0.0% to 100.0%" in text
    assert "| hybrid | 1.000 | 1.000 | 1.000 | 1.000 | 2.0 ms |" in text


def test_cli_reads_gzip_and_records_actual_input_path(tmp_path, monkeypatch):
    analyzer = load_analyzer()
    source = tmp_path / "sample results.json.gz"
    output = tmp_path / "report.md"
    with gzip.open(source, "wt", encoding="utf-8") as stream:
        json.dump(make_run([("repo/a", 0.0, 1.0, 1.0)]), stream)
    monkeypatch.setattr(sys, "argv", ["analyze", str(source), "--output", str(output)])
    analyzer.main()
    text = output.read_text(encoding="utf-8")
    assert f"'{source}' --output report.md" in text
    assert "1 gain and 0 losses" in text


@pytest.mark.parametrize("failed", [False, True])
def test_cli_rejects_unusable_run(tmp_path, monkeypatch, capsys, failed):
    analyzer = load_analyzer()
    source = tmp_path / "run.json"
    run = make_run([])
    if failed:
        run["failures"] = [{"instance_id": "failed-case", "error": "missing checkout"}]
    source.write_text(json.dumps(run), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["analyze", str(source)])
    with pytest.raises(SystemExit) as error:
        analyzer.main()
    assert error.value.code == 2
    expected = "failed instances" if failed else "no completed instances"
    assert expected in capsys.readouterr().err
