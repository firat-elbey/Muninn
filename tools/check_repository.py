#!/usr/bin/env python3
"""Validate the tracked repository and its published evaluation reports."""

from __future__ import annotations

import ast
import gzip
import hashlib
import importlib.util
import json
import math
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
LOCAL_LINK = re.compile(r"(?<!!)\[[^\]\n]*\]\(([^)\n]+)\)")
FINAL_NEWLINE_SUFFIXES = {".cff", ".md", ".py", ".toml", ".txt", ".yaml", ".yml"}
FINAL_NEWLINE_NAMES = {".gitignore", ".muninnignore", "DCO", "LICENSE", "NOTICE"}
PROSE_SUFFIXES = {".cff", ".md", ".py", ".rst", ".toml", ".txt", ".yaml", ".yml"}
PROSE_NAMES = {"DCO", "LICENSE", "NOTICE"}
FORBIDDEN_LANGUAGE = (
    re.compile(r"\bland the document\b", re.IGNORECASE),
    re.compile(r"\bship it\b", re.IGNORECASE),
    re.compile(r"\bspin up\b", re.IGNORECASE),
    re.compile(r"\bcircle back\b", re.IGNORECASE),
    re.compile(r"\bdouble-click\b", re.IGNORECASE),
    re.compile(r"\bhold the line\b", re.IGNORECASE),
    re.compile(r"\bwhat kills this\b", re.IGNORECASE),
    re.compile(r"\bbest-in-class\b", re.IGNORECASE),
    re.compile(r"\bdelve\b", re.IGNORECASE),
    re.compile(r"\bit is worth noting\b", re.IGNORECASE),
    re.compile(r"\bthe key insight\b", re.IGNORECASE),
    re.compile(r"\bthis underscores\b", re.IGNORECASE),
)
MIT_LICENSE_SHA256 = (
    "f629d99af73dec422cb69014947d5b864f5c0da6b36ff415174866a6525cc8da"
)
LONGMEMEVAL_LICENSE_SHA256 = (
    "d3c4b9aa54759df6ded337978a6f3b55b75615e5e4525c3b82d7e2627d4b9732"
)
REQUIRED_PUBLIC_PATHS = {
    Path(".github/dependabot.yml"),
    Path(".github/ISSUE_TEMPLATE/bug.yml"),
    Path(".github/ISSUE_TEMPLATE/config.yml"),
    Path(".github/ISSUE_TEMPLATE/feature.yml"),
    Path(".github/pull_request_template.md"),
    Path(".github/workflows/ci.yml"),
    Path(".github/workflows/release.yml"),
    Path("CHANGELOG.md"),
    Path("CITATION.cff"),
    Path("CONTRIBUTING.md"),
    Path("DCO"),
    Path("LICENSE"),
    Path("LICENSES/LongMemEval-MIT.txt"),
    Path("MANIFEST.in"),
    Path("NOTICE"),
    Path("SECURITY.md"),
    Path("THIRD_PARTY_NOTICES.md"),
    Path("docs/PUBLIC-RELEASE.md"),
    Path("eval/README.md"),
    Path("eval/longmemeval/verify_results.py"),
    Path("tools/check_distribution.py"),
}
FORBIDDEN_PUBLIC_PATHS = {
    Path("eval/competitive/RESULTS-dependeval-series5-confirmation-graphify-v1.json.gz"),
    Path("eval/competitive/RESULTS-dependeval-series5-confirmation-hybrid-v1.json.gz"),
    Path("eval/competitive/RESULTS-dependeval-series5-confirmation-muninn-tree-v1.json.gz"),
}
PRIVATE_NAMES = {".coverage", ".env", "ledger.jsonl", "state.json"}
PRIVATE_SUFFIXES = {".key", ".p12", ".pem", ".pfx"}
EVALUATION_INVENTORY = Path("tools/public-evaluation-inventory.json")
DATA_SUFFIXES = {".csv", ".tsv", ".json", ".jsonl", ".ndjson", ".parquet", ".gz", ".zip", ".tgz"}
PUBLIC_METADATA = {EVALUATION_INVENTORY, Path("npm/package.json")}
RUNTIME_DIRS = {".muninn", ".codex", ".claude", ".gemini", "graphify-out"}
BUNDLE_ROOT_PATHS = {"home.md", "index.md", "lessons", "notes", "projects", "threads", "style", "style-learned"}
RUNTIME_SUFFIXES = {".jsonl", ".ndjson", ".sqlite", ".sqlite3", ".db"}
LEDGER_KINDS = {"touch", "encode", "recall", "outcome", "session-begin", "supersede",
                "consolidate", "assoc", "code-relation", "feedback", "gap", "goal",
                "intent", "pin", "review", "rule"}
