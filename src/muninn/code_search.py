"""Deterministic source-file retrieval for coding tasks.

Source files remain authoritative.  This module builds an in-memory,
rebuildable view with three independent retrieval arms:

* BM25F over paths, symbols, documentation, and source text;
* exact path and symbol matching;
* a bounded walk over Python import and unambiguous symbol-reference edges.

Reciprocal-rank fusion combines the arms without assuming that their raw
scores are comparable.  Muninn's personal usage signals belong after this
candidate stage, where they can reorder results without changing source
files or the shared structural index.
"""

from __future__ import annotations

import ast
import io
import math
import os
import posixpath
import re
import tokenize
import warnings
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .activate import terms
from .code_context import (CodeContextPack, RelationEvidence,
                           assemble_code_context)
from .lexical import BM25FIndex, LexicalDocument, reciprocal_rank_fusion


SOURCE_EXTENSIONS = frozenset({
    ".c", ".cc", ".cpp", ".cs", ".css", ".go", ".h", ".hpp", ".html",
    ".java", ".js", ".jsx", ".kt", ".kts", ".lua", ".md", ".php",
    ".py", ".rb", ".rs", ".rst", ".scala", ".sh", ".sql", ".swift",
    ".toml", ".ts", ".tsx", ".yaml", ".yml",
})
SKIP_DIRECTORIES = frozenset({
    ".git", ".hg", ".muninn", ".mypy_cache", ".pytest_cache", ".tox",
    ".venv", "__pycache__", "build", "dist", "node_modules", "vendor",
})
MAX_SOURCE_BYTES = 512 * 1024
TEST_DIRECTORIES = frozenset({"test", "tests", "testing", "qa", "e2e",
                              "scenarios", "fixtures"})
DEPENDENCY_RELATION_WEIGHTS = {
    "imports": 2.0,
    "imports_ambiguous": 0.5,
    "calls": 2.0,
    "calls_ambiguous": 0.5,
    "implements": 1.5,
    "implements_ambiguous": 0.375,
    "inherits": 1.5,
    "inherits_ambiguous": 0.375,
    "references": 1.0,
    "references_ambiguous": 0.25,
}


def _counterpart_stem(path: str) -> str:
    stem = posixpath.splitext(posixpath.basename(path))[0].lower()
    stem = re.sub(r"^(?:test|tests)[_.-]+", "", stem)
    return re.sub(r"[_.-]+(?:test|tests|spec)$", "", stem)


@dataclass(frozen=True)
class SourceRecord:
    """One source file and the deterministic facts derived from it."""

    path: str
    text: str
    symbols: tuple[str, ...] = ()
    documentation: str = ""
    imports: tuple[str, ...] = ()
    references: tuple[str, ...] = ()
    relation_references: tuple[tuple[str, str], ...] = ()
    symbol_locations: tuple[tuple[str, int], ...] = ()
    symbol_relations: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class CodeHit:
    """One result with the rank contributed by each retrieval arm."""

    path: str
    score: float
    arms: tuple[tuple[str, int], ...]


class _PythonFacts(ast.NodeVisitor):
    def __init__(self):
        self.symbols: set[str] = set()
        self.symbol_locations: set[tuple[str, int]] = set()
        self.imports: set[str] = set()
        self.references: set[str] = set()
        self.relation_references: set[tuple[str, str]] = set()
        self.docs: list[str] = []

    def _definition(self, node) -> None:
        self.symbols.add(node.name)
        if getattr(node, "lineno", None):
            self.symbol_locations.add((node.name, int(node.lineno)))
        doc = ast.get_docstring(node, clean=False)
        if doc:
            self.docs.append(doc)
        self.generic_visit(node)

    def visit_FunctionDef(self, node):  # noqa: N802 (ast visitor contract)
        self._definition(node)

    def visit_AsyncFunctionDef(self, node):  # noqa: N802
        self._definition(node)

    def visit_ClassDef(self, node):  # noqa: N802
        self._definition(node)

    def visit_Import(self, node):  # noqa: N802
        for alias in node.names:
            self.imports.add(alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node):  # noqa: N802
        prefix = "." * node.level + (node.module or "")
        if prefix:
            self.imports.add(prefix)
        for alias in node.names:
            if alias.name != "*":
                self.imports.add(prefix + "." + alias.name if prefix else alias.name)
        self.generic_visit(node)

    def visit_Name(self, node):  # noqa: N802
        if len(node.id) >= 3:
            self.references.add(node.id)

    def visit_Attribute(self, node):  # noqa: N802
        if len(node.attr) >= 3:
            self.references.add(node.attr)
        self.generic_visit(node)

    def visit_Call(self, node):  # noqa: N802
        function = node.func
        target = (function.id if isinstance(function, ast.Name)
                  else function.attr if isinstance(function, ast.Attribute)
                  else "")
        if len(target) >= 3:
            self.relation_references.add(("calls", target))
        self.generic_visit(node)


