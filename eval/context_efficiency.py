"""Compare deterministic pack size and answer-span availability on public fixtures."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
LABELS = HERE / "context-efficiency-labels.json"
BASELINE_REVISION = "33c5acd3cba2db192d0a3f6329e8d2e4383f4048"
BASELINE_SOURCE_SHA256 = "13d8a63fbccb1dc555fe82360f6be4f891e54112ad68b80b1219ae523f41ea20"
BUDGETS = (200, 400, 900)
VARIANTS = ("baseline-standard", "current-standard", "current-no-index", "current-compact")
FROZEN_TIME = 1_700_000_000.0


def digest(value: object) -> str:
    """Identify the exact serialized experimental inputs."""
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def source_digest(source: Path) -> str:
    """Identify Python source content without publishing machine paths."""
    files = {str(path.relative_to(source)): hashlib.sha256(
        path.read_bytes()).hexdigest() for path in sorted(source.rglob("*.py"))}
    if not files:
        raise ValueError("The source directory contains no Python files.")
    return digest(files)


def load_fixture() -> tuple[dict, dict]:
    """Keep scoring labels separate from the retrieval worker inputs."""
    labels = json.loads(LABELS.read_text(encoding="utf-8"))
    spec = importlib.util.spec_from_file_location("public_aurora", HERE / "gen_corpus.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    legacy = json.loads((HERE / "questions.json").read_text(encoding="utf-8"))
    if [row["query"] for row in labels["questions"]] != [row["q"] for row in legacy]:
        raise ValueError("The frozen question list differs from the original 18 questions.")
    notes = []
    for path, title, description, tags, body, supersedes in module.NOTES:
        note = {"path": path, "title": title, "description": description,
                "tags": tags.split(","), "body": body, "type": "fact"}
        if supersedes:
            note["supersedes"] = [supersedes]
        notes.append(note)
    corpora = {"aurora": notes}
    questions = []
    scoring = {}
    for row, original in zip(labels["questions"], legacy):
        questions.append({"id": row["id"], "corpus": "aurora", "query": row["query"]})
        scoring[row["id"]] = {**row, "legacy_expect": original["expect"],
                              "category": original["kind"]}
    for row in labels["adversarial"]:
        source_notes = []
        for original in row["notes"]:
            note = dict(original)
            padding = note.pop("padding_paragraphs", 0)
            note["body"] = "\n\n".join([
                *(f"Maintenance entry {index}: inspect inventory labels and record "
                  "routine checks after scheduled equipment inspections."
                  for index in range(padding)), note["body"]])
            source_notes.append(note)
        corpora[row["id"]] = source_notes
        questions.append({"id": row["id"], "corpus": row["id"], "query": row["query"]})
        scoring[row["id"]] = {key: value for key, value in row.items() if key != "notes"}
    request = {"corpora": corpora, "questions": questions, "budgets": list(BUDGETS), "k": 5}
    return request, scoring


def replay_aurora(dyn, notes: list[dict]) -> None:
    """Replay the original public generator's usage without changing its files."""
    for note in notes:
        if note["path"].endswith("-old.md"):
            dyn.touch(note["path"])
    dyn.consolidate()
    for note in notes:
        for old in note.get("supersedes", []):
            dyn.supersede(old, note["path"])
    hot = ["infra/db-port.md", "infra/redis.md", "infra/vpn.md", "infra/backup.md",
           "infra/dns.md", "decisions/logs.md", "decisions/queue.md",
           "runbooks/restore-db.md", "projects/aurora.md", "infra/db-host.md"]
    for _round in range(4):
        for path in hot:
            dyn.touch(path)
    for path in ["notes/postgres-tuning.md", "notes/network-cheatsheet.md", "notes/redis-tips.md"]:
        dyn.touch(path)
        dyn.touch(path)
    dyn.pin("decisions/secrets.md")
    dyn.pin("decisions/access.md")
    for path in ["runbooks/restore-db.md", "infra/db-port.md", "infra/backup.md"]:
        dyn.touch(path)
    dyn.outcome(-0.9, why="restore drill failed against the stale port")
    for index in range(3):
        for path in ["projects/worker.md", "decisions/queue.md", "infra/monitoring.md"]:
            dyn.touch(path, session=f"deploy-{index}")
    for index in range(2):
        for path in ["runbooks/restore-db.md", "infra/backup.md", "infra/db-host.md"]:
            dyn.touch(path, session=f"drill-{index}")
    dyn.consolidate()


