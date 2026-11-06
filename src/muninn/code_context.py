"""Deterministic, token-budgeted context assembly for source retrieval."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from .activate import terms


DEFAULT_CODE_BUDGET = 1200
DEFAULT_EXCERPT_BUDGET = 180


@dataclass(frozen=True)
class RelationEvidence:
    """One named source-to-target relation used to explain retrieval."""

    relation: str
    symbol: str = ""
    provenance: str = "extracted"
    target_line: int | None = None
    source_line: int | None = None


@dataclass(frozen=True)
class CodeExcerpt:
    """One source excerpt admitted to a context pack."""

    path: str
    start_line: int
    end_line: int
    text: str
    arms: tuple[tuple[str, int], ...] = ()
    relations: tuple[RelationEvidence, ...] = ()


@dataclass(frozen=True)
class CodeContextPack:
    """Rendered context and the evidence needed to audit it."""

    text: str
    excerpts: tuple[CodeExcerpt, ...]
    estimated_tokens: int
    omitted_paths: tuple[str, ...]


def estimate_code_tokens(text: str) -> int:
    """Return the dependency-free estimate used for fixed-budget tests."""
    return math.ceil(len(text.encode("utf-8")) / 4)


def _symbol_terms(symbol: str) -> tuple[str, ...]:
    values = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", symbol)
    ignored = {"class", "function", "interface", "method", "namespace"}
    return tuple(dict.fromkeys(
        value for value in reversed(values) if value.casefold() not in ignored))


def _definition_score(line: str, symbol: str) -> float:
    escaped = re.escape(symbol)
    if not re.search(rf"\b{escaped}\b", line, re.IGNORECASE):
        return 0.0
    score = 20.0
    if re.search(
            rf"\b(?:class|def|enum|function|interface|record|struct|trait|"
            rf"type)\s+{escaped}\b|\b{escaped}\s*\(",
            line, re.IGNORECASE):
        score += 40.0
    return score


def select_code_excerpt(text: str, query: str, *,
                        symbols: Iterable[str] = (),
                        preferred_lines: Iterable[int] = (),
                        budget: int = DEFAULT_EXCERPT_BUDGET) -> tuple[
                            int, int, str]:
    """Select one query- or relation-centered excerpt without hidden labels."""
    lines = text.splitlines()
    if not lines or budget <= 0:
        return 0, 0, ""
    query_terms = set(terms(query))
    symbol_terms = tuple(dict.fromkeys(
        term for symbol in symbols for term in _symbol_terms(symbol)))
    preferred = {line for line in preferred_lines if 1 <= line <= len(lines)}
    scores = []
    for index, line in enumerate(lines, 1):
        line_terms = set(terms(line))
        score = 0.01 if line.strip() else 0.0
        score += 2.0 * len(query_terms & line_terms)
        score += sum(_definition_score(line, symbol)
                     for symbol in symbol_terms)
        if index in preferred:
            score += 80.0
        scores.append(score)
    center = max(range(1, len(lines) + 1),
                 key=lambda index: (scores[index - 1], -index))
    center_text = lines[center - 1]
    if estimate_code_tokens(center_text) > budget:
        limit = max(1, budget * 4)
        excerpt = center_text.encode("utf-8")[:limit].decode(
            "utf-8", "ignore")
        return center, center, excerpt

    start = end = center
    while start > 1 or end < len(lines):
        candidates = []
        if start > 1:
            content = "\n".join(lines[start - 2:end])
            if estimate_code_tokens(content) <= budget:
                candidates.append(
                    (scores[start - 2], 1, start - 1, end, content))
        if end < len(lines):
            content = "\n".join(lines[start - 1:end + 1])
            if estimate_code_tokens(content) <= budget:
                candidates.append(
                    (scores[end], 0, start, end + 1, content))
        if not candidates:
            break
        _score, _lower_tie, start, end, _content = max(candidates)
    excerpt = "\n".join(lines[start - 1:end])
    return start, end, excerpt


def _path_and_arms(item) -> tuple[str, tuple[tuple[str, int], ...]]:
    if isinstance(item, str):
        return item, ()
    return str(item.path), tuple(item.arms)


def _relation_line(relations: tuple[RelationEvidence, ...]) -> str:
    if not relations:
        return ""
    values = []
    for evidence in relations:
        value = evidence.relation
        if evidence.symbol:
            value += f" `{evidence.symbol}`"
        value += f" ({evidence.provenance})"
        values.append(value)
    return "Relation to the source: " + ", ".join(values) + "."


def assemble_code_context(
        records: Mapping[str, str], ranking: Iterable, query: str, *,
        source_path: str | None = None,
        relation_evidence: Mapping[str, tuple[RelationEvidence, ...]] | None = None,
        budget: int = DEFAULT_CODE_BUDGET,
        excerpt_budget: int = DEFAULT_EXCERPT_BUDGET) -> CodeContextPack:
    """Pack ranked source excerpts and explicit relation evidence."""
    evidence = relation_evidence or {}
    heading = "# Retrieved code context"
    if source_path:
        heading += f"\n\nSource file: `{source_path}`."
    blocks = [heading]
    excerpts = []
    omitted = []
    seen = set()
    for item in ranking:
        path, arms = _path_and_arms(item)
        if path == source_path or path in seen:
            continue
        seen.add(path)
        text = records.get(path)
        if text is None:
            omitted.append(path)
            continue
        relations = tuple(evidence.get(path, ()))
        symbols = [value.symbol for value in relations if value.symbol]
        preferred = [value.target_line for value in relations
                     if value.target_line is not None]
        prefix = [f"## `{path}`"]
        if arms:
            arm_text = ", ".join(f"{name} rank {rank}"
                                 for name, rank in arms)
            prefix.append(f"Retrieved by: {arm_text}.")
        relation_line = _relation_line(relations)
        if relation_line:
            prefix.append(relation_line)
        prefix += ["Lines 1-1:", "```", "", "```"]
        fixed_block = "\n".join(prefix)
        available = budget - estimate_code_tokens(
            "\n\n".join((*blocks, fixed_block)))
        excerpt_limit = min(excerpt_budget, max(0, available))
        admitted = False
        while excerpt_limit > 0:
            start, end, excerpt = select_code_excerpt(
                text, query, symbols=symbols, preferred_lines=preferred,
                budget=excerpt_limit)
            if not excerpt:
                break
            lines = list(prefix)
            lines[-4] = f"Lines {start}-{end}:"
            lines[-2] = excerpt
            block = "\n".join(lines)
            rendered = "\n\n".join((*blocks, block))
            excess = estimate_code_tokens(rendered) - budget
            if excess <= 0:
                blocks.append(block)
                excerpts.append(CodeExcerpt(
                    path, start, end, excerpt, arms=arms,
                    relations=relations))
                admitted = True
                break
            excerpt_limit -= excess
        if not admitted:
            omitted.append(path)
    rendered = "\n\n".join(blocks)
    if estimate_code_tokens(rendered) > max(0, budget):
        rendered = ""
    return CodeContextPack(
        rendered, tuple(excerpts), estimate_code_tokens(rendered),
        tuple(omitted))
