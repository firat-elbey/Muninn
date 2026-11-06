"""A field report requires evidence imported from the subject repository."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eval import field_probe


class FieldProbeTests(unittest.TestCase):
    def test_fresh_probe_reloads_extracted_notes(self):
        graph = {"nodes": [{"id": "dispatch", "label": "Dispatch", "lang": "python",
                            "kind": "function", "body": "Dispatch the request."}]}
        with tempfile.TemporaryDirectory() as base:
            (Path(base) / "work-sample" / "repo").mkdir(parents=True)
            with patch.object(field_probe.extract, "extract_path",
                              return_value=(graph, {"files": 1, "by_lang": {"python": 1}})):
                report = field_probe.probe("sample", "local", ["dispatch"], base)
            self.assertIn("notes: 1 ", Path(report).read_text(encoding="utf-8"))

    def test_empty_import_cannot_pass_on_a_retained_note(self):
        with tempfile.TemporaryDirectory() as base:
            work = Path(base) / "work-sample"
            (work / "repo").mkdir(parents=True)
            (work / "kb").mkdir()
            (work / "kb" / "old.md").write_text("Previous run.", encoding="utf-8")
            with patch.object(field_probe.extract, "extract_path",
                              return_value=({"nodes": []}, {"files": 1, "by_lang": {}})):
                with self.assertRaisesRegex(RuntimeError, "no.*notes"):
                    field_probe.probe("sample", "local", ["dispatch"], base)
            self.assertFalse((Path(base) / "reports" / "sample.txt").exists())
