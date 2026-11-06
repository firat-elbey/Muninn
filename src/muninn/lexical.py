"""Frequency-aware lexical retrieval with no external dependency.

The current recall floor measures distinct token overlap.  That method is
deterministic and inexpensive, but it treats a common word like a rare
identifier and ignores both term frequency and document length.  This module
provides BM25F as an independent candidate generator.  It does not apply
Muninn's graph, usage, goal, or correction signals; those remain later,
bounded ranking stages.

The index is an in-memory derived view.  Callers may rebuild it from source
files or Markdown notes without changing either source.  A persistent cache
can serialize the same statistics later without changing the ranking
contract.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from .activate import terms

DEFAULT_FIELD_WEIGHTS: Mapping[str, float] = {
    "path": 4.0,
    "title": 3.0,
    "symbols": 3.0,
    "tags": 3.0,
    "description": 2.0,
    "documentation": 2.0,
    "headings": 2.0,
    "body": 1.0,
}


@dataclass(frozen=True)
class LexicalDocument:
    """One searchable object and its named text fields."""

    key: str
    fields: Mapping[str, str]


@dataclass(frozen=True)
class LexicalHit:
    """One result with enough evidence to explain its score."""

    key: str
    score: float
    terms: tuple[tuple[str, float], ...]


class BM25FIndex:
    """A deterministic BM25F index over named document fields.

    BM25F first normalizes term frequency within each field, then combines
    fields before applying BM25 saturation.  Document frequency is measured
    once per document across all fields.  Unknown fields receive weight 1.0.
    """

    def __init__(self, documents: Iterable[LexicalDocument],
                 field_weights: Mapping[str, float] | None = None,
                 *, k1: float = 1.2, b: float = 0.75):
        if k1 <= 0:
            raise ValueError("k1 must be positive")
        if not 0.0 <= b <= 1.0:
            raise ValueError("b must be between 0 and 1")
        self.k1 = float(k1)
        self.b = float(b)
        self.field_weights = dict(DEFAULT_FIELD_WEIGHTS)
        if field_weights is not None:
            self.field_weights.update(field_weights)

        self.documents: dict[str, LexicalDocument] = {}
        self._tf: dict[str, dict[str, Counter[str]]] = {}
        self._length: dict[str, dict[str, int]] = {}
        self._postings: dict[str, set[str]] = defaultdict(set)
        field_totals: Counter[str] = Counter()
        field_counts: Counter[str] = Counter()

        for document in documents:
            if document.key in self.documents:
                raise ValueError(f"duplicate document key: {document.key}")
            self.documents[document.key] = document
            by_field: dict[str, Counter[str]] = {}
            lengths: dict[str, int] = {}
            seen: set[str] = set()
            for name, text in document.fields.items():
                counts = Counter(terms(text))
                by_field[name] = counts
                length = sum(counts.values())
                lengths[name] = length
                field_totals[name] += length
                field_counts[name] += 1
                seen.update(counts)
            self._tf[document.key] = by_field
            self._length[document.key] = lengths
            for term in seen:
                self._postings[term].add(document.key)

        self._average_length = {
            name: field_totals[name] / max(1, field_counts[name])
            for name in field_totals
        }

    def __len__(self) -> int:
        return len(self.documents)

    def _idf(self, term: str) -> float:
        n = len(self.documents)
        df = len(self._postings.get(term, ()))
        if n == 0 or df == 0:
            return 0.0
        return math.log(1.0 + (n - df + 0.5) / (df + 0.5))

    def _weighted_frequency(self, key: str, term: str) -> float:
        total = 0.0
        for field, counts in self._tf[key].items():
            frequency = counts.get(term, 0)
            if frequency <= 0:
                continue
            length = self._length[key][field]
            average = self._average_length.get(field, 0.0)
            normalization = (1.0 if average <= 0 else
                             1.0 - self.b + self.b * length / average)
            weight = self.field_weights.get(field, 1.0)
            total += weight * frequency / normalization
        return total

    def search(self, query: str, limit: int | None = None) -> list[LexicalHit]:
        """Return positive-scoring documents in deterministic rank order."""
        query_terms = set(terms(query))
        candidates: set[str] = set()
        for term in query_terms:
            candidates.update(self._postings.get(term, ()))

        hits: list[LexicalHit] = []
        for key in candidates:
            contributions: list[tuple[str, float]] = []
            score = 0.0
            for term in sorted(query_terms):
                tf = self._weighted_frequency(key, term)
                if tf <= 0:
                    continue
                value = self._idf(term) * ((self.k1 + 1.0) * tf
                                           / (self.k1 + tf))
                score += value
                contributions.append((term, round(value, 6)))
            if score > 0:
                hits.append(LexicalHit(key, round(score, 6),
                                       tuple(contributions)))
        hits.sort(key=lambda hit: (-hit.score, hit.key))
        return hits if limit is None else hits[:max(0, limit)]


def reciprocal_rank_fusion(
        rankings: Sequence[Sequence[str]], *, rank_constant: int = 60,
        weights: Sequence[float] | None = None,
        limit: int | None = None) -> list[tuple[str, float]]:
    """Combine independent rankings without comparing their score scales.

    Each document receives ``weight / (rank_constant + rank)`` from each
    ranking in which it appears.  Duplicate keys within one ranking count
    once.  Ties are resolved by key so repeated runs are byte-identical.
    """
    if rank_constant < 0:
        raise ValueError("rank_constant must be non-negative")
    if weights is None:
        weights = [1.0] * len(rankings)
    if len(weights) != len(rankings):
        raise ValueError("weights must match rankings")

    scores: dict[str, float] = defaultdict(float)
    for ranking, weight in zip(rankings, weights):
        seen: set[str] = set()
        rank = 0
        for key in ranking:
            if key in seen:
                continue
            seen.add(key)
            rank += 1
            scores[key] += float(weight) / (rank_constant + rank)
    fused = sorted(((key, round(score, 12)) for key, score in scores.items()),
                   key=lambda item: (-item[1], item[0]))
    return fused if limit is None else fused[:max(0, limit)]


def protected_rank_fusion(
        primary: Sequence[str], rankings: Sequence[Sequence[str]], *,
        protected: int = 3, rank_constant: int = 60,
        weights: Sequence[float] | None = None,
        limit: int | None = None) -> list[tuple[str, float]]:
    """Preserve a primary prefix, then fuse the remaining candidates.

    The protected prefix retains a high-precision ranker's exact order.
    Reciprocal-rank fusion fills the remaining positions from all supplied
    rankings. Duplicate keys count once, and key order resolves score ties.
    """
    return diverse_protected_rank_fusion(
        [(primary, protected)],
        rankings,
        rank_constant=rank_constant,
        weights=weights,
        limit=limit,
    )


def diverse_protected_rank_fusion(
        protected_rankings: Sequence[tuple[Sequence[str], int]],
        rankings: Sequence[Sequence[str]], *,
        rank_constant: int = 60,
        weights: Sequence[float] | None = None,
        limit: int | None = None) -> list[tuple[str, float]]:
    """Preserve bounded, novel prefixes from several ranking sources."""
    prefix: list[str] = []
    prefix_set: set[str] = set()
    for ranking, count in protected_rankings:
        if count < 0:
            raise ValueError("protected count must be non-negative")
        if count == 0:
            continue
        added = 0
        for key in ranking:
            if key in prefix_set:
                continue
            prefix.append(key)
            prefix_set.add(key)
            added += 1
            if added == count:
                break
    fused = reciprocal_rank_fusion(
        rankings,
        rank_constant=rank_constant,
        weights=weights,
    )
    tail = [item for item in fused if item[0] not in prefix_set]
    fused_scores = dict(fused)
    result = [
        (key, fused_scores.get(key, 0.0))
        for key in prefix
    ] + tail
    return result if limit is None else result[:max(0, limit)]
