"""Verify the bounded review of served and independently used context.

The tests enforce eight controls. Serving alone produces no evidence of use.
Learned associations cannot outrank a strong direct match. Unreinforced
associations decay. Repeatedly unused context receives one bounded reduction,
which resets after use. Each session is reviewed once, and ledger replay
reconstructs the same state. Associations, tokens, and gap imports have fixed
limits. Recorded gap paths remain repository-relative. `MUNINN_NO_REVIEW=1`
disables the ambient review.
"""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from muninn import cli, extract, review  # noqa: E402
from muninn.dynamics import Dynamics, gap_key, repository_identity  # noqa: E402
from muninn.observe import observe_event  # noqa: E402
from muninn.recall import recall  # noqa: E402
from muninn.store import Bundle  # noqa: E402

GIT = shutil.which("git")


def _session(dyn, sid, cue, served=(), used=()):
    """Simulate one hooked session: begin (with cue), pack serves notes
    (recall events), the agent really uses some (touch events)."""
    dyn.session_begin(sid, cue=cue)
    for n in served:
        dyn.touch(n, kind="recall", session=sid)
    for n in used:
        dyn.touch(n, session=sid)


class _Root(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.b = Bundle(self.root)
        for name, body in (("served-used.md", "alpha content used"),
                           ("served-only.md", "beta content ignored"),
                           ("used-only.md", "teardown appcontext correction")):
            self.b.write_note(name, {"title": name[:-3]}, body)
        self.b = Bundle(self.root)
        self.d = Dynamics(self.root)


class TestMetrics(_Root):
    def test_hits_misses_waste_from_the_ledger(self):
        _session(self.d, "s1", "flask ctx teardown lifecycle",
                 served=("served-used.md", "served-only.md"),
                 used=("served-used.md", "used-only.md"))
        m = review.review_session(self.b, self.d, "s1", build_gaps=False)
        self.assertEqual(m["hits"], ["served-used.md"])
        self.assertEqual(m["misses"], ["used-only.md"])
        self.assertEqual(m["waste"], ["served-only.md"])

    def test_g5_reviews_exactly_once_and_replays(self):
        _session(self.d, "s1", "flask ctx teardown lifecycle",
                 served=("served-only.md",), used=("used-only.md",))
        first = review.review_session(self.b, self.d, "s1", build_gaps=False)
        self.assertIsNotNone(first)
        self.assertIsNone(  # second pass: already reviewed
            review.review_session(self.b, self.d, "s1", build_gaps=False))
        self.assertEqual(self.d.serve_miss.get("served-only.md"), 1)
        os.remove(self.d.state_path)  # replay rebuilds identical state
        fresh = Dynamics(self.root)
        self.assertEqual(fresh.serve_miss.get("served-only.md"), 1)
        self.assertEqual(set(fresh.assocs), set(self.d.assocs))
        self.assertIn("s1", fresh.reviewed)

    def test_empty_session_is_not_marked(self):
        self.d.session_begin("s9", cue="nothing happened")
        self.assertIsNone(review.review_if_due(self.b, self.d))
        self.assertNotIn("s9", self.d.reviewed)


class TestAssocActuator(_Root):
    def test_miss_learns_and_next_prime_serves_it(self):
        # session 1: the situation cue shares NO vocabulary with the
        # The pack omits the corrective note, but the agent uses it independently.
        cue = "flask ctx lifecycle branch agent-a"
        _session(self.d, "s1", cue,
                 served=("served-used.md",),
                 used=("served-used.md", "used-only.md"))
        review.review_session(self.b, self.d, "s1", build_gaps=False)
        self.assertIn("used-only.md", self.d.assocs)
        # session 2, same situation: the learned association carries the
        # note into the pack's candidates
        hits = recall(self.b, self.d, cue, reactivate=False)
        paths = [n.path for n, _s, _w in hits]
        self.assertIn("used-only.md", paths)
        why = next(w for n, _s, w in hits if n.path == "used-only.md")
        self.assertIn("learned from a past miss", why)

    def test_g2_learned_lift_never_outranks_a_direct_hit(self):
        _session(self.d, "s1", "alpha content flask ctx",
                 served=(), used=("used-only.md",))
        review.review_session(self.b, self.d, "s1", build_gaps=False)
        for _ in range(6):  # even heavily reinforced ...
            self.d.assoc("used-only.md",
                         ["alpha", "content", "flask", "ctx"], w=1.0)
        hits = recall(self.b, self.d, "alpha content flask ctx",
                      reactivate=False)
        self.assertEqual(hits[0][0].path, "served-used.md")  # direct wins

    def test_g1_serving_alone_never_creates_an_assoc(self):
        _session(self.d, "s1", "flask ctx lifecycle",
                 served=("served-only.md",), used=())
        review.review_session(self.b, self.d, "s1", build_gaps=False)
        self.assertEqual(self.d.assocs, {})  # no touch, no learning

    def test_g3_unreinforced_assocs_dissolve(self):
        self.d.assoc("used-only.md", ["flask", "ctx"], w=0.3)
        for _ in range(12):
            self.d.consolidate()
        self.assertNotIn("used-only.md", self.d.assocs)

    def test_g6_caps_assocs_per_session_and_tokens(self):
        for i in range(12):
            self.b.write_note(f"m{i}.md", {"title": f"m{i}"}, "x")
        self.b = Bundle(self.root)
        _session(self.d, "s1", " ".join(f"tok{j}" for j in range(40)),
                 served=(), used=tuple(f"m{i}.md" for i in range(12)))
        review.review_session(self.b, self.d, "s1", build_gaps=False)
        self.assertLessEqual(len(self.d.assocs), review.MISS_CAP)
        for a in self.d.assocs.values():
            self.assertLessEqual(len(a["toks"]), review.ASSOC_TOKENS)


class TestWasteActuator(_Root):
    def _serve_ignore(self, sid):
        _session(self.d, sid, "flask ctx lifecycle",
                 served=("served-only.md", "served-used.md"),
                 used=("served-used.md",))
        review.review_session(self.b, self.d, sid, build_gaps=False)

    def test_g4_damp_after_n_wasted_serves_and_reset_on_use(self):
        base = recall(self.b, self.d, "beta content", reactivate=False,
                      use_dynamics=True)
        base_score = next(s for n, s, _w in base
                          if n.path == "served-only.md")
        for i in range(3):
            self._serve_ignore(f"s{i}")
        self.assertEqual(self.d.serve_miss["served-only.md"], 3)
        damped = recall(self.b, self.d, "beta content", reactivate=False)
        damped_score = next(s for n, s, _w in damped
                            if n.path == "served-only.md")
        self.assertLess(damped_score, base_score)
        self.assertGreater(damped_score, base_score * 0.7)  # one step only
        self.d.touch("served-only.md")  # real use: instantly forgiven
        self.assertNotIn("served-only.md", self.d.serve_miss)


@unittest.skipUnless(GIT, "git not installed")
class TestGapActuator(unittest.TestCase):
    def setUp(self):
        self.top = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.top, ignore_errors=True)
        self.kb = os.path.join(self.top, "kb")
        os.makedirs(self.kb)
        self.repo = os.path.join(self.top, "repo")
        os.makedirs(self.repo)
        subprocess.run(["git", "-C", self.repo, "init", "-q"], check=True)
        with open(os.path.join(self.repo, "helper.py"), "w") as fh:
            fh.write('def helper():\n    """The gap file."""\n    return 1\n')
        self.b = Bundle(self.kb)
        self.b.write_note("known.md", {"title": "Known"}, "mapped")
        self.b = Bundle(self.kb)
        self.d = Dynamics(self.kb)

    def test_previous_project_gaps_do_not_extract_the_current_project(self):
        for timestamp in (1000, 2000):
            observe_event(self.b, self.d, "touch",
                          os.path.join(self.repo, "helper.py"), now=timestamp)
        other = os.path.join(self.top, "other")
        os.makedirs(other)
        subprocess.run(["git", "-C", other, "init", "-q"], check=True)
        with open(os.path.join(other, "helper.py"), "w") as handle:
            handle.write("def unrelated(): pass\n")
        with (mock.patch.object(extract, "available", return_value=True),
              mock.patch.object(extract, "extract_file") as reader):
            self.assertEqual(review._build_gaps(self.b, self.d, other), [])
        reader.assert_not_called()

    def test_historical_unscoped_gaps_never_trigger_automatic_extraction(self):
        self.d.gap("helper.py", ts=1000)
        self.d.gap("helper.py", ts=2000)
        with (mock.patch.object(extract, "available", return_value=True),
              mock.patch.object(extract, "extract_file") as reader):
            self.assertEqual(review._build_gaps(self.b, self.d, self.repo), [])
        reader.assert_not_called()

    def test_gap_debounce_and_replay_keep_repository_identity(self):
        other = os.path.join(self.top, "other")
        os.makedirs(other)
        subprocess.run(["git", "-C", other, "init", "-q"], check=True)
        with open(os.path.join(other, "helper.py"), "w") as handle:
            handle.write("def unrelated(): pass\n")
        for repository in (self.repo, other):
            observe_event(self.b, self.d, "touch",
                          os.path.join(repository, "helper.py"), now=1000)
        expected = {gap_key("helper.py", repository_identity(repository)): 1
                    for repository in (self.repo, other)}
        self.assertEqual(self.d.gap_counts, expected)
        with open(self.d.ledger_path, encoding="utf-8") as handle:
            ledger = handle.read()
        self.assertNotIn(self.top, ledger)
        os.unlink(self.d.state_path)
        replayed = Dynamics(self.kb)
        self.assertEqual(replayed.gap_counts, expected)
        self.assertEqual(replayed.last_seen, self.d.last_seen)

    def test_gap_rejects_invalid_repository_identity_before_logging(self):
        for identity in ("invalid", "x" * 64, 42):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                self.d.gap("helper.py", repository=identity)
        self.assertFalse(os.path.exists(self.d.ledger_path))

    def test_g7_gap_paths_are_repo_relative_or_dropped(self):
        observe_event(self.b, self.d, "touch",
                      os.path.join(self.repo, "helper.py"))
        self.assertEqual(self.d.gap_counts,
                         {gap_key("helper.py", repository_identity(self.repo)): 1})
        outside = os.path.join(self.top, "loose.txt")  # not in any repo
        with open(outside, "w") as fh:
            fh.write("x")
        observe_event(self.b, self.d, "touch", outside)
        self.assertNotIn("loose.txt", self.d.gap_counts)
        for path in self.d.gap_counts:
            self.assertFalse(os.path.isabs(path))

    def test_gap_path_normalizes_equivalent_repository_paths(self):
        alias = os.path.join(self.top, "repo-alias")
        try:
            os.symlink(self.repo, alias)
        except OSError as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")
        helper = os.path.join(alias, "helper.py")

        with mock.patch("muninn.observe.git_out", return_value=self.repo):
            observe_event(self.b, self.d, "touch", helper)

        self.assertEqual(self.d.gap_counts,
                         {gap_key("helper.py", repository_identity(self.repo)): 1})

    def test_gap_path_rejects_a_symlink_outside_the_repository(self):
        outside_repo = os.path.join(self.top, "outside-repo")
        os.makedirs(outside_repo)
        subprocess.run(["git", "-C", outside_repo, "init", "-q"], check=True)
        outside = os.path.join(outside_repo, "outside.py")
        with open(outside, "w") as fh:
            fh.write("VALUE = True\n")
        linked = os.path.join(self.repo, "linked.py")
        try:
            os.symlink(outside, linked)
        except OSError as exc:
            self.skipTest(f"file symlinks unavailable: {exc}")

        observe_event(self.b, self.d, "touch", linked)

        self.assertEqual(self.d.gap_counts, {})

    @unittest.skipUnless(extract.available(), "tree-sitter not installed")
    def test_repeated_gaps_get_mapped_deterministically(self):
        for ts in (1000.0, 2000.0):  # two sightings, debounce-separated
            observe_event(self.b, self.d, "touch",
                          os.path.join(self.repo, "helper.py"), now=ts)
        self.d.session_begin("s1", cue="working on helper")
        self.d.touch("known.md", session="s1")
        cwd = os.getcwd()
        os.chdir(self.repo)
        try:
            m = review.review_session(self.b, self.d, "s1")
        finally:
            os.chdir(cwd)
        self.assertIn(gap_key("helper.py", repository_identity(self.repo)), m["built"])
        self.assertEqual(self.d.gap_counts, {})
        fresh = Bundle(self.kb)
        self.assertTrue(any(n.meta.get("resource") == "helper.py"
                            for n in fresh.notes.values()))


