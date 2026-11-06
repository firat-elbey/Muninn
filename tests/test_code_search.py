"""Source-file retrieval tests."""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from muninn import code_search
from muninn.code_context import (
    RelationEvidence,
    assemble_code_context,
    estimate_code_tokens,
    select_code_excerpt,
)
from muninn.code_memory import (
    CodeMemoryConfig,
    code_directory_key,
    code_key,
    learn_code_session,
    personalize_code_directory_hits,
    personalize_code_hits,
)
from muninn.code_search import (
    CodeSearchIndex,
    source_graph,
    source_graph_details,
    source_record,
    source_records,
)
from muninn.dynamics import Dynamics
from muninn.persistent_code_search import PersistentCodeSearchIndex


class TestCodeSearch(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, "ledger"))
        with open(os.path.join(self.root, "ledger", "replay.py"), "w") as fh:
            fh.write('''"""Restore derived state from immutable events."""
def replay_ledger(events):
    return list(events)
''')
        with open(os.path.join(self.root, "ledger", "repair.py"), "w") as fh:
            fh.write('''from .replay import replay_ledger
def repair_state(events):
    return replay_ledger(events)
''')
        with open(os.path.join(self.root, "session.py"), "w") as fh:
            fh.write('''def open_session():
    return "session"
''')
        os.makedirs(os.path.join(self.root, "node_modules"))
        with open(os.path.join(self.root, "node_modules", "ignored.py"), "w") as fh:
            fh.write("def replay_ledger(): pass\n")

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_source_discovery_skips_dependency_directories(self):
        paths = {record.path for record in source_records(self.root)}
        self.assertEqual(paths, {"ledger/repair.py", "ledger/replay.py",
                                 "session.py"})

    @unittest.skipUnless(hasattr(os, "mkfifo"), "named pipes are unavailable")
    def test_source_discovery_never_opens_named_pipes(self):
        os.mkfifo(os.path.join(self.root, "pipe.py"))
        original_read = code_search.Path.read_bytes

        def bounded_read(path):
            self.assertNotEqual(path.name, "pipe.py")
            return original_read(path)

        with mock.patch.object(code_search.Path, "read_bytes", bounded_read):
            paths = {record.path for record in source_records(self.root)}
        self.assertNotIn("pipe.py", paths)

    def test_bm25_finds_definition_from_prose_and_identifier(self):
        index = CodeSearchIndex.from_path(self.root)
        hits = index.search("where is immutable ledger state replayed",
                            mode="bm25f")
        self.assertEqual(hits[0].path, "ledger/replay.py")

    def test_source_record_extracts_python_facts_without_a_file(self):
        record = source_record(
            "candidate.py",
            '\"\"\"Ledger replay.\"\"\"\nfrom state import rebuild\n\n'
            "def replay_ledger():\n    return rebuild()\n",
        )
        self.assertEqual(record.symbols, ("replay_ledger",))
        self.assertIn(("replay_ledger", 4), record.symbol_locations)
        self.assertIn("state", record.imports)
        self.assertIn("rebuild", record.references)
        self.assertIn(("calls", "rebuild"), record.relation_references)
        self.assertIn("Ledger replay", record.documentation)

    def test_source_record_extracts_classes_async_functions_and_import_forms(self):
        record = source_record(
            "candidate.py",
            '"""Module documentation."""\n'
            "import package.module\n"
            "from .state import rebuild\n"
            "from package import *\n\n"
            "class Runner:\n"
            '    """Class documentation."""\n'
            "    async def execute(self):\n"
            '        """Method documentation."""\n'
            "        # Explain the operation.\n"
            "        return service.rebuild(rebuild)\n",
        )

        self.assertEqual(record.symbols, ("Runner", "execute"))
        self.assertIn("package.module", record.imports)
        self.assertIn(".state.rebuild", record.imports)
        self.assertIn("service", record.references)
        self.assertIn(("calls", "rebuild"), record.relation_references)
        self.assertIn("Method documentation", record.documentation)
        self.assertIn("Explain the operation", record.documentation)

    def test_source_record_fails_soft_on_invalid_python_and_comments(self):
        record = source_record("invalid.py", "def broken(:\n")
        self.assertEqual(record.symbols, ())
        self.assertEqual(record.text, "def broken(:\n")
        self.assertEqual(code_search._comments("'''unterminated"), [])

    def test_structural_record_fails_soft_when_extraction_is_unavailable(self):
        with mock.patch("muninn.extract.available", return_value=False):
            unavailable = code_search.structural_source_record(
                "src/a.py", "def a(): pass\n", "/tmp/a.py",
            )
        self.assertEqual(unavailable.symbols, ("a",))

        with (
            mock.patch("muninn.extract.available", return_value=True),
            mock.patch("muninn.extract.extract_file", side_effect=OSError),
        ):
            failed = code_search.structural_source_record(
                "src/a.py", "def a(): pass\n", "/tmp/a.py",
            )
        self.assertEqual(failed.symbols, ("a",))

    def test_structural_record_preserves_supported_graph_relations(self):
        graph = {
            "nodes": [
                {"id": "caller", "label": "run", "kind": "function"},
                {
                    "id": "target",
                    "label": "Store.SaveData",
                    "kind": "method",
                    "body": "Persist the record.",
                },
                {"id": "import", "label": "store", "kind": "import"},
            ],
            "links": [
                {"source": "caller", "target": "target", "relation": "calls"},
                {"source": "caller", "target": "missing", "relation": "uses"},
            ],
        }
        with (
            mock.patch("muninn.extract.available", return_value=True),
            mock.patch("muninn.extract.extract_file", return_value=graph),
            mock.patch(
                "muninn.extract.code_symbol_relations",
                return_value={"symbols": [], "relations": []},
            ),
        ):
            record = code_search.structural_source_record(
                "src/Store.cs", "class Store {}\n", "/tmp/Store.cs",
            )

        self.assertIn("Store.SaveData", record.symbols)
        self.assertIn("store", record.imports)
        self.assertIn("Store.SaveData", record.references)
        self.assertIn(("calls", "Store.SaveData"), record.relation_references)
        self.assertIn("Persist the record.", record.documentation)

    def test_source_discovery_skips_invalid_files_and_read_failures(self):
        with open(os.path.join(self.root, "ignored.txt"), "w") as handle:
            handle.write("ignored")
        with open(os.path.join(self.root, "large.py"), "w") as handle:
            handle.write("x" * 100)
        with open(os.path.join(self.root, "small.py"), "w") as handle:
            handle.write("x")
        os.symlink(
            os.path.join(self.root, "small.py"),
            os.path.join(self.root, "linked.py"),
        )

        self.assertEqual(
            [record.path for record in source_records(self.root, max_bytes=10)],
            ["small.py"],
        )
        with mock.patch.object(code_search.Path, "read_bytes", side_effect=OSError):
            self.assertEqual(source_records(self.root), [])

    def test_graph_connects_importer_and_definition(self):
        index = CodeSearchIndex.from_path(self.root)
        self.assertIn("ledger/replay.py", index.graph["ledger/repair.py"])
        hits = index.search("repair state events", mode="graph-fusion")
        paths = [hit.path for hit in hits]
        self.assertIn("ledger/replay.py", paths)
        replay = next(hit for hit in hits if hit.path == "ledger/replay.py")
        self.assertIn("graph", dict(replay.arms))

    def test_from_path_accepts_a_bounded_ambiguous_symbol_limit(self):
        os.makedirs(os.path.join(self.root, "views", "a"))
        os.makedirs(os.path.join(self.root, "views", "b"))
        with open(os.path.join(self.root, "caller.py"), "w") as fh:
            fh.write("def show():\n    return render()\n")
        for directory in ("a", "b"):
            with open(
                os.path.join(self.root, "views", directory, "view.py"), "w"
            ) as fh:
                fh.write("def render():\n    return None\n")

        strict = CodeSearchIndex.from_path(self.root)
        bounded = CodeSearchIndex.from_path(
            self.root, ambiguous_symbol_limit=2,
        )

        self.assertNotIn(
            "views/a/view.py", strict.graph.get("caller.py", {}),
        )
        self.assertIn("views/a/view.py", bounded.graph["caller.py"])
        self.assertIn("views/b/view.py", bounded.graph["caller.py"])

    def test_hybrid_preserves_the_bm25_first_result(self):
        index = CodeSearchIndex.from_path(self.root)
        query = "immutable ledger state replay"
        bm25 = index.search(query, mode="bm25f")
        hybrid = index.search(query, mode="hybrid")
        self.assertEqual(hybrid[0].path, bm25[0].path)

    def test_symbol_ranking_prefers_an_exact_definition(self):
        records = [
            code_search.SourceRecord(
                "src/OrderService.java", "", symbols=("processOrder",)),
            code_search.SourceRecord(
                "src/Order.java", "", symbols=("Order",)),
        ]
        index = CodeSearchIndex(records)
        self.assertEqual(
            index.symbol_ranking("return processOrder(request)")[0],
            "src/OrderService.java")

    def test_symbol_ranking_returns_no_result_without_terms(self):
        index = CodeSearchIndex([
            code_search.SourceRecord("src/a.py", "", symbols=("a",)),
        ])
        self.assertEqual(index.symbol_ranking("a to"), [])

    def test_structural_fusion_reports_the_symbol_arm(self):
        records = [
            code_search.SourceRecord(
                "src/Caller.java", "processOrder request",
                relation_references=(("calls", "processOrder"),)),
            code_search.SourceRecord(
                "src/OrderService.java", "", symbols=("processOrder",)),
        ]
        hit = CodeSearchIndex(records).search(
            "processOrder request", mode="structural-fusion", limit=1)[0]
        self.assertIn("symbol", dict(hit.arms))

    def test_rankings_are_deterministic(self):
        index = CodeSearchIndex.from_path(self.root)
        first = index.search("ledger state replay", mode="hybrid")
        second = index.search("ledger state replay", mode="hybrid")
        self.assertEqual(first, second)

    def test_test_file_is_demoted_but_remains_retrievable(self):
        index = CodeSearchIndex.from_path(self.root)
        with open(os.path.join(self.root, "test_replay.py"), "w") as fh:
            fh.write("def replay_ledger(): pass\n")
        index = CodeSearchIndex.from_path(self.root)
        normal = [hit.path for hit in index.search("replay ledger", mode="bm25f")]
        explicit = [hit.path for hit in index.search(
            "test replay ledger", mode="bm25f")]
        self.assertLess(normal.index("ledger/replay.py"),
                        normal.index("test_replay.py"))
        self.assertIn("test_replay.py", explicit)

    def test_unknown_mode_fails_explicitly(self):
        index = CodeSearchIndex.from_path(self.root)
        with self.assertRaises(ValueError):
            index.search("ledger", mode="unknown")

    def test_path_and_text_survive_a_recursive_ast(self):
        with mock.patch.object(code_search._PythonFacts, "visit",
                               side_effect=RecursionError):
            record = code_search._python_record(
                "deep.py", "def deeply_nested():\n    return 1\n")
        self.assertEqual(record.path, "deep.py")
        self.assertIn("deeply_nested", record.text)
        self.assertEqual(record.symbols, ())

    def test_persistent_index_matches_in_memory_rankings(self):
        records = source_records(self.root)
        memory = CodeSearchIndex(records)
        database = os.path.join(self.root, "code-index.sqlite")
        persistent = PersistentCodeSearchIndex.build_records(
            records, database, source_snapshot="snapshot-sha256"
        )
        try:
            for mode in ("overlap", "bm25f", "lexical-hybrid", "hybrid",
                         "graph-fusion", "symbol", "structural-fusion"):
                expected = memory.search(
                    "immutable ledger state replay", mode=mode, limit=10)
                actual = persistent.search(
                    "immutable ledger state replay", mode=mode, limit=10)
                self.assertEqual(actual, expected)
        finally:
            persistent.close()

        reopened = PersistentCodeSearchIndex(database)
        try:
            self.assertEqual(reopened.metadata["documents"], "3")
            self.assertEqual(
                reopened.metadata["source_snapshot"], "snapshot-sha256"
            )
            self.assertGreater(reopened.size_bytes, 0)
        finally:
            reopened.close()

    def test_persistent_index_matches_native_relation_rankings(self):
        records = [
            code_search.SourceRecord(
                "src/service.py",
                "def repair(): return recover()",
                symbols=("repair",),
                symbol_locations=(("repair", 1),),
                symbol_relations=(("repair", "recover", "calls"),),
            ),
            code_search.SourceRecord(
                "src/recovery.py",
                "def recover(): return True",
                symbols=("recover",),
                symbol_locations=(("recover", 1),),
            ),
            code_search.SourceRecord(
                "tests/test_recovery.py",
                "def test_recover(): assert recover()",
                symbols=("test_recover",),
                symbol_locations=(("test_recover", 1),),
            ),
        ]
        memory = CodeSearchIndex(records)
        database = os.path.join(self.root, "native-relations.sqlite")
        persistent = PersistentCodeSearchIndex.build_records(records, database)
        try:
            anchors = ["src/recovery.py"]
            self.assertEqual(
                persistent.directory_neighborhood_ranking(anchors),
                memory.directory_neighborhood_ranking(anchors),
            )
            self.assertEqual(
                persistent.symbol_dependency_ranking(
                    ["src/service.py"], symbols_per_anchor=3
                ),
                memory.symbol_dependency_ranking(
                    ["src/service.py"], symbols_per_anchor=3
                ),
            )
        finally:
            persistent.close()

    def test_persistent_overlap_preserves_literal_production_tag(self):
        records = [
            code_search.SourceRecord("src/product.py", "product catalog"),
            code_search.SourceRecord("src/other.py", "product"),
        ]
        memory = CodeSearchIndex(records)
        database = os.path.join(self.root, "literal-tag.sqlite")
        persistent = PersistentCodeSearchIndex.build_records(records, database)
        try:
            expected = memory.search("product", mode="overlap", limit=10)
            actual = persistent.search("product", mode="overlap", limit=10)
            self.assertEqual(actual, expected)
        finally:
            persistent.close()

    def test_persistent_bm25_matches_complete_in_memory_ranking(self):
        records = [code_search.SourceRecord(
            f"{'tests' if number % 4 == 0 else 'src'}/module_{number}.py",
            ("product ledger state " * (number % 7 + 1))
            + ("replay " * (number % 5 + 1)),
            symbols=(f"replay_{number % 9}",),
            documentation="immutable events" if number % 3 == 0 else "",
        ) for number in range(120)]
        memory = CodeSearchIndex(records)
        database = os.path.join(self.root, "complete-ranking.sqlite")
        persistent = PersistentCodeSearchIndex.build_records(records, database)
        try:
            self.assertEqual(
                persistent.bm25_ranking("product replay immutable state"),
                memory.bm25_ranking("product replay immutable state"))
        finally:
            persistent.close()

    def test_persistent_index_does_not_copy_source_bodies(self):
        database = os.path.join(self.root, "code-index.sqlite")
        index = PersistentCodeSearchIndex.build(self.root, database)
        index.close()
        connection = sqlite3.connect(database)
        try:
            columns = {row[1] for row in connection.execute(
                "PRAGMA table_info(documents)")}
            self.assertNotIn("text", columns)
            self.assertNotIn("body", columns)
            edge_columns = {row[1] for row in connection.execute(
                "PRAGMA table_info(edges)")}
            self.assertIn("relation", edge_columns)
            self.assertIn("provenance", edge_columns)
        finally:
            connection.close()

    def test_cross_language_relative_import_creates_a_typed_edge(self):
        records = [
            code_search.SourceRecord(
                "src/service.ts", "", imports=("./store",)),
            code_search.SourceRecord(
                "src/store.ts", "", symbols=("Store",)),
        ]
        graph, relations = source_graph(records)
        self.assertEqual(graph["src/service.ts"]["src/store.ts"], 2.0)
        self.assertEqual(
            relations[("src/service.ts", "src/store.ts")], ("imports",))

    def test_source_dependencies_preserve_direction(self):
        records = [
            code_search.SourceRecord(
                "src/service.ts", "", imports=("./store",)),
            code_search.SourceRecord("src/store.ts", ""),
        ]

        relations, evidence = code_search.source_dependency_details(records)

        self.assertEqual(
            relations[("src/service.ts", "src/store.ts")], ("imports",))
        self.assertNotIn(("src/store.ts", "src/service.ts"), relations)
        self.assertEqual(
            evidence[("src/service.ts", "src/store.ts")][0].relation,
            "imports")

    def test_dependency_ranking_selects_direction(self):
        records = [
            code_search.SourceRecord(
                "src/service.ts", "", imports=("./store",)),
            code_search.SourceRecord("src/store.ts", ""),
            code_search.SourceRecord(
                "tests/service.test.ts", "", imports=("../src/service",)),
        ]
        index = CodeSearchIndex(records)

        self.assertEqual(
            index.dependency_ranking(
                ["src/service.ts"], direction="dependencies"),
            ["src/store.ts"],
        )
        self.assertEqual(
            index.dependency_ranking(
                ["src/service.ts"], direction="dependents"),
            ["tests/service.test.ts"],
        )

    def test_dependency_ranking_rejects_invalid_controls(self):
        index = CodeSearchIndex([
            code_search.SourceRecord("src/service.ts", ""),
        ])
        with self.assertRaises(ValueError):
            index.dependency_ranking([], direction="sideways")
        with self.assertRaises(ValueError):
            index.dependency_ranking([], depth=-1)
        with self.assertRaises(ValueError):
            index.dependency_ranking([], decay=1.1)

    def test_symbol_dependency_ranking_walks_two_unresolved_hops(self):
        index = CodeSearchIndex([
            code_search.SourceRecord(
                "src/caller.py", "", symbols=("run",),
                symbol_relations=(("run", "load", "calls"),),
            ),
            code_search.SourceRecord(
                "src/loader.py", "", symbols=("load",),
                symbol_relations=(("load", "persist", "calls"),),
            ),
            code_search.SourceRecord(
                "src/store.py", "", symbols=("persist",),
            ),
        ])

        self.assertEqual(
            index.symbol_dependency_ranking(["src/caller.py"]),
            ["src/caller.py", "src/loader.py", "src/store.py"],
        )

    def test_symbol_dependency_ranking_selects_the_highest_degree_symbol(self):
        index = CodeSearchIndex([
            code_search.SourceRecord(
                "src/service.py", "", symbols=("cold", "hot"),
                symbol_relations=(
                    ("cold", "unused", "calls"),
                    ("hot", "load", "calls"),
                    ("hot", "persist", "calls"),
                ),
            ),
            code_search.SourceRecord("src/load.py", "", symbols=("load",)),
            code_search.SourceRecord(
                "src/store.py", "", symbols=("persist",),
            ),
            code_search.SourceRecord(
                "src/unused.py", "", symbols=("unused",),
            ),
        ])

        ranking = index.symbol_dependency_ranking(["src/service.py"], depth=1)

        self.assertEqual(
            ranking, ["src/service.py", "src/load.py", "src/store.py"],
        )
        self.assertNotIn("src/unused.py", ranking)

    def test_symbol_dependency_ranking_rejects_invalid_controls(self):
        index = CodeSearchIndex([code_search.SourceRecord("src/a.py", "")])
        with self.assertRaises(ValueError):
            index.symbol_dependency_ranking([], depth=-1)
        with self.assertRaises(ValueError):
            index.symbol_dependency_ranking([], neighbor_cap=0)
        with self.assertRaises(ValueError):
            index.symbol_dependency_ranking([], symbols_per_anchor=0)

    def test_directory_neighborhood_ranks_counterpart_before_adjacent_file(self):
        index = CodeSearchIndex([
            code_search.SourceRecord("tests/test_recovery.py", ""),
            code_search.SourceRecord("src/recovery.py", ""),
            code_search.SourceRecord("tests/helper.py", ""),
            code_search.SourceRecord("src/other.py", ""),
        ])

        self.assertEqual(
            index.directory_neighborhood_ranking(["tests/test_recovery.py"]),
            [
                "tests/test_recovery.py",
                "src/recovery.py",
                "tests/helper.py",
                "src/other.py",
            ],
        )

    def test_directory_neighborhood_rejects_empty_seed_budget(self):
        index = CodeSearchIndex([code_search.SourceRecord("src/a.py", "")])
        with self.assertRaises(ValueError):
            index.directory_neighborhood_ranking([], seed_files=0)

    def test_import_suffix_index_preserves_unique_resolution(self):
        records = [
            code_search.SourceRecord(
                "app/caller.py", "", imports=("services.orders",)),
            code_search.SourceRecord("packages/core/services/orders.py", ""),
        ]

        relations, _evidence = code_search.source_dependency_details(records)

        self.assertIn(
            ("app/caller.py", "packages/core/services/orders.py"), relations)

    def test_import_suffix_index_rejects_ambiguous_resolution(self):
        records = [
            code_search.SourceRecord(
                "app/caller.py", "", imports=("services.orders",)),
            code_search.SourceRecord("packages/a/services/orders.py", ""),
            code_search.SourceRecord("packages/b/services/orders.py", ""),
        ]

        relations, _evidence = code_search.source_dependency_details(records)

        self.assertNotIn(
            ("app/caller.py", "packages/a/services/orders.py"), relations)
        self.assertNotIn(
            ("app/caller.py", "packages/b/services/orders.py"), relations)

    def test_source_dependencies_can_bound_ambiguous_symbols(self):
        records = [
            code_search.SourceRecord(
                "app/caller.py", "",
                relation_references=(("calls", "render"),),
            ),
            code_search.SourceRecord(
                "packages/a/view.py", "", symbols=("render",),
            ),
            code_search.SourceRecord(
                "packages/b/view.py", "", symbols=("render",),
            ),
        ]

        strict, _evidence = code_search.source_dependency_details(records)
        bounded, evidence = code_search.source_dependency_details(
            records, ambiguous_symbol_limit=2,
        )

        self.assertFalse(strict)
        self.assertEqual(
            bounded[("app/caller.py", "packages/a/view.py")],
            ("calls_ambiguous",),
        )
        self.assertEqual(
            evidence[("app/caller.py", "packages/b/view.py")][0].relation,
            "calls_ambiguous",
        )

    def test_strict_resolution_counts_the_source_definition(self):
        records = [
            code_search.SourceRecord(
                "app/caller.py", "", symbols=("render",),
                relation_references=(("calls", "render"),),
            ),
            code_search.SourceRecord(
                "packages/view.py", "", symbols=("render",),
            ),
        ]

        strict, _strict_evidence = code_search.source_dependency_details(
            records,
        )
        bounded, evidence = code_search.source_dependency_details(
            records, ambiguous_symbol_limit=2,
        )

        edge = ("app/caller.py", "packages/view.py")
        self.assertNotIn(edge, strict)
        self.assertEqual(bounded[edge], ("calls_ambiguous",))
        self.assertEqual(evidence[edge][0].relation, "calls_ambiguous")

    def test_source_dependencies_reject_an_unbounded_symbol_limit(self):
        with self.assertRaises(ValueError):
            code_search.source_dependency_details(
                [], ambiguous_symbol_limit=0,
            )

    def test_source_dependencies_can_retain_only_imports(self):
        records = [
            code_search.SourceRecord(
                "src/service.ts", "", imports=("./store",),
                relation_references=(("calls", "persist"),)),
            code_search.SourceRecord(
                "src/store.ts", "", symbols=("persist",)),
        ]

        relations, _evidence = code_search.source_dependency_details(
            records, imports_only=True)

        self.assertEqual(
            relations[("src/service.ts", "src/store.ts")], ("imports",))

    def test_source_dependencies_cap_each_relation_family_at_one_hundred(self):
        import_targets = [
            code_search.SourceRecord(f"imports/{number}/view.py", "")
            for number in range(101)
        ]
        typed_targets = [
            code_search.SourceRecord(
                f"typed/{number}.py", "", symbols=(f"typed_{number}",),
            )
            for number in range(101)
        ]
        reference_targets = [
            code_search.SourceRecord(
                f"references/{number}.py", "", symbols=(f"plain_{number}",),
            )
            for number in range(101)
        ]
        records = [
            code_search.SourceRecord(
                "callers/importer.py", "", imports=("view",),
            ),
            code_search.SourceRecord(
                "callers/typed.py",
                "",
                relation_references=(
                    (("writes", "ignored"),)
                    + tuple(("calls", f"typed_{number}") for number in range(101))
                ),
            ),
            code_search.SourceRecord(
                "callers/reference.py",
                "",
                references=tuple(f"plain_{number}" for number in range(101)),
            ),
            *import_targets,
            *typed_targets,
            *reference_targets,
        ]

        relations, _evidence = code_search.source_dependency_details(
            records,
            ambiguous_symbol_limit=200,
        )
        for caller in (
            "callers/importer.py",
            "callers/typed.py",
            "callers/reference.py",
        ):
            self.assertEqual(
                sum(left == caller for left, _right in relations),
                100,
            )

    def test_typed_call_reference_creates_a_stronger_edge(self):
        records = [
            code_search.SourceRecord(
                "src/Caller.cs", "",
                relation_references=(("calls", "SaveData"),),
                references=("SaveData",)),
            code_search.SourceRecord(
                "src/Store.cs", "", symbols=("SaveData",)),
        ]
        graph, relations = source_graph(records)
        self.assertEqual(graph["src/Caller.cs"]["src/Store.cs"], 2.0)
        self.assertEqual(
            relations[("src/Caller.cs", "src/Store.cs")], ("calls",))

    def test_graph_retains_relation_symbol_evidence(self):
        records = [
            code_search.SourceRecord(
                "src/Caller.cs", "",
                relation_references=(("calls", "SaveData"),)),
            code_search.SourceRecord(
                "src/Store.cs", "", symbols=("SaveData",),
                symbol_locations=(("SaveData", 7),)),
        ]
        _graph, _relations, evidence = source_graph_details(records)
        self.assertEqual(evidence[("src/Caller.cs", "src/Store.cs")][0].symbol,
                         "SaveData")
        self.assertEqual(
            evidence[("src/Caller.cs", "src/Store.cs")][0].target_line, 7)

    def test_ambiguous_symbol_location_is_not_used_as_a_target(self):
        records = [
            code_search.SourceRecord(
                "src/Caller.cs", "",
                relation_references=(("calls", "SaveData"),)),
            code_search.SourceRecord(
                "src/Store.cs", "", symbols=("SaveData",),
                symbol_locations=(("SaveData", 7), ("SaveData", 19))),
        ]
        _graph, _relations, evidence = source_graph_details(records)
        self.assertIsNone(
            evidence[("src/Caller.cs", "src/Store.cs")][0].target_line)

    def test_java_fallback_records_declaration_lines(self):
        text = ("public class Store {\n"
                "  public Store() {\n"
                "    var value = new Intent();\n"
                "  }\n"
                "  public void SaveData() {\n"
                "  }\n"
                "}\n")
        self.assertEqual(
            code_search._java_csharp_declaration_locations(
                "src/Store.java", text),
            {("Store", 1), ("SaveData", 5)})

    def test_structural_record_indexes_qualified_location_aliases(self):
        graph = {
            "nodes": [{
                "id": "method:Store.SaveData",
                "label": "Store.SaveData",
                "kind": "method",
                "source_location": "L7",
            }],
            "links": [],
        }
        with (mock.patch("muninn.extract.available", return_value=True),
              mock.patch("muninn.extract.extract_file", return_value=graph)):
            record = code_search.structural_source_record(
                "src/Store.cs", "class Store {}\n", "/tmp/Store.cs")

        self.assertIn(("Store.SaveData", 7), record.symbol_locations)
        self.assertIn(("SaveData", 7), record.symbol_locations)

    def test_structural_record_retains_symbol_scoped_calls(self):
        symbol_graph = {
            "symbols": [{
                "qualified": "Store::SaveData",
                "start_line": 7,
            }],
            "relations": [{
                "source": "Store::SaveData",
                "target": "persist",
                "relation": "calls",
            }],
        }
        with (mock.patch("muninn.extract.available", return_value=True),
              mock.patch("muninn.extract.extract_file", return_value={
                  "nodes": [], "links": [],
              }),
              mock.patch(
                  "muninn.extract.code_symbol_relations",
                  return_value=symbol_graph,
              )):
            record = code_search.structural_source_record(
                "src/Store.cs", "class Store {}\n", "/tmp/Store.cs",
            )

        self.assertIn(("Store::SaveData", 7), record.symbol_locations)
        self.assertEqual(
            record.symbol_relations,
            (("Store::SaveData", "persist", "calls"),),
        )

    def test_fallback_location_supersedes_parser_annotation_line(self):
        graph = {
            "nodes": [{
                "id": "class:Quest",
                "label": "Quest",
                "kind": "class",
                "source_location": "L1",
            }],
            "links": [],
        }
        text = ("[CreateAssetMenu]\n"
                "[Serializable]\n"
                "public class Quest {\n"
                "}\n")
        with (mock.patch("muninn.extract.available", return_value=True),
              mock.patch("muninn.extract.extract_file", return_value=graph)):
            record = code_search.structural_source_record(
                "src/Quest.cs", text, "/tmp/Quest.cs")

        self.assertIn(("Quest", 3), record.symbol_locations)
        self.assertNotIn(("Quest", 1), record.symbol_locations)

    def test_excerpt_selection_keeps_a_contiguous_preferred_line(self):
        text = ("package example;\n\n"
                "import android.content.Intent;\n\n"
                "public class WindowPermissionCheck {\n\n"
                "    public static boolean checkPermission(Activity a) {\n"
                "        return true;\n"
                "    }\n"
                "}\n")
        start, end, excerpt = select_code_excerpt(
            text, "boolean permission =", symbols=("checkPermission",),
            preferred_lines=(7,), budget=40)
        self.assertLessEqual(start, 7)
        self.assertGreaterEqual(end, 7)
        self.assertEqual(
            excerpt, "\n".join(text.splitlines()[start - 1:end]))
        self.assertLessEqual(estimate_code_tokens(excerpt), 40)

    def test_context_pack_centers_relation_target_within_budget(self):
        records = [
            code_search.SourceRecord(
                "src/Caller.cs", "class Caller { void Run() { SaveData(); } }",
                relation_references=(("calls", "SaveData"),)),
            code_search.SourceRecord(
                "src/Store.cs",
                "class Store {\n  void SaveData() {\n    persist();\n  }\n}\n",
                symbols=("SaveData",),
                symbol_locations=(("SaveData", 2),)),
        ]
        index = CodeSearchIndex(records)
        pack = index.context_pack(
            "run the operation", source_path="src/Caller.cs",
            mode="graph-fusion", budget=100, excerpt_budget=60)
        self.assertEqual(pack.excerpts[0].path, "src/Store.cs")
        self.assertIn("SaveData", pack.excerpts[0].text)
        self.assertEqual(pack.excerpts[0].relations[0].relation, "calls")
        self.assertEqual(pack.excerpts[0].relations[0].target_line, 2)
        self.assertLessEqual(estimate_code_tokens(pack.text), 100)

    def test_context_pack_omits_an_empty_excerpt_that_cannot_fit(self):
        path = "src/" + "nested_" * 20 + "target.py"
        pack = assemble_code_context(
            {path: "\n\n\n"}, [path], "target", source_path="source.py",
            relation_evidence={path: (RelationEvidence(
                "references", "VeryLongSymbol" * 20),)},
            budget=80, excerpt_budget=20)
        self.assertEqual(pack.excerpts, ())
        self.assertIn(path, pack.omitted_paths)

    def test_context_pack_preserves_the_selected_center_at_its_limit(self):
        target = "\n".join(
            [f"line {number}" for number in range(1, 20)])
        records = {
            "src/large.py": "x = '" + "a" * 320 + "'\n",
            "src/target.py": target,
        }
        pack = assemble_code_context(
            records, ["src/large.py", "src/target.py"], "target",
            relation_evidence={"src/target.py": (RelationEvidence(
                "calls", "target", target_line=12),)},
            budget=180, excerpt_budget=100)
        excerpt = next(value for value in pack.excerpts
                       if value.path == "src/target.py")
        self.assertLessEqual(excerpt.start_line, 12)
        self.assertGreaterEqual(excerpt.end_line, 12)
        self.assertLessEqual(pack.estimated_tokens, 180)

    def test_code_key_is_stable_and_hides_the_repository(self):
        first = code_key("https://user:secret@github.com/Owner/Repo.git",
                         "./src/store.py")
        second = code_key("github.com/owner/repo", "src/store.py")
        self.assertEqual(first, second)
        self.assertNotIn("owner", first)
        self.assertNotIn("secret", first)

    def test_directory_key_is_relative_and_omits_root_files(self):
        key = code_directory_key(
            "https://user:secret@github.com/Owner/Repo.git",
            "src/ledger/replay.py")
        self.assertIsNotNone(key)
        self.assertTrue(key.endswith(":src/ledger"))
        self.assertNotIn("owner", key)
        self.assertNotIn("secret", key)
        self.assertIsNone(code_directory_key("owner/repo", "replay.py"))

    def test_serving_code_is_not_personal_evidence(self):
        dynamics = Dynamics(self.root)
        hits = self._memory_hits()
        learn_code_session(
            dynamics, "owner/repo", "served", "repair ledger replay",
            [hit.path for hit in hits], [])
        ranking = personalize_code_hits(
            hits, "repair ledger replay", dynamics, "owner/repo")
        self.assertEqual([hit.path for hit in ranking.hits],
                         [hit.path for hit in hits])
        self.assertTrue(all(item.strength == 0 for item in ranking.evidence))
        self.assertTrue(all(item.co_use == 0 for item in ranking.evidence))
        self.assertEqual(dynamics.coact, {})

    def test_personal_learning_reorders_tail_and_protects_first(self):
        dynamics = Dynamics(self.root)
        hits = self._memory_hits()
        used = hits[-1].path
        for number in range(10):
            learn_code_session(
                dynamics, "owner/repo", f"used-{number}",
                "repair ledger replay details", [], [used])
        ranking = personalize_code_hits(
            hits, "repair ledger replay details", dynamics, "owner/repo")
        paths = [hit.path for hit in ranking.hits]
        self.assertEqual(paths[0], hits[0].path)
        self.assertLess(paths.index(used), len(hits) - 1)
        learned = next(item for item in ranking.evidence if item.path == used)
        self.assertLessEqual(learned.factor, 1.5)

    def test_directory_route_admits_a_positive_shared_match(self):
        dynamics = Dynamics(self.root)
        hits = [code_search.CodeHit(
            ("src/familiar/target.py" if number == 21
             else f"src/other_{number}/candidate.py"),
            1.0 / number, (("bm25f", number),))
            for number in range(1, 31)]
        learn_code_session(
            dynamics, "owner/repo", "prior", "repair familiar storage",
            [], ["src/familiar/prior.py"], learn_directories=True)
        result = personalize_code_directory_hits(
            hits, dynamics, "owner/repo", route_weight=0.5, limit=10)
        paths = [hit.path for hit in result.hits]
        self.assertEqual(paths[0], hits[0].path)
        self.assertIn("src/familiar/target.py", paths)
        evidence = next(item for item in result.evidence
                        if item.path == "src/familiar/target.py")
        self.assertTrue(evidence.admitted)
        self.assertEqual(evidence.directory_route_rank, 1)

    def test_serving_does_not_create_directory_memory(self):
        dynamics = Dynamics(self.root)
        path = "src/familiar/served.py"
        learn_code_session(
            dynamics, "owner/repo", "served-directory", "repair storage",
            [path], [], learn_directories=True)
        self.assertNotIn(code_directory_key("owner/repo", path),
                         dynamics.entries)

    def test_code_memory_is_per_consumer_and_replayable(self):
        first_root = tempfile.mkdtemp()
        second_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, first_root, ignore_errors=True)
        self.addCleanup(shutil.rmtree, second_root, ignore_errors=True)
        first = Dynamics(first_root)
        second = Dynamics(second_root)
        hits = self._memory_hits()
        learn_code_session(
            first, "owner/repo", "one", "repair ledger replay details", [],
            [hits[-1].path])
        first_ranking = personalize_code_hits(
            hits, "repair ledger replay details", first, "owner/repo")
        second_ranking = personalize_code_hits(
            hits, "repair ledger replay details", second, "owner/repo")
        self.assertNotEqual(first_ranking.evidence, second_ranking.evidence)
        os.remove(first.state_path)
        replayed = Dynamics(first_root)
        self.assertEqual(
            first_ranking,
            personalize_code_hits(
                hits, "repair ledger replay details", replayed, "owner/repo"))

    def test_code_association_decays_without_reinforcement(self):
        dynamics = Dynamics(self.root)
        path = self._memory_hits()[-1].path
        learn_code_session(
            dynamics, "owner/repo", "miss", "repair ledger replay details",
            [], [path])
        key = code_key("owner/repo", path)
        self.assertIn(key, dynamics.assocs)
        for _ in range(12):
            dynamics.consolidate()
        self.assertNotIn(key, dynamics.assocs)

    def test_code_waste_is_one_step_and_use_resets_it(self):
        dynamics = Dynamics(self.root)
        hits = self._memory_hits()
        path = hits[-2].path
        for number in range(3):
            learn_code_session(
                dynamics, "owner/repo", f"waste-{number}",
                "repair ledger replay", [path], [])
        ranking = personalize_code_hits(
            hits, "repair ledger replay", dynamics, "owner/repo")
        evidence = next(item for item in ranking.evidence if item.path == path)
        self.assertTrue(evidence.wasted)
        self.assertEqual(evidence.factor, 0.8)
        learn_code_session(
            dynamics, "owner/repo", "forgiven", "repair ledger replay",
            [], [path])
        ranking = personalize_code_hits(
            hits, "repair ledger replay", dynamics, "owner/repo")
        evidence = next(item for item in ranking.evidence if item.path == path)
        self.assertFalse(evidence.wasted)

    def test_memory_and_persistent_personalized_search_match(self):
        records = source_records(self.root)
        memory = CodeSearchIndex(records)
        database = os.path.join(self.root, "personal-code-index.sqlite")
        persistent = PersistentCodeSearchIndex.build_records(records, database)
        dynamics = Dynamics(self.root)
        try:
            expected = memory.search_personalized(
                "immutable ledger state replay", dynamics=dynamics,
                repository="owner/repo", limit=3)
            actual = persistent.search_personalized(
                "immutable ledger state replay", dynamics=dynamics,
                repository="owner/repo", limit=3)
            self.assertEqual(actual, expected)
            learn_code_session(
                dynamics, "owner/repo", "directory-parity",
                "repair immutable ledger state", [], ["ledger/prior.py"],
                learn_directories=True)
            config = CodeMemoryConfig(directory_route_weight=0.25)
            expected = memory.search_personalized(
                "immutable ledger state replay", dynamics=dynamics,
                repository="owner/repo", limit=3, config=config)
            actual = persistent.search_personalized(
                "immutable ledger state replay", dynamics=dynamics,
                repository="owner/repo", limit=3, config=config)
            self.assertEqual(actual, expected)
        finally:
            persistent.close()

    @staticmethod
    def _memory_hits():
        return [code_search.CodeHit(
            f"src/candidate_{number}.py", 1.0 / number,
            (("bm25f", number),)) for number in range(1, 7)]


if __name__ == "__main__":
    unittest.main()
