"""Directed source dependencies with optional language-routed extraction."""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .code_context import RelationEvidence
from .code_search import SourceRecord, source_dependency_details, source_records


GRAPHIFY_EXTENSIONS = frozenset({
    ".c", ".cc", ".cpp", ".cs", ".h", ".hpp", ".php", ".ts", ".tsx",
})


@dataclass(frozen=True)
class DependencyGraph:
    """A directed file graph and the provider used for each source family."""

    relations: dict[tuple[str, str], tuple[str, ...]]
    evidence: dict[tuple[str, str], tuple[RelationEvidence, ...]]
    providers: tuple[tuple[str, str], ...]
    graphify_used: bool
    graphify_error: str | None = None


def resolve_source_path(value: str, available: set[str]) -> str | None:
    """Resolve a provider path only when one indexed path has that suffix."""
    raw = value.replace("\\", "/").lstrip("/")
    candidates = [path for path in available
                  if path == raw or path.endswith("/" + raw)]
    return candidates[0] if len(candidates) == 1 else None


def _line(value: object) -> int | None:
    match = re.match(r"L(\d+)", str(value or ""))
    return int(match.group(1)) if match else None


def _graphify_dependencies(root: Path, paths: list[Path],
                           cache_root: Path) -> tuple[
        dict[tuple[str, str], tuple[str, ...]],
        dict[tuple[str, str], tuple[RelationEvidence, ...]]]:
    from graphify.extract import extract

    extracted = extract(paths, cache_root=cache_root, root=root, parallel=False)
    available = {
        path.relative_to(root).as_posix()
        for path in paths
    }
    node_by_id = {
        str(node.get("id")): node
        for node in extracted.get("nodes", [])
        if node.get("id")
    }
    source_by_node = {}
    for node_id, node in node_by_id.items():
        path = resolve_source_path(str(node.get("source_file", "")), available)
        if path:
            source_by_node[node_id] = path
    relations: dict[tuple[str, str], set[str]] = {}
    evidence: dict[tuple[str, str], set[RelationEvidence]] = {}
    for edge in extracted.get("edges", []):
        source = source_by_node.get(str(edge.get("source")))
        target = source_by_node.get(str(edge.get("target")))
        if not source or not target or source == target:
            continue
        relation = str(edge.get("relation") or "references")
        key = (source, target)
        relations.setdefault(key, set()).add(relation)
        target_node = node_by_id.get(str(edge.get("target")), {})
        symbol = str(
            (edge.get("metadata") or {}).get("ref_token")
            or target_node.get("label", ""))
        provenance = "graphify:" + str(
            edge.get("confidence") or edge.get("_origin") or "extracted")
        evidence.setdefault(key, set()).add(RelationEvidence(
            relation=relation,
            symbol=symbol,
            provenance=provenance,
            source_line=_line(edge.get("source_location")),
            target_line=_line(target_node.get("source_location")),
        ))
    stable_relations = {
        edge: tuple(sorted(names))
        for edge, names in sorted(relations.items())
    }
    stable_evidence = {
        edge: tuple(sorted(values, key=lambda value: json.dumps({
            "relation": value.relation,
            "symbol": value.symbol,
            "provenance": value.provenance,
            "source_line": value.source_line,
            "target_line": value.target_line,
        }, sort_keys=True)))
        for edge, values in sorted(evidence.items())
    }
    return stable_relations, stable_evidence


def build_dependency_graph_records(root: str | Path,
                                   records: Iterable[SourceRecord], *,
                                   cache_root: str | Path | None = None,
                                   use_graphify: bool = True) -> DependencyGraph:
    """Build a directed graph from a complete, caller-supplied record set."""
    source_root = Path(root).resolve()
    records = list(records)
    internal_relations, internal_evidence = source_dependency_details(records)
    routed_paths = {
        record.path for record in records
        if Path(record.path).suffix.lower() in GRAPHIFY_EXTENSIONS
    }
    providers = tuple(sorted({
        (Path(record.path).suffix.lower(),
         "graphify" if (use_graphify and record.path in routed_paths)
         else "muninn")
        for record in records
    }))
    if not use_graphify or not routed_paths:
        return DependencyGraph(
            internal_relations, internal_evidence, providers, False)

    temporary = None
    if cache_root is None:
        temporary = tempfile.TemporaryDirectory(
            prefix="muninn-dependency-cache-")
        graphify_cache = Path(temporary.name)
    else:
        graphify_cache = Path(cache_root)
    try:
        paths = [source_root / record.path for record in records]
        graphify_relations, graphify_evidence = _graphify_dependencies(
            source_root, paths, graphify_cache)
    except Exception as error:  # The optional provider must fail soft.
        return DependencyGraph(
            internal_relations, internal_evidence, providers, False,
            f"{type(error).__name__}: {error}")
    finally:
        if temporary is not None:
            temporary.cleanup()

    relations = {
        edge: names for edge, names in internal_relations.items()
        if edge[0] not in routed_paths
    }
    relations.update({
        edge: names for edge, names in graphify_relations.items()
        if edge[0] in routed_paths
    })
    evidence = {
        edge: values for edge, values in internal_evidence.items()
        if edge[0] not in routed_paths
    }
    evidence.update({
        edge: values for edge, values in graphify_evidence.items()
        if edge[0] in routed_paths
    })
    return DependencyGraph(
        dict(sorted(relations.items())), dict(sorted(evidence.items())),
        providers, True)


def build_dependency_graph(root: str | Path, *,
                           cache_root: str | Path | None = None,
                           use_graphify: bool = True) -> DependencyGraph:
    """Scan a source tree and build its directed dependency graph."""
    source_root = Path(root).resolve()
    records = source_records(str(source_root), structural=True)
    return build_dependency_graph_records(
        source_root, records, cache_root=cache_root,
        use_graphify=use_graphify)
