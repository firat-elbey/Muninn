"""Tests for precision-gated passive entity recall."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from muninn import cli
from muninn.dynamics import Dynamics
from muninn.review import session_metrics
from muninn.skill import render as render_skill
from muninn.store import Bundle
from muninn.volunteer import (
    eligible_entity_paths,
    volunteer_explain,
    volunteer_pack,
)


class TestVolunteer(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        bundle = Bundle(self.root)
        bundle.write_note(
            "people/mireille-coppersmith.md",
            {
                "title": "Mireille Coppersmith",
                "type": "person",
                "aliases": ["Mireille"],
            },
            "Mireille leads the launch review.",
        )
        bundle.write_note(
            "people/yuki-quincewood.md",
            {"title": "Yuki Quincewood", "type": "person"},
            "Yuki owns the deployment decision.",
        )
        bundle.write_note(
            "people/yuki-pemberlake.md",
            {"title": "Yuki Pemberlake", "type": "person"},
            "Yuki owns the finance decision.",
        )
        bundle.write_note(
            "companies/kelpforge.md",
            {"title": "Kelpforge", "type": "company"},
            "Kelpforge uses regional processing.",
        )
        bundle.write_note(
            "concepts/monday-review.md",
            {"title": "Monday Review", "type": "concept"},
            "The review occurs each Monday.",
        )
        self.bundle = Bundle(self.root)
        self.dynamics = Dynamics(self.root)

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_complete_identity_is_eligible(self):
        self.assertEqual(
            eligible_entity_paths(self.bundle, "Ask Yuki Quincewood."),
            {"people/yuki-quincewood.md"},
        )

    def test_exact_unicode_identities_are_retrieved_without_transliteration(self):
        for title, query in (("李明", "Ask 李明."),
                             ("José García", "Ask Jose\u0301 Garci\u0301a."),
                             ("東京", "東京")):
            with self.subTest(title=title):
                self.bundle.write_note("unicode.md", {
                    "title": title, "type": "person"}, "Owns the review.")
                hits = volunteer_explain(self.bundle, None, query,
                                         reactivate=False)
                self.assertEqual([hit[0].path for hit in hits], ["unicode.md"])
        self.assertEqual(eligible_entity_paths(self.bundle, "东京都"), set())

    def test_unicode_identity_ambiguity_and_short_noise_stay_silent(self):
        for path in ("first.md", "second.md"):
            self.bundle.write_note(path, {"title": "李明", "type": "person"},
                                   "Owns the review.")
        self.assertEqual(eligible_entity_paths(self.bundle, "李明"), set())
        for title in ("李", "Li"):
            self.bundle.write_note("short.md", {"title": title, "type": "person"}, "")
            self.assertEqual(eligible_entity_paths(self.bundle, title), set())

    def test_ambiguous_component_stays_silent(self):
        self.assertEqual(eligible_entity_paths(self.bundle, "Ask Yuki."), set())

    def test_approximate_surname_rejects_single_token_alias(self):
        self.assertEqual(
            eligible_entity_paths(
                self.bundle, "Did Mireille Coppersmither reply?"
            ),
            set(),
        )

    def test_non_entity_concept_stays_silent(self):
        self.assertEqual(
            eligible_entity_paths(self.bundle, "What is the Monday review?"),
            set(),
        )

    def test_unique_single_token_company_is_eligible(self):
        self.assertEqual(
            eligible_entity_paths(self.bundle, "Status on Kelpforge."),
            {"companies/kelpforge.md"},
        )

    def test_repeat_serve_is_suppressed_by_session_ledger(self):
        first = volunteer_explain(
            self.bundle,
            self.dynamics,
            "Ask Yuki Quincewood.",
            session="session-one",
        )
        served = session_metrics(
            self.dynamics.ledger_path, "session-one"
        )["served"]
        second = volunteer_explain(
            self.bundle,
            self.dynamics,
            "Ask Yuki Quincewood again.",
            session="session-one",
            served_paths=served,
        )
        self.assertEqual(
            [item[0].path for item in first], ["people/yuki-quincewood.md"]
        )
        self.assertEqual(second, [])

    def test_cli_suppresses_a_repeat_within_one_session(self):
        first = StringIO()
        with redirect_stdout(first):
            cli.main(
                [
                    "--root",
                    self.root,
                    "volunteer",
                    "Ask Yuki Quincewood.",
                    "--session",
                    "session-cli",
                ]
            )
        second = StringIO()
        with redirect_stdout(second):
            cli.main(
                [
                    "--root",
                    self.root,
                    "volunteer",
                    "Ask Yuki Quincewood again.",
                    "--session",
                    "session-cli",
                ]
            )
        self.assertIn("Yuki Quincewood", first.getvalue())
        self.assertEqual(second.getvalue(), "")

    def test_no_match_emits_no_context(self):
        self.assertEqual(
            volunteer_pack(
                self.bundle,
                self.dynamics,
                "Summarize the architecture.",
                reactivate=False,
            ),
            "",
        )

    def test_small_budgets_do_not_serve_or_suppress_an_unshown_note(self):
        for budget in (0, 1, 2, 10, 20):
            with self.subTest(budget=budget):
                session = f"small-{budget}"
                text = volunteer_pack(
                    self.bundle, self.dynamics, "Yuki Quincewood",
                    budget=budget, session=session)
                self.assertEqual(text, "")
                self.assertEqual(session_metrics(
                    self.dynamics.ledger_path, session)["served"], [])
                self.assertIn("Yuki Quincewood", volunteer_pack(
                    self.bundle, self.dynamics, "Yuki Quincewood",
                    budget=400, session=session))

    def test_passive_stdout_and_rendering_obey_the_entire_budget(self):
        for budget in (0, 1, 10, 40, 100, 400):
            with self.subTest(budget=budget):
                text = volunteer_pack(
                    self.bundle, self.dynamics,
                    "Yuki Quincewood and Kelpforge", budget=budget, k=2,
                    reactivate=False)
                self.assertLessEqual(len(text), budget * 4)
                output = StringIO()
                with redirect_stdout(output):
                    cli.main(["--root", self.root, "volunteer",
                              "Yuki Quincewood and Kelpforge", "--budget",
                              str(budget), "-k", "2", "--no-reactivate"])
                self.assertLessEqual(len(output.getvalue()), budget * 4)

    def test_dynamics_superseded_source_cannot_volunteer_related_answer(self):
        self.bundle.write_note(
            "people/ada.md",
            {"title": "Ada", "type": "person"},
            "Person.",
        )
        self.bundle.write_note(
            "companies/acme.md",
            {"title": "Acme", "type": "company"},
            "Acme hired [[Ada]].",
        )
        self.bundle.write_note(
            "companies/acme-v2.md",
            {"title": "Acme correction", "type": "company"},
            "Current record without an employment claim.",
        )
        self.dynamics.supersede("companies/acme.md", "companies/acme-v2.md")

        hits = volunteer_explain(
            self.bundle,
            self.dynamics,
            "Who works at Acme?",
            reactivate=False,
        )

        self.assertEqual(hits, [])

    def test_default_pack_returns_one_entity(self):
        text = volunteer_pack(
            self.bundle,
            self.dynamics,
            "Ask Yuki Quincewood and Kelpforge.",
            reactivate=False,
        )
        self.assertEqual(text.count("\n### "), 1)
        self.assertIn("# Volunteered memory", text)

    def test_page_cap_is_bounded(self):
        with self.assertRaises(ValueError):
            volunteer_explain(
                self.bundle,
                self.dynamics,
                "Kelpforge",
                k=4,
                reactivate=False,
            )

    def test_agent_skill_routes_messages_through_passive_recall(self):
        text = render_skill("/tmp/knowledge")
        self.assertIn(
            'muninn --root /tmp/knowledge volunteer "<current user message>"',
            text,
        )
        self.assertIn("command for broad search.", text)


if __name__ == "__main__":
    unittest.main()
