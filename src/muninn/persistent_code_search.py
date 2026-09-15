"""Persistent, rebuildable source retrieval over a SQLite inverted index."""

from __future__ import annotations

import hashlib
import json
import math
import os
import posixpath
import re
import sqlite3
import tempfile
import zlib
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

from .activate import terms
from .code_search import (
    CodeHit,
    SourceRecord,
    _counterpart_stem,
    is_test_source,
    source_graph,
    source_records,
    source_symbol_relation_index,
)
from .lexical import DEFAULT_FIELD_WEIGHTS, reciprocal_rank_fusion


# Version 5 rebuilds postings with Unicode-aware lexical terms.
SCHEMA_VERSION = 5


def _chunks(values: list[str], size: int = 400):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _fields(record: SourceRecord) -> dict[str, str]:
    return {
        "path": record.path.replace("/", " "),
        "symbols": " ".join(record.symbols),
        "tags": "test" if is_test_source(record.path) else "production",
        "documentation": record.documentation,
        "body": record.text,
    }


def _fingerprint(records: list[SourceRecord]) -> str:
    result = hashlib.sha256()
    for record in sorted(records, key=lambda value: value.path):
        result.update(record.path.encode())
        result.update(b"\0")
        result.update(hashlib.sha256(record.text.encode()).digest())
    return result.hexdigest()


def _create_database(
    path: Path,
    records: list[SourceRecord],
    source_root: str,
    source_snapshot: str,
) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript("""
            PRAGMA journal_mode = DELETE;
            PRAGMA synchronous = FULL;
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE documents (
                path TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                bytes INTEGER NOT NULL,
                is_test INTEGER NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE field_stats (
                field TEXT PRIMARY KEY,
                average_length REAL NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE postings (
                term TEXT PRIMARY KEY,
                document_frequency INTEGER NOT NULL,
                payload BLOB NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE edges (
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                relation TEXT NOT NULL,
                provenance TEXT NOT NULL,
                weight REAL NOT NULL,
                PRIMARY KEY (source, target, relation, provenance)
            ) WITHOUT ROWID;
            CREATE INDEX edges_by_source ON edges(source);
            CREATE TABLE file_symbols (
                path TEXT NOT NULL,
                label TEXT NOT NULL,
                PRIMARY KEY (path, label)
            ) WITHOUT ROWID;
            CREATE TABLE symbol_nodes (
                path TEXT NOT NULL,
                label TEXT NOT NULL,
                PRIMARY KEY (path, label)
            ) WITHOUT ROWID;
            CREATE TABLE symbol_aliases (
                alias TEXT NOT NULL,
                path TEXT NOT NULL,
                label TEXT NOT NULL,
                PRIMARY KEY (alias, path, label)
            ) WITHOUT ROWID;
            CREATE INDEX symbol_aliases_by_alias ON symbol_aliases(alias);
            CREATE TABLE symbol_targets (
                path TEXT NOT NULL,
                label TEXT NOT NULL,
                target TEXT NOT NULL,
                PRIMARY KEY (path, label, target)
            ) WITHOUT ROWID;
        """)
        metadata = {
            "schema_version": str(SCHEMA_VERSION),
            "source_root": os.path.realpath(source_root) if source_root else "",
            "source_snapshot": source_snapshot,
            "source_fingerprint": _fingerprint(records),
            "documents": str(len(records)),
        }
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            sorted(metadata.items()))

        field_totals: Counter[str] = Counter()
        field_counts: Counter[str] = Counter()
        postings: dict[str, list[tuple[str, str, int, int]]] = defaultdict(list)
        for record in sorted(records, key=lambda value: value.path):
            encoded = record.text.encode()
            connection.execute(
                "INSERT INTO documents VALUES (?, ?, ?, ?)",
                (record.path, hashlib.sha256(encoded).hexdigest(), len(encoded),
                 int(is_test_source(record.path))))
            for field, text in _fields(record).items():
                counts = Counter(terms(text))
                length = sum(counts.values())
                field_totals[field] += length
                field_counts[field] += 1
                for term, frequency in counts.items():
                    postings[term].append(
                        (record.path, field, frequency, length))
                if field == "tags" and text:
                    # CodeSearchIndex's BM25F arm stems tags, while its
                    # historical overlap arm compares the literal tag.  Keep
                    # both representations so the persistent view can
                    # reproduce each contract without copying source bodies.
                    overlap_field = "overlap_tags"
                    field_totals[overlap_field] += 1
                    field_counts[overlap_field] += 1
                    postings[text].append(
                        (record.path, overlap_field, 1, 1))
        connection.executemany(
            "INSERT INTO field_stats VALUES (?, ?)",
            ((field, field_totals[field] / max(1, field_counts[field]))
             for field in sorted(field_totals)))
        for term in sorted(postings):
            values = sorted(postings[term])
            document_frequency = len({value[0] for value in values})
            payload = zlib.compress(json.dumps(
                values, ensure_ascii=False, separators=(",", ":")
            ).encode(), level=9)
            connection.execute(
                "INSERT INTO postings VALUES (?, ?, ?)",
                (term, document_frequency, payload))

        connection.executemany(
            "INSERT INTO file_symbols VALUES (?, ?)",
            (
                (record.path, label)
                for record in sorted(records, key=lambda value: value.path)
                for label in sorted(set(record.symbols))
            ),
        )
        nodes_by_path, aliases, targets = source_symbol_relation_index(records)
        connection.executemany(
            "INSERT INTO symbol_nodes VALUES (?, ?)",
            (
                node
                for path in sorted(nodes_by_path)
                for node in nodes_by_path[path]
            ),
        )
        connection.executemany(
            "INSERT INTO symbol_aliases VALUES (?, ?, ?)",
            (
                (alias, path, label)
                for alias in sorted(aliases)
                for path, label in aliases[alias]
            ),
        )
        connection.executemany(
            "INSERT INTO symbol_targets VALUES (?, ?, ?)",
            (
                (path, label, target)
                for (path, label), values in sorted(targets.items())
                for target in values
            ),
        )

        graph, relations = source_graph(records)
        edge_rows = []
        for source, neighbors in graph.items():
            for target, weight in neighbors.items():
                for relation in relations.get((source, target), ("related",)):
                    edge_rows.append(
                        (source, target, relation, "extracted", weight))
        connection.executemany(
            "INSERT INTO edges VALUES (?, ?, ?, ?, ?)", sorted(edge_rows))
        connection.commit()
        connection.execute("PRAGMA optimize")
    finally:
        connection.close()


