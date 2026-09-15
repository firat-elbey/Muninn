"""Regression tests derived from the first internal-use report:
B1 (build unbreakable), B2 (no silent empty focus), B3 (junk loop),
C1 (stemming + stopwords), C3/C4/C5 (visibility), M3 (.gitignore),
M4 (--no-reactivate), P1 (duplicate-title focus)."""

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
from muninn.ingest import import_graphify  # noqa: E402
from muninn.recall import context_pack, estimate_tokens, recall  # noqa: E402
from muninn.store import Bundle  # noqa: E402


def _write_graph(root, nodes, links=()):
    p = os.path.join(root, "graph.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"nodes": nodes, "links": list(links)}, fh)
    return p


class TestBuildUnbreakable(unittest.TestCase):
    """B1: the flagship `build` path must survive hostile node labels."""

    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_long_labels_clamp_and_stay_distinct(self):
        # the report's crash: 385-char heading slugs > the 255-byte APFS
        # name limit; two long labels sharing a 120-char prefix must not
        # collide after the clamp
        base = "heading with an embedded url " + "u" * 300
        gp = _write_graph(self.root, [
            {"id": "n1", "label": base + " one", "file_type": "document"},
            {"id": "n2", "label": base + " two", "file_type": "document"},
        ])
        written, skipped = import_graphify(Bundle(self.root), gp)
        self.assertEqual((written, skipped), (2, 0))
        paths = [p for p in Bundle(self.root).notes if p != "graph.json"]
        imported = [p for p in paths if p.startswith("imported/")]
        self.assertEqual(len(imported), 2)
        for p in imported:
            self.assertLessEqual(len(os.path.basename(p)), 140)

    def test_oserror_skips_and_warns_never_aborts(self):
        gp = _write_graph(self.root, [
            {"id": "a", "label": "Alpha", "file_type": "concept"},
            {"id": "b", "label": "Bad apple", "file_type": "concept"},
            {"id": "c", "label": "Gamma", "file_type": "concept"},
        ])
        b = Bundle(self.root)
        orig = b.write_note

        def flaky(rel, meta, body):
            if "bad-apple" in rel:
                raise OSError(63, "File name too long")
            return orig(rel, meta, body)

        buf = io.StringIO()
        with mock.patch.object(b, "write_note", side_effect=flaky), \
                redirect_stdout(buf):
            written, skipped = import_graphify(b, gp)
        self.assertEqual((written, skipped), (2, 0))  # partial, not aborted
        out = buf.getvalue()
        self.assertIn("warning: 1 note(s) could not be written", out)
        self.assertIn("bad-apple", out)  # the example names the culprit
        b2 = Bundle(self.root)
        self.assertIn("imported/alpha.md", b2.notes)
        self.assertIn("imported/gamma.md", b2.notes)

    def test_build_scratch_goes_outside_the_scanned_repo(self):
        kb = os.path.join(self.root, "kb")
        scanned = os.path.join(self.root, "repo")
        os.makedirs(kb)
        os.makedirs(scanned)
        graph = {"nodes": [{"id": "c1", "label": "Cache Strategy",
                            "file_type": "concept"}], "links": []}
        seen_env = {}

        def fake_run(cmd, **kw):
            out = kw["env"]["GRAPHIFY_OUT"]
            seen_env["out"] = out
            os.makedirs(out, exist_ok=True)
            with open(os.path.join(out, "graph.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(graph, fh)
            return mock.Mock(returncode=0)

        buf = io.StringIO()
        with mock.patch.dict(os.environ), \
                mock.patch("muninn.cli.shutil.which",
                           return_value="/usr/bin/graphify"), \
                mock.patch("muninn.cli.subprocess.run",
                           side_effect=fake_run), \
                redirect_stdout(buf):
            os.environ.pop("GRAPHIFY_OUT", None)
            cli.main(["--root", kb, "build", scanned])
        self.assertIn("imported 1 notes", buf.getvalue())
        # scratch lives in the bundle sidecar, never in the scanned repo
        self.assertEqual(seen_env["out"],
                         os.path.join(kb, ".muninn", "graphify-out"))
        self.assertFalse(os.path.exists(
            os.path.join(scanned, "graphify-out")))
        self.assertIn("imported/cache-strategy.md", Bundle(kb).notes)

    def test_stale_scratch_graph_never_shadows_legacy_output(self):
        # review-panel finding: a previous build's sidecar graph.json must
        # not win over a fresh graph written by a legacy graphify that
        # ignores GRAPHIFY_OUT and dumps into the scanned folder
        kb = os.path.join(self.root, "kb")
        scanned = os.path.join(self.root, "repo")
        out_dir = os.path.join(kb, ".muninn", "graphify-out")
        os.makedirs(out_dir)
        os.makedirs(scanned)
        with open(os.path.join(out_dir, "graph.json"), "w",
                  encoding="utf-8") as fh:  # stale scratch from a past run
            json.dump({"nodes": [{"id": "s", "label": "Stale Thing",
                                  "file_type": "concept"}], "links": []}, fh)

        def legacy_run(cmd, **kw):  # ignores GRAPHIFY_OUT entirely
            legacy = os.path.join(scanned, "graphify-out")
            os.makedirs(legacy, exist_ok=True)
            with open(os.path.join(legacy, "graph.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({"nodes": [{"id": "f", "label": "Fresh Thing",
                                      "file_type": "concept"}],
                           "links": []}, fh)
            return mock.Mock(returncode=0)

        buf = io.StringIO()
        with mock.patch.dict(os.environ), \
                mock.patch("muninn.cli.shutil.which",
                           return_value="/usr/bin/graphify"), \
                mock.patch("muninn.cli.subprocess.run",
                           side_effect=legacy_run), \
                redirect_stdout(buf):
            os.environ.pop("GRAPHIFY_OUT", None)
            cli.main(["--root", kb, "build", scanned])
        notes = Bundle(kb).notes
        self.assertIn("imported/fresh-thing.md", notes)
        self.assertNotIn("imported/stale-thing.md", notes)
        self.assertIn("scratch into the scanned folder", buf.getvalue())


class TestJunkLoop(unittest.TestCase):
    """B3: recall reactivation must stop amplifying junk."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        b = Bundle(self.root)
        b.write_note("stub.md", {"title": "What it does not do"},
                     "junk stub words that lexically match question packs")
        b.write_note("real.md", {"title": "Walk bounds"},
                     "the walk is bounded and damped")
        self.b = Bundle(self.root)
        self.d = Dynamics(self.root)

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_six_packs_never_lift_untouched_stub_above_touched_note(self):
        # the report watched an untouched stub climb 0.68 -> 0.88 across
        # successive read-only questions; recalls now add only a small
        # bounded term (max 0.1) and can never pass a once-touched note
        self.d.touch("real.md")
        once = self.d.strength("real.md")
        climb = []
        for _ in range(6):
            context_pack(self.b, self.d, "junk stub question words",
                         budget=300, k=2)  # reactivate=True, the default
            climb.append(self.d.strength("stub.md"))
        e = self.d.entries["stub.md"]
        self.assertGreaterEqual(e["recalls"], 6)  # it WAS recalled each time
        self.assertEqual(e["recurrence"], 0)      # but never counted as use
        self.assertLessEqual(max(climb), 0.1)     # bounded by construction
        self.assertLess(max(climb), once)

    def test_outcome_after_auto_recalls_captures_only_touched_notes(self):
        self.d.touch("real.md")
        for _ in range(3):
            context_pack(self.b, self.d, "junk stub question words",
                         budget=300, k=2)
        self.d.outcome(-0.7, why="pack missed the answer")
        self.assertNotIn("stub.md", self.d.order)  # Recall does not enter the use order.
        self.assertEqual(self.d.entries["stub.md"]["captured"], 0.0)
        self.assertGreater(self.d.entries["real.md"]["captured"], 0.0)
        # and the ledger replays to the identical state (new event rules)
        snapshot = {k: dict(v) for k, v in self.d.entries.items()}
        self.d.replay()
        self.assertEqual(snapshot, self.d.entries)

    def test_no_reactivate_flag_is_fully_read_only(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--root", self.root, "pack", "junk stub",
                      "--no-reactivate"])
            cli.main(["--root", self.root, "recall", "junk stub",
                      "--no-reactivate"])
        self.assertEqual(Dynamics(self.root).entries, {})  # nothing logged
        with redirect_stdout(io.StringIO()):
            cli.main(["--root", self.root, "recall", "junk stub"])
        d = Dynamics(self.root)
        self.assertGreaterEqual(d.entries["stub.md"]["recalls"], 1)

    def test_recall_cannot_resurrect_decayed_strength(self):
        # review-panel finding: a recall must NOT snap a decayed touched
        # note back to full priority (that reopens the loop for touched
        # junk): it moves at most one +0.01 step toward earned priority
        self.d.touch("real.md", valence=1.0)
        self.d.outcome(0.9)
        for _ in range(40):
            self.d.consolidate()
        decayed = self.d.strength("real.md")
        self.assertLess(decayed, 0.6)  # decay really happened
        self.d.touch("real.md", kind="recall")  # the reactivation path
        after = self.d.strength("real.md")
        self.assertGreaterEqual(after, decayed)
        self.assertLessEqual(after, decayed + 0.0101)

    def test_legacy_corrupted_cache_heals_on_load(self):
        # a bundle whose state.json was derived under the OLD rules
        # (recalls counted as use, junk captured by outcomes) must be
        # rebuilt from the ledger on first load under the new rules
        self.d.touch("real.md")
        for _ in range(6):
            self.d.touch("stub.md", kind="recall")
        self.d.outcome(-0.7, why="pack missed the answer")
        legacy_entry = {"recurrence": 6, "affect_mag": 0.0, "captured": 0.5,
                        "pinned": False, "superseded": False,
                        "last_ts": 2.0, "strength": 1.04}
        real_entry = dict(legacy_entry, recurrence=1, strength=0.84)
        with open(self.d.state_path, "w", encoding="utf-8") as fh:
            json.dump({"entries": {"stub.md": legacy_entry,
                                   "real.md": real_entry},
                       "order": ["real.md", "stub.md"],
                       "coact": {}, "session_notes": {}, "goals": {},
                       # applied_bytes matches, so ONLY the missing rules
                       # stamp can force the rebuild
                       "applied_bytes": os.path.getsize(
                           self.d.ledger_path)}, fh)
        healed = Dynamics(self.root)
        self.assertEqual(healed.entries["stub.md"]["recurrence"], 0)
        self.assertLess(healed.strength("stub.md"), 0.15)  # dormant again
        self.assertLess(healed.strength("stub.md"),
                        healed.strength("real.md"))


class TestStemming(unittest.TestCase):
    """C1: ask in your words, still hit the note in the author's words."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        b = Bundle(self.root)
        b.write_note("dorm.md", {"title": "Dormancy, decay, corrections"},
                     "below the floor a note goes dormant; decay compounds "
                     "each consolidation; corrections demote what they fix")
        b.write_note("stub.md", {"title": "Note"},
                     "Code-level node `Note` from the source graph.")
        b.write_note("d1.md", {"title": "Note taking tips"},
                     "keep notes short")
        b.write_note("d2.md", {"title": "Release notes"},
                     "the note about the release process")
        self.b = Bundle(self.root)
        self.d = Dynamics(self.root)

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_dormant_query_finds_dormancy_note_top3(self):
        hits = recall(self.b, self.d, "what makes a note dormant", k=5,
                      reactivate=False)
        paths = [n.path for n, _s, _w in hits]
        self.assertIn("dorm.md", paths[:3])
        # the stemming half specifically: without dormant~dormancy the
        # answer sat below the title-weighted code stub (report rank 6)
        self.assertEqual(paths[0], "dorm.md")

    def test_stemmer_guards(self):
        from muninn.recall import tokens
        toks = tokens("state.json v1.2 wal-g")
        # identifier-ish tokens (digits, dots, dashes) are never stemmed :
        # the WHOLE token survives verbatim ...
        self.assertLessEqual({"state.json", "v1.2", "wal-g"}, toks)
        # ... and (field-tested on Flask) its parts are emitted alongside,
        # so prose queries can reach identifier-bearing notes
        self.assertLessEqual({"state", "json", "wal", "v1"}, toks)
        # a stem that would BECOME a stopword is rejected, not emitted
        self.assertEqual(tokens("wills"), {"wills"})

    def test_question_words_carry_no_weight(self):
        from muninn.recall import tokens
        self.assertEqual(tokens("what it does not do"), set())
        hits = recall(self.b, self.d, "how do corrections affect recall",
                      k=4, reactivate=False)
        paths = [n.path for n, _s, _w in hits]
        self.assertEqual(paths[0], "dorm.md")  # not a does/not/it stub


class TestEmptyFocusFallback(unittest.TestCase):
    """B2: a pack with zero hits must degrade visibly, not silently."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        b = Bundle(self.root)
        b.write_note("strong.md", {"title": "Backup runbook"},
                     "nightly backup steps")
        b.write_note("weak.md", {"title": "Meeting log"},
                     "we met on tuesday")
        b.write_note("old.md", {"title": "Old fact"}, "stale content")
        b.write_note("new.md", {"title": "New fact",
                                "supersedes": ["old.md"]}, "fresh content")
        self.b = Bundle(self.root)
        self.d = Dynamics(self.root)
        for _ in range(5):
            self.d.touch("strong.md")

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_no_match_line_and_strength_ranked_fallback(self):
        before = {k: dict(v) for k, v in self.d.entries.items()}
        pack = context_pack(self.b, self.d, "zzzqqq wibble", budget=500)
        self.assertIn("*(no match for zzzqqq wibble; showing the strongest "
                      "notes instead)*", pack)
        focus = pack.split("## Focus", 1)[1]
        self.assertIn("### Backup runbook (strong.md)", focus)
        self.assertNotIn("(old.md)", focus)  # superseded never falls back
        # the strongest note leads the fallback
        self.assertLess(focus.index("(strong.md)"), focus.index("(weak.md)"))
        # fallback notes were NOT recalled: state is untouched
        self.assertEqual(before, self.d.entries)
        # and the fallback respects the budget like any other pack
        self.assertLessEqual(estimate_tokens(pack), 560)  # small overshoot ok


class TestGoalsVisible(unittest.TestCase):
    """C4: goals are visible: tilt marked in why-lines, reach reported.
    (Round 2 N1 changed the mechanism: goals tilt what the query reached,
    they no longer seed unreached notes: see test_report2.)"""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        b = Bundle(self.root)
        b.write_note("onboard.md", {"title": "Onboarding guide"},
                     "how to onboard new folks to the project")
        b.write_note("port.md", {"title": "Port config"},
                     "postgres port 7433")
        self.b = Bundle(self.root)
        self.d = Dynamics(self.root)

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_goal_tilts_reached_notes_never_seeds_unreached(self):
        self.d.goal("improve the onboarding guide", 0.8)
        # a query the goal note has NOTHING to do with: the goal must not
        # place it into Focus (round-2 N1: that was a slot tax on every
        # pack). A goal adjusts a reached note and does not create a seed.
        with_goal = context_pack(self.b, self.d, "postgres port",
                                 budget=800, reactivate=False)
        focus = with_goal.split("## Focus", 1)[1]
        self.assertIn("### Port config (port.md)", focus)
        self.assertNotIn("onboard.md", focus)
        # a query that DOES reach the goal-aligned note: tilt applies and
        # retains its provenance label
        reached = context_pack(self.b, self.d, "onboarding the new folks",
                               budget=800, reactivate=False)
        focus = reached.split("## Focus", 1)[1]
        self.assertIn("### Onboarding guide (onboard.md)", focus)
        self.assertIn(
            "aligned with active goal: improve the onboarding guide",
            focus,
        )

    def test_goal_listing_reports_alignment(self):
        self.d.goal("improve the onboarding guide", 0.8)
        self.d.goal("quantum blockchain synergy", 0.5)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--root", self.root, "goal"])
        out = buf.getvalue()
        self.assertIn("improve the onboarding guide  (aligns with 1 of 2 "
                      "notes)", out)
        self.assertIn("quantum blockchain synergy  (aligns with 0 of 2 "
                      "notes)", out)


class TestSmallVisibilityFixes(unittest.TestCase):
    """C3 consolidate counts, C5 prime header, M3 init .gitignore."""

    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_consolidate_reports_tracked_and_baseline(self):
        b = Bundle(self.root)
        for i in range(3):
            b.write_note(f"n{i}.md", {"title": f"Note {i}"}, "x")
        Dynamics(self.root).touch("n0.md")
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--root", self.root, "consolidate"])
        self.assertIn("1 tracked (2 notes at baseline)", buf.getvalue())

    def test_prime_header_is_readable_and_cue_truncated(self):
        Bundle(self.root).write_note("a.md", {"title": "Alpha"}, "alpha")
        deep = os.path.join(self.root, "x" * 160)
        os.makedirs(deep)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--root", self.root, "prime", "--cwd", deep])
        lines = buf.getvalue().splitlines()
        self.assertEqual(lines[0],
                         "# Context pack: primed from the current state")
        self.assertTrue(lines[1].startswith("*cue: "))
        self.assertLessEqual(len(lines[1]), 128)
        self.assertIn("…", lines[1])

    def test_init_writes_gitignore_once(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--root", self.root, "init"])
            cli.main(["--root", self.root, "init"])  # idempotent
        gi = os.path.join(self.root, ".gitignore")
        with open(gi, encoding="utf-8") as fh:
            content = fh.read()
        self.assertEqual(content.count(".muninn/"), 1)

    def test_init_appends_gitignore_without_mangling(self):
        # review-panel finding: appending to a file with no trailing
        # newline glued ".muninn/" onto the last pattern, breaking both
        gi = os.path.join(self.root, ".gitignore")
        with open(gi, "w", encoding="utf-8") as fh:
            fh.write("node_modules")  # no trailing newline
        with redirect_stdout(io.StringIO()):
            cli.main(["--root", self.root, "init"])
        with open(gi, encoding="utf-8") as fh:
            self.assertEqual(fh.read().splitlines(),
                             ["node_modules", ".muninn/"])


class TestDuplicateTitleFocus(unittest.TestCase):
    """P1: near-identical stubs must not stack the focus."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        b = Bundle(self.root)
        for i in range(1, 4):
            b.write_note(f"junk-{i}.md", {"title": "What it does not do"},
                         "stub pack behavior words")
        b.write_note("real.md", {"title": "Pack coverage rule"},
                     "the pack skips duplicate titles")
        self.b = Bundle(self.root)
        self.d = Dynamics(self.root)

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_same_title_admitted_once(self):
        pack = context_pack(self.b, self.d, "stub pack behavior duplicate",
                            budget=800, k=3, reactivate=False)
        focus = pack.split("## Focus", 1)[1]
        self.assertEqual(focus.count("### What it does not do"), 1)
        self.assertIn("### Pack coverage rule (real.md)", focus)


if __name__ == "__main__":
    unittest.main(verbosity=2)
