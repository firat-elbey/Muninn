"""Verify session observation identity and scrubbed judgment write boundaries."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from muninn.dynamics import SENSE_KEYS, STATE_RULES, Dynamics
from muninn.observe import DEBOUNCE, observe_event
from muninn.review import review_session
from muninn.store import Bundle


class TestSessionDebounce(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        bundle = Bundle(self.root)
        bundle.write_note("shared.md", {"type": "fact", "title": "Shared fact"},
                          "Both agents independently read this fact.")
        self.bundle = Bundle(self.root)
        self.now = 1_800_000_000.0
        clock = mock.patch("time.time", new=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def observe(self, session, kind="touch"):
        return observe_event(self.bundle, Dynamics(self.root), kind,
                             "shared.md", session=session)

    def test_interleaved_sessions_debounce_only_their_own_repeats_after_reload(self):
        for kind in ("touch", "encode"):
            with self.subTest(kind=kind):
                for session in ("alpha", "beta"):
                    self.assertEqual(self.observe(session, kind), "shared.md")
                    self.now += 1
                for session in ("alpha", "beta"):
                    self.assertIsNone(self.observe(session, kind))
                    self.now += 1
        with open(Dynamics(self.root).ledger_path, encoding="utf-8") as handle:
            events = [json.loads(line) for line in handle]
        self.assertEqual([(event["kind"], event["session"]) for event in events],
                         [(kind, session) for kind in ("touch", "encode")
                          for session in ("alpha", "beta")])

    def test_each_session_has_its_own_expiry(self):
        self.assertEqual(self.observe("alpha"), "shared.md")
        self.now += 30
        self.assertEqual(self.observe("beta"), "shared.md")
        self.now += DEBOUNCE - 30
        self.assertEqual(self.observe("alpha"), "shared.md")
        self.assertIsNone(self.observe("beta"))

    def test_replay_preserves_interleaved_debounce_and_global_observation_state(self):
        dynamics = Dynamics(self.root)
        for session in ("alpha", "beta"):
            dynamics.touch("shared.md", session=session)
            self.now += 1
        before = dict(dynamics.session_last_seen)
        global_seen = dict(dynamics.last_seen)
        os.unlink(dynamics.state_path)
        replayed = Dynamics(self.root)
        self.assertEqual(replayed.session_last_seen, before)
        self.assertEqual(replayed.last_seen, global_seen)
        for session in ("alpha", "beta"):
            self.assertIsNone(self.observe(session))
        self.assertEqual(self.observe("gamma"), "shared.md")

    def test_old_cache_rebuilds_session_debounce_from_existing_events(self):
        dynamics = Dynamics(self.root)
        for session in ("alpha", "beta"):
            dynamics.touch("shared.md", session=session)
            self.now += 1
        with open(dynamics.state_path, encoding="utf-8") as handle:
            state = json.load(handle)
        state["rules"] = STATE_RULES - 1
        state.pop("session_last_seen", None)
        with open(dynamics.state_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        for session in ("alpha", "beta"):
            self.assertIsNone(self.observe(session))
        self.assertEqual(self.observe("gamma"), "shared.md")

    def test_session_debounce_is_bounded_and_preserves_recent_keys_after_reload(self):
        dynamics = Dynamics(self.root)
        for index in range(SENSE_KEYS + 1):
            dynamics.touch("shared.md", session=f"agent-{index}")
        self.assertEqual(len(dynamics.session_last_seen), SENSE_KEYS)
        reloaded = Dynamics(self.root)
        self.assertEqual(reloaded.session_last_seen, dynamics.session_last_seen)
        self.assertEqual(reloaded.last_seen, {"touch|shared.md": self.now})
        self.assertIsNone(self.observe(f"agent-{SENSE_KEYS}"))
        self.assertEqual(self.observe("agent-0"), "shared.md")

    def test_recall_does_not_suppress_the_same_sessions_independent_read(self):
        dynamics = Dynamics(self.root)
        dynamics.touch("shared.md", kind="recall", session="alpha")
        self.assertEqual(self.observe("alpha"), "shared.md")

    def test_review_counts_both_agents_reads_as_hits_after_replay(self):
        for session in ("alpha", "beta"):
            dynamics = Dynamics(self.root)
            dynamics.session_begin(session, cue="shared fact")
            dynamics.touch("shared.md", kind="recall", session=session)
            self.observe(session)
            self.now += 1
        dynamics = Dynamics(self.root)
        os.unlink(dynamics.state_path)
        for session in ("alpha", "beta"):
            metrics = review_session(self.bundle, Dynamics(self.root), session,
                                     build_gaps=False)
            self.assertEqual(metrics["hits"], ["shared.md"])
            self.assertEqual(metrics["waste"], [])


class TestJudgmentTextPrivacy(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.dynamics = Dynamics(self.root)

    def ledger(self):
        with open(self.dynamics.ledger_path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle]

    def test_free_text_is_scrubbed_before_it_enters_the_ledger(self):
        text = "Continue with password=synthetic-private-value then verify."
        expected = "Continue with [scrubbed] then verify."
        self.dynamics.outcome(0.8, why=text)
        self.dynamics.goal(text)
        self.dynamics.intent("review", goal=text)
        self.dynamics.session_begin("session-1", cue=text)
        fields = ("why", "text", "goal", "cue")
        for event, field in zip(self.ledger(), fields):
            with self.subTest(field=field):
                self.assertEqual(event[field], expected)
        with open(self.dynamics.state_path, encoding="utf-8") as handle:
            state = handle.read()
        self.assertNotIn("synthetic-private-value", state)

    def test_scrubbing_precedes_text_limits(self):
        secret = "sk-" + "a" * 32
        outcome = "x" * 185 + " " + secret
        cue = "x" * 285 + " " + secret
        self.dynamics.outcome(0.8, why=outcome)
        self.dynamics.intent("review", goal=outcome)
        self.dynamics.session_begin("session-1", cue=cue)
        for event, field in zip(self.ledger(), ("why", "goal", "cue")):
            with self.subTest(field=field):
                self.assertNotIn("sk-", event[field])
                self.assertTrue(event[field].endswith("[scrubbed]"))

    def test_goal_retirement_uses_the_same_scrubbed_identity(self):
        raw = "Complete archive migration with token=synthetic-value"
        clean = "Complete archive migration with [scrubbed]"
        self.dynamics.goal(raw)
        self.assertEqual(self.dynamics.goals, {clean: 0.7})
        self.dynamics.goal(raw, weight=0)
        self.assertEqual(self.dynamics.goals, {})
        self.assertEqual([event["text"] for event in self.ledger()], [clean, clean])
        os.unlink(self.dynamics.state_path)
        self.assertEqual(Dynamics(self.root).goals, {})

    def test_legacy_goal_replay_normalizes_identity_without_rewriting_ledger(self):
        raw = "Complete archive migration with token=historical-value"
        clean = "Complete archive migration with [scrubbed]"
        with mock.patch("muninn.journal.scrub", side_effect=lambda text: text):
            self.dynamics.goal(raw)
        with open(self.dynamics.ledger_path, "rb") as handle:
            original = handle.read()
        with open(self.dynamics.state_path, encoding="utf-8") as handle:
            state = json.load(handle)
        state["rules"] = STATE_RULES - 1
        with open(self.dynamics.state_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        migrated = Dynamics(self.root)
        self.assertEqual(migrated.goals, {clean: 0.7})
        with open(self.dynamics.ledger_path, "rb") as handle:
            self.assertEqual(handle.read(), original)
        migrated.goal(raw, weight=0)
        self.assertEqual(migrated.goals, {})
        with open(self.dynamics.ledger_path, "rb") as handle:
            self.assertTrue(handle.read().startswith(original))

    def test_legacy_goal_collisions_follow_sequential_event_semantics(self):
        first = "Complete migration with token=first-value"
        second = "Complete migration with token=second-value"
        clean = "Complete migration with [scrubbed]"
        with mock.patch("muninn.journal.scrub", side_effect=lambda text: text):
            self.dynamics.goal(first, weight=0.2)
            self.dynamics.goal(second, weight=0.9)
        os.unlink(self.dynamics.state_path)
        migrated = Dynamics(self.root)
        self.assertEqual(migrated.goals, {clean: 0.9})
        with mock.patch("muninn.journal.scrub", side_effect=lambda text: text):
            migrated.goal(first, weight=0)
        os.unlink(migrated.state_path)
        self.assertEqual(Dynamics(self.root).goals, {})

    def test_explicit_paths_and_session_or_branch_identifiers_are_preserved(self):
        path = "notes/token=literal-path.md"
        branch = "review/token=literal-branch"
        session = "token=literal-session"
        self.dynamics.outcome(0.8, note=path, why="token=private-text",
                              session=session)
        self.dynamics.intent(branch, paths=[path], goal="token=private-text")
        self.dynamics.session_begin(session, cue="token=private-text")
        encoded, outcome, intent, started = self.ledger()
        self.assertEqual(encoded["note"], path)
        self.assertEqual(encoded["session"], session)
        self.assertEqual(intent["branch"], branch)
        self.assertEqual(intent["paths"], [path])
        self.assertEqual(started["session"], session)
        self.assertEqual(outcome["why"], "[scrubbed]")
        self.assertEqual(intent["goal"], "[scrubbed]")
        self.assertEqual(started["cue"], "[scrubbed]")


if __name__ == "__main__":
    unittest.main()