def _comments(text: str) -> list[str]:
    found: list[str] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.COMMENT:
                value = token.string.lstrip("#").strip()
                if value:
                    found.append(value)
    except (IndentationError, SyntaxError, tokenize.TokenError):
        pass
    return found


def _python_record(path: str, text: str) -> SourceRecord:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return SourceRecord(path, text)
    facts = _PythonFacts()
    module_doc = ast.get_docstring(tree, clean=False)
    if module_doc:
        facts.docs.append(module_doc)
    try:
        facts.visit(tree)
    except RecursionError:
        # A valid file may contain an expression deeper than NodeVisitor's
        # recursive walk can traverse.  The file must remain searchable by
        # path and text even when structural facts cannot be extracted.
        return SourceRecord(path, text)
    facts.docs.extend(_comments(text))
    return SourceRecord(
        path=path,
        text=text,
        symbols=tuple(sorted(facts.symbols)),
        symbol_locations=tuple(sorted(facts.symbol_locations)),
        documentation="\n".join(facts.docs),
        imports=tuple(sorted(facts.imports)),
        references=tuple(sorted(facts.references)),
        relation_references=tuple(sorted(facts.relation_references)),
    )


def source_record(path: str, text: str) -> SourceRecord:
    """Derive the structural facts available in the standard-library profile."""
    if Path(path).suffix.lower() == ".py":
        return _python_record(path, text)
    return SourceRecord(path, text)


def _java_csharp_declaration_locations(
        path: str, text: str) -> set[tuple[str, int]]:
    """Recover located declarations omitted by a capped structural graph."""
    if Path(path).suffix.lower() not in {".java", ".cs"}:
        return set()
    type_pattern = re.compile(
        r"\b(?:class|enum|interface|record|struct)\s+([A-Za-z_]\w*)")
    type_matches = list(type_pattern.finditer(text))
    type_names = {match.group(1) for match in type_matches}
    locations = {
        (match.group(1), text.count("\n", 0, match.start(1)) + 1)
        for match in type_matches
    }
    modifiers = (
        r"(?:public|private|protected|internal|static|final|abstract|"
        r"synchronized|native|virtual|override|async|sealed|extern)")
    method_pattern = re.compile(
        rf"^\s*(?:{modifiers}\s+)+"
        rf"(?:[A-Za-z_][\w<>,.?\[\]]*\s+)?([A-Za-z_]\w*)\s*\(",
        re.MULTILINE)
    for match in method_pattern.finditer(text):
        if match.group(1) in type_names:
            continue
        line = text.count("\n", 0, match.start(1)) + 1
        locations.add((match.group(1), line))
    return locations


def _java_csharp_declarations(path: str, text: str) -> set[str]:
    """Return declaration names recovered by the bounded fallback."""
    return {name for name, _line in
            _java_csharp_declaration_locations(path, text)}


def _node_line(node: dict) -> int | None:
    """Return one line from the extractor's deterministic location field."""
    match = re.fullmatch(r"L(\d+)", str(node.get("source_location", "")))
    return int(match.group(1)) if match else None


def _symbol_location_aliases(label: str, line: int) -> set[tuple[str, int]]:
    """Index a parser label under its exact and unqualified forms."""
    unqualified = label.rsplit("::", 1)[-1].rsplit(".", 1)[-1]
    return {(label, line), (unqualified, line)}