class PersistentCodeSearchIndex:
    """Query a source index without loading its corpus into Python memory."""

    def __init__(self, database: str | os.PathLike[str]):
        self.database = Path(database).resolve()
        if not self.database.is_file():
            raise FileNotFoundError(self.database)
        self.connection = sqlite3.connect(self.database)
        try:
            self.connection.execute("PRAGMA query_only = ON")
            self.metadata = dict(self.connection.execute(
                "SELECT key, value FROM metadata"))
        except sqlite3.DatabaseError as error:
            self.close()
            raise ValueError("code index is unreadable") from error
        if int(self.metadata.get("schema_version", -1)) != SCHEMA_VERSION:
            self.close()
            raise ValueError("code index schema version is unsupported")
        self.document_count = int(self.metadata["documents"])
        self.field_weights = dict(DEFAULT_FIELD_WEIGHTS)
        self.average_lengths = dict(self.connection.execute(
            "SELECT field, average_length FROM field_stats"))
        self.paths = tuple(row[0] for row in self.connection.execute(
            "SELECT path FROM documents ORDER BY path"))
        self.path_set = set(self.paths)
        self.test_paths = {row[0] for row in self.connection.execute(
            "SELECT path FROM documents WHERE is_test = 1")}
        directories: dict[str, list[str]] = defaultdict(list)
        stems: dict[str, list[str]] = defaultdict(list)
        for path in self.paths:
            directories[posixpath.dirname(path)].append(path)
            stems[_counterpart_stem(path)].append(path)
        self.directory_paths = {
            directory: tuple(paths)
            for directory, paths in sorted(directories.items())
        }
        self.counterpart_stem_paths = {
            stem: tuple(paths) for stem, paths in sorted(stems.items())
        }
        file_symbols: dict[str, list[str]] = defaultdict(list)
        for path, label in self.connection.execute(
            "SELECT path, label FROM file_symbols ORDER BY path, label"
        ):
            file_symbols[path].append(label)
        self.file_symbols = {
            path: tuple(values) for path, values in file_symbols.items()
        }
        nodes: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for path, label in self.connection.execute(
            "SELECT path, label FROM symbol_nodes ORDER BY path, label"
        ):
            nodes[path].append((path, label))
        self.symbol_nodes_by_path = {
            path: tuple(values) for path, values in nodes.items()
        }
        aliases: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for alias, path, label in self.connection.execute(
            "SELECT alias, path, label FROM symbol_aliases "
            "ORDER BY alias, path, label"
        ):
            aliases[alias].append((path, label))
        self.symbol_aliases = {
            alias: tuple(values) for alias, values in aliases.items()
        }
        targets: dict[tuple[str, str], list[str]] = defaultdict(list)
        for path, label, target in self.connection.execute(
            "SELECT path, label, target FROM symbol_targets "
            "ORDER BY path, label, target"
        ):
            targets[(path, label)].append(target)
        self.symbol_relation_targets = {
            node: tuple(values) for node, values in targets.items()
        }

    @classmethod
    def build(cls, root: str, database: str | os.PathLike[str], *,
              structural: bool = False, source_snapshot: str = "",
              **source_options
              ) -> "PersistentCodeSearchIndex":
        records = source_records(
            root, structural=structural, **source_options)
        return cls.build_records(
            records,
            database,
            source_root=root,
            source_snapshot=source_snapshot,
        )

    @classmethod
    def build_records(cls, records: Iterable[SourceRecord],
                      database: str | os.PathLike[str], *,
                      source_root: str = "", source_snapshot: str = ""
                      ) -> "PersistentCodeSearchIndex":
        material = list(records)
        if len({record.path for record in material}) != len(material):
            raise ValueError("source records contain duplicate paths")
        target = Path(database).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".code-index-", suffix=".sqlite", dir=target.parent)
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            _create_database(
                temporary,
                material,
                source_root,
                source_snapshot,
            )
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()
        return cls(target)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "PersistentCodeSearchIndex":
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    @property
    def size_bytes(self) -> int:
        return self.database.stat().st_size

    def _postings(self, query_terms: set[str]):
        for group in _chunks(sorted(query_terms)):
            placeholders = ",".join("?" for _ in group)
            rows = self.connection.execute(
                "SELECT term, document_frequency, payload FROM postings "
                f"WHERE term IN ({placeholders})", group)
            for term, document_frequency, payload in rows:
                values = json.loads(zlib.decompress(payload))
                for path, field, frequency, length in values:
                    yield (term, path, field, frequency, length,
                           self.average_lengths[field], document_frequency)

    def bm25_ranking(self, query: str) -> list[str]:
        query_terms = set(terms(query))
        values: dict[str, dict[str, dict[str, tuple[int, int, float]]]] = \
            defaultdict(lambda: defaultdict(dict))
        frequencies: dict[str, int] = {}
        for term, path, field, frequency, length, average, document_frequency \
                in self._postings(query_terms):
            values[path][term][field] = (frequency, length, average)
            frequencies[term] = document_frequency
        field_order = ("path", "symbols", "tags", "documentation", "body")
        ranked = []
        for path, by_term in values.items():
            score = 0.0
            for term in sorted(query_terms):
                weighted_frequency = 0.0
                by_field = by_term.get(term, {})
                for field in field_order:
                    value = by_field.get(field)
                    if value is None:
                        continue
                    frequency, length, average = value
                    normalization = (1.0 if average <= 0 else
                                     1.0 - 0.75 + 0.75 * length / average)
                    weighted_frequency += (
                        self.field_weights.get(field, 1.0)
                        * frequency / normalization)
                if weighted_frequency <= 0:
                    continue
                document_frequency = frequencies[term]
                inverse = math.log(
                    1.0 + (self.document_count - document_frequency + 0.5)
                    / (document_frequency + 0.5))
                score += inverse * (
                    2.2 * weighted_frequency / (1.2 + weighted_frequency))
            # BM25FIndex exposes six-decimal scores.  CodeSearchIndex applies
            # the test-file penalty after that boundary, so parity requires
            # the persistent view to retain the same rounding point.
            score = round(score, 6)
            if path in self.test_paths:
                score *= 0.5
            if score > 0:
                ranked.append((path, score))
        ranked.sort(key=lambda item: (-item[1], item[0]))
        return [path for path, _score in ranked]

    def _field_matches(self, query: str) -> dict[str, dict[str, set[str]]]:
        query_terms = set(terms(query))
        matches: dict[str, dict[str, set[str]]] = defaultdict(
            lambda: defaultdict(set))
        for term, path, field, _frequency, _length, _average, _df \
                in self._postings(query_terms):
            matches[path][field].add(term)
        return matches

    def exact_ranking(self, query: str) -> list[str]:
        scored = []
        for path, fields in self._field_matches(query).items():
            score = (4.0 * len(fields.get("path", ()))
                     + 3.0 * len(fields.get("symbols", ())))
            if path in self.test_paths:
                score *= 0.5
            if score > 0:
                scored.append((path, score))
        scored.sort(key=lambda item: (-item[1], item[0]))
        return [path for path, _score in scored]

    def overlap_ranking(self, query: str) -> list[str]:
        query_terms = set(terms(query))
        weights = {"path": 4.0, "symbols": 3.0, "overlap_tags": 3.0,
                   "documentation": 2.0, "body": 1.0}
        scored = []
        for path, fields in self._field_matches(query).items():
            score = sum(weights[name] * len(values)
                        for name, values in fields.items()
                        if name in weights)
            if path in self.test_paths:
                score *= 0.5
            if score > 0:
                scored.append((path, score / max(1, len(query_terms))))
        scored.sort(key=lambda item: (-item[1], item[0]))
        return [path for path, _score in scored]

    def _neighbors(self, paths: list[str]) -> dict[str, dict[str, float]]:
        output: dict[str, dict[str, float]] = defaultdict(dict)
        for group in _chunks(sorted(set(paths))):
            placeholders = ",".join("?" for _ in group)
            rows = self.connection.execute(
                "SELECT source, target, max(weight) FROM edges "
                f"WHERE source IN ({placeholders}) GROUP BY source, target",
                group)
            for source, target, weight in rows:
                output[source][target] = weight
        return output

    def graph_ranking(self, query: str, *, bm25: list[str] | None = None,
                      exact: list[str] | None = None) -> list[str]:
        bm25 = (self.bm25_ranking(query) if bm25 is None else bm25)[:10]
        exact = (self.exact_ranking(query) if exact is None else exact)[:10]
        seeds = reciprocal_rank_fusion([bm25, exact], limit=15)
        frontier = {path: score for path, score in seeds}
        reached: dict[str, float] = defaultdict(float)
        for iteration in range(2):
            neighbors_by_path = self._neighbors(list(frontier))
            next_frontier: dict[str, float] = defaultdict(float)
            for path, amount in sorted(frontier.items()):
                neighbors = neighbors_by_path.get(path, {})
                normalizer = sum(neighbors.values())
                if normalizer <= 0:
                    continue
                for neighbor, weight in neighbors.items():
                    value = amount * weight / normalizer * 0.6 ** (iteration + 1)
                    reached[neighbor] += value
                    next_frontier[neighbor] += value
            frontier = next_frontier
        return [path for path, _score in sorted(
            reached.items(), key=lambda item: (-item[1], item[0]))]

    def symbol_ranking(self, query: str) -> list[str]:
        """Rank files by their strongest persisted definition-name match."""
        query_terms = list(dict.fromkeys(
            token for token in re.findall(r"\w+", query.casefold())
            if not (token.isascii() and token.isalpha() and len(token) <= 2)
        ))
        if not query_terms:
            return []
        labels = {
            path: tuple(dict.fromkeys((
                *self.file_symbols.get(path, ()), Path(path).name,
            )))
            for path in self.paths
        }
        population = sum(len(values) for values in labels.values()) or 1
        document_frequency = {
            term: sum(
                term in label.casefold()
                for values in labels.values()
                for label in values
            )
            for term in query_terms
        }
        inverse = {
            term: math.log(
                1.0 + population / (1.0 + document_frequency[term])
            )
            for term in query_terms
        }
        scored = []
        for path, values in labels.items():
            best_score = 0.0
            best_length = 0
            source = path.casefold()
            for label in values:
                normalized = label.casefold().removesuffix("()")
                matched = 0
                tiered = 0.0
                score = 0.0
                for term in query_terms:
                    weight = inverse[term]
                    if term == normalized:
                        tiered += 1000.0 * weight
                        matched += 1
                    elif normalized.startswith(term):
                        tiered += 100.0 * weight
                        matched += 1
                    elif term in normalized:
                        score += weight
                        matched += 1
                    if term in source:
                        score += 0.5 * weight
                if tiered:
                    score += tiered * (matched / len(query_terms)) ** 2
                if score > best_score or (
                    score == best_score and len(label) < best_length
                ):
                    best_score = score
                    best_length = len(label)
            if best_score > 0:
                scored.append((path, best_score, best_length))
        scored.sort(key=lambda item: (-item[1], item[2], item[0]))
        return [path for path, _score, _length in scored]

    def directory_neighborhood_ranking(
        self,
        anchors: Iterable[str],
        *,
        seed_files: int = 10,
    ) -> list[str]:
        """Rank persisted test counterparts and adjacent source files."""
        if seed_files < 1:
            raise ValueError("directory seed files must be positive")
        anchor_paths = list(dict.fromkeys(
            path for path in anchors if path in self.path_set
        ))[:seed_files]
        scores: dict[str, tuple[float, int]] = {}

        def admit(path: str, score: float, seed_rank: int) -> None:
            candidate = (score, -seed_rank)
            if candidate > scores.get(path, (-1.0, 0)):
                scores[path] = candidate

        for seed_rank, path in enumerate(anchor_paths, 1):
            admit(path, 100.0 / seed_rank, seed_rank)
            directory = posixpath.dirname(path)
            for candidate in self.directory_paths.get(directory, ()):
                admit(candidate, 1.0 / seed_rank, seed_rank)
            for candidate in self.counterpart_stem_paths.get(
                _counterpart_stem(path), ()
            ):
                admit(candidate, 50.0 / seed_rank, seed_rank)

        return sorted(
            self.paths,
            key=lambda path: (
                -scores.get(path, (0.0, 0))[0],
                -scores.get(path, (0.0, 0))[1],
                path,
            ),
        )

    def symbol_dependency_ranking(
        self,
        anchors: Iterable[str],
        *,
        depth: int = 2,
        neighbor_cap: int = 50,
        symbols_per_anchor: int = 1,
    ) -> list[str]:
        """Walk persisted symbol calls from bounded source-file seeds."""
        if depth < 0:
            raise ValueError("symbol dependency depth must be non-negative")
        if neighbor_cap < 1:
            raise ValueError("symbol dependency neighbor cap must be positive")
        if symbols_per_anchor < 1:
            raise ValueError("symbols per anchor must be positive")

        anchor_paths = list(dict.fromkeys(
            path for path in anchors if path in self.path_set
        ))
        seen: dict[tuple[str, str], tuple[float, int]] = {}
        frontier = []
        for rank, path in enumerate(anchor_paths, 1):
            nodes = self.symbol_nodes_by_path.get(path, ())
            selected_nodes = sorted(
                nodes,
                key=lambda node: (
                    -len(self.symbol_relation_targets.get(node, ())),
                    node[1],
                ),
            )[:symbols_per_anchor]
            for selected in selected_nodes:
                seen[selected] = (1.0 / rank, 0)
                frontier.append(selected)

        for hop in range(1, depth + 1):
            following = []
            for node in sorted(
                frontier, key=lambda item: (-seen[item][0], item)
            ):
                candidates = []
                for target in self.symbol_relation_targets.get(node, ()):
                    for candidate in self.symbol_aliases.get(target, ()):
                        if candidate == node or candidate in candidates:
                            continue
                        candidates.append(candidate)
                        if len(candidates) >= neighbor_cap:
                            break
                    if len(candidates) >= neighbor_cap:
                        break
                for candidate in candidates:
                    if candidate in seen:
                        continue
                    score = seen[node][0] * (1.0 / (1 + hop))
                    seen[candidate] = (score, hop)
                    following.append(candidate)
            frontier = following
            if not frontier:
                break

        path_scores: dict[str, tuple[float, int]] = {}
        for (path, _symbol), value in seen.items():
            current = path_scores.get(path)
            if current is None or (-value[0], value[1]) < (
                -current[0], current[1]
            ):
                path_scores[path] = value
        return [
            path for path, _value in sorted(
                path_scores.items(),
                key=lambda item: (-item[1][0], item[1][1], item[0]),
            )
        ]

    def search(self, query: str, *, mode: str = "hybrid",
               limit: int = 10) -> list[CodeHit]:
        rankings: dict[str, list[str]] = {}

        def ranking(name: str) -> list[str]:
            if name not in rankings:
                if name == "overlap":
                    rankings[name] = self.overlap_ranking(query)
                elif name == "bm25f":
                    rankings[name] = self.bm25_ranking(query)
                elif name == "exact":
                    rankings[name] = self.exact_ranking(query)
                elif name == "graph":
                    rankings[name] = self.graph_ranking(
                        query, bm25=ranking("bm25f"), exact=ranking("exact"))
                elif name == "symbol":
                    rankings[name] = self.symbol_ranking(query)
                else:
                    raise ValueError(f"unknown retrieval arm: {name}")
            return rankings[name]

        if mode == "overlap":
            fused = [(path, 1.0 / rank)
                     for rank, path in enumerate(ranking("overlap"), 1)]
            used = ("overlap",)
        elif mode == "bm25f":
            fused = [(path, 1.0 / rank)
                     for rank, path in enumerate(ranking("bm25f"), 1)]
            used = ("bm25f",)
        elif mode == "lexical-hybrid":
            fused = reciprocal_rank_fusion(
                [ranking("bm25f"), ranking("exact")], weights=[1.0, 1.0])
            used = ("bm25f", "exact")
        elif mode == "hybrid":
            lexical = reciprocal_rank_fusion(
                [ranking("bm25f"), ranking("exact")], weights=[1.0, 1.0])
            first = ranking("bm25f")[:1]
            ordered = first + [path for path, _score in lexical
                               if path not in set(first)]
            fused = [(path, 1.0 / rank)
                     for rank, path in enumerate(ordered, 1)]
            used = ("bm25f", "exact")
        elif mode == "graph-fusion":
            fused = reciprocal_rank_fusion(
                [ranking("bm25f"), ranking("exact"), ranking("graph")],
                weights=[1.0, 1.0, 0.5])
            used = ("bm25f", "exact", "graph")
        elif mode == "symbol":
            fused = [(path, 1.0 / rank)
                     for rank, path in enumerate(ranking("symbol"), 1)]
            used = ("symbol",)
        elif mode == "structural-fusion":
            fused = reciprocal_rank_fusion(
                [ranking("bm25f"), ranking("exact"), ranking("graph"),
                 ranking("symbol")],
                weights=[1.0, 1.0, 0.5, 1.0])
            used = ("bm25f", "exact", "graph", "symbol")
        else:
            raise ValueError(f"unknown code-search mode: {mode}")

        rank_maps = {name: {path: rank for rank, path in enumerate(values, 1)}
                     for name, values in rankings.items()}
        hits = []
        for path, score in fused[:max(0, limit)]:
            arms = tuple((name, rank_maps[name][path]) for name in used
                         if path in rank_maps[name])
            hits.append(CodeHit(path, round(score, 12), arms))
        return hits

    def search_personalized(self, query: str, *, dynamics,
                            repository: str, mode: str = "hybrid",
                            limit: int = 10, candidate_limit: int = 20,
                            config=None):
        """Apply one consumer's bounded memory after shared retrieval."""
        from .code_memory import (CodeMemoryConfig,
                                  personalize_code_directory_hits,
                                  personalize_code_hits)
        if candidate_limit < limit:
            raise ValueError("candidate limit must cover the result limit")
        resolved = config or CodeMemoryConfig()
        if resolved.directory_route_weight:
            candidates = self.search(
                query, mode=mode, limit=self.document_count)
            return personalize_code_directory_hits(
                candidates, dynamics, repository,
                route_weight=resolved.directory_route_weight, limit=limit)
        candidates = self.search(query, mode=mode, limit=candidate_limit)
        return personalize_code_hits(
            candidates, query, dynamics, repository, limit=limit,
            config=resolved)
