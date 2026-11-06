"""Malformed optional provider data must preserve lexical retrieval."""

import contextlib
import io
import json
import math
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from muninn import embed, enrich
from muninn.store import Bundle


class TestEmbeddingValidation(unittest.TestCase):
    def test_bad_provider_vectors_fail_soft_without_cache_writes(self):
        values = ([10 ** 400], [float("nan")], [float("inf")],
                  [True], [], "123", [None])
        for vector in values:
            with self.subTest(vector=repr(vector)[:40]), tempfile.TemporaryDirectory() as root:
                bundle = Bundle(root)
                bundle.write_note("memory.md", {"title": "Memory"}, "A decision.")
                payload = json.dumps({"data": [{"embedding": vector}]}).encode()
                opener = mock.Mock()
                opener.open.side_effect = lambda *_a, data=payload, **_kw: io.BytesIO(data)
                with mock.patch.dict(os.environ, {"MUNINN_EMBED_URL":
                                     "http://127.0.0.1:9999/embeddings"}, clear=True), \
                        mock.patch.object(embed, "_OPENER", opener), \
                        contextlib.redirect_stderr(io.StringIO()):
                    self.assertIsNone(embed.embed_text("decision"))
                    self.assertEqual(embed.embed_notes(bundle), {})
                self.assertFalse(os.path.exists(embed._cache_path(bundle)))

    def test_invalid_cached_vectors_do_not_break_offline_recall(self):
        with tempfile.TemporaryDirectory() as root:
            bundle = Bundle(root)
            bundle.write_note("memory.md", {"title": "Memory"}, "A decision.")
            digest = embed._sha(embed._note_text(bundle.notes["memory.md"]))
            for entry in (42, {"sha": digest, "vec": [float("nan")]},
                          {"sha": digest, "vec": [10 ** 400]}):
                with self.subTest(entry=repr(entry)[:40]), \
                        mock.patch.dict(os.environ, {}, clear=True), \
                        mock.patch.object(embed, "_load_cache", return_value={
                            "model": None, "notes": {"memory.md": entry}}):
                    self.assertEqual(embed.embed_notes(bundle), {})

    def test_cosine_handles_large_finite_vectors(self):
        value = embed.cosine([1e308, 1e308], [1e308, 1e308])
        self.assertTrue(math.isfinite(value))
        self.assertAlmostEqual(value, 1.0)


class TestTemperatureValidation(unittest.TestCase):
    def test_nonfinite_temperature_uses_finite_default(self):
        for value in ("nan", "inf", "-inf", "1e999", "invalid"):
            with self.subTest(value=value), mock.patch.dict(
                    os.environ, {"MUNINN_ENRICH_TEMPERATURE": value}):
                self.assertEqual(enrich._temperature(), 0.0)


if __name__ == "__main__":
    unittest.main()
