"""Fused-graph tests: typed links, code stubs, hash-skip re-import, export."""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from muninn import cli  # noqa: E402
from muninn.dynamics import Dynamics  # noqa: E402
from muninn.export import export_graph  # noqa: E402
from muninn.ingest import CODE_CAP, import_graphify  # noqa: E402
from muninn.store import Bundle  # noqa: E402


def _write_graph(root, nodes, links=(), key="links"):
    p = os.path.join(root, "graph.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"nodes": nodes, key: list(links)}, fh)
    return p


def _concept(nid, label, **extra):
    return {"id": nid, "label": label, "file_type": "concept", **extra}


class TestGraphifyWriterVariants(unittest.TestCase):
    """Current graphify has TWO graph.json writers: the clustered path
    stores edges under ``links`` (networkx node-link default), the raw
    ``--no-cluster`` path: exactly what ``muninn build`` invokes :
    stores them under ``edges``. Reading only one key silently imports a
    graph with zero connections."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_raw_no_cluster_edges_key_imports_connections(self):
        nodes = [_concept("a", "Alpha", _origin="ast"), _concept("b", "Beta")]
        links = [{"source": "a", "target": "b", "relation": "imports",
                  "confidence": "EXTRACTED"}]
        gp = _write_graph(self.root, nodes, links, key="edges")
        import_graphify(Bundle(self.root), gp)
        b = Bundle(self.root)
        note = b.notes["imported/alpha.md"]
        self.assertIn("# Connections", note.body)
        self.assertEqual(note.typed_links[0]["target"], "imported/beta.md")
        self.assertEqual(note.typed_links[0]["relation"], "imports")

    def test_src_tgt_markers_outrank_flipped_endpoints(self):
        # graphs written from undirected storage can persist FLIPPED
        # source/target with the true direction stashed in _src/_tgt
        # (graphify #563/#2309): the markers are the truth
        nodes = [_concept("a", "Alpha"), _concept("b", "Beta")]
        links = [{"source": "b", "target": "a", "relation": "calls",
                  "confidence": "EXTRACTED", "_src": "a", "_tgt": "b"}]
        gp = _write_graph(self.root, nodes, links)
        import_graphify(Bundle(self.root), gp)
        b = Bundle(self.root)
        self.assertIn("# Connections", b.notes["imported/alpha.md"].body)
        self.assertNotIn("# Connections", b.notes["imported/beta.md"].body)


class TestTypedLinks(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_ingest_roundtrip_weights(self):
        """What ingest writes under '# Connections', store reads back as
        typed weighted edges: with the confidence -> weight table."""
        nodes = [_concept("a", "Alpha", _origin="ast"),
                 _concept("b", "Beta"), _concept("c", "Gamma"),
                 _concept("d", "Delta")]
        links = [
            {"source": "a", "target": "b", "relation": "imports",
             "confidence": "EXTRACTED"},
            {"source": "a", "target": "c", "relation": "refines",
             "confidence": "INFERRED"},
            {"source": "a", "target": "d", "relation": "mentions",
             "confidence": "AMBIGUOUS"},
        ]
        gp = _write_graph(self.root, nodes, links)
        import_graphify(Bundle(self.root), gp)
        b = Bundle(self.root)
        note = b.notes["imported/alpha.md"]
        by_rel = {e["relation"]: e for e in note.typed_links}
        self.assertEqual(by_rel["imports"]["weight"], 1.0)
        self.assertEqual(by_rel["refines"]["weight"], 0.5)
        self.assertEqual(by_rel["mentions"]["weight"], 0.2)
        self.assertEqual(by_rel["imports"]["target"], "imported/beta.md")
        # zero behavior break: typed targets are still plain links too
        for e in note.typed_links:
            self.assertIn(e["target"], note.links)
        self.assertIn("imported/alpha.md", b.typed_edges())

    def test_defaults_and_unknown_confidence_word(self):
        b = Bundle(self.root)
        b.write_note("t.md", {"title": "Target"}, "x")
        b.write_note("s.md", {"title": "S"},
                     "- [[Target]]\n- uses [[Target]] (0.9)")
        tl = Bundle(self.root).notes["s.md"].typed_links
        by_rel = {e["relation"]: e for e in tl}
        # missing relation/confidence -> related_to, extracted, 1.0
        self.assertEqual(by_rel["related_to"]["confidence_word"], "extracted")
        self.assertEqual(by_rel["related_to"]["weight"], 1.0)
        # unknown confidence word -> 0.5
        self.assertEqual(by_rel["uses"]["weight"], 0.5)

    def test_prose_wikilinks_stay_plain(self):
        b = Bundle(self.root)
        b.write_note("t.md", {"title": "Target"}, "x")
        b.write_note("s.md", {"title": "S"},
                     "see [[Target]] inline\n- see [[Target]] for details\n"
                     "- read [[Target]] (old version)")  # multi-word parens
        note = Bundle(self.root).notes["s.md"]
        self.assertEqual(note.typed_links, [])  # none of these is typed
        self.assertEqual(note.links, ["t.md"])  # but the link survives

    def test_multiword_relation_and_broken_target(self):
        b = Bundle(self.root)
        b.write_note("t.md", {"title": "Target"}, "x")
        b.write_note("s.md", {"title": "S"},
                     "- depends on [[Target]] (inferred)\n"
                     "- imports [[No Such Note]] (extracted)")
        tl = Bundle(self.root).notes["s.md"].typed_links
        self.assertEqual([(e["relation"], e["weight"]) for e in tl],
                         [("depends on", 0.5)])  # broken target dropped

    def test_by_resource_index(self):
        b = Bundle(self.root)
        b.write_note("a.md", {"title": "A", "resource": "src/a.py"}, "x")
        b.write_note("b.md", {"title": "B"}, "x")
        idx = Bundle(self.root).by_resource()
        self.assertEqual(idx["src/a.py"].path, "a.md")
        self.assertEqual(len(idx), 1)


class TestIncludeCode(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root)

    def _code_graph(self, n_code=2):
        nodes = [_concept("c1", "Cache Strategy")]
        links = []
        for i in range(n_code):
            nodes.append({"id": f"f{i}", "label": f"parser_{i}",
                          "file_type": "code",
                          "source_file": f"src/parser_{i}.py"})
            links.append({"source": f"f{i}", "target": "c1",
                          "relation": "implements",
                          "confidence": "EXTRACTED"})
        return _write_graph(self.root, nodes, links)

    def test_code_stubs_written_with_flag(self):
        gp = self._code_graph()
        written, skipped = import_graphify(Bundle(self.root), gp,
                                           include_code=True)
        self.assertEqual((written, skipped), (3, 0))
        note = Bundle(self.root).notes["imported/code/parser-0.md"]
        self.assertEqual(note.meta["provenance"], "extracted")
        self.assertEqual(note.meta["resource"], "src/parser_0.py")
        self.assertEqual(note.meta["tags"], ["graphify-import", "code"])
        # the one-line body (a node without rationale must not become "None")
        self.assertIn("Code-level node `parser_0` from the source graph.",
                      note.body)
        self.assertNotIn("None", note.body)
        self.assertIn("# Connections", note.body)
        self.assertEqual(note.typed_links[0]["relation"], "implements")
        self.assertEqual(note.typed_links[0]["weight"], 1.0)

    def test_slug_collisions_get_distinct_paths(self):
        nodes = [{"id": f"f{i}", "label": "main", "file_type": "code",
                  "source_file": f"pkg{i}/main.py"} for i in range(2)]
        gp = _write_graph(self.root, nodes)
        self.assertEqual(import_graphify(Bundle(self.root), gp,
                                         include_code=True), (2, 0))
        b = Bundle(self.root)
        self.assertEqual(b.notes["imported/code/main.md"].meta["resource"],
                         "pkg0/main.py")
        self.assertEqual(b.notes["imported/code/main-2.md"].meta["resource"],
                         "pkg1/main.py")
        # and the re-import converges to a full no-op
        self.assertEqual(import_graphify(Bundle(self.root), gp,
                                         include_code=True), (0, 2))

    def test_wikilink_metachars_cannot_forge_edges(self):
        """A label like 'Secret note|mask' must not resolve a typed edge
        to an unrelated curated note."""
        b = Bundle(self.root)
        b.write_note("secret-note.md", {"title": "Secret note"}, "private")
        nodes = [_concept("a", "Alpha"),
                 _concept("evil", "Secret note|mask")]
        links = [{"source": "a", "target": "evil", "relation": "cites",
                  "confidence": "EXTRACTED"}]
        gp = _write_graph(self.root, nodes, links)
        import_graphify(Bundle(self.root), gp)
        note = Bundle(self.root).notes["imported/alpha.md"]
        self.assertNotIn("secret-note.md", note.links)
        self.assertNotIn("secret-note.md",
                         [e["target"] for e in note.typed_links])

    def test_code_skipped_without_flag(self):
        written, _ = import_graphify(Bundle(self.root), self._code_graph())
        self.assertEqual(written, 1)  # concept only
        self.assertNotIn("imported/code/parser-0.md",
                         Bundle(self.root).notes)

    def test_cap_is_counted_not_silent(self):
        self.assertEqual(CODE_CAP, 500)  # the real cap
        gp = self._code_graph(n_code=8)
        buf = io.StringIO()
        with mock.patch("muninn.ingest.CODE_CAP", 5), redirect_stdout(buf):
            written, _ = import_graphify(Bundle(self.root), gp,
                                         include_code=True)
        self.assertEqual(written, 6)  # 1 concept + 5 stubs
        self.assertIn("skipped 3", buf.getvalue())


class TestHashSkip(unittest.TestCase):
    def test_second_import_is_git_diff_clean(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        gp = _write_graph(root, [_concept("a", "Alpha"), _concept("b", "B")],
                          [{"source": "a", "target": "b",
                            "relation": "imports", "confidence": "EXTRACTED"}])
        self.assertEqual(import_graphify(Bundle(root), gp), (2, 0))
        path = os.path.join(root, "imported", "alpha.md")
        with open(path, "rb") as fh:
            before = fh.read()
        self.assertEqual(import_graphify(Bundle(root), gp), (0, 2))
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), before)  # byte-identical


class TestExportGraph(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        b = Bundle(self.root)
        b.write_note("b.md", {"title": "Beta"}, "plain")
        b.write_note("c.md", {"title": "Gamma"}, "plain")
        b.write_note("a.md", {"title": "Alpha"},
                     "prose mentions [[Gamma]].\n\n# Connections\n\n"
                     "- imports [[Beta]] (inferred)")
        b.write_note("old.md", {"title": "Old fact"}, "stale")
        b.write_note("new.md", {"title": "New fact",
                                "supersedes": ["old.md"]}, "fresh")
        d = Dynamics(self.root)
        d.touch("a.md", session="s1")  # co-use pair a.md|b.md
        d.touch("b.md", session="s1")

    def tearDown(self):
        shutil.rmtree(self.root)

    def _edges(self, g, kind):
        return [e for e in g["links"] if e["kind"] == kind]

    def test_all_four_edge_kinds_present(self):
        g = export_graph(Bundle(self.root), Dynamics(self.root))
        self.assertEqual({n["id"] for n in g["nodes"]},
                         {"a.md", "b.md", "c.md", "old.md", "new.md"})
        typed = self._edges(g, "typed")
        self.assertEqual([(e["source"], e["target"], e["relation"],
                           e["weight"]) for e in typed],
                         [("a.md", "b.md", "imports", 0.5)])
        plain = self._edges(g, "link")
        self.assertIn(("a.md", "c.md"),
                      [(e["source"], e["target"]) for e in plain])
        sup = self._edges(g, "supersedes")
        self.assertEqual([(e["source"], e["target"]) for e in sup],
                         [("new.md", "old.md")])
        co = self._edges(g, "used_with")
        self.assertEqual([(e["source"], e["target"]) for e in co],
                         [("a.md", "b.md")])
        self.assertAlmostEqual(co[0]["weight"], round(1 / 3, 4))
        # node shape: strength from the sidecar, provenance defaulted
        by_id = {n["id"]: n for n in g["nodes"]}
        self.assertGreater(by_id["a.md"]["strength"], 0.0)
        self.assertEqual(by_id["c.md"]["provenance"], "curated")

    def test_typed_edge_not_double_counted_as_plain(self):
        g = export_graph(Bundle(self.root), Dynamics(self.root))
        plain_pairs = [(e["source"], e["target"])
                       for e in self._edges(g, "link")]
        self.assertNotIn(("a.md", "b.md"), plain_pairs)

    def test_export_is_deterministic(self):
        one = export_graph(Bundle(self.root), Dynamics(self.root))
        two = export_graph(Bundle(self.root), Dynamics(self.root))
        self.assertEqual(one, two)

    def test_cli_export_graph_writes_file(self):
        out = os.path.join(self.root, "fused.json")
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--root", self.root, "export-graph", out])
        self.assertIn("wrote 5 nodes", buf.getvalue())
        with open(out, encoding="utf-8") as fh:
            g = json.load(fh)
        self.assertEqual({e["kind"] for e in g["links"]},
                         {"typed", "link", "supersedes", "used_with"})

    def test_cli_export_graph_stdout(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--root", self.root, "export-graph"])
        g = json.loads(buf.getvalue())
        self.assertEqual(len(g["nodes"]), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
