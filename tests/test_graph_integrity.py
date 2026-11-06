"""Preserve graph weights, correction history, and imported node identities."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from muninn.activate import merged_graph, superseded_paths
from muninn.dynamics import Dynamics
from muninn.export import export_graph
from muninn.extract import import_graph
from muninn.ingest import import_graphify
from muninn.store import Bundle


class TestGraphIntegrity(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = self.temporary.name
        self.bundle = Bundle(self.root)

    def note(self, path, title, body="A synthetic fact.", **meta):
        self.bundle.write_note(path, {"type": "note", "title": title, **meta}, body)

    def test_repeated_relation_uses_strongest_confidence_in_either_order(self):
        self.note("target.md", "Target")
        for confidences in (("inferred", "extracted"),
                            ("extracted", "inferred")):
            with self.subTest(confidences=confidences):
                self.note("source.md", "Source", "\n".join(
                    f"- imports [[Target]] ({confidence})"
                    for confidence in confidences))
                for bundle in (self.bundle, Bundle(self.root)):
                    self.assertEqual(
                        merged_graph(bundle, None)["source.md"],
                        [("target.md", 1.0, "link")])
                    self.assertEqual(bundle.notes["source.md"].typed_links, [{
                        "target": "target.md", "relation": "imports",
                        "confidence_word": "extracted", "weight": 1.0,
                    }])

    def test_export_keeps_independent_plain_and_typed_edges(self):
        self.note("target.md", "Target")
        self.note("source.md", "Source",
                  "- imports [[Target]] (inferred)\n\nPlain [[Target]].")
        graph = export_graph(self.bundle)
        edges = [edge for edge in graph["links"]
                 if edge["source"] == "source.md"]
        self.assertEqual(
            {(edge["kind"], edge["weight"]) for edge in edges},
            {("typed", 0.5), ("link", 1.0)})
        self.assertEqual(max(edge["weight"] for edge in edges),
                         merged_graph(self.bundle, None)["source.md"][0][1])

    def test_export_preserves_authored_and_ledger_corrections(self):
        self.note("old.md", "Old")
        self.note("first.md", "First", supersedes=["old.md"])
        self.note("second.md", "Second", supersedes=["old.md"])
        self.note("latest.md", "Latest")
        dynamics = Dynamics(self.root)
        dynamics.supersede("old", "latest")
        dynamics.supersede("first.md", "second.md")
        dynamics.supersede("first.md", "second.md")
        dynamics.supersede("absent.md", "latest.md")
        self.assertIn("old.md", superseded_paths(self.bundle, dynamics))
        expected = {
            ("first.md", "old.md"), ("second.md", "old.md"),
            ("latest.md", "old.md"), ("second.md", "first.md"),
        }
        for state in (dynamics, Dynamics(self.root)):
            graph = export_graph(self.bundle, state)
            edges = [edge for edge in graph["links"]
                     if edge["kind"] == "supersedes"]
            self.assertEqual({(edge["source"], edge["target"])
                              for edge in edges}, expected)
            self.assertEqual(len(edges), len(expected))
        self.assertEqual(export_graph(self.bundle, dynamics),
                         export_graph(self.bundle, Dynamics(self.root)))

    def test_import_uses_exact_targets_for_duplicate_labels(self):
        graph = {
            "nodes": [
                {"id": "first", "label": "Shared name", "file_type": "concept"},
                {"id": "second", "label": "Shared name", "file_type": "concept"},
                {"id": "third", "label": "Third", "file_type": "concept"},
            ],
            "links": [
                {"source": "first", "target": "second", "relation": "depends_on",
                 "confidence": "extracted"},
                {"source": "third", "target": "second", "relation": "relates_to",
                 "confidence": "inferred"},
            ],
        }
        path = os.path.join(self.root, "graph.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(graph, handle)
        self.assertEqual(import_graphify(self.bundle, path), (3, 0))
        expected = {("imported/shared-name.md", "imported/shared-name-2.md"),
                    ("imported/third.md", "imported/shared-name-2.md")}
        for bundle in (self.bundle, Bundle(self.root)):
            self.assertEqual({(edge["source"], edge["target"])
                              for edge in export_graph(bundle)["links"]}, expected)
        self.assertEqual(import_graphify(Bundle(self.root), path), (0, 3))

    def test_export_import_preserves_generated_node_provenance(self):
        self.note("observed.md", "Observed", provenance="extracted")
        self.note("inference.md", "Inference", provenance="inferred")
        self.note("curated.md", "Curated", provenance="curated")
        path = os.path.join(self.root, "graph.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(export_graph(self.bundle), handle)
        destination = os.path.join(self.root, "destination")
        os.mkdir(destination)
        imported = Bundle(destination)
        self.assertEqual(import_graphify(imported, path), (3, 0))
        for bundle in (imported, Bundle(destination)):
            self.assertEqual(
                {note.title: note.meta["provenance"]
                 for note in bundle.notes.values()},
                {"Observed": "extracted", "Inference": "inferred",
                 "Curated": "inferred"})
        self.assertEqual(import_graphify(Bundle(destination), path), (0, 3))

    def test_explicit_provenance_precedes_legacy_origin(self):
        cases = [
            ("explicit-extracted", {"provenance": "extracted", "_origin": "model"}, "extracted"),
            ("explicit-inferred", {"provenance": "inferred", "_origin": "ast"}, "inferred"),
            ("curated", {"provenance": "curated", "_origin": "ast"}, "inferred"),
            ("unknown", {"provenance": "unrecognized", "_origin": "ast"}, "inferred"),
            ("null", {"provenance": None, "_origin": "ast"}, "inferred"),
            ("malformed", {"provenance": [], "_origin": "ast"}, "inferred"),
            ("legacy", {"_origin": "ast"}, "extracted"),
            ("missing", {}, "inferred"),
        ]
        graph = {"nodes": [
            {"id": name, "label": name, "file_type": "concept", **fields}
            for name, fields, _expected in cases
        ]}
        path = os.path.join(self.root, "graph.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(graph, handle)
        import_graphify(self.bundle, path)
        for name, _fields, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(
                    self.bundle.notes[f"imported/{name}.md"].meta["provenance"],
                    expected)

    def test_imported_target_does_not_resolve_to_a_curated_title(self):
        self.note("curated.md", "Worker", provenance="curated")
        graph = {"nodes": [
            {"id": "caller", "label": "Caller", "kind": "function", "lang": "python"},
            {"id": "worker", "label": "Worker", "kind": "function", "lang": "python"},
        ], "links": [{"source": "caller", "target": "worker",
                       "relation": "calls", "confidence": "extracted"}]}
        import_graph(self.bundle, graph)
        for bundle in (self.bundle, Bundle(self.root)):
            self.assertEqual(bundle.notes["extracted/python/caller.md"].links,
                             ["extracted/python/worker.md"])
        self.assertEqual(self.bundle.notes["curated.md"].meta["provenance"], "curated")

    def test_import_drops_incident_edges_for_protected_destination(self):
        graph = {"nodes": [
            {"id": name, "label": name.title(), "kind": "function",
             "lang": "python", "file_type": "concept"}
            for name in ("caller", "worker", "helper")
        ], "links": [
            {"source": "caller", "target": "worker", "relation": "calls",
             "confidence": "extracted"},
            {"source": "worker", "target": "helper", "relation": "calls",
             "confidence": "extracted"},
            {"source": "caller", "target": "helper", "relation": "calls",
             "confidence": "extracted"},
        ]}
        for adapter, prefix in (("extract", "extracted/python"),
                                ("graphify", "imported")):
            for provenance in ("curated", None):
                with self.subTest(adapter=adapter, provenance=provenance):
                    root = os.path.join(self.root, adapter, str(provenance))
                    os.makedirs(root)
                    bundle = Bundle(root)
                    meta = {"title": "An unrelated policy", "type": "note"}
                    if provenance is not None:
                        meta["provenance"] = provenance
                    target = f"{prefix}/worker.md"
                    bundle.write_note(target, meta, "Preserve this policy.")
                    with open(os.path.join(root, target), "rb") as handle:
                        original = handle.read()
                    output = io.StringIO()
                    with redirect_stdout(output):
                        if adapter == "extract":
                            import_graph(bundle, graph)
                        else:
                            graph_path = os.path.join(root, "graph.json")
                            with open(graph_path, "w", encoding="utf-8") as handle:
                                json.dump(graph, handle)
                            import_graphify(bundle, graph_path)
                    for current in (bundle, Bundle(root)):
                        self.assertEqual(
                            current.notes[f"{prefix}/caller.md"].links,
                            [f"{prefix}/helper.md"])
                        self.assertEqual(len(current.notes), 3)
                    with open(os.path.join(root, target), "rb") as handle:
                        self.assertEqual(handle.read(), original)
                    self.assertEqual(len(output.getvalue().splitlines()), 1)
                    self.assertIn("skipped 2 generated edges", output.getvalue())

    def test_reimport_removes_refused_inferred_forms_only(self):
        self.note("extracted/python/worker.md", "An unrelated policy",
                  provenance="curated")
        self.note("other.md", "Other")
        self.note("extracted/python/caller.md", "Caller",
                  "## Connections\n\n"
                  "- calls [[Worker]] (inferred)\n"
                  "- calls [[extracted/python/worker.md|Worker]] (inferred)\n"
                  "- calls [[extracted/python/worker|Worker]] (inferred)\n"
                  "- calls [[EXTRACTED/python/worker.md|Old label]] (INFERRED)\n"
                  "- calls [[Other]] (inferred)\n"
                  "- documents [[Worker]] (inferred)", provenance="extracted")
        graph = {"nodes": [
            {"id": "caller", "label": "Caller", "kind": "function", "lang": "python"},
            {"id": "worker", "label": "Worker", "kind": "function", "lang": "python"},
        ], "links": [{"source": "caller", "target": "worker",
                       "relation": "calls", "confidence": "extracted"}]}
        with redirect_stdout(io.StringIO()):
            import_graph(self.bundle, graph)
        for bundle in (self.bundle, Bundle(self.root)):
            note = bundle.notes["extracted/python/caller.md"]
            self.assertEqual(
                {(edge["relation"], edge["target"], edge["weight"])
                 for edge in note.typed_links},
                {("calls", "other.md", 0.5),
                 ("documents", "extracted/python/worker.md", 0.5)})
            self.assertNotIn("- calls [[Worker]]", note.body)
            self.assertNotIn("- calls [[extracted/python/worker.md", note.body)
            self.assertNotIn("- calls [[extracted/python/worker|", note.body)
            self.assertNotIn("- calls [[EXTRACTED/python/worker.md", note.body)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(import_graph(Bundle(self.root), graph), (0, 1))

    def test_extracted_repeated_headings_keep_their_targets(self):
        graph = {"nodes": [
            {"id": "first", "label": "Overview", "kind": "heading",
             "lang": "markdown", "source_file": "first.md"},
            {"id": "second", "label": "Overview", "kind": "heading",
             "lang": "markdown", "source_file": "second.md"},
        ], "links": [{"source": "first", "target": "second",
                       "relation": "references", "confidence": "extracted"}]}
        import_graph(self.bundle, graph)
        for bundle in (self.bundle, Bundle(self.root)):
            self.assertEqual(bundle.notes["extracted/markdown/overview.md"].links,
                             ["extracted/markdown/overview-2.md"])

    def test_reimport_replaces_legacy_ambiguous_inferred_connections(self):
        self.note("imported/caller.md", "Caller",
                  "## Connections\n\n- calls [[Shared worker]] (inferred)",
                  provenance="inferred")
        graph = {"nodes": [
            {"id": "first", "label": "Shared worker", "file_type": "concept"},
            {"id": "second", "label": "Shared worker", "file_type": "concept"},
            {"id": "caller", "label": "Caller", "file_type": "concept"},
        ], "links": [{"source": "caller", "target": "second",
                       "relation": "calls", "confidence": "inferred"}]}
        path = os.path.join(self.root, "graph.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(graph, handle)
        import_graphify(self.bundle, path)
        self.assertEqual(self.bundle.notes["imported/caller.md"].links,
                         ["imported/shared-worker-2.md"])

    def test_qualified_target_resolves_after_incremental_creation(self):
        self.note("caller.md", "Caller",
                  "- calls [[nested/worker.md|Worker]] (extracted)")
        self.note("other.md", "Worker")
        self.note("nested/worker.md", "Worker")
        self.assertEqual(self.bundle.notes["caller.md"].links,
                         ["nested/worker.md"])
        self.assertEqual(self.bundle.notes["caller.md"].typed_links[0]["target"],
                         "nested/worker.md")

    def test_imported_label_cannot_redirect_to_an_existing_note_path(self):
        self.note("private.md", "Private policy", provenance="curated")
        graph = {"nodes": [
            {"id": "caller", "label": "Caller", "kind": "function", "lang": "python"},
            {"id": "worker", "label": "private.md", "kind": "function", "lang": "python"},
        ], "links": [{"source": "caller", "target": "worker",
                       "relation": "calls", "confidence": "extracted"}]}
        import_graph(self.bundle, graph)
        self.assertEqual(self.bundle.notes["extracted/python/caller.md"].links,
                         ["extracted/python/private-md.md"])

if __name__ == "__main__":
    unittest.main()