def structural_source_record(path: str, text: str,
                             filesystem_path: str) -> SourceRecord:
    """Add optional tree-sitter facts while preserving the stdlib facts."""
    base = source_record(path, text)
    try:
        from . import extract
        if not extract.available():
            return base
        graph = extract.extract_file(filesystem_path, src=path)
        symbol_graph = extract.code_symbol_relations(
            filesystem_path, src=path,
        )
    except (ImportError, OSError):
        return base
    nodes = graph.get("nodes", [])
    labels = {node.get("id"): str(node.get("label", ""))
              for node in nodes}
    symbol_kinds = {
        "class", "component", "constructor", "enum", "export", "function",
        "impl", "interface", "method", "namespace", "struct", "trait",
        "type",
    }
    symbols = {str(node.get("label", "")) for node in nodes
               if node.get("kind") in symbol_kinds and node.get("label")}
    fallback_locations = _java_csharp_declaration_locations(path, text)
    symbols.update(name for name, _line in fallback_locations)
    symbol_locations = set(base.symbol_locations) | fallback_locations
    fallback_names = {name for name, _line in fallback_locations}
    for node in nodes:
        label = str(node.get("label", ""))
        line = _node_line(node)
        unqualified = label.rsplit("::", 1)[-1].rsplit(".", 1)[-1]
        if (node.get("kind") in symbol_kinds and label and line is not None
                and unqualified not in fallback_names):
            symbol_locations.update(_symbol_location_aliases(label, line))
    for symbol in symbol_graph.get("symbols", []):
        label = str(symbol.get("qualified", ""))
        line = int(symbol.get("start_line", 0) or 0)
        if label and line > 0 and symbol.get("kind") != "module":
            symbol_locations.update(_symbol_location_aliases(label, line))
    imports = {str(node.get("label", "")) for node in nodes
               if node.get("kind") == "import" and node.get("label")}
    documentation = [str(node.get("body", "")) for node in nodes
                     if node.get("body")]
    distinct_documentation = list(dict.fromkeys(
        value for value in (base.documentation, *documentation) if value))
    references = set()
    relation_references = set()
    for edge in graph.get("links", []):
        relation = str(edge.get("relation", ""))
        if relation not in {
                "calls", "configures", "implements", "inherits",
                "references"}:
            continue
        target = labels.get(edge.get("target"), edge.get("target", ""))
        if target:
            value = str(target)
            references.add(value)
            relation_references.add((relation, value))
    symbol_relations = {
        (
            str(edge.get("source", "")),
            str(edge.get("target", "")),
            str(edge.get("relation", "")),
        )
        for edge in symbol_graph.get("relations", [])
        if edge.get("source") and edge.get("target") and edge.get("relation")
    }
    return SourceRecord(
        path=path,
        text=text,
        symbols=tuple(sorted(set(base.symbols) | symbols)),
        symbol_locations=tuple(sorted(symbol_locations)),
        documentation="\n".join(distinct_documentation),
        imports=tuple(sorted(set(base.imports) | imports)),
        references=tuple(sorted(set(base.references) | references)),
        relation_references=tuple(sorted(
            set(base.relation_references) | relation_references)),
        symbol_relations=tuple(sorted(symbol_relations)),
    )


def source_records(root: str, *, extensions: Iterable[str] | None = None,
                   max_bytes: int = MAX_SOURCE_BYTES,
                   structural: bool = False) -> list[SourceRecord]:
    """Read bounded source files below ``root`` in deterministic order."""
    base = Path(root).resolve()
    allowed = SOURCE_EXTENSIONS if extensions is None else frozenset(extensions)
    records: list[SourceRecord] = []
    for directory, names, files in os.walk(base):
        names[:] = sorted(name for name in names
                          if name not in SKIP_DIRECTORIES
                          and not name.startswith(".git"))
        for name in sorted(files):
            path = Path(directory, name)
            if path.suffix.lower() not in allowed:
                continue
            try:
                if (path.is_symlink() or not path.is_file()
                        or path.stat().st_size > max_bytes):
                    continue
                text = path.read_bytes().decode("utf-8", "replace")
            except OSError:
                continue
            relative = path.relative_to(base).as_posix()
            record = (structural_source_record(relative, text, str(path))
                      if structural else source_record(relative, text))
            records.append(record)
    return records


def _module_aliases(path: str) -> set[str]:
    normalized = path.replace("\\", "/")
    suffix = Path(normalized).suffix
    stemmed = normalized[:-len(suffix)] if suffix else normalized
    parts = stemmed.split("/")
    if parts[-1] in {"__init__", "index", "mod"}:
        parts = parts[:-1]
    aliases = set()
    if parts:
        aliases.update({"/".join(parts), ".".join(parts), parts[-1]})
    if parts and parts[0] in {"app", "lib", "pkg", "python", "src"}:
        aliases.update({"/".join(parts[1:]), ".".join(parts[1:])})
    return {alias for alias in aliases if alias}


