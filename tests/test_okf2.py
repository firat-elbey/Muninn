"""Verify Muninn's Open Knowledge Format v0.2 conformance.

Consumers accept the legacy `timestamp` field and prefer `generated` metadata.
Optional trust and lifecycle fields remain preserved. Deprecated notes follow
the supersession rules, while `stale_after` adds a retrieval explanation
without excluding the note.
"""

import io
import os
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from muninn import cli  # noqa: E402
from muninn.dynamics import Dynamics  # noqa: E402
from muninn.journal import add_episode, threads_section  # noqa: E402
from muninn.recall import recall  # noqa: E402
from muninn.store import (MUNINN_ACTOR, Bundle, parse_frontmatter,  # noqa: E402
                          render_note)


class _Root(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _run(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--root", self.root, *argv])
        return buf.getvalue()


class TestFrontmatterFamilies(_Root):
    def test_flow_mapping_parses_and_round_trips(self):
        text = ("---\ntype: Metric\n"
                "generated: { by: reference_agent/gemini-2.5-pro, "
                "at: 2026-06-20T22:53:05Z }\n"
                "verified: { by: human:ahormati, at: 2026-06-25T09:00:00Z }\n"
                "usage_window: { from: 2026-06-01, to: 2026-06-30 }\n"
                "---\n\nbody\n")
        meta, body = parse_frontmatter(text)
        self.assertEqual(meta["generated"],
                         {"by": "reference_agent/gemini-2.5-pro",
                          "at": "2026-06-20T22:53:05Z"})
        self.assertEqual(meta["verified"]["by"], "human:ahormati")
        # the colon inside the time survives the mapping split
        self.assertEqual(meta["generated"]["at"], "2026-06-20T22:53:05Z")
        meta2, _ = parse_frontmatter(render_note(meta, body))
        self.assertEqual(meta2["generated"], meta["generated"])

    def test_block_list_of_flow_mappings_parses(self):
        text = ("---\ntype: Metric\n"
                "verified:\n"
                "  - { by: human:ahormati, at: 2026-06-25T09:00:00Z }\n"
                "  - { by: process:nightly, at: 2026-06-26T02:00:00Z }\n"
                "---\n\nbody\n")
        meta, _ = parse_frontmatter(text)
        self.assertEqual([v["by"] for v in meta["verified"]],
                         ["human:ahormati", "process:nightly"])

    def test_flow_list_of_flow_mappings_keeps_entries_whole(self):
        text = ("---\ntype: T\n"
                "verified: [{ by: human:a, at: 2026-01-01T00:00:00Z }, "
                "{ by: process:b, at: 2026-01-02T00:00:00Z }]\n---\n\nx\n")
        meta, _ = parse_frontmatter(text)
        self.assertEqual(len(meta["verified"]), 2)
        self.assertEqual(meta["verified"][1]["by"], "process:b")

    def test_sources_family_is_preserved_verbatim(self):
        b = Bundle(self.root)
        b.write_note("m.md", {"type": "Metric",
                              "sources": [{"id": "h", "resource":
                                           "https://wiki/x", "usage_count": 5000}]},
                     "body")
        note = Bundle(self.root).notes["m.md"]
        self.assertEqual(note.meta["sources"][0]["usage_count"], 5000)


class TestWritersEmitV02(_Root):
    def test_add_writes_generated_not_timestamp(self):
        self._run("add", "a.md", "--title", "A", "--body", "x")
        meta = Bundle(self.root).notes["a.md"].meta
        self.assertNotIn("timestamp", meta)
        self.assertEqual(meta["generated"]["by"], MUNINN_ACTOR)
        self.assertRegex(meta["generated"]["at"],
                         r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")

    def test_lesson_and_journal_write_generated(self):
        self._run("lesson", "Burn", "--body", "how to avoid")
        self._run("journal", "T", "--body", "ep", "--state", "CURRENT: x")
        b = Bundle(self.root)
        for path, note in b.notes.items():
            if path == "index.md":
                continue
            self.assertNotIn("timestamp", note.meta, path)
            self.assertIn("generated", note.meta, path)

    def test_index_declares_v02(self):
        Bundle(self.root).generate_index()
        with open(os.path.join(self.root, "index.md"),
                  encoding="utf-8") as fh:
            self.assertIn('okf_version: "0.2"', fh.read())


class TestLegacyV01StillWorks(_Root):
    def test_thread_recency_falls_back_to_legacy_timestamp(self):
        # a bundle written by muninn-before-v0.2 (or any v0.1 producer)
        b, d = Bundle(self.root), Dynamics(self.root)
        os.makedirs(os.path.join(self.root, "threads", "old"))
        b.write_note("threads/old/thread.md",
                     {"type": "thread", "title": "Old",
                      "timestamp": "2026-01-05T10:00:00"}, "CURRENT: legacy")
        b.write_note("threads/old/20260105-100000.md",
                     {"type": "episode", "title": "Old ep",
                      "timestamp": "2026-01-05T10:00:00"}, "did things")
        add_episode(b, d, "New", "fresh work",
                    state="CURRENT: new", when=time.time())
        section = threads_section(Bundle(self.root), d)
        self.assertIn("New", section)  # the v0.2 thread wins recency
        # and the legacy thread is still readable, not rejected
        self.assertIn("threads/old/thread.md", Bundle(self.root).notes)


class TestLifecycleShapesRecall(_Root):
    def test_deprecated_is_excluded_like_superseded(self):
        b = Bundle(self.root)
        b.write_note("old.md", {"type": "note", "title": "Port note",
                                "status": "deprecated"},
                     "postgres port is 5432")
        b.write_note("new.md", {"type": "note", "title": "Port note new"},
                     "postgres port is 7433")
        d = Dynamics(self.root)
        paths = [n.path for n, _s, _w in
                 recall(Bundle(self.root), d, "postgres port", k=5,
                        reactivate=False)]
        self.assertIn("new.md", paths)
        self.assertNotIn("old.md", paths)

    def test_stale_after_labels_but_never_hides(self):
        b = Bundle(self.root)
        b.write_note("runbook.md", {"type": "note", "title": "Restore",
                                    "stale_after": "2020-01-01"},
                     "restore postgres from basebackup")
        d = Dynamics(self.root)
        hits = recall(Bundle(self.root), d, "restore postgres", k=3,
                      reactivate=False)
        self.assertEqual(hits[0][0].path, "runbook.md")  # served
        self.assertIn("STALE since 2020-01-01", hits[0][2])  # and labeled

    def test_future_stale_after_is_not_stale(self):
        b = Bundle(self.root)
        b.write_note("r.md", {"type": "note", "title": "R",
                              "stale_after": "2999-01-01"}, "restore steps")
        hits = recall(Bundle(self.root), Dynamics(self.root),
                      "restore steps", k=3, reactivate=False)
        self.assertNotIn("STALE", hits[0][2])


if __name__ == "__main__":
    unittest.main()
