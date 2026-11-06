from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_evaluator():
    spec = importlib.util.spec_from_file_location(
        "context_efficiency", ROOT / "eval" / "context_efficiency.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_frozen_labels_never_enter_worker_inputs():
    evaluator = load_evaluator()
    request, labels = evaluator.load_fixture()

    assert len(request["questions"]) == 22
    assert len(labels) == 22
    assert request["budgets"] == [200, 400, 900]
    assert all(set(question) == {"id", "corpus", "query"}
               for question in request["questions"])
    assert len(request["corpora"]["aurora"]) == 40
    assert all("required" not in note and "expect" not in note
               for notes in request["corpora"].values() for note in notes)


def test_complete_evidence_requires_every_fact_and_qualification():
    evaluator = load_evaluator()
    row = {"id": "case", "text": "The limit is 8 attempts.",
           "tokens": 10, "budget": 200, "corpus": "qualification"}
    label = {"required": [["8 attempts"], ["2 attempts"], ["during recovery"]]}

    partial = evaluator.score_row(row, label)
    complete = evaluator.score_row(
        {**row, "text": "Use 8 attempts. Use 2 attempts during recovery."}, label)

    assert partial["matched_groups"] == 1
    assert partial["complete_evidence"] is False
    assert complete["complete_evidence"] is True


def test_no_match_is_not_scored_as_complete_evidence():
    evaluator = load_evaluator()
    row = {"id": "case", "text": "No matching notes.", "tokens": 9,
           "budget": 200, "corpus": "no-match"}

    scored = evaluator.score_row(row, {"required": [], "no_match": True})

    assert scored["complete_evidence"] is False
    assert scored["answerable"] is False
    assert scored["no_match_disclosed"] is True


def test_budget_and_supersession_failures_are_retained():
    evaluator = load_evaluator()
    row = {"id": "case", "text": "Use 8271, previously 1113.",
           "tokens": 201, "budget": 200, "corpus": "frontmatter",
           "utf8_bytes": 500, "latency_ms": 1}

    scored = evaluator.score_row(row, {"required": [["8271"]], "forbidden": ["1113"]})
    summary = evaluator.aggregate([scored])

    assert summary["cases"] == 1
    assert summary["budget_violations"] == 1
    assert summary["forbidden_span_cases"] == 1
    assert scored["complete_evidence"] is False


def test_paired_comparison_rejects_different_cases():
    evaluator = load_evaluator()
    before = [{"id": "a", "budget": 200}]
    after = [{"id": "b", "budget": 200}]

    with pytest.raises(ValueError, match="different cases"):
        evaluator.compare(before, after)


def test_worker_builds_isolated_public_bundle_and_returns_all_budgets():
    evaluator = load_evaluator()
    request = {"corpora": {"test": [{"path": "note.md", "title": "Printer",
                                      "body": "The printer port is 8721."}]},
               "questions": [{"id": "test", "corpus": "test", "query": "printer port"}],
               "budgets": [200, 400], "k": 5}

    rows = evaluator.run_variant(request, ROOT / "src", False)

    assert [(row["id"], row["budget"]) for row in rows] == [("test", 200), ("test", 400)]
    assert all("8721" in row["text"] for row in rows)


def test_explicit_tokenizer_requires_its_optional_dependency(monkeypatch):
    evaluator = load_evaluator()
    monkeypatch.setitem(sys.modules, "tiktoken", None)

    with pytest.raises(RuntimeError, match="optional tiktoken"):
        evaluator.load_tokenizer("cl100k_base")


def test_tokenizer_counts_include_headers_and_measure_a_different_budget():
    evaluator = load_evaluator()

    class Tokenizer:
        def encode(self, text, disallowed_special):
            assert disallowed_special == ()
            return text.split()

    row = {"id": "case", "text": "Header body contains four more words",
           "tokens": 2, "budget": 3, "corpus": "test",
           "utf8_bytes": 30, "latency_ms": 1}
    rows = [evaluator.score_row(row, {"required": [["body"]]})]
    evaluator.measure_tokens(rows, Tokenizer())
    summary = evaluator.aggregate(rows)

    assert summary["tokens"] == 2
    assert summary["budget_violations"] == 0
    assert summary["tokenizer_tokens"] == 6
    assert summary["tokenizer_budget_exceedances"] == 1
    after = [dict(rows[0], tokenizer_tokens=4)]
    paired = evaluator.compare(rows, after)
    assert paired["tokenizer_reduction"] == pytest.approx(2 / 6)