def is_test_source(path: str) -> bool:
    """Return whether a source path belongs to test or fixture code."""
    parts = path.lower().replace("\\", "/").split("/")
    name = parts[-1]
    return (any(part in TEST_DIRECTORIES for part in parts[:-1])
            or name.startswith("test_") or name.endswith("_test.py")
            or name == "conftest.py")


def _absolute_import(source_path: str, imported: str) -> str:
    if not imported.startswith("."):
        return imported
    level = len(imported) - len(imported.lstrip("."))
    suffix = imported[level:]
    aliases = sorted(_module_aliases(source_path), key=len)
    module = aliases[0] if aliases else ""
    package = module.split(".")
    if not source_path.endswith("/__init__.py") and package:
        package = package[:-1]
    keep = max(0, len(package) - level + 1)
    base = package[:keep]
    if suffix:
        base.extend(suffix.split("."))
    return ".".join(base)


def _import_probes(source_path: str, imported: str) -> list[str]:
    value = imported.strip().strip("'\"")
    probes = []
    if value.startswith(".") and not source_path.endswith(".py"):
        parent = posixpath.dirname(source_path.replace("\\", "/"))
        joined = posixpath.normpath(posixpath.join(parent, value))
        probes.extend((joined, joined.replace("/", ".")))
    elif source_path.endswith(".py"):
        absolute = _absolute_import(source_path, value)
        probes.extend((absolute, absolute.replace(".", "/")))
    normalized = value.replace("::", ".").replace("/", ".")
    probes.extend((value, normalized, normalized.rsplit(".", 1)[-1]))
    return list(dict.fromkeys(probe for probe in probes if probe))


def source_dependency_details(records: Iterable[SourceRecord], *,
                              imports_only: bool = False,
                              ambiguous_symbol_limit: int = 1) -> tuple[
        dict[tuple[str, str], tuple[str, ...]],
        dict[tuple[str, str], tuple[RelationEvidence, ...]]]:
    """Return directed file dependencies and their extracted evidence."""
    if ambiguous_symbol_limit < 1:
        raise ValueError("ambiguous symbol limit must be positive")
    material = list(records)
    module_paths: dict[str, list[str]] = defaultdict(list)
    module_suffix_paths: dict[str, set[str]] = defaultdict(set)
    symbol_paths: dict[str, list[str]] = defaultdict(list)
    symbol_lines: dict[str, dict[str, set[int]]] = defaultdict(
        lambda: defaultdict(set))
    for record in material:
        for alias in _module_aliases(record.path):
            module_paths[alias].append(record.path)
        for symbol in record.symbols:
            symbol_paths[symbol].append(record.path)
        for symbol, line in record.symbol_locations:
            symbol_lines[record.path][symbol].add(line)

    for alias, paths in module_paths.items():
        for position, character in enumerate(alias):
            if character in {".", "/"} and position + 1 < len(alias):
                module_suffix_paths[alias[position + 1:]].update(paths)

    relations: dict[tuple[str, str], set[str]] = defaultdict(set)
    evidence: dict[tuple[str, str], set[RelationEvidence]] = defaultdict(set)

    def connect(left: str, right: str, relation: str,
                symbol: str = "") -> None:
        if left == right:
            return
        relations[(left, right)].add(relation)
        locations = symbol_lines[right].get(symbol, set())
        target_line = next(iter(locations)) if len(locations) == 1 else None
        item = RelationEvidence(relation, symbol, target_line=target_line)
        evidence[(left, right)].add(item)

    for record in material:
        import_links = 0
        for imported in record.imports:
            candidates: set[str] = set()
            for probe in _import_probes(record.path, imported):
                candidates = set(module_paths.get(probe, ()))
                if candidates:
                    break
                candidates = module_suffix_paths.get(probe, set())
                if candidates:
                    break
            targets = sorted(candidates - {record.path})
            if targets and len(candidates) <= ambiguous_symbol_limit:
                relation = (
                    "imports" if len(candidates) == 1
                    else "imports_ambiguous"
                )
                for target in targets:
                    connect(record.path, target, relation, imported)
                    import_links += 1
                    if ambiguous_symbol_limit > 1 and import_links >= 100:
                        break
            if ambiguous_symbol_limit > 1 and import_links >= 100:
                break
        if imports_only:
            continue
        linked = 0
        typed_symbols = set()
        typed_relations = {"calls", "implements", "inherits", "references"}
        for relation, symbol in record.relation_references:
            if linked >= 100:
                break
            if relation not in typed_relations:
                continue
            typed_symbols.add(symbol)
            candidates = sorted(symbol_paths.get(symbol, ()))
            targets = [path for path in candidates if path != record.path]
            if targets and len(candidates) <= ambiguous_symbol_limit:
                edge_relation = (
                    relation if len(candidates) == 1
                    else f"{relation}_ambiguous"
                )
                for target in targets:
                    connect(record.path, target, edge_relation, symbol)
                    linked += 1
                    if linked >= 100:
                        break
            if linked >= 100:
                break
        for symbol in record.references:
            if linked >= 100:
                break
            if symbol in typed_symbols:
                continue
            candidates = sorted(symbol_paths.get(symbol, ()))
            targets = [path for path in candidates if path != record.path]
            if targets and len(candidates) <= ambiguous_symbol_limit:
                relation = (
                    "references" if len(candidates) == 1
                    else "references_ambiguous"
                )
                for target in targets:
                    connect(record.path, target, relation, symbol)
                    linked += 1
                    if linked >= 100:
                        break
            if linked >= 100:
                break
    stable_relations = {edge: tuple(sorted(values))
                        for edge, values in sorted(relations.items())}
    stable_evidence = {edge: tuple(sorted(
        values,
        key=lambda value: (value.relation, value.symbol, value.provenance,
                           value.target_line or 0)))
                       for edge, values in sorted(evidence.items())}
    return stable_relations, stable_evidence


