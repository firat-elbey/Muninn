from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_audit_module():
    path = ROOT / "tools" / "check_repository.py"
    spec = importlib.util.spec_from_file_location("check_repository", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("prefix", "suffix"),
    [
        ("ghp_", "a" * 36),
        ("sk-proj-", "a" * 36),
        ("AKIA", "A" * 16),
        ("-----BEGIN ", "PRIVATE KEY-----"),
    ],
)
def test_sensitive_text_is_rejected(prefix: str, suffix: str) -> None:
    audit = load_audit_module()
    findings: list[str] = []

    audit.check_sensitive_text(Path("example.txt"), prefix + suffix, findings)

    assert findings == ["possible credential in example.txt"]


def test_plain_examples_do_not_trigger_secret_check() -> None:
    audit = load_audit_module()
    findings: list[str] = []

    audit.check_sensitive_text(
        Path("README.md"),
        "Set MUNINN_ENRICH_CMD and keep API credentials outside the repository.",
        findings,
    )

    assert findings == []


def test_machine_local_user_path_is_rejected() -> None:
    audit = load_audit_module()
    findings: list[str] = []

    audit.check_sensitive_text(
        Path("README.md"), "/Users/" + "example/private/file.txt", findings
    )

    assert findings == ["machine-local user path in README.md"]


def test_em_dash_is_rejected() -> None:
    audit = load_audit_module()
    findings: list[str] = []

    audit.check_prose_characters(
        Path("README.md"), "claim" + chr(0x2014) + "qualification", findings
    )

    assert findings == ["em dash in README.md"]


def test_excluded_language_is_rejected() -> None:
    audit = load_audit_module()
    findings: list[str] = []

    audit.check_prose_characters(
        Path("README.md"), "We should circle " + "back tomorrow.", findings
    )

    assert findings == ["excluded informal or stock language in README.md"]


def test_extractor_extra_preserves_the_network_free_contract() -> None:
    audit = load_audit_module()
    findings: list[str] = []
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    audit.check_extractor_contract(text, findings)

    assert findings == []
    assert 'tree-sitter-language-pack>=0.13,<1.0' in text


def test_public_contract_is_satisfied_by_the_repository() -> None:
    audit = load_audit_module()
    findings: list[str] = []

    audit.check_public_contract(audit.tracked_paths(), findings)

    assert findings == []


def mit_contract(monkeypatch, tmp_path):
    audit = load_audit_module()
    for name in ("pyproject.toml", "CITATION.cff", "NOTICE"):
        shutil.copyfile(ROOT / name, tmp_path / name)
    (tmp_path / "LICENSES").mkdir()
    upstream = ROOT / "LICENSES/LongMemEval-MIT.txt"
    shutil.copyfile(upstream, tmp_path / "LICENSES/LongMemEval-MIT.txt")
    (tmp_path / "LICENSE").write_text(
        upstream.read_text().replace("Copyright (c) 2024 Di Wu", "Copyright (c) 2026 Firat Elbey"),
        encoding="utf-8",
    )
    for name in ("pyproject.toml", "CITATION.cff"):
        path = tmp_path / name
        path.write_text(path.read_text().replace("Apache-2.0", "MIT"), encoding="utf-8")
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    return audit


def test_original_license_is_mit_without_changing_third_party_terms() -> None:
    upstream = (ROOT / "LICENSES/LongMemEval-MIT.txt").read_bytes()
    assert hashlib.sha256(upstream).hexdigest() == (
        "d3c4b9aa54759df6ded337978a6f3b55b75615e5e4525c3b82d7e2627d4b9732"
    )
    expected = upstream.replace(b"Copyright (c) 2024 Di Wu", b"Copyright (c) 2026 Firat Elbey")
    assert (ROOT / "LICENSE").read_bytes() == expected
    assert 'license = "MIT"' in (ROOT / "pyproject.toml").read_text()
    assert "license: MIT\n" in (ROOT / "CITATION.cff").read_text()


def test_public_contract_accepts_canonical_mit(monkeypatch, tmp_path):
    audit = mit_contract(monkeypatch, tmp_path)
    findings = []

    audit.check_public_contract(list(audit.REQUIRED_PUBLIC_PATHS), findings)

    assert findings == []


