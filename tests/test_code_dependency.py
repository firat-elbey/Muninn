from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from muninn import code_dependency
from muninn.code_search import SourceRecord, structural_source_record


class CodeDependencyTests(unittest.TestCase):
    def test_graphify_line_parser_accepts_only_prefixed_numbers(self):
        self.assertEqual(code_dependency._line("L42:C3"), 42)
        self.assertIsNone(code_dependency._line("42"))
        self.assertIsNone(code_dependency._line(None))

    def test_source_path_resolution_requires_one_suffix(self):
        self.assertEqual(
            code_dependency.resolve_source_path(
                "store.py", {"src/store.py", "src/service.py"}),
            "src/store.py")
        self.assertIsNone(code_dependency.resolve_source_path(
            "store.py", {"src/store.py", "tests/store.py"}))

    def test_internal_provider_preserves_directed_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pkg").mkdir()
            (root / "pkg" / "a.py").write_text("from .b import work\n")
            (root / "pkg" / "b.py").write_text("def work():\n    pass\n")

            graph = code_dependency.build_dependency_graph(
                root, use_graphify=False)

        self.assertIn(("pkg/a.py", "pkg/b.py"), graph.relations)
        self.assertFalse(graph.graphify_used)

    def test_optional_provider_failure_retains_internal_graph(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.ts").write_text("import { value } from './b';\n")
            (root / "b.ts").write_text("export const value = 1;\n")
            records = [
                SourceRecord("a.ts", "", imports=("./b",)),
                SourceRecord("b.ts", ""),
            ]
            with mock.patch.object(
                    code_dependency, "_graphify_dependencies",
                    side_effect=RuntimeError("unavailable")):
                graph = code_dependency.build_dependency_graph_records(
                    root, records)

        self.assertIn(("a.ts", "b.ts"), graph.relations)
        self.assertFalse(graph.graphify_used)
        self.assertEqual(graph.graphify_error, "RuntimeError: unavailable")

    def test_graphify_adapter_normalizes_relations_and_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / "src" / "a.ts", root / "src" / "b.ts"]
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("content\n")

            def extract(*_args, **_kwargs):
                return {
                    "nodes": [
                        {
                            "id": "source",
                            "source_file": "src/a.ts",
                            "label": "source",
                            "source_location": "L2:C1",
                        },
                        {
                            "id": "target",
                            "source_file": "src/b.ts",
                            "label": "targetSymbol",
                            "source_location": "L9:C2",
                        },
                    ],
                    "edges": [
                        {
                            "source": "source",
                            "target": "target",
                            "relation": "calls",
                            "confidence": "inferred",
                            "source_location": "L4:C5",
                            "metadata": {"ref_token": "callTarget"},
                        },
                        {
                            "source": "source",
                            "target": "source",
                            "relation": "self",
                        },
                        {
                            "source": "missing",
                            "target": "target",
                        },
                    ],
                }

            graphify = types.ModuleType("graphify")
            graphify_extract = types.ModuleType("graphify.extract")
            graphify_extract.extract = extract
            graphify.extract = graphify_extract
            with mock.patch.dict(
                "sys.modules",
                {"graphify": graphify, "graphify.extract": graphify_extract},
            ):
                relations, evidence = code_dependency._graphify_dependencies(
                    root, paths, root / "cache"
                )

        self.assertEqual(relations, {("src/a.ts", "src/b.ts"): ("calls",)})
        item = evidence[("src/a.ts", "src/b.ts")][0]
        self.assertEqual(item.relation, "calls")
        self.assertEqual(item.symbol, "callTarget")
        self.assertEqual(item.provenance, "graphify:inferred")
        self.assertEqual(item.source_line, 4)
        self.assertEqual(item.target_line, 9)

    def test_successful_graphify_route_replaces_only_routed_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache"
            records = [
                SourceRecord("a.ts", "", imports=("./b",)),
                SourceRecord("b.ts", ""),
                SourceRecord("c.py", "", imports=("d",)),
                SourceRecord("d.py", ""),
            ]
            graphify_relations = {
                ("a.ts", "b.ts"): ("calls",),
                ("c.py", "d.py"): ("incorrect-provider",),
            }
            graphify_evidence = {
                ("a.ts", "b.ts"): (),
                ("c.py", "d.py"): (),
            }
            with mock.patch.object(
                code_dependency,
                "_graphify_dependencies",
                return_value=(graphify_relations, graphify_evidence),
            ) as provider:
                graph = code_dependency.build_dependency_graph_records(
                    root,
                    records,
                    cache_root=cache,
                )

        provider.assert_called_once()
        self.assertTrue(graph.graphify_used)
        self.assertEqual(graph.graphify_error, None)
        self.assertEqual(graph.relations[("a.ts", "b.ts")], ("calls",))
        self.assertNotEqual(
            graph.relations[("c.py", "d.py")], ("incorrect-provider",)
        )
        self.assertIn((".ts", "graphify"), graph.providers)
        self.assertIn((".py", "muninn"), graph.providers)

    def test_record_builder_does_not_apply_directory_exclusions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "build" / "caller.py"
            target = root / "build" / "target.py"
            source.parent.mkdir(parents=True)
            source.write_text("from .target import value\n")
            target.write_text("value = 1\n")
            records = [
                structural_source_record(
                    "build/caller.py", source.read_text(), str(source)),
                structural_source_record(
                    "build/target.py", target.read_text(), str(target)),
            ]
            scanned = code_dependency.build_dependency_graph(
                root, use_graphify=False)
            provided = code_dependency.build_dependency_graph_records(
                root, records, use_graphify=False)

        self.assertEqual(scanned.relations, {})
        self.assertIn(
            ("build/caller.py", "build/target.py"), provided.relations)


if __name__ == "__main__":
    unittest.main()