def _source_graph_from_dependencies(
        dependencies: dict[tuple[str, str], tuple[str, ...]],
        dependency_evidence: dict[
            tuple[str, str], tuple[RelationEvidence, ...]],
) -> tuple[
        dict[str, dict[str, float]],
        dict[tuple[str, str], tuple[str, ...]],
        dict[tuple[str, str], tuple[RelationEvidence, ...]]]:
    """Build an undirected retrieval graph from resolved dependencies."""
    graph: dict[str, dict[str, float]] = defaultdict(dict)
    relations: dict[tuple[str, str], set[str]] = defaultdict(set)
    for (left, right), names in dependencies.items():
        weight = max(
            DEPENDENCY_RELATION_WEIGHTS.get(name, 0.5) for name in names)
        graph[left][right] = max(graph[left].get(right, 0.0), weight)
        graph[right][left] = max(graph[right].get(left, 0.0), weight)
        relations[(left, right)].update(names)
        relations[(right, left)].update(names)
    stable_graph = {path: dict(sorted(neighbors.items()))
                    for path, neighbors in sorted(graph.items())}
    stable_relations = {edge: tuple(sorted(values))
                        for edge, values in sorted(relations.items())}
    return stable_graph, stable_relations, dependency_evidence


def source_graph_details(records: Iterable[SourceRecord]) -> tuple[
        dict[str, dict[str, float]],
        dict[tuple[str, str], tuple[str, ...]],
        dict[tuple[str, str], tuple[RelationEvidence, ...]]]:
    """Build an undirected retrieval graph from directed dependencies."""
    dependencies, dependency_evidence = source_dependency_details(records)
    return _source_graph_from_dependencies(
        dependencies, dependency_evidence)


def source_graph(records: Iterable[SourceRecord]) -> tuple[
        dict[str, dict[str, float]],
        dict[tuple[str, str], tuple[str, ...]]]:
    """Return the compatibility view of the detailed source graph."""
    graph, relations, _evidence = source_graph_details(records)
    return graph, relations


