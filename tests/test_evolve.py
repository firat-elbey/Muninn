"""Verify promotion of repeated feedback into local style rules.

Compatible feedback becomes a candidate and reaches the local
`style-learned/` overlay only after the required events and sessions. The
adopted style source remains unchanged, opposite evidence can supersede a
rule, isolated observations do not become rules, replay rebuilds the same
registry, and `MUNINN_NO_EVOLVE` disables ambient promotion.
"""

import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from muninn import cli, evolve  # noqa: E402
from muninn.dynamics import Dynamics  # noqa: E402
from muninn.store import Bundle  # noqa: E402

class _Root(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.b = Bundle(self.root)
        self.d = Dynamics(self.root)

    def _fb(self, text, session, domain="writing", polarity=-1):
        self.d.feedback(text, domain=domain, polarity=polarity,
                        session=session)

    def _learned(self, domain="writing"):
        p = os.path.join(self.root, "style-learned", f"{domain}.md")
        if not os.path.exists(p):
            return ""
        with open(p, encoding="utf-8") as fh:
            return fh.read()


class TestFeedbackEvents(_Root):
    def test_feedback_lands_bounded_and_scrubbed(self):
        self.d.feedback("too formal, drop the greeting sk-"
                        + "a" * 40, domain="email", polarity=-1,
                        session="s1")
        fbs = evolve.feedback_events(self.d.ledger_path)
        self.assertEqual(len(fbs), 1)
        self.assertNotIn("sk-aaaa", fbs[0]["text"])  # secrets scrubbed
        self.assertEqual(fbs[0]["domain"], "email")

    def test_cli_feedback_verb(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--root", self.root, "feedback",
                      "user rewrote intro to be one sentence",
                      "--domain", "blog"])
        self.assertIn("feedback noted", buf.getvalue())
        self.assertEqual(
            evolve.feedback_events(self.d.ledger_path)[0]["domain"], "blog")


