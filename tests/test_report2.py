"""Regression tests derived from the second internal-use report:
U1 (real content in imported note bodies), N1 (goals tilt, never seed),
N2 (`goal --off` without text), N3 (dormant split + unused why-lines),
P2-residual (graph nodes with no id skip-and-warn)."""

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
from muninn.activate import goal_alignment, relevance  # noqa: E402
from muninn.dynamics import Dynamics  # noqa: E402
from muninn.ingest import import_graphify  # noqa: E402
from muninn.recall import context_pack, recall, tokens  # noqa: E402
from muninn.store import Bundle  # noqa: E402


def _write_graph(root, nodes, links=()):
    p = os.path.join(root, "graph.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"nodes": nodes, "links": list(links)}, fh)
    return p


SPEC_MD = """# Spec

Intro line.

## The sidecar

The sidecar is derived usage state, so it is safe to delete it: recall
strengths rebuild from the ledger and the notes themselves stay intact.

A second paragraph with extra detail for the section.

## Other topic

Unrelated words.
"""

TOOL_PY = '''"""Restore tooling for the demo database."""


def restore_db(host, port=7433):
    """Restores the database from the newest basebackup.

    Longer body here.
    """
    return host


def long_sig(host,
             port=7433):
    """Docstring after a wrapped signature."""
    return port
'''


class TestImportedBodies(unittest.TestCase):
    """U1: imported notes carry answer text from the source file, not
    only a heading echo: the whole remaining round-2 scoreboard gap."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.kb = os.path.join(self.tmp, "kb")
        self.src = os.path.join(self.tmp, "src")
        os.makedirs(self.kb)
        os.makedirs(self.src)
        with open(os.path.join(self.src, "SPEC.md"), "w",
                  encoding="utf-8") as fh:
            fh.write(SPEC_MD)
        with open(os.path.join(self.src, "tool.py"), "w",
                  encoding="utf-8") as fh:
            fh.write(TOOL_PY)
        self.nodes = [
            {"id": "spec_sidecar", "label": "The sidecar",
             "file_type": "document", "source_file": "SPEC.md",
             "_origin": "ast"},
            {"id": "tool_restore_db", "label": "restore_db",
             "file_type": "code", "source_file": "tool.py"},
            {"id": "tool_long_sig", "label": "long_sig",
             "file_type": "code", "source_file": "tool.py"},
            {"id": "tool", "label": "tool.py", "file_type": "code",
             "source_file": "tool.py", "source_location": "L1"},
        ]

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _import(self, source_root=None, nodes=None):
        gp = _write_graph(self.kb, nodes or self.nodes)
        import_graphify(Bundle(self.kb), gp, include_code=True,
                        source_root=source_root)
        return Bundle(self.kb)

    def test_document_body_contains_section_paragraph(self):
        b = self._import(source_root=self.src)
        body = b.notes["imported/the-sidecar.md"].body
        self.assertIn("safe to delete it", body)
        self.assertIn("rebuild from the ledger", body)
        self.assertIn("A second paragraph", body)
        self.assertNotIn("Unrelated words", body)  # next section excluded

    def test_pack_serves_answer_text_not_just_pointer(self):
        # Q7-class repro: "is it safe to delete the sidecar": the pack
        # must contain the answer sentence, not only the heading
        self._import(source_root=self.src)
        b = Bundle(self.kb)
        d = Dynamics(self.kb)
        pack = context_pack(b, d, "is it safe to delete the sidecar",
                            budget=400, k=2, reactivate=False)
        focus = pack.split("## Focus", 1)[1]
        self.assertIn("(imported/the-sidecar.md)", focus)
        self.assertIn("safe to delete it", focus)

    def test_code_note_gets_def_line_and_docstring(self):
        b = self._import(source_root=self.src)
        body = b.notes["imported/code/restore-db.md"].body
        self.assertIn("def restore_db(host, port=7433):", body)
        self.assertIn("Restores the database from the newest basebackup.",
                      body)
        self.assertNotIn('"""', body)  # quotes stripped, prose kept
        # a wrapped (multi-line) signature still yields its docstring
        body2 = b.notes["imported/code/long-sig.md"].body
        self.assertIn("def long_sig(host,", body2)
        self.assertIn("Docstring after a wrapped signature.", body2)

    def test_dotted_method_label_finds_the_symbol(self):
        # graphify qualifies method labels ('Tool.long_sig()'): the last
        # dotted segment is the symbol to scan for
        nodes = [{"id": "m", "label": "Tool.long_sig()",
                  "file_type": "code", "source_file": "tool.py"}]
        b = self._import(source_root=self.src, nodes=nodes)
        body = b.notes["imported/code/tool-long-sig.md"].body
        self.assertIn("Docstring after a wrapped signature.", body)

    def test_file_level_code_node_gets_module_docstring(self):
        b = self._import(source_root=self.src)
        body = b.notes["imported/code/tool-py.md"].body
        self.assertIn("Restore tooling for the demo database.", body)

    def test_fail_soft_without_source_root_keeps_stub(self):
        b = self._import(source_root=None)
        self.assertNotIn("safe to delete",
                         b.notes["imported/the-sidecar.md"].body)
        self.assertIn("Code-level node `restore_db`",
                      b.notes["imported/code/restore-db.md"].body)

    def test_fail_soft_on_missing_file_and_unmatched_heading(self):
        nodes = [
            {"id": "gone", "label": "Some heading",
             "file_type": "document", "source_file": "missing.md"},
            {"id": "nohead", "label": "No such heading anywhere",
             "file_type": "document", "source_file": "SPEC.md"},
        ]
        b = self._import(source_root=self.src, nodes=nodes)
        self.assertEqual(b.notes["imported/some-heading.md"].body.strip(),
                         "Some heading")
        self.assertEqual(
            b.notes["imported/no-such-heading-anywhere.md"].body.strip(),
            "No such heading anywhere")

    def test_source_location_disambiguates_repeated_headings(self):
        doc = ("# Survey\n\n## Entry one\n\n### Verdict\n\n"
               "First verdict paragraph.\n\n## Entry two\n\n### Verdict\n\n"
               "Second verdict paragraph.\n")
        with open(os.path.join(self.src, "SURVEY.md"), "w",
                  encoding="utf-8") as fh:
            fh.write(doc)
        nodes = [{"id": "v2", "label": "Verdict", "file_type": "document",
                  "source_file": "SURVEY.md", "source_location": "L11"}]
        b = self._import(source_root=self.src, nodes=nodes)
        body = b.notes["imported/verdict.md"].body
        self.assertIn("Second verdict paragraph.", body)
        self.assertNotIn("First verdict paragraph.", body)

    def test_long_paragraph_truncates_cleanly(self):
        long_para = " ".join(f"word{i}" for i in range(150))  # ~1000 chars
        with open(os.path.join(self.src, "LONG.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("# Long\n\n## Big section\n\n" + long_para + "\n")
        nodes = [{"id": "big", "label": "Big section",
                  "file_type": "document", "source_file": "LONG.md"}]
        b = self._import(source_root=self.src, nodes=nodes)
        content = b.notes["imported/big-section.md"].body.strip().split("\n")[0]
        self.assertLessEqual(len(content), 420)  # ~400-char cap held
        self.assertTrue(content.endswith("…"))   # marked, clean cut
        # no mid-word chop: the kept text is an exact prefix of the
        # original ending at a word boundary
        prefix = content[:-2]  # strip " …"
        self.assertTrue(long_para.startswith(prefix))
        self.assertEqual(long_para[len(prefix)], " ")

    def test_empty_section_stays_a_stub_not_the_next_section(self):
        # review-panel finding: a heading with no content must not pull
        # the NEXT section's paragraph under its own confident title
        with open(os.path.join(self.src, "EMPTY.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("# Doc\n\n## Empty section\n\n## Other topic\n\n"
                     "Other topic's words.\n")
        nodes = [{"id": "e", "label": "Empty section",
                  "file_type": "document", "source_file": "EMPTY.md"}]
        b = self._import(source_root=self.src, nodes=nodes)
        body = b.notes["imported/empty-section.md"].body
        self.assertNotIn("Other topic's words", body)
        self.assertEqual(body.strip(), "Empty section")  # the stub

    def test_stale_source_location_falls_back_to_label_scan(self):
        # a location pointing at a DIFFERENT heading (stale graph) must
        # not silently serve that other section's content
        with open(os.path.join(self.src, "STALE.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("# Doc\n\n## Wrong place\n\nWrong words.\n\n"
                     "## Right place\n\nRight words.\n")
        nodes = [{"id": "r", "label": "Right place",
                  "file_type": "document", "source_file": "STALE.md",
                  "source_location": "L3"}]  # L3 = "## Wrong place"
        b = self._import(source_root=self.src, nodes=nodes)
        body = b.notes["imported/right-place.md"].body
        self.assertIn("Right words.", body)
        self.assertNotIn("Wrong words.", body)

    def test_dotfiles_and_config_types_are_never_excerpted(self):
        # secret-exposure guard: an untrusted graph.json must not be able
        # to pull .env-class file contents into LLM-bound note bodies
        with open(os.path.join(self.src, ".env"), "w",
                  encoding="utf-8") as fh:
            fh.write("API_KEY=sk-live-TOPSECRET\n")
        with open(os.path.join(self.src, "conf.py"), "w",
                  encoding="utf-8") as fh:
            fh.write("## looks like a heading\nPASSWORD = 'hunter2'\n")
        nodes = [
            {"id": "env", "label": ".env", "file_type": "document",
             "source_file": ".env"},
            {"id": "conf", "label": "looks like a heading",
             "file_type": "document", "source_file": "conf.py"},
        ]
        b = self._import(source_root=self.src, nodes=nodes)
        joined = " ".join(n.body for n in b.notes.values())
        self.assertNotIn("TOPSECRET", joined)
        self.assertNotIn("hunter2", joined)

    def test_control_characters_are_stripped_from_excerpts(self):
        with open(os.path.join(self.src, "ESC.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("# Doc\n\n## Colors\n\ntext \x1b[31mred\x1b[0m "
                     "and\x07 more.\n")
        nodes = [{"id": "c", "label": "Colors", "file_type": "document",
                  "source_file": "ESC.md"}]
        b = self._import(source_root=self.src, nodes=nodes)
        body = b.notes["imported/colors.md"].body
        self.assertIn("more.", body)
        self.assertNotIn("\x1b", body)
        self.assertNotIn("\x07", body)

    def test_reimport_with_source_root_is_deterministic(self):
        self._import(source_root=self.src)
        gp = os.path.join(self.kb, "graph.json")
        # byte-identical second import: all skipped
        written, skipped = import_graphify(Bundle(self.kb), gp,
                                           include_code=True,
                                           source_root=self.src)
        self.assertEqual(written, 0)
        self.assertGreater(skipped, 0)
        # a curated body survives a source-file change (ownership rule)
        Bundle(self.kb).write_note("imported/the-sidecar.md",
                                   {"title": "The sidecar"}, "precious")
        with open(os.path.join(self.src, "SPEC.md"), "a",
                  encoding="utf-8") as fh:
            fh.write("\nChanged.\n")
        import_graphify(Bundle(self.kb), gp, include_code=True,
                        source_root=self.src)
        self.assertIn("precious",
                      Bundle(self.kb).notes["imported/the-sidecar.md"].body)

    def test_graph_paths_cannot_escape_the_source_root(self):
        with open(os.path.join(self.tmp, "secret.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("# Escape\n\nTOPSECRET contents.\n")
        nodes = [{"id": "esc", "label": "Escape", "file_type": "document",
                  "source_file": "../secret.md"}]
        b = self._import(source_root=self.src, nodes=nodes)
        self.assertNotIn("TOPSECRET", b.notes["imported/escape.md"].body)

    def test_import_cli_passes_source_root_flag(self):
        gp = _write_graph(self.kb, self.nodes)
        with redirect_stdout(io.StringIO()):
            cli.main(["--root", self.kb, "import", gp, "--include-code",
                      "--source-root", self.src])
        self.assertIn("safe to delete it",
                      Bundle(self.kb).notes["imported/the-sidecar.md"].body)

    def test_build_passes_the_scanned_folder_as_source_root(self):
        scanned = os.path.join(self.tmp, "repo")
        os.makedirs(scanned)
        with open(os.path.join(scanned, "GUIDE.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("# Widget guide\n\nTurn the widget clockwise "
                     "to disarm it safely.\n")
        graph = {"nodes": [{"id": "g", "label": "Widget guide",
                            "file_type": "document",
                            "source_file": "GUIDE.md"}], "links": []}

        def fake_run(cmd, **kw):
            out = kw["env"]["GRAPHIFY_OUT"]
            os.makedirs(out, exist_ok=True)
            with open(os.path.join(out, "graph.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(graph, fh)
            return mock.Mock(returncode=0)

        with mock.patch.dict(os.environ), \
                mock.patch("muninn.cli.shutil.which",
                           return_value="/usr/bin/graphify"), \
                mock.patch("muninn.cli.subprocess.run",
                           side_effect=fake_run), \
                redirect_stdout(io.StringIO()):
            os.environ.pop("GRAPHIFY_OUT", None)
            cli.main(["--root", self.kb, "build", scanned])
        self.assertIn("Turn the widget clockwise",
                      Bundle(self.kb).notes["imported/widget-guide.md"].body)


class TestGoalTiltNotSeed(unittest.TestCase):
    """N1: a goal must not place notes into Focus the query never
    reached, and a lone generic token inside a URL is not alignment."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        b = Bundle(self.root)
        b.write_note("port.md", {"title": "Port config"},
                     "postgres port 7433")
        b.write_note(
            "engram.md",
            {"title": "github.com/x/engram ~4.8k stars README + "
                      "docs/ARCHITECTURE.md + docs/beta/brain.md fetched"},
            "Imported prior-art stub.")
        self.b = Bundle(self.root)
        self.d = Dynamics(self.root)
        self.d.goal("improve onboarding docs", 0.8)

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_url_fragment_title_is_not_goal_alignment(self):
        gtoks = tokens("improve onboarding docs")
        note = self.b.notes["engram.md"]
        self.assertGreater(relevance(gtoks, note), 0)  # lexically it DOES hit
        self.assertEqual(goal_alignment(gtoks, note), 0.0)  # but no alignment

    def test_goal_report_shows_zero_notes_for_url_only_match(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--root", self.root, "goal"])
        self.assertIn("improve onboarding docs  (aligns with 0 of 2 notes)",
                      buf.getvalue())

    def test_unrelated_pack_has_no_goal_tagalong_slot(self):
        # the round-2 tax: this stub rode into ALL 10 exam packs' last
        # Focus slot; it must not appear at all now
        pack = context_pack(self.b, self.d, "postgres port", budget=800,
                            k=5, reactivate=False)
        focus = pack.split("## Focus", 1)[1]
        self.assertIn("### Port config (port.md)", focus)
        self.assertNotIn("engram.md", focus)
        self.assertNotIn("aligned with active goal", focus)

    def test_nomatch_pack_falls_back_without_goal_seeding(self):
        pack = context_pack(self.b, self.d, "zebra kayak paragliding",
                            budget=400, k=2, reactivate=False)
        self.assertIn("*(no match for zebra kayak paragliding", pack)
        self.assertNotIn("aligned with active goal", pack)  # nothing goal-seeded

    def test_single_token_match_outside_urls_still_aligns(self):
        Bundle(self.root).write_note("plain.md", {"title": "Docs cleanup"},
                                     "tidy the docs index this sprint")
        note = Bundle(self.root).notes["plain.md"]
        self.assertGreater(
            goal_alignment(tokens("improve onboarding docs"), note), 0)

    def test_two_distinct_matched_tokens_align_even_from_urls(self):
        # the spec'd rule kept simple: >=2 distinct matching tokens is
        # alignment, wherever they occur (bounded: tilt-only, no seeding)
        Bundle(self.root).write_note(
            "two.md", {"title": "github.com/x/onboarding/docs/README"},
            "stub")
        note = Bundle(self.root).notes["two.md"]
        self.assertGreater(
            goal_alignment(tokens("improve onboarding docs"), note), 0)


class TestGoalOffWithoutText(unittest.TestCase):
    """N2: `muninn goal --off` with no text must retire the single
    active goal or report an explicit error. The operation cannot silently do nothing."""

    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_single_active_goal_is_retired(self):
        Dynamics(self.root).goal("ship the migration", 0.8)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--root", self.root, "goal", "--off"])
        self.assertIn("retired goal: ship the migration", buf.getvalue())
        self.assertEqual(Dynamics(self.root).goals, {})

    def test_several_goals_lists_and_exits_nonzero(self):
        d = Dynamics(self.root)
        d.goal("ship the migration", 0.8)
        d.goal("clean the backlog", 0.5)
        buf = io.StringIO()
        with redirect_stdout(buf), self.assertRaises(SystemExit) as cm:
            cli.main(["--root", self.root, "goal", "--off"])
        self.assertNotEqual(cm.exception.code, 0)
        out = buf.getvalue()
        self.assertIn("specify which goal to retire", out)
        self.assertIn("ship the migration", out)
        self.assertIn("clean the backlog", out)
        self.assertEqual(len(Dynamics(self.root).goals), 2)  # untouched

    def test_no_goals_is_loud_not_silent(self):
        buf = io.StringIO()
        with redirect_stdout(buf), self.assertRaises(SystemExit) as cm:
            cli.main(["--root", self.root, "goal", "--off"])
        self.assertNotEqual(cm.exception.code, 0)
        self.assertIn("no active goal to retire", buf.getvalue())

    def test_off_with_unknown_text_fails_loud(self):
        # review-panel finding: `goal "<typo>" --off` used to print
        # "retired goal: <typo>" over unchanged state
        Dynamics(self.root).goal("ship the migration", 0.8)
        buf = io.StringIO()
        with redirect_stdout(buf), self.assertRaises(SystemExit) as cm:
            cli.main(["--root", self.root, "goal", "ship the migrtion",
                      "--off"])
        self.assertNotEqual(cm.exception.code, 0)
        out = buf.getvalue()
        self.assertIn("no active goal matches: ship the migrtion", out)
        self.assertIn("ship the migration", out)  # the real one, listed
        self.assertEqual(list(Dynamics(self.root).goals),
                         ["ship the migration"])


class TestDormantSplit(unittest.TestCase):
    """N3: faded-from-use vs recall-capped-never-touched are different
    things and must read differently."""

    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_consolidate_and_stats_split_the_dormant_count(self):
        b = Bundle(self.root)
        for i in range(4):
            b.write_note(f"n{i}.md", {"title": f"Note {i}"}, "x")
        d = Dynamics(self.root)
        d.touch("n2.md")
        for _ in range(80):
            d.consolidate()  # n2 really fades
        d.touch("n0.md")  # active
        d.touch("n3.md", kind="recall")  # tracked but never touched
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--root", self.root, "consolidate"])
        self.assertIn("consolidated: 1 active, 1 faded, 1 recall-only "
                      "(never touched): 3 tracked (1 notes at baseline)",
                      buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--root", self.root, "stats"])
        self.assertIn("notes: 4  tracked: 3 "
                      "(1 active, 1 faded, 1 recall-only)", buf.getvalue())

    def test_why_line_says_unused_not_a_tiny_strength(self):
        b = Bundle(self.root)
        b.write_note("real.md", {"title": "Walk bounds"},
                     "the walk is bounded and damped")
        b.write_note("stub.md", {"title": "Walk stub"},
                     "walk words in a stub")
        b = Bundle(self.root)
        d = Dynamics(self.root)
        d.touch("real.md")
        d.touch("stub.md", kind="recall")  # strength 0.01, never touched
        whys = {n.path: w for n, _s, w in
                recall(b, d, "walk bounded", k=5, reactivate=False)}
        self.assertIn("strength 0.34", whys["real.md"])  # earned: a number
        self.assertIn("unused", whys["stub.md"])
        self.assertNotIn("strength 0.0", whys["stub.md"])
        # recall semantics themselves are unchanged
        self.assertTrue(d.is_dormant("stub.md"))
        self.assertFalse(d.is_dormant("real.md"))


class TestNodeWithoutId(unittest.TestCase):
    """P2 residual: a graph node with no usable `id` (missing, null, or
    unhashable) must skip-and-warn, never abort the import."""

    def test_id_less_node_skips_and_warns(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        gp = _write_graph(root, [
            {"label": "no id at all"},
            {"id": ["un", "hashable"], "label": "list id"},
            {"id": None, "label": "null id"},
            {"id": "a", "label": "Alpha", "file_type": "concept"},
        ])
        buf = io.StringIO()
        with redirect_stdout(buf):
            written, skipped = import_graphify(Bundle(root), gp)
        self.assertEqual((written, skipped), (1, 0))
        self.assertIn("skipped 3 node(s) with no usable id", buf.getvalue())
        self.assertIn("imported/alpha.md", Bundle(root).notes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