def source_symbol_relation_index(records: Iterable[SourceRecord]) -> tuple[
        dict[str, tuple[tuple[str, str], ...]],
        dict[str, tuple[tuple[str, str], ...]],
        dict[tuple[str, str], tuple[str, ...]]]:
    """Build the stable symbol nodes, aliases, and directed targets."""
    material = {record.path: record for record in records}
    nodes_by_path: dict[str, set[tuple[str, str]]] = defaultdict(set)
    aliases: dict[str, set[tuple[str, str]]] = defaultdict(set)
    targets: dict[tuple[str, str], set[str]] = defaultdict(set)

    for path, record in sorted(material.items()):
        by_line: dict[int, set[str]] = defaultdict(set)
        for label, line in record.symbol_locations:
            if label:
                by_line[line].add(label)
        labels = set()
        for values in by_line.values():
            labels.add(max(
                values,
                key=lambda value: (
                    value.count("::") + value.count("."),
                    len(value),
                    value,
                ),
            ))
        known_aliases = {
            alias
            for label in labels
            for alias in (label, re.split(r"::|\.", label)[-1])
        }
        labels.update(
            label for label in record.symbols if label not in known_aliases
        )
        labels.update(
            source for source, _target, _relation in record.symbol_relations
        )
        if not labels:
            labels.add(f"@file:{path}")

        for label in sorted(labels):
            node = (path, label)
            nodes_by_path[path].add(node)
            aliases[label].add(node)
            aliases[re.split(r"::|\.", label)[-1]].add(node)
        for source, target, _relation in record.symbol_relations:
            node = (path, source)
            nodes_by_path[path].add(node)
            targets[node].add(target)

    return (
        {
            path: tuple(sorted(nodes))
            for path, nodes in sorted(nodes_by_path.items())
        },
        {
            alias: tuple(sorted(nodes))
            for alias, nodes in sorted(aliases.items())
        },
        {
            node: tuple(sorted(values))
            for node, values in sorted(targets.items())
        },
    )