SECRET_PATTERNS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
LOCAL_PATH_PATTERNS = (
    re.compile(r"/Users/[A-Za-z0-9._-]+/"),
    re.compile(r"/home/[A-Za-z0-9._-]+/"),
    re.compile("/private/var/" + r"folders/"),
    re.compile(r"[A-Za-z]:\\Users\\[A-Za-z0-9._-]+\\"),
)


def tracked_paths() -> list[Path]:
    output = subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=ROOT
    ).decode("utf-8")
    return [Path(item) for item in output.split("\0") if item]


def markdown_prose(text: str) -> str:
    """Remove fenced and inline code before interpreting Markdown links."""
    lines: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            current = marker.group(1)[0]
            if fence is None:
                fence = current
            elif current == fence:
                fence = None
            continue
        if fence is None:
            lines.append(line)
    return re.sub(r"`+[^`\n]*`+", "", "\n".join(lines))


def check_markdown(path: Path, text: str, findings: list[str]) -> None:
    for raw in LOCAL_LINK.findall(markdown_prose(text)):
        target = raw.strip().split(maxsplit=1)[0].strip("<>")
        if target.startswith(("#", "/", "http://", "https://", "mailto:")):
            continue
        target = unquote(target.split("#", 1)[0])
        if not target:
            continue
        destination = (path.parent / target).resolve()
        if not destination.exists():
            findings.append(f"broken Markdown link: {path.relative_to(ROOT)} -> {raw}")


def check_sensitive_text(relative: Path, text: str, findings: list[str]) -> None:
    """Reject common credentials without printing the matched value."""
    if any(pattern.search(text) for pattern in SECRET_PATTERNS):
        findings.append(f"possible credential in {relative}")
    if any(pattern.search(text) for pattern in LOCAL_PATH_PATTERNS):
        findings.append(f"machine-local user path in {relative}")


