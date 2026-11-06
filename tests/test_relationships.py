"""Tests for exact typed relationship retrieval."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from muninn.activate import Cue
from muninn.recall import (
    _ranked_for,
    context_pack,
    recall_explain,
)
from muninn.relationships import (
    parse_relationship_query,
    relationship_edges,
    relationship_hits,
)
from muninn.store import Bundle
from muninn.volunteer import volunteer_explain


class TestRelationships(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        bundle = Bundle(self.root)
        bundle.write_note(
            "companies/acme.md",
            {"title": "Acme", "type": "company"},
            "Acme was founded by [Ada](/people/ada). "
            "Its seed investors include [Grace](/people/grace). "
            "[Lin](/people/lin) serves as an advisor.",
        )
        bundle.write_note(
            "people/ada.md",
            {"title": "Ada Lovelace", "type": "person", "aliases": ["Ada"]},
            "Ada founded [Acme](/companies/acme).",
        )
        bundle.write_note(
            "people/grace.md",
            {"title": "Grace Hopper", "type": "person", "aliases": ["Grace"]},
            "Grace is a seed investor in [Acme](/companies/acme).",
        )
        bundle.write_note(
            "people/lin.md",
            {"title": "Lin Chen", "type": "person", "aliases": ["Lin"]},
            "Lin advises [Acme](/companies/acme).",
        )
        bundle.write_note(
            "people/sam.md",
            {"title": "Sam Lee", "type": "person", "aliases": ["Sam"]},
            "Sam is the engineer at [Acme](/companies/acme).",
        )
        bundle.write_note(
            "meetings/review.md",
            {"title": "Launch Review", "type": "meeting"},
            "[Ada](/people/ada) and [Sam](/people/sam) attended.",
        )
        bundle.write_note(
            "notes/noise.md",
            {"title": "Acme staffing report", "type": "note"},
            "Who works at Acme appears here many times. Acme Acme Acme.",
        )
        self.bundle = Bundle(self.root)

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_edges_canonicalize_both_link_directions(self):
        edges = relationship_edges(self.bundle)
        triples = {(edge.subject, edge.object, edge.relation) for edge in edges}
        self.assertIn(("people/ada.md", "companies/acme.md", "founded"), triples)
        self.assertIn(("people/grace.md", "companies/acme.md", "invested_in"), triples)
        self.assertIn(("people/lin.md", "companies/acme.md", "advises"), triples)
        self.assertIn(("people/sam.md", "companies/acme.md", "works_at"), triples)
        self.assertIn(("people/ada.md", "meetings/review.md", "attended"), triples)

    def test_passive_advice_language_extracts_an_advising_edge(self):
        self.bundle.write_note(
            "companies/beta.md",
            {"title": "Beta", "type": "company"},
            "Beta received advice from [Lin](../people/lin).",
        )
        triples = {
            (edge.subject, edge.object, edge.relation)
            for edge in relationship_edges(self.bundle)
        }
        self.assertIn(("people/lin.md", "companies/beta.md", "advises"), triples)

    def test_advice_language_preserves_direction(self):
        cases = {
            "asked.md": "Lin asked [Acme](/companies/acme) for advice.",
            "received.md": "Zoe received advice from [Acme](/companies/acme).",
            "gave.md": "Beta gave advice to [Lin](/people/lin).",
        }
        self.bundle.write_note(
            "people/zoe.md",
            {"title": "Zoe", "type": "person"},
            cases["received.md"],
        )
        self.bundle.write_note(
            "people/lin-asked.md",
            {"title": "Lin Asked", "type": "person"},
            cases["asked.md"],
        )
        self.bundle.write_note(
            "companies/beta-gave.md",
            {"title": "Beta Gave", "type": "company"},
            cases["gave.md"],
        )
        false_edges = {
            ("people/lin-asked.md", "companies/acme.md", "advises"),
            ("people/zoe.md", "companies/acme.md", "advises"),
            ("people/lin.md", "companies/beta-gave.md", "advises"),
        }
        triples = {
            (edge.subject, edge.object, edge.relation)
            for edge in relationship_edges(self.bundle)
        }
        self.assertTrue(false_edges.isdisjoint(triples))

    def test_unbound_relation_words_do_not_create_edges(self):
        cases = {
            "companies/capital-discussion.md": (
                "Acme discussed capital allocation with [Ada](/people/ada).",
                "invested_in",
            ),
            "companies/term-sheet-review.md": (
                "Acme asked [Ada](/people/ada) to review the term sheet.",
                "invested_in",
            ),
            "companies/portfolio-review.md": (
                "Acme included [Ada](/people/ada) in its product portfolio review.",
                "invested_in",
            ),
            "companies/round-planning.md": (
                "Acme invited [Ada](/people/ada) to a seed round planning meeting.",
                "invested_in",
            ),
            "companies/guidance-citation.md": (
                "Acme cited technical guidance written by [Ada](/people/ada).",
                "advises",
            ),
        }
        for path, (body, _) in cases.items():
            self.bundle.write_note(
                path,
                {"title": path.rsplit("/", 1)[-1][:-3], "type": "company"},
                body,
            )
        self.bundle.write_note(
            "meetings/reference-only.md",
            {"title": "Reference Only", "type": "meeting"},
            "The meeting cited [Ada](/people/ada) as the report author.",
        )
        self.bundle.write_note(
            "companies/coordinated-decoy.md",
            {"title": "Coordinated Decoy", "type": "company"},
            "The company discussed capital allocation with "
            "[Ada](/people/ada) and [Grace](/people/grace).",
        )
        triples = {
            (edge.subject, edge.object, edge.relation)
            for edge in relationship_edges(self.bundle)
        }
        for path, (_, relation) in cases.items():
            with self.subTest(path=path):
                self.assertNotIn(("people/ada.md", path, relation), triples)
        self.assertNotIn(
            ("people/ada.md", "meetings/reference-only.md", "attended"),
            triples,
        )
        for person in ("people/ada.md", "people/grace.md"):
            self.assertNotIn(
                (person, "companies/coordinated-decoy.md", "invested_in"),
                triples,
            )

    def test_relation_sentences_must_name_the_source_note(self):
        self.bundle.write_note(
            "companies/foreign.md",
            {"title": "Foreign", "type": "company"},
            "An unrelated company.",
        )
        self.bundle.write_note(
            "meetings/foreign.md",
            {"title": "Foreign Meeting", "type": "meeting"},
            "An unrelated meeting.",
        )
        self.bundle.write_note(
            "people/zoe-mismatch.md",
            {"title": "Zoe Mismatch", "type": "person"},
            "Ada founded [Foreign](/companies/foreign). Grace invested in "
            "[Foreign](/companies/foreign). Lin advises "
            "[Foreign](/companies/foreign). Sam works at "
            "[Foreign](/companies/foreign). Ada attended "
            "[Foreign Meeting](/meetings/foreign).",
        )
        self.bundle.write_note(
            "companies/beta-mismatch.md",
            {"title": "Beta Mismatch", "type": "company"},
            "Acme was founded by [Ada](/people/ada). Acme received funding "
            "from [Grace](/people/grace). Acme received advice from "
            "[Lin](/people/lin). Acme employs [Sam](/people/sam).",
        )
        self.bundle.write_note(
            "companies/beta-coordinated-mismatch.md",
            {"title": "Beta Coordinated Mismatch", "type": "company"},
            "Acme was founded by [Ada](/people/ada) and was funded by "
            "[Grace](/people/grace).",
        )
        self.bundle.write_note(
            "meetings/board-mismatch.md",
            {"title": "Board Mismatch", "type": "meeting"},
            "Launch Review was attended by [Ada](/people/ada).",
        )
        triples = {
            (edge.subject, edge.object, edge.relation)
            for edge in relationship_edges(self.bundle)
        }
        for relation in (
            "founded",
            "invested_in",
            "advises",
            "works_at",
        ):
            with self.subTest(direction="person", relation=relation):
                self.assertNotIn(
                    ("people/zoe-mismatch.md", "companies/foreign.md", relation),
                    triples,
                )
        self.assertNotIn(
            (
                "people/zoe-mismatch.md",
                "meetings/foreign.md",
                "attended",
            ),
            triples,
        )
        mismatched_org_edges = {
            ("people/ada.md", "companies/beta-mismatch.md", "founded"),
            ("people/grace.md", "companies/beta-mismatch.md", "invested_in"),
            ("people/lin.md", "companies/beta-mismatch.md", "advises"),
            ("people/sam.md", "companies/beta-mismatch.md", "works_at"),
            (
                "people/grace.md",
                "companies/beta-coordinated-mismatch.md",
                "invested_in",
            ),
            ("people/ada.md", "meetings/board-mismatch.md", "attended"),
        }
        self.assertTrue(mismatched_org_edges.isdisjoint(triples))

    def test_denied_and_false_relation_claims_do_not_create_edges(self):
        self.bundle.write_note(
            "companies/denial-target.md",
            {"title": "Denial Target", "type": "company"},
            "A company used to test claim polarity.",
        )
        bodies = (
            (
                "Nora Stone denied that Nora Stone founded "
                "[Denial Target](/companies/denial-target)."
            ),
            (
                "The rumor that Nora Stone founded "
                "[Denial Target](/companies/denial-target) was false."
            ),
            (
                "Nora Stone reportedly founded "
                "[Denial Target](/companies/denial-target)."
            ),
            (
                "There is no evidence that Nora Stone founded "
                "[Denial Target](/companies/denial-target)."
            ),
            (
                "It is unclear whether Nora Stone founded "
                "[Denial Target](/companies/denial-target)."
            ),
        )
        for index, body in enumerate(bodies):
            self.bundle.write_note(
                f"people/nora-denial-{index}.md",
                {"title": "Nora Stone", "type": "person"},
                body,
            )
        triples = {
            (edge.subject, edge.object, edge.relation)
            for edge in relationship_edges(self.bundle)
        }
        self.assertFalse(
            any(
                subject.startswith("people/nora-denial-")
                and object_path == "companies/denial-target.md"
                and relation == "founded"
                for subject, object_path, relation in triples
            )
        )

    def test_not_only_preserves_an_asserted_relation(self):
        self.bundle.write_note(
            "companies/not-only-target.md",
            {"title": "Not Only Target", "type": "company"},
            "A company used to test an affirmative not-only construction.",
        )
        self.bundle.write_note(
            "people/not-only-founder.md",
            {"title": "Nora Stone", "type": "person"},
            "Nora Stone not only founded "
            "[Not Only Target](/companies/not-only-target), but also led it.",
        )
        triples = {
            (edge.subject, edge.object, edge.relation)
            for edge in relationship_edges(self.bundle)
        }
        self.assertIn(
            (
                "people/not-only-founder.md",
                "companies/not-only-target.md",
                "founded",
            ),
            triples,
        )

    def test_false_claims_are_rejected_for_every_relation(self):
        self.bundle.write_note(
            "companies/false-claim-target.md",
            {"title": "False Claim Target", "type": "company"},
            "A company used to test non-assertive relations.",
        )
        self.bundle.write_note(
            "meetings/false-claim-meeting.md",
            {"title": "False Claim Meeting", "type": "meeting"},
            "A meeting used to test non-assertive attendance.",
        )
        cases = {
            "people/false-founder.md": (
                "False Founder",
                (
                    "The allegation that False Founder founded "
                    "[False Claim Target](/companies/false-claim-target) was false."
                ),
            ),
            "people/false-investor.md": (
                "False Investor",
                (
                    "The allegation that False Investor invested in "
                    "[False Claim Target](/companies/false-claim-target) was false."
                ),
            ),
            "people/false-adviser.md": (
                "False Adviser",
                (
                    "The allegation that False Adviser advised "
                    "[False Claim Target](/companies/false-claim-target) was false."
                ),
            ),
            "people/false-worker.md": (
                "False Worker",
                (
                    "The allegation that False Worker worked at "
                    "[False Claim Target](/companies/false-claim-target) was false."
                ),
            ),
            "people/false-attendee.md": (
                "False Attendee",
                (
                    "The allegation that False Attendee attended "
                    "[False Claim Meeting](/meetings/false-claim-meeting) was false."
                ),
            ),
        }
        for path, (title, body) in cases.items():
            self.bundle.write_note(path, {"title": title, "type": "person"}, body)
        subjects = set(cases)
        self.assertFalse(
            any(edge.subject in subjects for edge in relationship_edges(self.bundle))
        )

    def test_third_person_pronouns_do_not_bind_to_the_note_subject(self):
        self.bundle.write_note(
            "companies/pronoun-target.md",
            {"title": "Pronoun Target", "type": "company"},
            "A company used to test pronoun binding.",
        )
        bodies = (
            (
                "Customers said they work at "
                "[Pronoun Target](/companies/pronoun-target)."
            ),
            (
                "Nora Stone interviewed Omar Vale. He founded "
                "[Pronoun Target](/companies/pronoun-target)."
            ),
            (
                "Omar Vale profiled Priya Shah. She works at "
                "[Pronoun Target](/companies/pronoun-target)."
            ),
        )
        for index, body in enumerate(bodies):
            self.bundle.write_note(
                f"people/pronoun-source-{index}.md",
                {"title": f"Pronoun Source {index}", "type": "person"},
                body,
            )
        triples = {
            (edge.subject, edge.object, edge.relation)
            for edge in relationship_edges(self.bundle)
        }
        self.assertFalse(
            any(
                subject.startswith("people/pronoun-source-")
                and object_path == "companies/pronoun-target.md"
                for subject, object_path, _ in triples
            )
        )

    def test_embedded_organization_and_meeting_pronouns_do_not_bind(self):
        bodies = {
            "companies/pronoun-company-founder.md": (
                "company",
                "The report said it was founded by [Ada](/people/ada).",
            ),
            "companies/pronoun-company-investor.md": (
                "company",
                "The report said it was funded by [Grace](/people/grace).",
            ),
            "companies/pronoun-company-adviser.md": (
                "company",
                "The report said it received advice from [Lin](/people/lin).",
            ),
            "companies/pronoun-company-worker.md": (
                "company",
                "The report said it employed [Sam](/people/sam).",
            ),
            "meetings/pronoun-meeting-attendee.md": (
                "meeting",
                "The minutes said it was attended by [Ada](/people/ada).",
            ),
        }
        for path, (entity_type, body) in bodies.items():
            self.bundle.write_note(
                path,
                {"title": path.rsplit("/", 1)[-1][:-3], "type": entity_type},
                body,
            )
        triples = {
            (edge.subject, edge.object, edge.relation)
            for edge in relationship_edges(self.bundle)
        }
        self.assertTrue(
            {
                (
                    "people/ada.md",
                    "companies/pronoun-company-founder.md",
                    "founded",
                ),
                (
                    "people/grace.md",
                    "companies/pronoun-company-investor.md",
                    "invested_in",
                ),
                (
                    "people/lin.md",
                    "companies/pronoun-company-adviser.md",
                    "advises",
                ),
                (
                    "people/sam.md",
                    "companies/pronoun-company-worker.md",
                    "works_at",
                ),
                (
                    "people/ada.md",
                    "meetings/pronoun-meeting-attendee.md",
                    "attended",
                ),
            }.isdisjoint(triples)
        )

    def test_implicit_source_requires_a_source_antecedent(self):
        self.bundle.write_note(
            "companies/pronoun-mismatch-company.md",
            {"title": "Pronoun Mismatch Company", "type": "company"},
            "Aurora API released a hiring update. It employed "
            "[Sam](/people/sam).",
        )
        self.bundle.write_note(
            "meetings/pronoun-mismatch-meeting.md",
            {"title": "Pronoun Mismatch Meeting", "type": "meeting"},
            "Orion Review had a satellite workshop. It was attended by "
            "[Ada](/people/ada).",
        )
        triples = {
            (edge.subject, edge.object, edge.relation)
            for edge in relationship_edges(self.bundle)
        }
        self.assertNotIn(
            (
                "people/sam.md",
                "companies/pronoun-mismatch-company.md",
                "works_at",
            ),
            triples,
        )

    def test_implicit_source_rejects_an_alternate_named_antecedent(self):
        self.bundle.write_note(
            "companies/alternate-company.md",
            {"title": "Alternate Company", "type": "company"},
            "Alternate Company acquired Aurora API. It was founded by "
            "[Ada](/people/ada).",
        )
        self.bundle.write_note(
            "meetings/alternate-meeting.md",
            {"title": "Alternate Meeting", "type": "meeting"},
            "Alternate Meeting followed Aurora Review. It was attended by "
            "[Ada](/people/ada).",
        )
        triples = {
            (edge.subject, edge.object, edge.relation)
            for edge in relationship_edges(self.bundle)
        }
        self.assertNotIn(
            ("people/ada.md", "companies/alternate-company.md", "founded"),
            triples,
        )

    def test_implicit_source_allows_non_entity_context(self):
        self.bundle.write_note(
            "companies/geographic-context.md",
            {"title": "Geographic Context", "type": "company"},
            "Geographic Context is based in New York. It employed "
            "[Sam](/people/sam).",
        )
        self.bundle.write_note(
            "meetings/company-topic-context.md",
            {"title": "Company Topic Context", "type": "meeting"},
            "Company Topic Context discussed Aurora API. It was attended by "
            "[Ada](/people/ada).",
        )
        triples = {
            (edge.subject, edge.object, edge.relation)
            for edge in relationship_edges(self.bundle)
        }
        self.assertIn(
            (
                "people/sam.md",
                "companies/geographic-context.md",
                "works_at",
            ),
            triples,
        )
        self.assertIn(
            (
                "people/ada.md",
                "meetings/company-topic-context.md",
                "attended",
            ),
            triples,
        )
        self.assertNotIn(
            ("people/ada.md", "meetings/alternate-meeting.md", "attended"),
            triples,
        )
        self.assertNotIn(
            (
                "people/ada.md",
                "meetings/pronoun-mismatch-meeting.md",
                "attended",
            ),
            triples,
        )

    def test_direct_generic_meeting_objects_bind_to_the_meeting_note(self):
        self.bundle.write_note(
            "meetings/generic-object.md",
            {"title": "Generic Object Meeting", "type": "meeting"},
            "Generic Object Meeting was held today. [Ada](/people/ada) "
            "attended the meeting. [Grace](/people/grace) also attended the "
            "session.",
        )
        triples = {
            (edge.subject, edge.object, edge.relation)
            for edge in relationship_edges(self.bundle)
        }
        self.assertTrue(
            {
                ("people/ada.md", "meetings/generic-object.md", "attended"),
                ("people/grace.md", "meetings/generic-object.md", "attended"),
            }.issubset(triples)
        )

    def test_document_reference_can_carry_a_meeting_subject(self):
        self.bundle.write_note(
            "meetings/document-reference.md",
            {"title": "Document Reference Meeting", "type": "meeting"},
            "Document Reference Meeting was held today. The page identifies "
            "the topic. It records attendance by [Ada](/people/ada) and "
            "[Grace](/people/grace).",
        )
        triples = {
            (edge.subject, edge.object, edge.relation)
            for edge in relationship_edges(self.bundle)
        }
        self.assertTrue(
            {
                ("people/ada.md", "meetings/document-reference.md", "attended"),
                (
                    "people/grace.md",
                    "meetings/document-reference.md",
                    "attended",
                ),
            }.issubset(triples)
        )

    def test_established_first_name_can_name_the_person_subject(self):
        self.bundle.write_note(
            "companies/first-name-target.md",
            {"title": "First Name Target", "type": "company"},
            "A company used to test a shortened subject name.",
        )
        self.bundle.write_note(
            "people/wendy-brown-short.md",
            {"title": "Wendy Brown", "type": "person"},
            "Wendy Brown is a partner. Her profile records one investment. "
            "Wendy invested in [First Name Target](/companies/first-name-target).",
        )
        triples = {
            (edge.subject, edge.object, edge.relation)
            for edge in relationship_edges(self.bundle)
        }
        self.assertIn(
            (
                "people/wendy-brown-short.md",
                "companies/first-name-target.md",
                "invested_in",
            ),
            triples,
        )

    def test_first_name_shorthand_rejects_an_intervening_namesake(self):
        self.bundle.write_note(
            "companies/namesake-target.md",
            {"title": "Namesake Target", "type": "company"},
            "A company used to test an ambiguous first name.",
        )
        self.bundle.write_note(
            "people/wendy-brown-ambiguous.md",
            {"title": "Wendy Brown", "type": "person"},
            "Wendy Brown profiled Wendy Singh. Wendy invested in "
            "[Namesake Target](/companies/namesake-target).",
        )
        triples = {
            (edge.subject, edge.object, edge.relation)
            for edge in relationship_edges(self.bundle)
        }
        self.assertNotIn(
            (
                "people/wendy-brown-ambiguous.md",
                "companies/namesake-target.md",
                "invested_in",
            ),
            triples,
        )

    def test_possessive_targets_do_not_create_exact_edges(self):
        self.bundle.write_note(
            "companies/possessive-target.md",
            {"title": "Possessive Target", "type": "company"},
            "A company used to test possessive target boundaries.",
        )
        self.bundle.write_note(
            "meetings/possessive-meeting.md",
            {"title": "Possessive Meeting", "type": "meeting"},
            "A meeting used to test possessive target boundaries.",
        )
        cases = {
            "people/possessive-founder.md": (
                "Possessive Founder",
                "founded",
                (
                    "Possessive Founder founded "
                    "[Possessive Target](/companies/possessive-target)'s club."
                ),
                "companies/possessive-target.md",
            ),
            "people/possessive-investor.md": (
                "Possessive Investor",
                "invested_in",
                (
                    "Possessive Investor invested in "
                    "[Possessive Target](/companies/possessive-target)'s program."
                ),
                "companies/possessive-target.md",
            ),
            "people/possessive-adviser.md": (
                "Possessive Adviser",
                "advises",
                (
                    "Possessive Adviser advised "
                    "[Possessive Target](/companies/possessive-target)'s founder."
                ),
                "companies/possessive-target.md",
            ),
            "people/possessive-worker.md": (
                "Possessive Worker",
                "works_at",
                (
                    "Possessive Worker worked at "
                    "[Possessive Target](/companies/possessive-target)'s booth."
                ),
                "companies/possessive-target.md",
            ),
            "people/possessive-attendee.md": (
                "Possessive Attendee",
                "attended",
                (
                    "Possessive Attendee attended "
                    "[Possessive Meeting](/meetings/possessive-meeting)'s call."
                ),
                "meetings/possessive-meeting.md",
            ),
        }
        for path, (title, _, body, _) in cases.items():
            self.bundle.write_note(path, {"title": title, "type": "person"}, body)
        triples = {
            (edge.subject, edge.object, edge.relation)
            for edge in relationship_edges(self.bundle)
        }
        for path, (_, relation, _, target) in cases.items():
            with self.subTest(path=path):
                self.assertNotIn((path, target, relation), triples)

    def test_coordinated_gapped_predicates_keep_the_source(self):
        self.bundle.write_note(
            "companies/gapped-passive.md",
            {"title": "Gapped Passive", "type": "company"},
            "Gapped Passive was founded by [Ada](/people/ada), funded by "
            "[Grace](/people/grace), advised by [Lin](/people/lin), and "
            "employed [Sam](/people/sam).",
        )
        triples = {
            (edge.subject, edge.object, edge.relation)
            for edge in relationship_edges(self.bundle)
        }
        expected = {
            ("people/ada.md", "companies/gapped-passive.md", "founded"),
            ("people/grace.md", "companies/gapped-passive.md", "invested_in"),
            ("people/lin.md", "companies/gapped-passive.md", "advises"),
            ("people/sam.md", "companies/gapped-passive.md", "works_at"),
        }
        self.assertTrue(expected.issubset(triples))

    def test_unrelated_coordinated_segment_stops_relation_inheritance(self):
        for slug in ("amber", "beryl", "cobalt"):
            self.bundle.write_note(
                f"companies/{slug}.md",
                {"title": f"{slug.title()} Quay", "type": "company"},
                "A company used to test coordination boundaries.",
            )
        self.bundle.write_note(
            "people/coordinated-barrier.md",
            {"title": "Rhea Solari", "type": "person"},
            "Rhea Solari founded [Amber Quay](/companies/amber), compared "
            "[Beryl Quay](/companies/beryl), and "
            "[Cobalt Quay](/companies/cobalt).",
        )
        triples = {
            (edge.subject, edge.object, edge.relation)
            for edge in relationship_edges(self.bundle)
        }
        self.assertIn(
            (
                "people/coordinated-barrier.md",
                "companies/amber.md",
                "founded",
            ),
            triples,
        )
        self.assertNotIn(
            (
                "people/coordinated-barrier.md",
                "companies/cobalt.md",
                "founded",
            ),
            triples,
        )

    def test_passive_canonical_forms_bind_both_endpoints(self):
        self.bundle.write_note(
            "companies/passive.md",
            {"title": "Passive Company", "type": "company"},
            "Passive Company was founded by [Ada](/people/ada). "
            "It employed [Sam](/people/sam). It received investment from "
            "[Grace](/people/grace). It received advice from "
            "[Lin](/people/lin).",
        )
        self.bundle.write_note(
            "meetings/passive.md",
            {"title": "Passive Meeting", "type": "meeting"},
            "Passive Meeting was attended by [Ada](/people/ada).",
        )
        triples = {
            (edge.subject, edge.object, edge.relation)
            for edge in relationship_edges(self.bundle)
        }
        expected = {
            ("people/ada.md", "companies/passive.md", "founded"),
            ("people/sam.md", "companies/passive.md", "works_at"),
            ("people/grace.md", "companies/passive.md", "invested_in"),
            ("people/lin.md", "companies/passive.md", "advises"),
            ("people/ada.md", "meetings/passive.md", "attended"),
        }
        self.assertTrue(expected.issubset(triples))

    def test_common_passive_and_joined_forms_create_edges(self):
        self.bundle.write_note(
            "companies/common-forms.md",
            {"title": "Common Forms", "type": "company"},
            "Common Forms was established by [Ada](/people/ada).",
        )
        self.bundle.write_note(
            "companies/company-suffix.md",
            {"title": "Osprey Inc.", "type": "company"},
            "Osprey Inc. was established by [Ada](/people/ada). It employed "
            "[Sam](/people/sam).",
        )
        self.bundle.write_note(
            "companies/passive-pronoun-target.md",
            {"title": "Passive Pronoun Target", "type": "company"},
            "A company used to test a person-page object pronoun.",
        )
        self.bundle.write_note(
            "people/passive-pronoun.md",
            {"title": "Nora Stone", "type": "person"},
            "[Passive Pronoun Target](/companies/passive-pronoun-target) was "
            "founded by her.",
        )
        self.bundle.write_note(
            "people/joined-employee.md",
            {"title": "Nora Stone", "type": "person"},
            "Nora Stone joined [Common Forms](/companies/common-forms) as an "
            "employee.",
        )
        triples = {
            (edge.subject, edge.object, edge.relation)
            for edge in relationship_edges(self.bundle)
        }
        self.assertTrue(
            {
                ("people/ada.md", "companies/common-forms.md", "founded"),
                ("people/ada.md", "companies/company-suffix.md", "founded"),
                ("people/sam.md", "companies/company-suffix.md", "works_at"),
                (
                    "people/passive-pronoun.md",
                    "companies/passive-pronoun-target.md",
                    "founded",
                ),
                (
                    "people/joined-employee.md",
                    "companies/common-forms.md",
                    "works_at",
                ),
            }.issubset(triples)
        )

    def test_meeting_attendance_lists_bind_every_link(self):
        self.bundle.write_note(
            "meetings/attendance-by.md",
            {"title": "Attendance By", "type": "meeting"},
            "The record reports attendance by [Ada](/people/ada), "
            "[Sam](/people/sam), and [Grace](/people/grace).",
        )
        self.bundle.write_note(
            "meetings/listed-attendees.md",
            {"title": "Listed Attendees", "type": "meeting"},
            "It lists [Ada](/people/ada), [Sam](/people/sam), and "
            "[Grace](/people/grace) as attendees.",
        )
        self.bundle.write_note(
            "meetings/stated-attendees.md",
            {"title": "Stated Attendees", "type": "meeting"},
            "It states that [Ada](/people/ada), [Sam](/people/sam), and "
            "[Grace](/people/grace) attended. [Lin](/people/lin) also "
            "attended. [Ada](/people/ada) was another attendee.",
        )
        triples = {
            (edge.subject, edge.object, edge.relation)
            for edge in relationship_edges(self.bundle)
        }
        for meeting in (
            "meetings/attendance-by.md",
            "meetings/listed-attendees.md",
            "meetings/stated-attendees.md",
        ):
            for person in (
                "people/ada.md",
                "people/sam.md",
                "people/grace.md",
            ):
                with self.subTest(meeting=meeting, person=person):
                    self.assertIn((person, meeting, "attended"), triples)
        self.assertIn(
            ("people/lin.md", "meetings/stated-attendees.md", "attended"),
            triples,
        )

    def test_person_page_passive_forms_name_the_source(self):
        endpoints = (
            ("companies/source-founded.md", "Source Founded", "company"),
            ("companies/source-work.md", "Source Work", "company"),
            ("companies/source-invested.md", "Source Invested", "company"),
            ("companies/source-advised.md", "Source Advised", "company"),
            ("meetings/source-attended.md", "Source Attended", "meeting"),
        )
        for path, title, entity_type in endpoints:
            self.bundle.write_note(
                path,
                {"title": title, "type": entity_type},
                "An endpoint note.",
            )
        people = (
            (
                "people/pat-founder.md",
                "Pat Founder",
                "[Source Founded](/companies/source-founded) was founded by Pat Founder.",
                "companies/source-founded.md",
                "founded",
            ),
            (
                "people/pat-worker.md",
                "Pat Worker",
                "[Source Work](/companies/source-work) employed Pat Worker.",
                "companies/source-work.md",
                "works_at",
            ),
            (
                "people/pat-investor.md",
                "Pat Investor",
                (
                    "[Source Invested](/companies/source-invested) received "
                    "investment from Pat Investor."
                ),
                "companies/source-invested.md",
                "invested_in",
            ),
            (
                "people/pat-advisor.md",
                "Pat Advisor",
                (
                    "[Source Advised](/companies/source-advised) received advice "
                    "from Pat Advisor."
                ),
                "companies/source-advised.md",
                "advises",
            ),
            (
                "people/pat-attendee.md",
                "Pat Attendee",
                (
                    "[Source Attended](/meetings/source-attended) was attended by "
                    "Pat Attendee."
                ),
                "meetings/source-attended.md",
                "attended",
            ),
        )
        for path, title, body, _, _ in people:
            self.bundle.write_note(
                path,
                {"title": title, "type": "person"},
                body,
            )
        triples = {
            (edge.subject, edge.object, edge.relation)
            for edge in relationship_edges(self.bundle)
        }
        for path, _, _, target, relation in people:
            with self.subTest(path=path):
                self.assertIn((path, target, relation), triples)

    def test_mixed_relation_sentence_classifies_each_link_locally(self):
        self.bundle.write_note(
            "companies/mixed.md",
            {"title": "Mixed", "type": "company"},
            "Mixed was founded by [Ada](/people/ada) and employs "
            "[Sam](/people/sam). It received advice from "
            "[Lin](/people/lin) and was funded by [Grace](/people/grace).",
        )
        triples = {
            (edge.subject, edge.object, edge.relation)
            for edge in relationship_edges(self.bundle)
        }
        expected = {
            ("people/ada.md", "companies/mixed.md", "founded"),
            ("people/sam.md", "companies/mixed.md", "works_at"),
            ("people/lin.md", "companies/mixed.md", "advises"),
            ("people/grace.md", "companies/mixed.md", "invested_in"),
        }
        self.assertTrue(expected.issubset(triples))
        self.assertNotIn(
            ("people/sam.md", "companies/mixed.md", "founded"),
            triples,
        )

    def test_relative_link_dots_do_not_split_relation_sentences(self):
        self.bundle.write_note(
            "meetings/relative-links.md",
            {"title": "Relative Links", "type": "meeting"},
            "Relative Links' participants include [Ada](../people/ada) and "
            "[Grace](../people/grace).",
        )
        triples = {
            (edge.subject, edge.object, edge.relation)
            for edge in relationship_edges(self.bundle)
        }
        self.assertTrue(
            {
                ("people/ada.md", "meetings/relative-links.md", "attended"),
                ("people/grace.md", "meetings/relative-links.md", "attended"),
            }.issubset(triples)
        )

    def test_coordinated_link_list_inherits_the_nearest_relation(self):
        self.bundle.write_note(
            "companies/list.md",
            {"title": "List Company", "type": "company"},
            "List Company was founded by [Ada](/people/ada) and "
            "[Grace](/people/grace).",
        )
        triples = {
            (edge.subject, edge.object, edge.relation)
            for edge in relationship_edges(self.bundle)
        }
        self.assertIn(
            ("people/ada.md", "companies/list.md", "founded"),
            triples,
        )
        self.assertIn(
            ("people/grace.md", "companies/list.md", "founded"),
            triples,
        )

    def test_coordinated_employment_and_passive_attendance_lists_are_complete(self):
        self.bundle.write_note(
            "companies/coordinated-work.md",
            {"title": "Coordinated Work", "type": "company"},
            "Coordinated Work employs [Sam](/people/sam), "
            "[Ada](/people/ada), and [Grace](/people/grace).",
        )
        self.bundle.write_note(
            "companies/coordinated-hires.md",
            {"title": "Coordinated Hires", "type": "company"},
            "Coordinated Hires hired [Sam](/people/sam), [Ada](/people/ada), and "
            "[Grace](/people/grace).",
        )
        self.bundle.write_note(
            "meetings/coordinated-review.md",
            {"title": "Coordinated Review", "type": "meeting"},
            "Coordinated Review was attended by [Sam](/people/sam), "
            "[Ada](/people/ada), and [Grace](/people/grace).",
        )
        triples = {
            (edge.subject, edge.object, edge.relation)
            for edge in relationship_edges(self.bundle)
        }
        for person in ("people/sam.md", "people/ada.md", "people/grace.md"):
            with self.subTest(person=person, relation="works_at"):
                self.assertIn(
                    (person, "companies/coordinated-work.md", "works_at"),
                    triples,
                )
                self.assertIn(
                    (person, "companies/coordinated-hires.md", "works_at"),
                    triples,
                )
            with self.subTest(person=person, relation="attended"):
                self.assertIn(
                    (person, "meetings/coordinated-review.md", "attended"),
                    triples,
                )

    def test_exact_seed_resolution_is_bounded(self):
        request = parse_relationship_query(self.bundle, "Who founded Acme?")
        self.assertIsNotNone(request)
        self.assertEqual(request.seed, "companies/acme.md")
        self.assertIsNone(parse_relationship_query(self.bundle, "Who supports Acme?"))
        self.assertIsNone(parse_relationship_query(self.bundle, "Who founded Acm?"))
        self.assertIsNone(
            parse_relationship_query(
                self.bundle, "Who invested time in learning about Acme?"
            )
        )

    def test_relation_intent_accepts_independent_word_order(self):
        cases = {
            "Identify the participants present at Launch Review.": "attended",
            "Which people supplied financing to Acme?": "invested_in",
            "Name the members of Acme's advisory board.": "advises",
            "List the staff employed by Acme.": "works_at",
            "Which people established Acme?": "founded",
            "Who worked at or founded Acme?": "works_at",
        }
        for cue, expected in cases.items():
            with self.subTest(cue=cue):
                request = parse_relationship_query(self.bundle, cue)
                self.assertIsNotNone(request)
                self.assertIn(expected, request.edge_types)
                expected_seed = (
                    "companies/acme.md" if "Acme" in cue else "meetings/review.md"
                )
                self.assertEqual(request.seed, expected_seed)

    def test_relation_intent_accepts_compositional_plain_language(self):
        cases = {
            "Which people held jobs at or created Acme?": {
                "works_at",
                "founded",
            },
            "Which people were on the payroll of or launched Acme?": {
                "works_at",
                "founded",
            },
            "Which people purchased an ownership stake in Acme?": {"invested_in"},
            "Which people made an equity contribution to Acme?": {"invested_in"},
            "Which people offered expert direction to Acme?": {"advises"},
            "Which people gave strategic recommendations to Acme?": {"advises"},
            "Which people participated in Launch Review?": {"attended"},
            "Which people appeared at Launch Review?": {"attended"},
        }
        for cue, expected in cases.items():
            with self.subTest(cue=cue):
                request = parse_relationship_query(self.bundle, cue)
                self.assertIsNotNone(request)
                self.assertEqual(request.edge_types, frozenset(expected))

    def test_relation_intent_accepts_additional_natural_forms(self):
        cases = {
            "Who is the founder of Acme?": {"founded"},
            "Who are the founders of Acme?": {"founded"},
            "Who are Acme's founders?": {"founded"},
            "Who are Acme founders?": {"founded"},
            "Who is an investor in Acme?": {"invested_in"},
            "Who are the investors in Acme?": {"invested_in"},
            "Who are Acme's investors?": {"invested_in"},
            "Who are Acme investors?": {"invested_in"},
            "Who financed Acme?": {"invested_in"},
            "Who wrote a check to Acme?": {"invested_in"},
            "Who backs Acme?": {"invested_in"},
            "Who took a financial interest in Acme?": {"invested_in"},
            "Who is an advisor to Acme?": {"advises"},
            "Who are the advisors to Acme?": {"advises"},
            "Who are Acme advisors?": {"advises"},
            "Who serves as an adviser to Acme?": {"advises"},
            "Who acts in an advisory capacity to Acme?": {"advises"},
            "Who provides advisory services to Acme?": {"advises"},
            "Who provides guidance to Acme?": {"advises"},
            "Who consulted for Acme?": {"advises"},
            "Who is an employee at Acme?": {"works_at"},
            "Who are the employees at Acme?": {"works_at"},
            "Who are Acme employees?": {"works_at"},
            "Who is on the team at Acme?": {"works_at"},
            "Who has been employed by Acme?": {"works_at"},
            "Who is a participant at Launch Review?": {"attended"},
            "Who are the participants at the Launch Review?": {"attended"},
            "Who are Launch Review attendees?": {"attended"},
            "Who dropped by Launch Review?": {"attended"},
            "Which people are the founders of Acme?": {"founded"},
            "List the people who are the investors in Acme.": {"invested_in"},
            "List the people who founded Acme.": {"founded"},
            "List the people that founded Acme.": {"founded"},
            "Show me the individuals who invested in Acme.": {"invested_in"},
            "Who invests in Acme?": {"invested_in"},
            "Who put money into Acme?": {"invested_in"},
            "Who took an equity stake in Acme?": {"invested_in"},
            "Who contributed capital as investors in Acme?": {"invested_in"},
            "Who provided funding as investors in Acme?": {"invested_in"},
            "Who is advising Acme?": {"advises"},
            "Who are advisers to Acme?": {"advises"},
            "Who acted in an advisory capacity to Acme?": {"advises"},
            "Who provided advisory services to Acme?": {"advises"},
            "Who offered expert guidance to Acme?": {"advises"},
            "Who served as employees or founders of Acme?": {
                "founded",
                "works_at",
            },
            "Who worked at Acme or founded Acme?": {"founded", "works_at"},
            "Who founded Acme or worked at Acme?": {"founded", "works_at"},
            "Who started or worked for Acme?": {"founded", "works_at"},
            "Who set Acme up?": {"founded"},
            "Who got Acme started?": {"founded"},
            "From whom did Acme get funding?": {"invested_in"},
            "Who helped fund Acme?": {"invested_in"},
            "Who bankrolled the company Acme?": {"invested_in"},
            "From whom did Acme get advice?": {"advises"},
            "Who did Acme consult?": {"advises"},
            "Who consulted with the company Acme?": {"advises"},
            "Who has advised Acme over time?": {"advises"},
            "Who used to work at Acme?": {"works_at"},
            "Who did Acme hire?": {"works_at"},
            "Who stopped by Launch Review?": {"attended"},
            "By whom was Launch Review attended?": {"attended"},
            "Who worked as staff members for or established Acme?": {
                "founded",
                "works_at",
            },
            "Who were members of the workforce or founders of Acme?": {
                "founded",
                "works_at",
            },
            "Who had employment roles at or created Acme?": {
                "founded",
                "works_at",
            },
        }
        for cue, expected in cases.items():
            with self.subTest(cue=cue):
                request = parse_relationship_query(self.bundle, cue)
                self.assertIsNotNone(request)
                self.assertEqual(request.edge_types, frozenset(expected))

    def test_conversational_framing_preserves_one_bounded_question(self):
        cases = {
            "Quick question: who founded Acme?": {"founded"},
            "Do you remember who founded Acme?": {"founded"},
            "Can you tell me who invested in Acme?": {"invested_in"},
            "Can you list the people who founded Acme?": {"founded"},
            "Please list the people who invested in Acme.": {"invested_in"},
            "Can you remind me who advised Acme?": {"advises"},
            "Do you recall who was at Launch Review?": {"attended"},
            "Who founded Acme, again?": {"founded"},
            "Who attended Launch Review? I forget.": {"attended"},
        }
        for cue, expected in cases.items():
            with self.subTest(cue=cue):
                request = parse_relationship_query(self.bundle, cue)
                self.assertIsNotNone(request)
                self.assertEqual(request.edge_types, frozenset(expected))

    def test_broad_development_paraphrases_preserve_relation_semantics(self):
        cases = {
            "works_at": (
                "Who has worked for Acme?",
                "Who had a role at Acme?",
                "Who was part of Acme's team?",
                "Who belonged to Acme's staff?",
                "Who worked on Acme's team?",
                "Who served on the staff at Acme?",
                "Who held a position at Acme?",
                "Who was on Acme's payroll?",
                "Who drew a salary from Acme?",
                "Who joined Acme's team?",
                "Who did Acme employ?",
                "Who was employed at Acme?",
                "Which people made up Acme's staff?",
                "Who did Acme have on its payroll?",
                "Who served as an employee of Acme?",
                "Who occupied roles at Acme?",
                "Who held jobs with Acme?",
                "Who was a member of Acme's team?",
                "Which people comprised Acme's workforce?",
                "Who worked under Acme?",
            ),
            "founded": (
                "Who incorporated Acme?",
                "Who began Acme?",
                "Who co-created Acme?",
                "Who was behind the creation of Acme?",
                "By whom was Acme founded?",
                "Who did Acme have as its founder?",
                "Who founded the startup Acme?",
                "Who established the company Acme?",
                "Who brought Acme into being?",
                "Who was Acme founded by?",
                "Acme was founded by whom?",
                "Who can be credited with founding Acme?",
                "Who started the business Acme?",
                "Who was the founder behind Acme?",
                "Which people launched Acme?",
                "Who created Acme from the outset?",
                "Who established Acme as a company?",
                "Who originally founded Acme?",
            ),
            "invested_in": (
                "Who gave Acme its seed funding?",
                "Who led Acme's seed round?",
                "Who wrote Acme a check?",
                "Who was an early backer of Acme?",
                "Who furnished capital to Acme?",
                "Who supplied Acme with capital?",
                "Who financed the company Acme?",
                "Who invested in the company Acme?",
                "Who injected capital into Acme?",
                "Who participated in the funding of Acme?",
                "Who did Acme raise funding from?",
                "From whom did Acme raise capital?",
                "Who supplied the money behind Acme?",
                "Who was among Acme's investors?",
                "Who put capital behind Acme?",
                "Who funded the startup Acme?",
                "Who backed the company Acme?",
                "Who wrote the first check for Acme?",
                "Which people took stakes in Acme?",
                "Who led Acme seed round?",
                "Who led the seed round for Acme?",
                "Who led the Series A for Acme?",
                "Who committed an equity contribution to Acme?",
                "Who participated in Acme's funding round?",
                "Who participated in Acme's seed round?",
                "Who participated in Acme's investment round?",
                "Who participated in Acme's financing round?",
            ),
            "advises": (
                "Who advised the company Acme?",
                "Who was retained as an adviser by Acme?",
                "Who consults with Acme?",
                "Who coached Acme?",
                "Who was a sounding board for Acme?",
                "Who has advised Acme?",
                "Who sat on Acme's advisory board?",
                "Who gave Acme strategic advice?",
                "Who provided counsel for Acme?",
                "Who offered recommendations to Acme?",
                "Who was Acme advised by?",
                "Who did Acme receive advice from?",
                "From whom did Acme receive guidance?",
                "Who served Acme in an advisory role?",
                "Who was on the advisory board of Acme?",
                "Which people acted as consultants for Acme?",
                "Who guided Acme?",
                "Who acted as a mentor to Acme?",
            ),
            "attended": (
                "Who sat in on Launch Review?",
                "Who was there at Launch Review?",
                "Who joined the Launch Review?",
                "Who took part in the Launch Review?",
                "Who was present at the Launch Review?",
                "Who participated in the Launch Review?",
                "Who showed up at Launch Review?",
                "Who appeared for Launch Review?",
                "Who attended the meeting Launch Review?",
                "Who was in the room during Launch Review?",
                "Who did Launch Review include as attendees?",
                "Who was among the attendees at Launch Review?",
                "Which people were present during Launch Review?",
                "Who joined in at Launch Review?",
                "Who participated in the event Launch Review?",
                "Who was counted as present at Launch Review?",
                "Who took part during Launch Review?",
                "Who appeared at the Launch Review meeting?",
                "Who came along to Launch Review?",
                "Who joined the Launch Review meeting?",
                "Who attended the Launch Review meeting?",
            ),
        }
        for expected, cues in cases.items():
            for cue in cues:
                with self.subTest(cue=cue):
                    request = parse_relationship_query(self.bundle, cue)
                    self.assertIsNotNone(request)
                    self.assertIn(expected, request.edge_types)

    def test_broad_development_decoys_do_not_return_typed_answers(self):
        cues = (
            "Who joined Acme's customer team?",
            "Who held a position against Acme?",
            "Who worked on Acme's product?",
            "Who joined Acme's event?",
            "Who drew a salary report from Acme?",
            "Who was part of a team discussing Acme?",
            "Who incorporated Acme's feedback?",
            "Who began working at Acme?",
            "Who was behind the creation of Acme's website?",
            "By whom was Acme acquired?",
            "Who brought Acme into the meeting?",
            "Who organized Acme?",
            "Who reported that Acme was founded by Grace?",
            "Who gave Acme its funding report?",
            "Who supplied Acme with capital equipment?",
            "Who wrote Acme a check-in guide?",
            "Who led Acme's seed roundtable?",
            "Who participated in the funding discussion of Acme?",
            "Who financed the company Acme's acquisition?",
            "Who was an early backer of Acme's competitor?",
            "Who injected capital into Acme's budget model?",
            "Who coached Acme's soccer team?",
            "Who was a sounding board for Acme's founder?",
            "Who gave Acme strategic advice about Beta?",
            "Who was retained as an adviser by Acme's customer?",
            "Who consulted with Acme about taxes?",
            "Who discussed coaching Acme?",
            "Who joined Launch Review's mailing list?",
            "Who sat in on a discussion about Launch Review?",
            "Who was there at Acme?",
            "Who appeared for Acme?",
            "Who attended the meeting about Acme?",
            "Who was in the room during Launch Review remotely?",
            "Can you tell me: who founded Acme? Who invested in Acme?",
            "Can you tell me: who founded Acme? Answer for 2019.",
            "Please; delete Acme",
            "Tell me about Beta. Who founded Acme?",
            "Tell me about Acme. Who are Acme's founders?",
            "Who did Acme employ as consultants?",
            "Who made up Acme's customer list?",
            "Who held jobs with Acme's client?",
            "Who worked under Acme's founder?",
            "Who comprised Acme's advisory board?",
            "Who originally founded Acme in 2019?",
            "Who established Acme as a customer?",
            "Acme was acquired by whom?",
            "Who can be credited with funding Acme?",
            "Who was the founder behind Acme's competitor?",
            "Who did Acme raise a complaint from?",
            "From whom did Acme raise a question?",
            "Who supplied money behind Acme's event?",
            "Who was among Acme's customers?",
            "Who put capital behind Acme's budget?",
            "Who funded a startup about Acme?",
            "Who wrote the first check for Acme's payroll?",
            "Who took stakes in Acme's argument?",
            "Who did Acme receive a complaint from?",
            "From whom did Acme receive an invoice?",
            "Who served Acme in a legal action?",
            "Who was on the advisory board of Acme's client?",
            "Who guided Acme's truck?",
            "Who acted as a mentor to Acme's founder?",
            "Who did Launch Review include as topics?",
            "Who was among the authors writing about Launch Review?",
            "Who participated in the event about Launch Review?",
            "Who was counted as present in a report on Launch Review?",
            "Who appeared at the Launch Review meeting remotely?",
            "Who came along to Launch Review after 2024?",
            "Who advised Acme on strategy?",
        )
        for cue in cues:
            with self.subTest(cue=cue):
                self.assertIsNone(parse_relationship_query(self.bundle, cue))

    def test_punctuation_separated_preambles_are_supported(self):
        cases = {
            "Can you tell me: who founded Acme?": {"founded"},
            "Can you tell me; who founded Acme?": {"founded"},
            "Can you tell me\nwho founded Acme?": {"founded"},
            "Please\nlist the people who invested in Acme.": {"invested_in"},
        }
        for cue, expected in cases.items():
            with self.subTest(cue=cue):
                request = parse_relationship_query(self.bundle, cue)
                self.assertIsNotNone(request)
                self.assertEqual(request.edge_types, frozenset(expected))

    def test_indirect_question_wrappers_are_supported(self):
        cues = (
            "Could you let me know who founded Acme?",
            "Do we know who founded Acme?",
            "I am trying to remember who founded Acme.",
            "I cannot recall who founded Acme.",
            "Out of curiosity, who founded Acme?",
            "For reference, who founded Acme?",
            "Please tell me the names of the people who founded Acme.",
            "Can you say who founded Acme?",
            "Would you identify who founded Acme?",
            "I need to know who founded Acme.",
            "Name everyone who founded Acme.",
            "Identify the person who founded Acme.",
            "Give me the names of Acme's founders.",
            "Could you let me know: who founded Acme?",
            "Do we know; who founded Acme?",
        )
        for cue in cues:
            with self.subTest(cue=cue):
                request = parse_relationship_query(self.bundle, cue)
                self.assertIsNotNone(request)
                self.assertEqual(request.edge_types, frozenset({"founded"}))

    def test_non_substantive_conversational_frames_are_ignored(self):
        cues = (
            "Can you help? Who founded Acme?",
            "What a day! Who founded Acme?",
            "Please listen. Who founded Acme?",
            "Who knows. Who founded Acme?",
            "Do you understand? Who founded Acme?",
            "Tell me something. Who founded Acme?",
        )
        for cue in cues:
            with self.subTest(cue=cue):
                request = parse_relationship_query(self.bundle, cue)
                self.assertIsNotNone(request)
                self.assertEqual(request.edge_types, frozenset({"founded"}))

    def test_substantive_unparsed_questions_prevent_partial_answers(self):
        cues = (
            "Where was I? Who founded Acme?",
            "What do you think? Who founded Acme?",
            "Tell me about Beta. Who founded Acme?",
        )
        for cue in cues:
            with self.subTest(cue=cue):
                self.assertIsNone(parse_relationship_query(self.bundle, cue))

    def test_multiple_or_qualified_questions_remain_bounded(self):
        cues = (
            "Who founded Acme in 2019?",
            "Who founded Acme? Who invested in Acme?",
            "Who founded Acme? Who acquired Acme?",
            "Who founded Acme? Please identify its customers.",
        )
        for cue in cues:
            with self.subTest(cue=cue):
                self.assertIsNone(parse_relationship_query(self.bundle, cue))

    def test_repeated_identical_question_is_one_request(self):
        request = parse_relationship_query(
            self.bundle,
            "Who founded Acme? Who founded Acme?",
        )
        self.assertIsNotNone(request)
        self.assertEqual(request.edge_types, frozenset({"founded"}))

    def test_explicit_financial_contributions_are_investments(self):
        cues = (
            "Who provided an investment to Acme?",
            "Who committed an investment to Acme?",
            "Who provided an equity contribution to Acme?",
        )
        for cue in cues:
            with self.subTest(cue=cue):
                request = parse_relationship_query(self.bundle, cue)
                self.assertIsNotNone(request)
                self.assertEqual(request.edge_types, frozenset({"invested_in"}))

    def test_opened_holdout_paraphrases_are_development_regressions(self):
        entities = {
            "affiliation": ("Atlas Labs", "company", {"works_at", "founded"}),
            "investment": ("Beacon Systems", "company", {"invested_in"}),
            "advising": ("Cobalt Works", "company", {"advises"}),
            "attendance": ("Delta Summit", "meeting", {"attended"}),
        }
        clauses = {
            "affiliation": (
                "worked for or founded {seed}",
                "were employed by or founded {seed}",
                "founded or worked at {seed}",
                "were founders of or employees of {seed}",
                "helped found or worked for {seed}",
            ),
            "investment": (
                "made an investment in {seed}",
                "provided investment capital to {seed}",
                "backed {seed} with money",
                "committed capital to {seed}",
                "made financial investments in {seed}",
            ),
            "advising": (
                "served as advisers to {seed}",
                "gave advice to {seed}",
                "provided advice to {seed}",
                "counseled {seed}",
                "acted as advisers for {seed}",
            ),
            "attendance": (
                "were in attendance at {seed}",
                "were present for {seed}",
                "went to {seed}",
                "came to {seed}",
                "were at {seed}",
            ),
        }
        prefixes = (
            "Which people",
            "Which individuals",
            "What people",
            "What individuals",
        )
        for family, (title, note_type, _) in entities.items():
            self.bundle.write_note(
                f"{'meetings' if note_type == 'meeting' else 'companies'}/"
                f"{title.casefold().replace(' ', '-')}.md",
                {"title": title, "type": note_type},
                "Development fixture.",
            )
        for family, family_clauses in clauses.items():
            title, _, expected = entities[family]
            for clause in family_clauses:
                for prefix in prefixes:
                    cue = f"{prefix} {clause.format(seed=title)}?"
                    with self.subTest(cue=cue):
                        request = parse_relationship_query(self.bundle, cue)
                        self.assertIsNotNone(request)
                        self.assertEqual(request.edge_types, frozenset(expected))

    def test_seed_titles_do_not_supply_relation_language(self):
        cases = (
            ("Cobalt Works", "company", "gave advice to", {"advises"}),
            ("Founders Summit", "meeting", "attended", {"attended"}),
            ("Advisory Capital", "company", "founded", {"founded"}),
        )
        for title, entity_type, phrase, expected in cases:
            directory = "meetings" if entity_type == "meeting" else "companies"
            self.bundle.write_note(
                f"{directory}/{title.casefold().replace(' ', '-')}.md",
                {"title": title, "type": entity_type},
                "Seed-name isolation fixture.",
            )
            cue = f"Which people {phrase} {title}?"
            with self.subTest(cue=cue):
                request = parse_relationship_query(self.bundle, cue)
                self.assertIsNotNone(request)
                self.assertEqual(request.edge_types, frozenset(expected))

    def test_vague_attendance_language_requires_a_meeting_seed(self):
        for cue in ("Which people came to Acme?", "Which people were at Acme?"):
            with self.subTest(cue=cue):
                self.assertIsNone(parse_relationship_query(self.bundle, cue))

    def test_relation_queries_require_compatible_seed_types(self):
        cues = (
            "Where did Acme work?",
            "What companies did Acme invest in?",
            "Who attended Acme?",
            "Who founded Launch Review?",
            "Who invested in Launch Review?",
            "Who advised Launch Review?",
            "Who worked at Launch Review?",
        )
        for cue in cues:
            with self.subTest(cue=cue):
                self.assertIsNone(parse_relationship_query(self.bundle, cue))

    def test_outbound_relation_queries_traverse_from_a_person(self):
        cases = {
            "Where does Sam Lee work?": ("works_at", "companies/acme.md"),
            "Where has Sam Lee worked?": ("works_at", "companies/acme.md"),
            "Where was Sam Lee employed?": ("works_at", None),
            "Which companies did Ada found?": ("founded", "companies/acme.md"),
            "What companies has Ada founded?": ("founded", "companies/acme.md"),
            "What companies has Ada created?": ("founded", "companies/acme.md"),
            "Which companies did Grace Hopper invest in?": (
                "invested_in",
                "companies/acme.md",
            ),
            "What companies has Grace Hopper invested in?": (
                "invested_in",
                "companies/acme.md",
            ),
            "What companies has Grace Hopper backed?": (
                "invested_in",
                "companies/acme.md",
            ),
            "Which companies does Lin Chen advise?": (
                "advises",
                "companies/acme.md",
            ),
            "What companies has Lin Chen advised?": (
                "advises",
                "companies/acme.md",
            ),
            "Which companies has Lin Chen mentored?": (
                "advises",
                "companies/acme.md",
            ),
            "Which meetings did Ada attend?": ("attended", "meetings/review.md"),
            "What meetings has Ada attended?": ("attended", "meetings/review.md"),
            "Which meetings did Ada go to?": ("attended", "meetings/review.md"),
            "Which businesses were founded by Ada?": (
                "founded",
                "companies/acme.md",
            ),
            "Which firms were financed by Grace Hopper?": (
                "invested_in",
                "companies/acme.md",
            ),
            "Which firms were advised by Lin Chen?": (
                "advises",
                "companies/acme.md",
            ),
            "Which employers hired Sam Lee?": (
                "works_at",
                "companies/acme.md",
            ),
            "Which events were attended by Ada?": (
                "attended",
                "meetings/review.md",
            ),
        }
        for cue, (relation, expected_path) in cases.items():
            with self.subTest(cue=cue):
                request = parse_relationship_query(self.bundle, cue)
                self.assertIsNotNone(request)
                self.assertEqual(request.edge_types, frozenset({relation}))
                self.assertEqual(request.direction, "out")
                self.assertEqual(
                    [hit.path for hit in relationship_hits(self.bundle, cue)],
                    [] if expected_path is None else [expected_path],
                )

    def test_passive_inbound_queries_resolve_the_seed(self):
        cases = {
            "Who was Acme financed by?": ("invested_in", "people/grace.md"),
            "By whom was Acme financed?": ("invested_in", "people/grace.md"),
            "By whom was Acme advised?": ("advises", "people/lin.md"),
            "Who was hired by Acme?": ("works_at", "people/sam.md"),
            "Who was Launch Review attended by?": (
                "attended",
                ("people/ada.md", "people/sam.md"),
            ),
        }
        for cue, (relation, expected_paths) in cases.items():
            with self.subTest(cue=cue):
                request = parse_relationship_query(self.bundle, cue)
                self.assertIsNotNone(request)
                self.assertEqual(request.edge_types, frozenset({relation}))
                self.assertEqual(request.direction, "in")
                if isinstance(expected_paths, str):
                    expected_paths = (expected_paths,)
                self.assertEqual(
                    [hit.path for hit in relationship_hits(self.bundle, cue)],
                    list(expected_paths),
                )

    def test_launched_is_a_founding_relation_in_both_directions(self):
        self.bundle.write_note(
            "companies/helio-grid.md",
            {"title": "Helio Grid", "type": "company"},
            "A company launched by its founder.",
        )
        self.bundle.write_note(
            "people/nora-stone.md",
            {"title": "Nora Stone", "type": "person"},
            "Nora Stone launched [Helio Grid](/companies/helio-grid).",
        )
        inbound = relationship_hits(self.bundle, "Who launched Helio Grid?")
        outbound = relationship_hits(
            self.bundle,
            "Which firms has Nora Stone launched?",
        )
        self.assertEqual([hit.path for hit in inbound], ["people/nora-stone.md"])
        self.assertEqual([hit.path for hit in outbound], ["companies/helio-grid.md"])

    def test_advice_direction_remains_bounded(self):
        for cue in (
            "Which people received advice from Acme?",
            "Which people asked Acme for advice?",
        ):
            with self.subTest(cue=cue):
                self.assertIsNone(parse_relationship_query(self.bundle, cue))

    def test_relation_terms_must_bind_to_the_seed(self):
        cues = (
            "Who interviewed founders of Acme?",
            "Who tracked attendance at Launch Review?",
            "Who gave advice to Acme's customers?",
            "Who can advise Acme?",
            "Which people researched investment in Acme?",
            "Which people discussed capital allocation at Acme?",
            "Which people advised another company while at Acme?",
            "Which people worked on reports for Acme?",
            "Which people attended a meeting about Acme?",
            "Which people founded a club after leaving Acme?",
        )
        for cue in cues:
            with self.subTest(cue=cue):
                self.assertIsNone(parse_relationship_query(self.bundle, cue))

    def test_investment_language_requires_a_financial_relation(self):
        cues = (
            "Who made money in Acme?",
            "Who made contributions to Acme?",
            "Who provided resources to Acme?",
            "Who supplied resources to Acme?",
            "Who took an interest in Acme?",
        )
        for cue in cues:
            with self.subTest(cue=cue):
                self.assertIsNone(parse_relationship_query(self.bundle, cue))

    def test_compositional_relation_language_remains_bounded(self):
        cues = (
            "Which people created reports at Acme?",
            "Which people purchased software from Acme?",
            "Which people gave recommendations about another topic at Acme?",
            "Which people appeared in documents about Launch Review?",
            "Which people received payroll reports from Acme?",
        )
        for cue in cues:
            with self.subTest(cue=cue):
                self.assertIsNone(parse_relationship_query(self.bundle, cue))

    def test_relation_intent_rejects_statements_and_compound_relations(self):
        self.assertIsNone(
            parse_relationship_query(self.bundle, "Grace supplied financing to Acme.")
        )
        self.assertIsNone(
            parse_relationship_query(self.bundle, "Who joined and later advised Acme?")
        )

    def test_ambiguous_seed_stays_on_the_lexical_path(self):
        self.bundle.write_note(
            "funds/acme.md",
            {"title": "Acme", "type": "fund"},
            "A separate entity with the same title.",
        )
        self.assertIsNone(parse_relationship_query(self.bundle, "Who founded Acme?"))

    def test_identity_cache_is_invalidated_when_notes_change(self):
        self.assertIsNotNone(parse_relationship_query(self.bundle, "Who founded Acme?"))
        self.bundle.write_note(
            "companies/beta.md",
            {"title": "Beta", "type": "company"},
            "A company added after the first relationship query.",
        )
        request = parse_relationship_query(self.bundle, "Who founded Beta?")
        self.assertIsNotNone(request)
        self.assertEqual(request.seed, "companies/beta.md")

        self.bundle.write_note(
            "funds/acme.md",
            {"title": "Acme", "type": "fund"},
            "A second entity with the same title.",
        )
        self.assertIsNone(parse_relationship_query(self.bundle, "Who founded Acme?"))

    def test_longest_containing_identity_resolves_the_seed(self):
        self.bundle.write_note(
            "companies/acme-labs.md",
            {"title": "Acme Labs", "type": "company"},
            "A company with a title that contains another company title.",
        )
        request = parse_relationship_query(self.bundle, "Who founded Acme Labs?")
        self.assertIsNotNone(request)
        self.assertEqual(request.seed, "companies/acme-labs.md")

    def test_longer_relation_like_titles_do_not_target_a_shorter_seed(self):
        fixtures = (
            ("companies/acme-advisors.md", "Acme Advisors", "company"),
            ("companies/acme-investors.md", "Acme Investors", "company"),
            (
                "meetings/review-participants.md",
                "Launch Review Participants",
                "meeting",
            ),
        )
        for path, title, entity_type in fixtures:
            self.bundle.write_note(
                path,
                {"title": title, "type": entity_type},
                "An overlapping entity title.",
            )
        for cue in (
            "Who are Acme Advisors?",
            "Who are Acme Investors?",
            "Who are Launch Review Participants?",
        ):
            with self.subTest(cue=cue):
                self.assertIsNone(parse_relationship_query(self.bundle, cue))

    def test_separate_entity_mentions_remain_ambiguous(self):
        self.bundle.write_note(
            "companies/beta.md",
            {"title": "Beta", "type": "company"},
            "A second company.",
        )
        self.assertIsNone(
            parse_relationship_query(self.bundle, "Who founded Acme and Beta?")
        )

    def test_relation_nouns_that_are_note_titles_do_not_hide_the_seed(self):
        titles = ("Employee", "Advisor", "Investors", "Participants", "Founders")
        for title in titles:
            self.bundle.write_note(
                f"notes/{title.casefold()}.md",
                {"title": title, "type": "note"},
                "A relation noun used as an unrelated note title.",
            )
        cases = {
            "Who is an advisor to Acme?": {"advises"},
            "Who are the investors in Acme?": {"invested_in"},
            "Who are the participants at Launch Review?": {"attended"},
            "Who are the founders of Acme?": {"founded"},
            "Who is an employee at Acme?": {"works_at"},
        }
        for cue, expected in cases.items():
            with self.subTest(cue=cue):
                request = parse_relationship_query(self.bundle, cue)
                self.assertIsNotNone(request)
                self.assertEqual(request.edge_types, frozenset(expected))

    def test_relation_noun_entity_titles_preserve_the_role_word(self):
        fixtures = (
            (
                "companies/employee.md",
                "Employee",
                "company",
                "[Sam](/people/sam) works at Employee.",
                "Who is an employee at Employee?",
                "people/sam.md",
            ),
            (
                "companies/founders.md",
                "Founders",
                "company",
                "[Ada](/people/ada) founded Founders.",
                "Who are the founders of Founders?",
                "people/ada.md",
            ),
            (
                "companies/advisor.md",
                "Advisor",
                "company",
                "[Lin](/people/lin) advises Advisor.",
                "Who is an advisor to Advisor?",
                "people/lin.md",
            ),
            (
                "companies/investors.md",
                "Investors",
                "company",
                "[Grace](/people/grace) invested in Investors.",
                "Who are the investors in Investors?",
                "people/grace.md",
            ),
            (
                "meetings/participants.md",
                "Participants",
                "meeting",
                "[Ada](/people/ada) attended Participants.",
                "Who are the participants at Participants?",
                "people/ada.md",
            ),
        )
        for path, title, entity_type, body, _, _ in fixtures:
            self.bundle.write_note(
                path,
                {"title": title, "type": entity_type},
                body,
            )
        for _, _, _, _, cue, expected_path in fixtures:
            with self.subTest(cue=cue):
                hits = relationship_hits(self.bundle, cue)
                self.assertEqual([hit.path for hit in hits], [expected_path])

    def test_employee_query_excludes_founder_only_edges(self):
        paths = [
            hit.path
            for hit in relationship_hits(self.bundle, "Who are Acme employees?")
        ]
        self.assertEqual(paths, ["people/sam.md"])

    def test_relationship_hits_return_only_typed_answers(self):
        paths = [
            hit.path for hit in relationship_hits(self.bundle, "Who works at Acme?")
        ]
        self.assertEqual(paths, ["people/sam.md"])
        attended = {
            hit.path
            for hit in relationship_hits(self.bundle, "Who attended Launch Review?")
        }
        self.assertEqual(attended, {"people/ada.md", "people/sam.md"})

    def test_conversational_role_question_returns_typed_answers(self):
        paths = [
            hit.path
            for hit in relationship_hits(
                self.bundle,
                "Quick question: who is the founder of Acme?",
            )
        ]
        self.assertEqual(paths, ["people/ada.md"])

    def test_ambiguous_financial_language_returns_no_typed_answers(self):
        self.assertEqual(
            relationship_hits(self.bundle, "Who made money in Acme?"),
            (),
        )

    def test_recall_places_graph_answers_before_lexical_noise(self):
        hits = recall_explain(
            self.bundle,
            None,
            "Who works at Acme?",
            k=len(self.bundle.notes),
            use_dynamics=False,
            reactivate=False,
        )
        paths = [item[0].path for item in hits]
        self.assertEqual(paths[0], "people/sam.md")
        self.assertIn("notes/noise.md", paths)
        evidence = {item[0].path: item[3] for item in hits}
        self.assertEqual(evidence["people/sam.md"].relation, "works_at")
        self.assertEqual(evidence["people/sam.md"].relation_seed, "companies/acme.md")
        self.assertEqual(evidence["people/sam.md"].relation_hop, 1)

    def test_unrecognized_query_preserves_existing_ranking(self):
        cue = "Who supports Acme?"
        expected = _ranked_for(
            self.bundle, None, [Cue(cue, 1.0, "query")], False, False
        )
        actual = recall_explain(
            self.bundle,
            None,
            cue,
            k=len(self.bundle.notes),
            use_dynamics=False,
            reactivate=False,
        )
        self.assertEqual(
            [(item[0].path, item[1], item[2]) for item in actual],
            [(item[0].path, item[1], item[2]) for item in expected],
        )

    def test_volunteer_uses_explicit_relational_answer(self):
        hits = volunteer_explain(
            self.bundle,
            None,
            "Who advises Acme?",
            reactivate=False,
        )
        self.assertEqual([item[0].path for item in hits], ["people/lin.md"])

    def test_context_pack_uses_typed_answers(self):
        text = context_pack(
            self.bundle,
            None,
            "Who works at Acme?",
            k=2,
            budget=600,
            mode="flat",
            reactivate=False,
            index=False,
        )
        self.assertIn("### Sam Lee", text)
        self.assertNotIn("### Ada Lovelace", text)
        self.assertIn("works_at one hop from [[Acme]]", text)

    def test_explicit_typed_link_is_indexed(self):
        self.bundle.write_note(
            "people/typed.md",
            {"title": "Typed Person", "type": "person"},
            "# Connections\n\n- works_at [[Acme]] (extracted)",
        )
        same_bundle = {
            (edge.subject, edge.object, edge.relation)
            for edge in relationship_edges(self.bundle)
        }
        self.assertIn(
            ("people/typed.md", "companies/acme.md", "works_at"),
            same_bundle,
        )
        reloaded = Bundle(self.root)
        triples = {
            (edge.subject, edge.object, edge.relation)
            for edge in relationship_edges(reloaded)
        }
        self.assertIn(("people/typed.md", "companies/acme.md", "works_at"), triples)

    def test_typed_link_resolves_when_its_target_is_written_later(self):
        self.bundle.write_note(
            "people/typed-early.md",
            {"title": "Typed Early", "type": "person"},
            "# Connections\n\n- works_at [[Late Company]] (extracted)",
        )
        self.assertEqual(self.bundle.notes["people/typed-early.md"].typed_links, [])
        self.assertFalse(
            any(
                edge.subject == "people/typed-early.md"
                for edge in relationship_edges(self.bundle)
            )
        )
        self.assertIsNone(
            parse_relationship_query(self.bundle, "Who works at Late Company?")
        )
        self.bundle.write_note(
            "companies/late-company.md",
            {"title": "Late Company", "type": "company"},
            "A company written after its typed-link source.",
        )
        expected = ("people/typed-early.md", "companies/late-company.md", "works_at")
        same_bundle = {
            (edge.subject, edge.object, edge.relation)
            for edge in relationship_edges(self.bundle)
        }
        self.assertIn(expected, same_bundle)
        self.assertEqual(
            self.bundle.notes["people/typed-early.md"].typed_links[0]["target"],
            "companies/late-company.md",
        )
        self.assertEqual(
            [
                hit.path
                for hit in relationship_hits(
                    self.bundle,
                    "Who works at Late Company?",
                )
            ],
            ["people/typed-early.md"],
        )

    def test_deprecated_graph_answer_is_not_recalled(self):
        self.bundle.write_note(
            "people/old.md",
            {"title": "Old Employee", "type": "person", "status": "deprecated"},
            "Old Employee works at [Acme](/companies/acme).",
        )
        reloaded = Bundle(self.root)
        paths = [
            item[0].path
            for item in recall_explain(
                reloaded,
                None,
                "Who works at Acme?",
                k=len(reloaded.notes),
                use_dynamics=False,
                reactivate=False,
            )
        ]
        self.assertNotIn("people/old.md", paths)

    def test_write_invalidates_the_derived_relationship_index(self):
        relationship_edges(self.bundle)
        self.bundle.write_note(
            "people/new-advisor.md",
            {"title": "New Advisor", "type": "person"},
            "New Advisor advises [Acme](/companies/acme).",
        )
        paths = {
            hit.path for hit in relationship_hits(self.bundle, "Who advises Acme?")
        }
        self.assertIn("people/new-advisor.md", paths)


if __name__ == "__main__":
    unittest.main()
