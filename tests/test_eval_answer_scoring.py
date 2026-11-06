"""Model failures and abstentions must remain distinct from successful answers."""

import importlib.util
from pathlib import Path
import subprocess

import pytest


def runner(path):
    source = Path(__file__).resolve().parents[1] / "eval" / path
    spec = importlib.util.spec_from_file_location("eval_runner", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_aurora_keeps_errors_out_of_scoring(monkeypatch):
    module = runner("run_eval.py")

    def timeout(*args):
        raise subprocess.TimeoutExpired(["claude", "expected value"], 5)

    monkeypatch.setattr(module, "ask", timeout)
    job = module.answer_job({"pack": "expected value", "q": {"q": "Which?", "expect": ["value"]}})
    assert job["error"] and job["answer"] == ""
    assert not job["correct"] and not job["unknown"]


def test_aurora_scores_explicit_abstention(monkeypatch):
    module = runner("run_eval.py")
    monkeypatch.setattr(module, "ask", lambda *args: "UNKNOWN. The cue mentions cobalt.")
    job = module.answer_job({"pack": "", "q": {"q": "Which?", "expect": ["cobalt"]}})
    assert job["unknown"] and not job["correct"]


def test_runner_rejects_failed_provider_stdout(monkeypatch):
    module = runner("run_eval.py")
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess([], 1, stdout="provider error", stderr=""))
    with pytest.raises(RuntimeError):
        module.ask("evidence", "Which?")


def test_longmemeval_requires_unambiguous_judgment(monkeypatch):
    module = runner("longmemeval/run.py")
    monkeypatch.setattr(module, "claude", lambda *args, **kwargs: "No. Yesterday's value is incorrect.")
    q = {"question_type": "knowledge-update", "question": "Which?", "answer": "x", "abstention": False}
    with pytest.raises(ValueError):
        module.judge(q, "y")


@pytest.mark.parametrize("as_events", [True, False])
def test_longmemeval_uses_the_structured_verdict(monkeypatch, as_events):
    import json
    module = runner("longmemeval/run.py")
    final = {"type": "result", "is_error": False, "result": "Yesterday",
             "structured_output": {"verdict": "no"}}

    def provider(command, **kwargs):
        assert "--json-schema" in command
        body = [final] if as_events else final
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(body), stderr="")

    monkeypatch.setattr(module.subprocess, "run", provider)
    q = {"question_type": "knowledge-update", "question": "Which?", "answer": "x", "abstention": False}
    assert module.judge(q, "y") == (False, "no")