def worker(request: dict, source: Path, compact: bool, index: bool = True) -> list[dict]:
    """Expose only notes, queries, budgets, and fixed options to retrieval."""
    sys.path.insert(0, str(source.resolve()))
    from muninn.dynamics import Dynamics
    from muninn.recall import context_pack, estimate_tokens
    from muninn.store import Bundle

    results = []
    with (tempfile.TemporaryDirectory(prefix="muninn-context-eval-") as directory,
          patch("time.time", return_value=FROZEN_TIME)):
        bundles = {}
        for name, notes in request["corpora"].items():
            root = Path(directory) / name
            root.mkdir()
            bundle = Bundle(str(root))
            for note in notes:
                metadata = {key: value for key, value in note.items()
                            if key not in {"path", "body"}}
                bundle.write_note(note["path"], metadata, note["body"])
            bundle = Bundle(str(root))
            bundle.generate_index()
            dyn = Dynamics(str(root)) if name == "aurora" else None
            if dyn is not None:
                replay_aurora(dyn, notes)
            bundles[name] = (bundle, dyn)
        for question in request["questions"]:
            bundle, dyn = bundles[question["corpus"]]
            for budget in request["budgets"]:
                options = {"compact": True} if compact else {}
                started = time.perf_counter()
                text = context_pack(bundle, dyn, question["query"], budget=budget,
                                    k=request["k"], reactivate=False, index=index, **options)
                results.append({"id": question["id"], "corpus": question["corpus"],
                                "budget": budget, "text": text,
                                "tokens": estimate_tokens(text),
                                "utf8_bytes": len(text.encode()),
                                "latency_ms": (time.perf_counter() - started) * 1000})
    return results


def run_variant(request: dict, source: Path, compact: bool, index: bool = True) -> list[dict]:
    """Use a fresh interpreter so source trees cannot contaminate each other."""
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--source", str(source)]
    if compact:
        command.append("--compact")
    if not index:
        command.append("--no-index")
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("MUNINN_") and key != "PYTHONPATH"}
    completed = subprocess.run(command, input=json.dumps(request), capture_output=True,
                               text=True, check=True, env=environment, timeout=180)
    rows = json.loads(completed.stdout)
    expected = {(question["id"], budget) for question in request["questions"]
                for budget in request["budgets"]}
    actual = [(row["id"], row["budget"]) for row in rows]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError("A worker omitted, duplicated, or added an evaluation case.")
    return rows


def score_row(row: dict, label: dict) -> dict:
    """Score complete fact groups without converting retrieval into answer accuracy."""
    text = " ".join(row["text"].lower().split())
    required = label["required"]
    matched = sum(any(" ".join(span.lower().split()) in text for span in alternatives)
                  for alternatives in required)
    forbidden = [span for span in label.get("forbidden", []) if span.lower() in text]
    return {**row, "answerable": not label.get("no_match", False),
            "required_groups": len(required), "matched_groups": matched,
            "complete_evidence": bool(required) and matched == len(required) and not forbidden,
            "legacy_any_span": any(span.lower() in text for span in label.get("legacy_expect", [])),
            "forbidden_spans_found": forbidden, "within_budget": row["tokens"] <= row["budget"],
            "no_match_disclosed": "no match" in text or "no matching" in text}


def load_tokenizer(name: str):
    """Require an explicit optional dependency for measured tokenizer counts."""
    try:
        import tiktoken
    except ImportError as error:
        raise RuntimeError("The requested tokenizer requires the optional tiktoken package.") from error
    return tiktoken.get_encoding(name), tiktoken.__version__


def measure_tokens(rows: list[dict], tokenizer) -> None:
    """Count the complete rendered context rather than note bodies alone."""
    for row in rows:
        row["tokenizer_tokens"] = len(tokenizer.encode(row["text"], disallowed_special=()))
        row["within_tokenizer_budget"] = row["tokenizer_tokens"] <= row["budget"]