@pytest.mark.parametrize("name", ["LICENSE", "LICENSES/LongMemEval-MIT.txt"])
def test_public_contract_rejects_changed_license_text(monkeypatch, tmp_path, name):
    audit = mit_contract(monkeypatch, tmp_path)
    (tmp_path / name).write_text("Changed license terms.\n", encoding="utf-8")
    findings = []

    audit.check_public_contract(list(audit.REQUIRED_PUBLIC_PATHS), findings)

    assert any("MIT license" in finding for finding in findings)


@pytest.mark.parametrize("name", ["pyproject.toml", "CITATION.cff"])
def test_public_contract_rejects_stale_license_metadata(monkeypatch, tmp_path, name):
    audit = mit_contract(monkeypatch, tmp_path)
    path = tmp_path / name
    path.write_text(path.read_text().replace("MIT", "Apache-2.0"), encoding="utf-8")
    findings = []

    audit.check_public_contract(list(audit.REQUIRED_PUBLIC_PATHS), findings)

    assert f"{name} does not declare MIT" in findings


def context_artifact(monkeypatch, tmp_path):
    audit = load_audit_module()
    evaluator_path = ROOT / "eval" / "context_efficiency.py"
    spec = importlib.util.spec_from_file_location("context_efficiency_audit", evaluator_path)
    assert spec is not None and spec.loader is not None
    evaluator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(evaluator)
    name = "RESULTS-context-efficiency-2026-09"
    run = json.loads((ROOT / "eval" / f"{name}.json").read_text(encoding="utf-8"))
    directory = tmp_path / "eval"
    directory.mkdir()
    artifact = directory / f"{name}.json"
    report = directory / f"{name}.md"
    artifact.write_text(json.dumps(run), encoding="utf-8")
    report.write_text(evaluator.report(run), encoding="utf-8")
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    monkeypatch.setattr(audit, "load_context_evaluator", lambda: evaluator, raising=False)
    return audit, evaluator, run, artifact, report


def test_context_artifact_is_audited_without_optional_tokenizer(monkeypatch, tmp_path):
    audit, evaluator, _run, _artifact, _report = context_artifact(monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "tiktoken", None)

    def reject_tokenizer(_name):
        raise AssertionError("The repository audit must not load a tokenizer.")

    monkeypatch.setattr(evaluator, "load_tokenizer", reject_tokenizer)
    findings = []

    assert audit.check_context_efficiency(findings) == 1
    assert findings == []


def test_context_artifact_rejects_corrupt_comparison(monkeypatch, tmp_path):
    audit, evaluator, run, artifact, report = context_artifact(monkeypatch, tmp_path)
    comparison = next(iter(run["comparisons"].values()))
    comparison["weighted_token_reduction"] = 0.99
    artifact.write_text(json.dumps(run), encoding="utf-8")
    report.write_text(evaluator.report(run), encoding="utf-8")
    findings = []

    audit.check_context_efficiency(findings)

    assert any("comparison" in finding for finding in findings)


def test_context_artifact_rejects_corrupt_score(monkeypatch, tmp_path):
    audit, evaluator, run, artifact, report = context_artifact(monkeypatch, tmp_path)
    row = run["results"]["current-compact"][0]
    row["complete_evidence"] = not row["complete_evidence"]
    artifact.write_text(json.dumps(run), encoding="utf-8")
    report.write_text(evaluator.report(run), encoding="utf-8")
    findings = []

    audit.check_context_efficiency(findings)

    assert any("score" in finding for finding in findings)


def test_context_artifact_rejects_corrupt_report(monkeypatch, tmp_path):
    audit, _evaluator, _run, _artifact, report = context_artifact(monkeypatch, tmp_path)
    report.write_text("Unsubstantiated result.\n", encoding="utf-8")
    findings = []

    audit.check_context_efficiency(findings)

    assert any("report differs" in finding for finding in findings)


def test_context_artifact_rejects_omitted_case(monkeypatch, tmp_path):
    audit, _evaluator, run, artifact, _report = context_artifact(monkeypatch, tmp_path)
    run["results"]["current-compact"].pop()
    artifact.write_text(json.dumps(run), encoding="utf-8")
    findings = []

    audit.check_context_efficiency(findings)

    assert any("coverage" in finding for finding in findings)


def test_context_artifact_recounts_character_estimate(monkeypatch, tmp_path):
    audit, _evaluator, run, artifact, _report = context_artifact(monkeypatch, tmp_path)
    run["results"]["current-compact"][0]["tokens"] += 1
    artifact.write_text(json.dumps(run), encoding="utf-8")
    findings = []

    audit.check_context_efficiency(findings)

    assert any("count" in finding for finding in findings)