def check_prose_characters(relative: Path, text: str, findings: list[str]) -> None:
    """Reject language excluded by the repository writing standard."""
    if "\N{EM DASH}" in text:
        findings.append(f"em dash in {relative}")
    prose = markdown_prose(text) if relative.suffix.lower() == ".md" else text
    if any(pattern.search(prose) for pattern in FORBIDDEN_LANGUAGE):
        findings.append(f"excluded informal or stock language in {relative}")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_public_contract(paths: list[Path], findings: list[str]) -> None:
    """Check the legal, privacy, and release boundary of the public tree."""
    present = set(paths)
    for path in sorted(REQUIRED_PUBLIC_PATHS - present):
        findings.append(f"required public-release file is not tracked: {path}")
    for path in sorted(FORBIDDEN_PUBLIC_PATHS & present):
        findings.append(f"unlicensed raw evaluation artifact is tracked: {path}")

    for path in paths:
        lower = Path(path.as_posix().casefold())
        if (RUNTIME_DIRS.intersection(lower.parts) or lower.parts[0] in BUNDLE_ROOT_PATHS
                or lower.name in PRIVATE_NAMES or RUNTIME_SUFFIXES.intersection(lower.suffixes)):
            findings.append(f"private state path is tracked: {path}")
        elif path.suffix.lower() in PRIVATE_SUFFIXES:
            findings.append(f"credential file is tracked: {path}")

    license_path = ROOT / "LICENSE"
    if license_path.exists() and file_sha256(license_path) != MIT_LICENSE_SHA256:
        findings.append("LICENSE differs from the MIT license text for Muninn")
    third_party_license = ROOT / "LICENSES" / "LongMemEval-MIT.txt"
    if (third_party_license.exists()
            and file_sha256(third_party_license) != LONGMEMEVAL_LICENSE_SHA256):
        findings.append("LongMemEval license text differs from the upstream MIT license")

    pyproject = ROOT / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text(encoding="utf-8")
        check_extractor_contract(text, findings)
        if 'license = "MIT"' not in text:
            findings.append("pyproject.toml does not declare MIT")
        for name in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md", "LICENSES/*.txt"):
            if f'"{name}"' not in text:
                findings.append(f"pyproject.toml does not package {name}")

    citation = ROOT / "CITATION.cff"
    if citation.exists() and "license: MIT" not in citation.read_text(encoding="utf-8").splitlines():
        findings.append("CITATION.cff does not declare MIT")

    report = ROOT / "eval/competitive/RESULTS-dependeval-series5-confirmation.md"
    if report.exists() and "does not declare a redistribution license" not in report.read_text(
            encoding="utf-8"):
        findings.append("DependEval report does not explain the omitted raw archives")


def check_evaluation_inventory(paths: list[Path], findings: list[str]) -> None:
    """Require a reviewed source and digest for every evaluation file or data export."""
    tracked = set(paths)
    eligible = {path for path in tracked if path not in PUBLIC_METADATA
                and (path.parts[0].casefold() == "eval" or path.suffix.casefold() in DATA_SUFFIXES)}
    if EVALUATION_INVENTORY not in tracked:
        findings.append("public evaluation inventory is missing")
        return
    approved = set()
    try:
        manifest = json.loads((ROOT / EVALUATION_INVENTORY).read_text(encoding="utf-8"))
        groups = manifest["groups"]
        if not isinstance(groups, list):
            raise ValueError
        for group in groups:
            kind = group["kind"]
            source = group["source"]
            if (kind not in {"public-benchmark", "independent-synthetic", "project-code"}
                    or not isinstance(source, str) or not source.strip()
                    or (kind == "public-benchmark" and not source.startswith("https://"))
                    or not isinstance(group["redistribution"], str) or not group["redistribution"].strip()):
                findings.append("public artifact group lacks reviewed provenance")
            for name, digest in group["files"].items():
                path = Path(name)
                if path in approved or path not in eligible:
                    findings.append(f"duplicate or untracked public artifact in inventory: {name}")
                    continue
                approved.add(path)
                target = ROOT / path
                if target.is_symlink() or not target.is_file() or file_sha256(target) != digest:
                    findings.append(f"public artifact digest differs: {name}")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        findings.append("invalid public evaluation inventory")
    for path in sorted(eligible - approved):
        findings.append(f"unregistered public artifact: {path.as_posix()}")


def check_json_content(relative: Path, record, findings: list[str]) -> None:
    """Inspect decoded strings and reject recognizable runtime data under any filename."""
    remaining = [record]
    strings = []
    contains_runtime = False
    while remaining:
        value = remaining.pop()
        if isinstance(value, list):
            remaining.extend(value)
        elif isinstance(value, dict):
            kind = value.get("kind")
            role = value.get("role")
            contains_runtime |= (
                (isinstance(kind, str) and kind in LEDGER_KINDS and "ts" in value)
                or {"entries", "coact", "session_notes"} <= value.keys()
                or (isinstance(role, str) and role in {"user", "assistant", "system", "tool"}
                    and "content" in value)
            )
            remaining.extend(value.values())
            strings.extend(value.keys())
        elif isinstance(value, str):
            strings.append(value)
    if contains_runtime:
        findings.append(f"runtime records in {relative.as_posix()}")
    check_sensitive_text(relative, "\n".join(strings), findings)


def check_extractor_contract(text: str, findings: list[str]) -> None:
    """Require the bundled parser line used by network-free extraction."""
    requirement = '"tree-sitter-language-pack>=0.13,<1.0"'
    if requirement not in text:
        findings.append(
            "extract extra must constrain tree-sitter-language-pack to >=0.13,<1.0"
        )


def check_file(relative: Path, findings: list[str]) -> None:
    path = ROOT / relative
    if path.is_symlink():
        target = path.readlink()
        if target.is_absolute() or not path.resolve().is_relative_to(ROOT):
            findings.append(f"symlink leaves public checkout: {relative.as_posix()}")
        elif not path.exists():
            findings.append(f"broken symlink: {relative.as_posix()}")
        return

    if relative.name.endswith(".json.gz"):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as source:
                text = source.read()
            check_json_content(relative, json.loads(text), findings)
            check_sensitive_text(relative, text, findings)
            check_prose_characters(relative, text, findings)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            findings.append(f"invalid compressed JSON: {relative}: {error}")
        return

    data = path.read_bytes()
    if b"\x00" in data:
        findings.append(f"unexpected binary file: {relative}")
        return
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        findings.append(f"non-UTF-8 text: {relative}: {error}")
        return

    check_sensitive_text(relative, text, findings)
    if relative.suffix.lower() in PROSE_SUFFIXES or relative.name in PROSE_NAMES:
        check_prose_characters(relative, text, findings)

    if (relative.suffix in FINAL_NEWLINE_SUFFIXES
            or relative.name in FINAL_NEWLINE_NAMES):
        if data and not data.endswith(b"\n"):
            findings.append(f"missing final newline: {relative}")
    if "\r\n" in text:
        findings.append(f"CRLF line endings: {relative}")
    if re.search(r"(?m)[ \t]+$", text):
        findings.append(f"trailing whitespace: {relative}")
    if re.search(r"(?m)^(<<<<<<<|=======|>>>>>>>)", text):
        findings.append(f"merge marker: {relative}")

    try:
        if relative.suffix.casefold() == ".json":
            check_json_content(relative, json.loads(text), findings)
        elif relative.suffix == ".py":
            ast.parse(text, filename=str(relative))
    except (SyntaxError, json.JSONDecodeError) as error:
        findings.append(f"invalid {relative.suffix[1:]}: {relative}: {error}")

    if relative.suffix.casefold() != ".json" and text.lstrip().startswith(("{", "[")):
        # A renamed JSON document or line-delimited export can still carry runtime data.
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            try:
                payload = [json.loads(line) for line in text.splitlines() if line.strip()]
            except json.JSONDecodeError:
                payload = None
        if payload is not None:
            check_json_content(relative, payload, findings)

    if relative.suffix == ".md":
        check_markdown(path, text, findings)


def check_evidence(findings: list[str]) -> int:
    """Regenerate reports whose raw inputs may be redistributed."""
    commands = [
        (
            [sys.executable, "eval/public_benchmarks.py", "--output", "{output}"],
            Path("eval/PUBLIC-BENCHMARKS-2026-11.md"),
        ),
        (
            [sys.executable, "eval/code_retrieval/analyze_swebench.py",
             "eval/code_retrieval/RESULTS-swebench-lite-300.json.gz",
             "--output", "{output}"],
            Path("eval/code_retrieval/RESULTS-swebench-lite-300.md"),
        ),
        (
            [sys.executable,
             "eval/code_retrieval/analyze_persistent_parity.py",
             "--run",
             "eval/code_retrieval/RESULTS-persistent-parity-swebench-lite-300.json.gz",
             "--source",
             "eval/code_retrieval/RESULTS-swebench-lite-300.json.gz",
             "--output", "{output}"],
            Path("eval/code_retrieval/RESULTS-persistent-parity-swebench-lite-300.md"),
        ),
    ]
    with tempfile.TemporaryDirectory(prefix="muninn-repository-check-") as temp:
        for index, (template, expected) in enumerate(commands):
            output = Path(temp) / f"report-{index}.md"
            command = [part.format(output=str(output)) for part in template]
            result = subprocess.run(
                command, cwd=ROOT, capture_output=True, text=True
            )
            if result.returncode:
                detail = (result.stderr or result.stdout).strip().splitlines()
                findings.append(
                    f"report generation failed: {expected}: "
                    f"{detail[-1] if detail else 'no diagnostic'}"
                )
                continue
            if output.read_bytes() != (ROOT / expected).read_bytes():
                findings.append(f"published report differs from raw results: {expected}")
    return len(commands)


def check_longmemeval(findings: list[str]) -> None:
    """Verify LongMemEval claims against the committed raw records."""
    command = [sys.executable, "eval/longmemeval/verify_results.py"]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        findings.append(
            "LongMemEval verification failed: "
            + (detail[-1] if detail else "no diagnostic")
        )


def load_context_evaluator():
    """Load only the deterministic scoring and report functions."""
    path = ROOT / "eval" / "context_efficiency.py"
    spec = importlib.util.spec_from_file_location("context_efficiency_audit", path)
    if spec is None or spec.loader is None:
        raise ValueError("The context evaluator cannot be loaded.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_context_run(run: dict, evaluator) -> dict:
    """Recompute evidence and arithmetic without a model or tokenizer dependency.

    Stored tokenizer counts support arithmetic checks only. Recounting their
    underlying strings requires the optional, named tokenizer implementation.
    """
    request, scoring = evaluator.load_fixture()
    metadata = {
        "schema": 1,
        "baseline_revision": evaluator.BASELINE_REVISION,
        "baseline_source_sha256": evaluator.BASELINE_SOURCE_SHA256,
        "labels_sha256": file_sha256(evaluator.LABELS),
        "worker_inputs_sha256": evaluator.digest(request),
        "budgets": list(evaluator.BUDGETS),
        "fixed_time": evaluator.FROZEN_TIME,
    }
    if any(run.get(key) != value for key, value in metadata.items()):
        raise ValueError("Context input provenance differs from the registered fixture.")
    if set(run["results"]) != set(evaluator.VARIANTS):
        raise ValueError("Context variant coverage differs from the registered experiment.")
    questions = {question["id"]: question for question in request["questions"]}
    expected = {(identifier, budget) for identifier in questions
                for budget in request["budgets"]}
    measured = bool(run.get("tokenizer"))
    if measured and (run["tokenizer"] != "cl100k_base" or not run.get("tokenizer_version")):
        raise ValueError("Context tokenizer metadata is incomplete or unsupported.")
    variants = {}
    for name in evaluator.VARIANTS:
        rows = run["results"][name]
        pairs = [(row["id"], row["budget"]) for row in rows]
        if len(pairs) != len(set(pairs)) or set(pairs) != expected:
            raise ValueError("Context case coverage contains omissions or duplicates.")
        rebuilt = []
        for row in rows:
            if row["corpus"] != questions[row["id"]]["corpus"]:
                raise ValueError("Context corpus identity differs from the fixture.")
            text = row["text"]
            if (row["tokens"] != max(1, len(text) // 4)
                    or row["utf8_bytes"] != len(text.encode("utf-8"))):
                raise ValueError("Context character or byte counts differ from rendered output.")
            if not math.isfinite(row["latency_ms"]) or row["latency_ms"] < 0:
                raise ValueError("Context latency is not a finite nonnegative measurement.")
            raw = {key: row[key] for key in (
                "id", "corpus", "budget", "text", "tokens", "utf8_bytes", "latency_ms")}
            if measured:
                count = row["tokenizer_tokens"]
                if type(count) is not int or count < 0:
                    raise ValueError("Context tokenizer count is not a nonnegative integer.")
                raw.update({"tokenizer_tokens": count,
                            "within_tokenizer_budget": count <= row["budget"]})
            rescored = evaluator.score_row(raw, scoring[row["id"]])
            if row != rescored:
                raise ValueError("Context evidence score or budget flag differs from rendered output.")
            rebuilt.append(rescored)
        variants[name] = rebuilt
    comparisons = {}
    for after, before in (
        ("current-standard", "baseline-standard"),
        ("current-compact", "baseline-standard"),
        ("current-compact", "current-no-index"),
        ("current-compact", "current-standard"),
    ):
        comparisons[f"{after} vs {before}"] = evaluator.compare(variants[before], variants[after])
    if run["comparisons"] != comparisons:
        raise ValueError("Context comparison summaries differ from rescored outputs.")
    return {**run, "results": variants, "comparisons": comparisons}


def check_context_efficiency(findings: list[str]) -> int:
    """Check stored evidence and regenerate the context report without retrieval."""
    artifact = ROOT / "eval" / "RESULTS-context-efficiency-2026-09.json"
    published = artifact.with_suffix(".md")
    try:
        evaluator = load_context_evaluator()
        run = json.loads(artifact.read_text(encoding="utf-8"))
        checked = validate_context_run(run, evaluator)
        if evaluator.report(checked).encode("utf-8") != published.read_bytes():
            findings.append("published context report differs from rescored raw results")
    except (OSError, ValueError, KeyError, TypeError, ImportError, AttributeError) as error:
        findings.append(f"context-efficiency validation failed: {error}")
    return 1


def main() -> int:
    findings: list[str] = []
    paths = tracked_paths()
    check_public_contract(paths, findings)
    check_evaluation_inventory(paths, findings)
    folded: dict[str, Path] = {}
    for path in paths:
        key = str(path).casefold()
        if key in folded and folded[key] != path:
            findings.append(f"case-insensitive path collision: {folded[key]} and {path}")
        folded[key] = path
        check_file(path, findings)

    reports = check_evidence(findings)
    reports += check_context_efficiency(findings)
    check_longmemeval(findings)
    if findings:
        for finding in findings:
            print(f"ERROR: {finding}")
        return 1
    print(
        f"Repository audit passed: {len(paths)} tracked files and "
        f"{reports} regenerated evaluation reports; five LongMemEval reports "
        "match their raw records."
    )
    print("Stored tokenizer arithmetic is checked. Tokenizer recount requires optional tiktoken.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