def aggregate(rows: list[dict]) -> dict:
    """Summarize every case and retain failures in the denominator."""
    answerable = [row for row in rows if row["answerable"]]
    result = {"cases": len(rows), "answerable_cases": len(answerable),
            "complete_evidence": sum(row["complete_evidence"] for row in answerable),
            "matched_groups": sum(row["matched_groups"] for row in answerable),
            "required_groups": sum(row["required_groups"] for row in answerable),
            "legacy_any_span": sum(row["legacy_any_span"] for row in rows if row["corpus"] == "aurora"),
            "budget_violations": sum(not row["within_budget"] for row in rows),
            "forbidden_span_cases": sum(bool(row["forbidden_spans_found"]) for row in rows),
            "no_match_cases": sum(not row["answerable"] for row in rows),
            "no_match_disclosures": sum(not row["answerable"] and row["no_match_disclosed"] for row in rows),
            "tokens": sum(row["tokens"] for row in rows),
            "utf8_bytes": sum(row["utf8_bytes"] for row in rows),
            "latency_ms_median": statistics.median(row["latency_ms"] for row in rows)}
    if rows and "tokenizer_tokens" in rows[0]:
        result["tokenizer_tokens"] = sum(row["tokenizer_tokens"] for row in rows)
        result["tokenizer_budget_exceedances"] = sum(not row["within_tokenizer_budget"] for row in rows)
    return result


def compare(before: list[dict], after: list[dict]) -> dict:
    """Report paired evidence changes and measured token reduction."""
    prior = {(row["id"], row["budget"]): row for row in before}
    if set(prior) != {(row["id"], row["budget"]) for row in after}:
        raise ValueError("Paired conditions contain different cases.")
    gains, losses = [], []
    for row in after:
        old = prior[(row["id"], row["budget"])]
        if row["answerable"] and row["complete_evidence"] != old["complete_evidence"]:
            (gains if row["complete_evidence"] else losses).append(
                {"id": row["id"], "budget": row["budget"]})
    before_tokens = sum(row["tokens"] for row in before)
    after_tokens = sum(row["tokens"] for row in after)
    result = {"gains": gains, "losses": losses, "tokens_before": before_tokens,
            "tokens_after": after_tokens,
            "weighted_token_reduction": 1 - after_tokens / before_tokens if before_tokens else 0.0}
    if before and "tokenizer_tokens" in before[0]:
        prior_count = sum(row["tokenizer_tokens"] for row in before)
        current_count = sum(row["tokenizer_tokens"] for row in after)
        result.update({"tokenizer_tokens_before": prior_count,
                       "tokenizer_tokens_after": current_count,
                       "tokenizer_reduction": 1 - current_count / prior_count if prior_count else 0.0})
    return result