class TestAmbientLoop(_Root):
    def test_g8_kill_switch(self):
        _session(self.d, "s1", "flask ctx", served=("served-only.md",),
                 used=("used-only.md",))
        with mock.patch.dict(os.environ, {"MUNINN_NO_REVIEW": "1"}):
            self.assertIsNone(review.review_if_due(self.b, self.d))
        self.assertNotIn("s1", self.d.reviewed)

    def test_review_if_due_reviews_the_last_session_once(self):
        _session(self.d, "s1", "flask ctx", served=("served-only.md",),
                 used=("used-only.md",))
        m = review.review_if_due(self.b, self.d)
        self.assertEqual(m["misses"], ["used-only.md"])
        self.assertIsNone(review.review_if_due(self.b, self.d))

    def test_cli_review_reports_the_loop(self):
        _session(self.d, "s1", "flask ctx", served=("served-used.md",
                                                    "served-only.md"),
                 used=("served-used.md", "used-only.md"))
        review.review_session(self.b, self.d, "s1", build_gaps=False)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--root", self.root, "review"])
        out = buf.getvalue()
        self.assertIn("s1", out)
        self.assertIn("1 hit", out)
        self.assertIn("1 miss", out)
        self.assertIn("1 wasted", out)


if __name__ == "__main__":
    unittest.main()
