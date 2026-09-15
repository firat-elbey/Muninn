"""Keep saved source relationships stable as a shared map grows."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from muninn import cli, extract
from muninn.store import Bundle


def _graph(source_file="app.py"):
    return {
        "nodes": [
            {"id": "module", "label": "app.py", "kind": "module",
             "lang": "python", "source_file": source_file},
            {"id": "function", "label": "quote", "kind": "function",
             "lang": "python", "source_file": source_file},
        ],
        "links": [{"source": "module", "target": "function",
                   "relation": "contains", "confidence": "extracted"}],
    }


class TestScopedGraphLinks(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_later_project_and_partial_refresh_preserve_resolved_edges(self):
        brain = self.root / "brain"
        alpha = self.root / "alpha"
        beta = self.root / "beta"
        brain.mkdir()
        alpha.mkdir()
        (alpha / "src").mkdir()
        beta.mkdir()
        subprocess.run(["git", "init", "-q", str(alpha)], check=True)

        def save(source, source_file="app.py"):
            cli._import_source_graph(Bundle(str(brain)), _graph(source_file),
                                     str(source))

        def snapshot():
            return {path.relative_to(brain): path.read_bytes()
                    for path in brain.rglob("*.md")}

        save(alpha, "src/app.py")
        alpha_before = snapshot()
        save(beta)
        self.assertEqual(len(snapshot()), 4)
        for path, body in alpha_before.items():
            self.assertEqual((brain / path).read_bytes(), body)
        bundle = Bundle(str(brain))
        modules = [note for note in bundle.notes.values()
                   if note.meta["type"] == "module"]
        self.assertEqual(len(modules), 2)
        for module in modules:
            target = str(Path(module.path).with_name("quote.md"))
            self.assertEqual(module.links, [target])
            self.assertIn(f"[[{target}|quote]]", module.body)
        before_refresh = snapshot()
        save(alpha / "src")
        self.assertEqual(snapshot(), before_refresh)

    def test_scoped_import_preserves_curated_sources_and_targets(self):
        for protected_kind in ("module", "function"):
            with self.subTest(protected_kind=protected_kind):
                brain = self.root / protected_kind
                brain.mkdir()
                bundle = Bundle(str(brain))
                extract.import_graph(bundle, _graph(), scoped=True)
                protected = next(note for note in bundle.notes.values()
                                 if note.meta["type"] == protected_kind)
                bundle.write_note(protected.path,
                                  {"type": "concept", "title": protected.title,
                                   "provenance": "curated"},
                                  "Preserve this authored note.")
                before = (brain / protected.path).read_bytes()
                extract.import_graph(bundle, _graph(), scoped=True)
                self.assertEqual((brain / protected.path).read_bytes(), before)
                self.assertTrue(all(not note.links for note in
                                    Bundle(str(brain)).notes.values()))

    def test_unscoped_unique_links_retain_their_existing_format(self):
        brain = self.root / "unscoped"
        brain.mkdir()
        bundle = Bundle(str(brain))
        extract.import_graph(bundle, _graph())
        module = next(note for note in bundle.notes.values()
                      if note.meta["type"] == "module")
        self.assertIn("- contains [[quote]] (extracted)", module.body)


if __name__ == "__main__":
    unittest.main()