def report(run: dict) -> str:
    """Render measured results and limits without fixed numerical claims."""
    lines = ["# Public context-efficiency evaluation", "",
             ("This deterministic evaluation measures pack size and answer-span availability. "
             "It does not measure generated-answer accuracy or agent task completion."), "",
             ("The fixed corpus contains all 18 Aurora questions and four adversarial cases. "
             "Each variant runs at 200, 400, and 900 estimated tokens with five candidate notes."), "",
             ("The no-index condition uses the existing index omission option. "
             "It isolates savings from that option before adding compact extraction."), "",
             ("Expected spans remain outside retrieval inputs. Complete-evidence scoring requires "
             "every fact group. The separate legacy score accepts any original answer substring."), "",
             "| Variant | Aurora complete | Legacy any span | Adversarial complete | Estimated tokens | Budget violations | Forbidden evidence |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for variant in VARIANTS:
        rows = run["results"][variant]
        aurora = aggregate([row for row in rows if row["corpus"] == "aurora"])
        extra = aggregate([row for row in rows if row["corpus"] != "aurora"])
        total = aggregate(rows)
        lines.append(f"| {variant} | {aurora['complete_evidence']}/{aurora['answerable_cases']} | "
                     f"{aurora['legacy_any_span']}/{aurora['answerable_cases']} | "
                     f"{extra['complete_evidence']}/{extra['answerable_cases']} | "
                     f"{total['tokens']} | {total['budget_violations']} | {total['forbidden_span_cases']} |")
    lines += ["", "| Comparison | Estimated reduction | Evidence gains | Evidence losses |",
              "|---|---:|---:|---:|"]
    for name, paired in run["comparisons"].items():
        lines.append(f"| {name} | {paired['weighted_token_reduction']:.2%} | "
                     f"{len(paired['gains'])} | {len(paired['losses'])} |")
    if run.get("tokenizer"):
        lines += ["", (f"The following counts use `{run['tokenizer']}` "
                  f"from tiktoken {run['tokenizer_version']}."), "",
                  "| Variant | Tokenizer tokens | Tokenizer budget exceedances |",
                  "|---|---:|---:|"]
        for variant in VARIANTS:
            summary = aggregate(run["results"][variant])
            lines.append(f"| {variant} | {summary['tokenizer_tokens']} | "
                         f"{summary['tokenizer_budget_exceedances']} |")
        lines += ["", "| Comparison | Tokenizer reduction |", "|---|---:|"]
        for name, paired in run["comparisons"].items():
            lines.append(f"| {name} | {paired['tokenizer_reduction']:.2%} |")
        lines += ["", ("These counts are exact for the named tokenizer. "
                  "They remain a proxy for models with a different tokenizer."), "",
                  ("Tokenizer budget exceedances show where the production character estimate is insufficient. "
                  "They are separate from violations of the declared estimate budget.")]
    lines += ["", ("The same questions repeat at three budgets, so the 66 observations are not independent samples. "
              "The four new fixtures are development diagnostics, not an untouched confirmation set."), "",
              ("Muninn's character-based token estimate is the budget contract. It is not the active model tokenizer. "
              "The artifact also records UTF-8 bytes, per-case outputs, and one-run retrieval latency."), "",
              ("Complete blocks can exceed a small budget. An omitted fact is scored as unavailable, "
              "even when a source reference allows a later read. Follow-up read costs are not measured."), "",
              "The qualification fixture uses one paragraph. These results do not establish preservation of qualifications across separate paragraphs.", "",
              (f"The baseline revision is `{run['baseline_revision']}`. "
              f"The frozen label SHA-256 is `{run['labels_sha256']}`."), "",
              "Reproduce with an immutable baseline source export and the following command:", "",
              "```bash", ("python eval/context_efficiency.py --baseline-source /tmp/muninn-baseline/src "
              "--output /tmp/context-efficiency.json --report /tmp/context-efficiency.md"
              + (f" --tokenizer {run['tokenizer']}" if run.get("tokenizer") else "")), "```", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-source", type=Path)
    parser.add_argument("--source", type=Path, default=ROOT / "src")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--tokenizer", choices=("cl100k_base",))
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--compact", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-index", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(json.load(sys.stdin), args.source, args.compact, not args.no_index)))
        return 0
    if args.baseline_source is None or args.output is None:
        parser.error("--baseline-source and --output are required")
    request, scoring = load_fixture()
    tokenizer, tokenizer_version = None, None
    if args.tokenizer:
        try:
            tokenizer, tokenizer_version = load_tokenizer(args.tokenizer)
        except RuntimeError as error:
            parser.error(str(error))
    baseline_digest = source_digest(args.baseline_source)
    if baseline_digest != BASELINE_SOURCE_SHA256:
        parser.error("The baseline source does not match the registered revision.")
    current_digest = source_digest(args.source)
    variants = {}
    for name in VARIANTS:
        source = args.baseline_source if name == "baseline-standard" else args.source
        rows = run_variant(request, source, compact=name == "current-compact",
                           index=name != "current-no-index")
        if tokenizer is not None:
            measure_tokens(rows, tokenizer)
        variants[name] = [score_row(row, scoring[row["id"]]) for row in rows]
    if source_digest(args.source) != current_digest:
        raise RuntimeError("The candidate source changed during evaluation.")
    run = {"schema": 1, "baseline_revision": BASELINE_REVISION,
           "baseline_source_sha256": baseline_digest,
           "current_source_sha256": current_digest,
           "labels_sha256": hashlib.sha256(LABELS.read_bytes()).hexdigest(),
           "worker_inputs_sha256": digest(request), "budgets": list(BUDGETS),
           "tokenizer": args.tokenizer, "tokenizer_version": tokenizer_version,
           "fixed_time": FROZEN_TIME, "model_calls": 0,
           "network_calls_measured": False,
           "results": variants, "comparisons": {
               "current-standard vs baseline-standard": compare(variants["baseline-standard"], variants["current-standard"]),
               "current-compact vs baseline-standard": compare(variants["baseline-standard"], variants["current-compact"]),
               "current-compact vs current-no-index": compare(variants["current-no-index"], variants["current-compact"]),
               "current-compact vs current-standard": compare(variants["current-standard"], variants["current-compact"])}}
    args.output.write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
    rendered = report(run)
    if args.report:
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