class TestPromotion(_Root):
    def test_implicit_feedback_does_not_guess_between_shared_hook_sessions(self):
        with mock.patch.dict(os.environ, {"MUNINN_SESSION": ""}):
            self.d.session_begin("session-a")
            self.d.session_begin("session-b")
            for timestamp in (2000, 2001, 2002):
                with mock.patch("muninn.dynamics.time.time", return_value=timestamp):
                    self.d.feedback("Use shorter sentences in release notes.")
        events = evolve.feedback_events(self.d.ledger_path)
        self.assertEqual({event["session"] for event in events}, {""})
        self.assertEqual(evolve.evolve_once(self.b, self.d)["promoted"], [])

    def test_cli_feedback_uses_explicit_session_before_environment_or_shared_state(self):
        self.d.session_begin("session-b")
        with (mock.patch.dict(os.environ, {"MUNINN_SESSION": "session-env"}),
              redirect_stdout(io.StringIO())):
            cli.main(["--root", self.root, "feedback", "Use shorter release notes.",
                      "--session", "session-a"])
        events = evolve.feedback_events(self.d.ledger_path)
        self.assertEqual(events[0]["session"], "session-a")

    def test_unknown_feedback_sessions_do_not_supply_cross_session_evidence(self):
        with mock.patch.dict(os.environ, {"MUNINN_SESSION": ""}):
            for timestamp in (2000, 2001, 2002):
                with mock.patch("muninn.dynamics.time.time", return_value=timestamp):
                    self.d.feedback("Use shorter sentences in release notes.")
        self.d.feedback("Use shorter sentences in release notes.", session="known")
        self.assertEqual(evolve.evolve_once(self.b, self.d)["promoted"], [])

    def test_feedback_does_not_reuse_a_reviewed_session(self):
        self.d.session_begin("finished")
        self.d.review_mark("finished", [], [], [])
        with mock.patch.dict(os.environ, {"MUNINN_SESSION": ""}):
            self.d.feedback("Use shorter sentences in release notes.")
        self.assertEqual(evolve.feedback_events(self.d.ledger_path)[0]["session"], "")

    def test_below_threshold_is_candidate_not_rule(self):
        self._fb("prefers shorter emails without greetings", "s1")
        self._fb("again asked for shorter email no greeting", "s2")
        r = evolve.evolve_once(self.b, self.d)
        self.assertEqual(r["promoted"], [])
        self.assertTrue(any(c["count"] == 2 for c in r["candidates"]))
        self.assertEqual(self._learned(), "")  # nothing written yet

    def test_three_events_two_sessions_promotes_and_seals(self):
        self._fb("prefers shorter emails without greetings", "s1")
        self._fb("again asked for shorter email no greeting", "s2")
        self._fb("cut the email greeting keep it shorter", "s2b")
        r = evolve.evolve_once(self.b, self.d)
        self.assertEqual(len(r["promoted"]), 1)
        text = self._learned()
        self.assertIn("shorter", text)
        self.assertIn("evidence: 3 event(s)", text)
        # idempotent: a second pass promotes nothing new
        r2 = evolve.evolve_once(self.b, self.d)
        self.assertEqual(r2["promoted"], [])
        self.assertEqual(self._learned().count("evidence:"), 1)
        # the learned file is a NOTE too: recall can serve it
        self.assertIn("style-learned/writing.md", Bundle(self.root).notes)

    def test_single_session_repetition_never_promotes(self):
        for _ in range(4):  # heat of one moment != a preference
            self._fb("make this shorter please", "s-only")
        r = evolve.evolve_once(self.b, self.d)
        self.assertEqual(r["promoted"], [])

    def test_unclustered_one_offs_never_promote(self):
        self._fb("prefers oxford commas", "s1")
        self._fb("liked the pirate joke", "s2")
        self._fb("wants tables not prose here", "s3")
        self.assertEqual(evolve.evolve_once(self.b, self.d)["promoted"], [])

    def test_contradiction_demotes_the_old_rule(self):
        for s in ("s1", "s2", "s3"):
            self._fb("prefers shorter emails no greetings", s, polarity=-1)
        evolve.evolve_once(self.b, self.d)
        self.assertIn("shorter", self._learned())
        # months later, repeated opposite evidence on the same ground
        for s in ("s7", "s8", "s9"):
            self._fb("actually wants warmer longer emails with greetings",
                     s, polarity=1)
        r = evolve.evolve_once(self.b, self.d)
        self.assertEqual(len(r["promoted"]), 1)
        text = self._learned()
        self.assertIn("superseded", text.lower())  # old rule marked, kept
        self.assertIn("warmer", text)

    def test_a_settled_reversal_never_flip_flops(self):
        # the ledger is append-only, so the OLD cluster still qualifies
        # on every future pass: without ts-gating each session-end
        # would demote and re-promote both sides forever
        for s in ("s1", "s2", "s3"):
            self._fb("prefers shorter emails no greetings", s, polarity=-1)
        evolve.evolve_once(self.b, self.d)
        for s in ("s7", "s8", "s9"):
            self._fb("actually wants warmer longer emails with greetings",
                     s, polarity=1)
        evolve.evolve_once(self.b, self.d)

        def rule_events():
            with open(self.d.ledger_path, encoding="utf-8") as fh:
                return sum(1 for ln in fh if '"kind": "rule"' in ln
                           or '"kind":"rule"' in ln)

        baseline = rule_events()
        for _ in range(3):  # three more ambient passes: dead quiet
            r = evolve.evolve_once(self.b, self.d)
            self.assertEqual((r["promoted"], r["demoted"]), ([], []))
        self.assertEqual(rule_events(), baseline)  # no ledger growth
        active = [x for x in self.d.rules.values()
                  if x.get("status") != "superseded"]
        self.assertEqual(len(active), 1)
        self.assertIn("warmer", active[0]["text"])

    def test_same_wording_reversal_keeps_the_audit_trail(self):
        # a reversal phrased in the SAME words must mint a NEW rule id :
        # otherwise promote overwrites the just-superseded entry and the
        # "marked, dated, never deleted" contract silently breaks
        for s in ("s1", "s2", "s3"):
            self._fb("short one line email subject", s, polarity=-1)
        evolve.evolve_once(self.b, self.d)
        for s in ("s7", "s8", "s9"):
            self._fb("short one line email subject", s, polarity=1)
        evolve.evolve_once(self.b, self.d)
        self.assertEqual(len(self.d.rules), 2)  # both survive
        statuses = sorted(r.get("status", "active")
                          for r in self.d.rules.values())
        self.assertEqual(statuses, ["active", "superseded"])
        self.assertIn("~~", self._learned())  # struck through, not gone

    def test_replay_rebuilds_rule_registry(self):
        for s in ("s1", "s2", "s3"):
            self._fb("prefers shorter emails no greetings", s)
        evolve.evolve_once(self.b, self.d)
        os.remove(self.d.state_path)
        fresh = Dynamics(self.root)
        self.assertEqual(set(fresh.rules), set(self.d.rules))


class TestShareability(_Root):
    def test_promoted_rule_never_changes_the_adopted_repo(self):
        style = os.path.join(self.root, "style")
        os.makedirs(style)
        source = os.path.join(style, "source.md")
        with open(source, "w", encoding="utf-8") as fh:
            fh.write("Source content.\n")
        secretish = "user said: shorter emails (ref DEAL-9931 acme corp)"
        for s in ("s1", "s2", "s3"):
            self._fb(secretish, s)
        evolve.evolve_once(self.b, self.d)
        self.assertFalse(os.path.exists(os.path.join(style, "learned")))
        with open(source, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "Source content.\n")
        self.assertIn("shorter emails", self._learned())

    def test_kill_switch(self):
        for s in ("s1", "s2", "s3"):
            self._fb("prefers shorter emails no greetings", s)
        with mock.patch.dict(os.environ, {"MUNINN_NO_EVOLVE": "1"}):
            self.assertIsNone(evolve.evolve_if_due(self.b, self.d))
        self.assertEqual(self._learned(), "")


class TestWhisper(_Root):
    def test_unconfirmed_candidates_whisper_into_prime(self):
        self._fb("prefers shorter emails without greetings", "s1")
        self._fb("again shorter email no greeting", "s2")
        out = evolve.whisper_section(self.d)
        self.assertIn("Recently observed (unconfirmed)", out)
        self.assertIn("shorter", out)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--root", self.root, "prime", "--cwd", self.root,
                      "--budget", "200"])
        self.assertIn("Recently observed", buf.getvalue())

    def test_no_feedback_no_whisper(self):
        self.assertEqual(evolve.whisper_section(self.d), "")


if __name__ == "__main__":
    unittest.main()