class CodeSearchIndex:
    """Search source files through lexical, exact, and structural arms."""

    def __init__(self, records: Iterable[SourceRecord], *,
                 ambiguous_symbol_limit: int = 1):
        if ambiguous_symbol_limit < 1:
            raise ValueError("ambiguous symbol limit must be positive")
        self.ambiguous_symbol_limit = ambiguous_symbol_limit
        self.records = {record.path: record for record in records}
        documents = [LexicalDocument(record.path, {
            "path": record.path.replace("/", " "),
            "symbols": " ".join(record.symbols),
            "tags": "test" if is_test_source(record.path) else "production",
            "documentation": record.documentation,
            "body": record.text,
        }) for record in self.records.values()]
        self.lexical = BM25FIndex(documents)
        self._fields = {
            record.path: {
                "path": set(terms(record.path.replace("/", " "))),
                "symbols": set(terms(" ".join(record.symbols))),
                "tags": ({"test"} if is_test_source(record.path)
                         else {"production"}),
                "documentation": set(terms(record.documentation)),
                "body": set(terms(record.text)),
            }
            for record in self.records.values()
        }
        self._build_symbol_relation_index()
        self._build_directory_index()
        self.graph = self._build_graph()

    @classmethod
    def from_path(cls, root: str, *, ambiguous_symbol_limit: int = 1,
                  **kwargs) -> "CodeSearchIndex":
        return cls(
            source_records(root, **kwargs),
            ambiguous_symbol_limit=ambiguous_symbol_limit,
        )

    def _build_symbol_relation_index(self) -> None:
        (
            self.symbol_nodes_by_path,
            self.symbol_aliases,
            self.symbol_relation_targets,
        ) = source_symbol_relation_index(self.records.values())

    def _build_directory_index(self) -> None:
        directories: dict[str, list[str]] = defaultdict(list)
        stems: dict[str, list[str]] = defaultdict(list)
        for path in sorted(self.records):
            directories[posixpath.dirname(path)].append(path)
            stems[_counterpart_stem(path)].append(path)
        self.directory_paths = {
            directory: tuple(paths)
            for directory, paths in sorted(directories.items())
        }
        self.counterpart_stem_paths = {
            stem: tuple(paths) for stem, paths in sorted(stems.items())
        }

    def directory_neighborhood_ranking(
        self,
        anchors: Iterable[str],
        *,
        seed_files: int = 10,
    ) -> list[str]:
        """Rank test counterparts and files adjacent to bounded seed files."""
        if seed_files < 1:
            raise ValueError("directory seed files must be positive")
        anchor_paths = list(dict.fromkeys(
            path for path in anchors if path in self.records
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
                _counterpart_stem(path), (),
            ):
                admit(candidate, 50.0 / seed_rank, seed_rank)

        return sorted(
            self.records,
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
        """Walk unresolved symbol calls from one high-degree symbol per file."""
        if depth < 0:
            raise ValueError("symbol dependency depth must be non-negative")
        if neighbor_cap < 1:
            raise ValueError("symbol dependency neighbor cap must be positive")
        if symbols_per_anchor < 1:
            raise ValueError("symbols per anchor must be positive")

        anchor_paths = list(dict.fromkeys(
            path for path in anchors if path in self.records
        ))
        seen: dict[tuple[str, str], tuple[float, int]] = {}
        frontier = []
        for rank, path in enumerate(anchor_paths, 1):
            nodes = self.symbol_nodes_by_path.get(path, ())
            if not nodes:
                continue
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
                    frontier, key=lambda item: (-seen[item][0], item)):
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
                    -current[0], current[1]):
                path_scores[path] = value
        return [
            path for path, _value in sorted(
                path_scores.items(),
                key=lambda item: (-item[1][0], item[1][1], item[0]),
            )
        ]

    def _build_graph(self) -> dict[str, dict[str, float]]:
        self.dependency_relations, self.dependency_evidence = \
            source_dependency_details(
                self.records.values(),
                ambiguous_symbol_limit=self.ambiguous_symbol_limit,
            )
        graph, self.graph_relations, self.graph_evidence = \
            _source_graph_from_dependencies(
                self.dependency_relations, self.dependency_evidence)
        dependencies: dict[str, dict[str, float]] = defaultdict(dict)
        dependents: dict[str, dict[str, float]] = defaultdict(dict)
        for (source, target), relations in self.dependency_relations.items():
            weight = max(
                DEPENDENCY_RELATION_WEIGHTS.get(relation, 0.5)
                for relation in relations
            )
            dependencies[source][target] = weight
            dependents[target][source] = weight
        self.dependencies = {
            path: dict(sorted(neighbors.items()))
            for path, neighbors in sorted(dependencies.items())
        }
        self.dependents = {
            path: dict(sorted(neighbors.items()))
            for path, neighbors in sorted(dependents.items())
        }
        return graph

    def dependency_ranking(
        self,
        anchors: Iterable[str],
        *,
        direction: str = "both",
        depth: int = 2,
        decay: float = 0.6,
    ) -> list[str]:
        """Walk typed dependencies from known files under a fixed direction."""
        if direction not in {"dependencies", "dependents", "both"}:
            raise ValueError(f"unknown dependency direction: {direction}")
        if depth < 0:
            raise ValueError("dependency depth must be non-negative")
        if not 0.0 <= decay <= 1.0:
            raise ValueError("dependency decay must be between zero and one")
        anchor_set = {
            path for path in anchors
            if path in self.records
        }
        frontier = {path: 1.0 for path in anchor_set}
        reached: dict[str, float] = defaultdict(float)
        for iteration in range(depth):
            following: dict[str, float] = defaultdict(float)
            for path, amount in sorted(frontier.items()):
                neighbors: dict[str, float] = defaultdict(float)
                if direction in {"dependencies", "both"}:
                    for neighbor, weight in self.dependencies.get(path, {}).items():
                        neighbors[neighbor] += weight
                if direction in {"dependents", "both"}:
                    for neighbor, weight in self.dependents.get(path, {}).items():
                        neighbors[neighbor] += weight
                normalizer = sum(neighbors.values())
                if normalizer <= 0:
                    continue
                for neighbor, weight in sorted(neighbors.items()):
                    value = (
                        amount * weight / normalizer
                        * decay ** (iteration + 1)
                    )
                    reached[neighbor] += value
                    following[neighbor] += value
            frontier = following
        for path in anchor_set:
            reached.pop(path, None)
        return [
            path for path, _score in sorted(
                reached.items(), key=lambda item: (-item[1], item[0]))
        ]

    def context_pack(self, query: str, *, source_path: str | None = None,
                     mode: str = "structural-fusion", limit: int = 20,
                     budget: int = 1200,
                     excerpt_budget: int = 180) -> CodeContextPack:
        """Return ranked source excerpts within one explicit token budget."""
        hits = self.search(query, mode=mode, limit=limit)
        evidence = ({path: values for (source, path), values
                     in self.graph_evidence.items() if source == source_path}
                    if source_path else {})
        return assemble_code_context(
            {path: record.text for path, record in self.records.items()},
            hits, query, source_path=source_path,
            relation_evidence=evidence, budget=budget,
            excerpt_budget=excerpt_budget)

    def overlap_ranking(self, query: str) -> list[str]:
        """Return the former distinct-overlap floor at file granularity."""
        query_terms = set(terms(query))
        weights = {"path": 4.0, "symbols": 3.0, "tags": 3.0,
                   "documentation": 2.0, "body": 1.0}
        scored: list[tuple[str, float]] = []
        for path, fields in self._fields.items():
            score = sum(weights[name] * len(query_terms & values)
                        for name, values in fields.items())
            if is_test_source(path):
                score *= 0.5
            if score > 0:
                scored.append((path, score / max(1, len(query_terms))))
        scored.sort(key=lambda item: (-item[1], item[0]))
        return [path for path, _score in scored]

    def bm25_ranking(self, query: str) -> list[str]:
        scored = [(hit.key, hit.score * (0.5 if is_test_source(hit.key) else 1.0))
                  for hit in self.lexical.search(query)]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return [path for path, _score in scored]

    def exact_ranking(self, query: str) -> list[str]:
        query_terms = set(terms(query))
        scored: list[tuple[str, float]] = []
        for path, fields in self._fields.items():
            score = (4.0 * len(query_terms & fields["path"])
                     + 3.0 * len(query_terms & fields["symbols"]))
            if is_test_source(path):
                score *= 0.5
            if score > 0:
                scored.append((path, score))
        scored.sort(key=lambda item: (-item[1], item[0]))
        return [path for path, _score in scored]

    def symbol_ranking(self, query: str) -> list[str]:
        """Rank files by their strongest definition-name match."""
        query_terms = list(dict.fromkeys(
            token for token in re.findall(r"\w+", query.casefold())
            if not (token.isascii() and token.isalpha() and len(token) <= 2)))
        if not query_terms:
            return []
        labels = {
            path: tuple(dict.fromkeys((*record.symbols, Path(path).name)))
            for path, record in self.records.items()
        }
        population = sum(len(values) for values in labels.values()) or 1
        document_frequency = {
            term: sum(term in label.casefold()
                      for values in labels.values() for label in values)
            for term in query_terms
        }
        inverse = {
            term: math.log(
                1.0 + population / (1.0 + document_frequency[term]))
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
                        score == best_score and len(label) < best_length):
                    best_score = score
                    best_length = len(label)
            if best_score > 0:
                scored.append((path, best_score, best_length))
        scored.sort(key=lambda item: (-item[1], item[2], item[0]))
        return [path for path, _score, _length in scored]

    def graph_ranking(self, query: str, *, bm25: list[str] | None = None,
                      exact: list[str] | None = None) -> list[str]:
        """Expand lexical and exact seeds for two bounded graph steps."""
        bm25 = (self.bm25_ranking(query) if bm25 is None else bm25)[:10]
        exact = (self.exact_ranking(query) if exact is None else exact)[:10]
        seeds = reciprocal_rank_fusion([bm25, exact], limit=15)
        frontier = {path: score for path, score in seeds}
        reached: dict[str, float] = defaultdict(float)
        for iteration in range(2):
            next_frontier: dict[str, float] = defaultdict(float)
            for path, amount in sorted(frontier.items()):
                neighbors = self.graph.get(path, {})
                normalizer = sum(neighbors.values())
                if normalizer <= 0:
                    continue
                for neighbor, weight in neighbors.items():
                    value = amount * (weight / normalizer) * (0.6 ** (iteration + 1))
                    reached[neighbor] += value
                    next_frontier[neighbor] += value
            frontier = next_frontier
        return [path for path, _score in sorted(
            reached.items(), key=lambda item: (-item[1], item[0]))]

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
                elif name == "symbol":
                    rankings[name] = self.symbol_ranking(query)
                elif name == "graph":
                    rankings[name] = self.graph_ranking(
                        query, bm25=ranking("bm25f"), exact=ranking("exact"))
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
                query, mode=mode, limit=len(self.records))
            return personalize_code_directory_hits(
                candidates, dynamics, repository,
                route_weight=resolved.directory_route_weight, limit=limit)
        candidates = self.search(query, mode=mode, limit=candidate_limit)
        return personalize_code_hits(
            candidates, query, dynamics, repository, limit=limit,
            config=resolved)
