"""Adversarial regressions for the Muninn relational retrieval candidates.

These tests encode the independent v5 audit findings. They are intentionally
deterministic and implementation-facing: a candidate must pass them before it
is eligible for another frozen review.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import muninn.relationships as relationship_module
from muninn.activate import merged_graph
from muninn.dynamics import Dynamics
from muninn.recall import recall_explain
from muninn.relationships import (
    parse_relationship_query,
    relationship_edges,
    relationship_hits,
)
from muninn.store import Bundle


def _triples(bundle: Bundle) -> set[tuple[str, str, str]]:
    return {
        (edge.subject, edge.object, edge.relation)
        for edge in relationship_edges(bundle)
    }


class V6AdversarialRelationshipTests(unittest.TestCase):
    def make_bundle(self) -> tuple[str, Bundle]:
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        return root, Bundle(root)

    @staticmethod
    def write(
        bundle: Bundle,
        path: str,
        title: str,
        note_type: str,
        body: str,
        *,
        aliases: list[str] | None = None,
        **extra: object,
    ) -> str:
        meta: dict[str, object] = {
            "title": title,
            "type": note_type,
            **extra,
        }
        if aliases is not None:
            meta["aliases"] = aliases
        return bundle.write_note(path, meta, body)

    def assert_hits(self, bundle: Bundle, cue: str, expected: list[str]) -> None:
        self.assertEqual([hit.path for hit in relationship_hits(bundle, cue)], expected)

    def test_write_note_canonicalizes_dot_paths_to_one_live_key(self):
        _, bundle = self.make_bundle()

        first = self.write(bundle, "./a.md", "First", "note", "First body.")
        second = self.write(bundle, "a.md", "Second", "note", "Second body.")

        self.assertEqual(first, "a.md")
        self.assertEqual(second, "a.md")
        self.assertEqual(sorted(bundle.notes), ["a.md"])
        self.assertEqual(bundle.notes["a.md"].title, "Second")

    def test_cmd_add_uses_canonical_path_for_dynamics(self):
        root, _bundle = self.make_bundle()
        from muninn.cli import main

        with mock.patch("builtins.print"):
            main(
                [
                    "--root",
                    root,
                    "add",
                    "./notes/a.md",
                    "--title",
                    "A",
                    "--body",
                    "Body.",
                ]
            )

        from muninn.dynamics import Dynamics

        dynamics = Dynamics(root)
        self.assertIn("notes/a.md", dynamics.entries)
        self.assertNotIn("./notes/a.md", dynamics.entries)

    def test_write_note_uses_existing_disk_case_on_case_insensitive_filesystems(self):
        root, bundle = self.make_bundle()
        self.write(bundle, "Case.md", "First", "note", "First body.")
        if not os.path.exists(os.path.join(root, "case.md")):
            self.skipTest("filesystem is case-sensitive")

        returned = self.write(bundle, "case.md", "Second", "note", "Second body.")

        self.assertEqual(returned, "Case.md")
        self.assertEqual(sorted(bundle.notes), ["Case.md"])
        self.assertEqual(bundle.notes["Case.md"].title, "Second")
        self.assertEqual(sorted(Bundle(root).notes), ["Case.md"])

    def test_write_note_canonicalizes_backslashes_before_link_resolution(self):
        _, bundle = self.make_bundle()

        returned = self.write(
            bundle,
            "people\\ada.md",
            "Ada",
            "person",
            "Person.",
        )
        self.write(
            bundle,
            "companies/acme.md",
            "Acme",
            "company",
            "Acme hired [[Ada]].",
        )

        self.assertEqual(returned, "people/ada.md")
        self.assertEqual(sorted(bundle.notes), ["companies/acme.md", "people/ada.md"])
        self.assertEqual(bundle.notes["companies/acme.md"].links, ["people/ada.md"])
        self.assertIn(
            ("people/ada.md", "companies/acme.md", "works_at"),
            _triples(bundle),
        )

    def test_write_note_rejects_paths_that_the_loader_will_ignore(self):
        _, bundle = self.make_bundle()

        for path in (
            "extensionless",
            "note.txt",
            "index.md",
            "log.md",
            ".hidden/note.md",
            ".muninn/note.md",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.write(bundle, path, "Invalid", "note", "Body.")

        self.assertEqual(bundle.notes, {})

    def test_write_note_rejects_existing_directory_symlink_escape(self):
        root, bundle = self.make_bundle()
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside)
        os.symlink(outside, os.path.join(root, "escape"))

        with self.assertRaises(ValueError):
            self.write(bundle, "escape/note.md", "Invalid", "note", "Body.")

        self.assertEqual(os.listdir(outside), [])

    def test_write_note_rejects_in_bundle_directory_and_file_symlinks(self):
        root, bundle = self.make_bundle()
        os.makedirs(os.path.join(root, "real"))
        os.symlink("real", os.path.join(root, "alias"))

        with self.assertRaises(ValueError):
            self.write(bundle, "alias/note.md", "Invalid", "note", "Body.")

        real_note = os.path.join(root, "real", "target.md")
        with open(real_note, "w", encoding="utf-8") as handle:
            handle.write("external")
        os.symlink("real/target.md", os.path.join(root, "alias-file.md"))
        with self.assertRaises(ValueError):
            self.write(bundle, "alias-file.md", "Invalid", "note", "Body.")

        with open(real_note, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "external")

    def test_private_load_clears_notes_and_backlinks_for_deleted_files(self):
        root, bundle = self.make_bundle()
        self.write(bundle, "people/ada.md", "Ada", "person", "Person.")
        self.write(
            bundle,
            "companies/acme.md",
            "Acme",
            "company",
            "Acme hired [[Ada]].",
        )
        self.assertIn("companies/acme.md", bundle.notes)
        self.assertEqual(bundle.backlinks["people/ada.md"], ["companies/acme.md"])

        os.remove(os.path.join(root, "companies", "acme.md"))
        bundle._load()

        self.assertNotIn("companies/acme.md", bundle.notes)
        self.assertNotIn("people/ada.md", bundle.backlinks)

    def test_receive_and_get_funding_queries_resolve_to_investors(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "people/grace.md", "Grace Hopper", "person", "Person.")
        self.write(
            bundle,
            "companies/acme.md",
            "Acme",
            "company",
            "Acme received funding from [[Grace Hopper]].",
        )

        self.assert_hits(bundle, "Who did Acme receive funding from?", ["people/grace.md"])
        self.assert_hits(bundle, "Who did Acme get funding from?", ["people/grace.md"])

    def test_if_any_wrapper_preserves_founder_query(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "people/ada.md", "Ada Lovelace", "person", "Person.")
        self.write(
            bundle,
            "companies/acme.md",
            "Acme",
            "company",
            "Acme was founded by [[Ada Lovelace]].",
        )

        request = parse_relationship_query(bundle, "Who, if anyone, founded Acme?")
        self.assertIsNotNone(request)
        self.assertEqual(request.edge_types, frozenset({"founded"}))
        self.assert_hits(bundle, "Who, if anyone, founded Acme?", ["people/ada.md"])

    def test_passive_founding_with_date_adjunct_extracts_founder(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "people/ada.md", "Ada", "person", "Person.")
        self.write(
            bundle,
            "companies/chronos.md",
            "Chronos Labs",
            "company",
            "Chronos Labs was founded in 2020 by [[Ada]].",
        )

        self.assertIn(("people/ada.md", "companies/chronos.md", "founded"), _triples(bundle))

    def test_seed_funding_modifier_extracts_investor(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "people/grace.md", "Grace", "person", "Person.")
        self.write(
            bundle,
            "companies/beacon.md",
            "Beacon Systems",
            "company",
            "Beacon Systems received seed funding from [[Grace]].",
        )

        self.assertIn(
            ("people/grace.md", "companies/beacon.md", "invested_in"),
            _triples(bundle),
        )

    def test_strategic_advice_modifier_extracts_adviser(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "people/lin.md", "Lin", "person", "Person.")
        self.write(
            bundle,
            "companies/cobalt.md",
            "Cobalt Analytics",
            "company",
            "Cobalt Analytics received strategic advice from [[Lin]].",
        )

        self.assertIn(("people/lin.md", "companies/cobalt.md", "advises"), _triples(bundle))

    def test_possessive_ceo_form_extracts_employee(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "people/sam.md", "Sam", "person", "Person.")
        self.write(
            bundle,
            "companies/delta.md",
            "Delta Systems",
            "company",
            "Delta Systems' CEO is [[Sam]].",
        )

        self.assertIn(("people/sam.md", "companies/delta.md", "works_at"), _triples(bundle))
        self.assert_hits(bundle, "Who is Delta Systems' CEO?", ["people/sam.md"])

    def test_executive_query_excludes_other_employees(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "people/sam.md", "Sam", "person", "Person.")
        self.write(bundle, "people/ari.md", "Ari", "person", "Person.")
        self.write(
            bundle,
            "companies/delta.md",
            "Delta Systems",
            "company",
            "Delta Systems' CEO is [[Sam]]. Delta Systems hired [[Ari]].",
        )

        self.assert_hits(bundle, "Who is Delta Systems' CEO?", ["people/sam.md"])
        self.assert_hits(
            bundle,
            "Who works at Delta Systems?",
            ["people/sam.md", "people/ari.md"],
        )

    def test_specific_advisory_and_round_queries_exclude_generic_relations(self):
        _, bundle = self.make_bundle()
        for path, title in (
            ("sana", "Sana Qureshi"),
            ("omar", "Omar Reed"),
            ("vera", "Vera Lin"),
            ("nia", "Nia Patel"),
        ):
            self.write(bundle, f"people/{path}.md", title, "person", "Person.")
        self.write(bundle, "companies/harbor.md", "Harbor AI", "company", "Company.")
        self.write(
            bundle,
            "people/sana.md",
            "Sana Qureshi",
            "person",
            "Sana Qureshi sat on [[Harbor AI]]'s advisory board.",
        )
        self.write(
            bundle,
            "people/omar.md",
            "Omar Reed",
            "person",
            "Omar Reed advised [[Harbor AI]].",
        )
        self.write(
            bundle,
            "people/vera.md",
            "Vera Lin",
            "person",
            "Vera Lin led [[Harbor AI]]'s seed round.",
        )
        self.write(
            bundle,
            "people/nia.md",
            "Nia Patel",
            "person",
            "Nia Patel invested in [[Harbor AI]].",
        )

        self.assert_hits(
            bundle,
            "Who sat on Harbor AI's advisory board?",
            ["people/sana.md"],
        )
        self.assert_hits(
            bundle,
            "Who led Harbor AI's seed round?",
            ["people/vera.md"],
        )

    def test_company_side_role_forms_extract_with_specific_qualifiers(self):
        _, bundle = self.make_bundle()
        for path, title in (
            ("mira", "Mira Chen"),
            ("ilya", "Ilya Park"),
            ("omar", "Omar Singh"),
            ("vera", "Vera Moss"),
            ("nia", "Nia Cole"),
        ):
            self.write(bundle, f"people/{path}.md", title, "person", "Person.")
        self.write(
            bundle,
            "companies/novabyte.md",
            "NovaByte",
            "company",
            "NovaByte's CEO is [[Mira Chen]]. "
            "NovaByte's advisory board includes [[Ilya Park]]. "
            "NovaByte received advisory help from [[Omar Singh]]. "
            "NovaByte's seed round was led by [[Vera Moss]]. "
            "NovaByte's seed investors include [[Nia Cole]].",
        )

        qualifiers = {
            (edge.subject, edge.relation): edge.qualifier
            for edge in relationship_edges(bundle)
            if edge.object == "companies/novabyte.md"
        }
        self.assertEqual(qualifiers[("people/mira.md", "works_at")], "ceo")
        self.assertEqual(
            qualifiers[("people/ilya.md", "advises")],
            "advisory_board",
        )
        self.assertEqual(qualifiers[("people/omar.md", "advises")], "")
        self.assertEqual(
            qualifiers[("people/vera.md", "invested_in")],
            "led_round",
        )
        self.assertEqual(qualifiers[("people/nia.md", "invested_in")], "")
        self.assert_hits(bundle, "Who is the CEO of NovaByte?", ["people/mira.md"])
        self.assert_hits(
            bundle,
            "Who are the advisory board members of NovaByte?",
            ["people/ilya.md"],
        )
        self.assert_hits(
            bundle,
            "Who advises NovaByte?",
            ["people/ilya.md", "people/omar.md"],
        )
        self.assert_hits(
            bundle,
            "Who was the lead investor in NovaByte's seed round?",
            ["people/vera.md"],
        )
        self.assert_hits(
            bundle,
            "Who invested in NovaByte?",
            ["people/vera.md", "people/nia.md"],
        )

    def test_person_side_role_forms_extract_with_specific_qualifiers(self):
        _, bundle = self.make_bundle()
        for path, title in (
            ("lumen", "Lumen Grid"),
            ("cobalt", "Cobalt Rail"),
            ("apex", "Apex Maps"),
        ):
            self.write(bundle, f"companies/{path}.md", title, "company", "Company.")
        self.write(
            bundle,
            "people/asa.md",
            "Asa Finch",
            "person",
            "Asa Finch serves on the advisory board of [[Lumen Grid]].",
        )
        self.write(
            bundle,
            "people/lead.md",
            "Lead Investor",
            "person",
            "Lead Investor was the lead investor in [[Cobalt Rail]]'s seed round.",
        )
        self.write(
            bundle,
            "people/celia.md",
            "Celia Ward",
            "person",
            "Celia Ward served as CEO of [[Apex Maps]].",
        )

        qualifiers = {
            (edge.subject, edge.object, edge.relation): edge.qualifier
            for edge in relationship_edges(bundle)
        }
        self.assertEqual(
            qualifiers[("people/asa.md", "companies/lumen.md", "advises")],
            "advisory_board",
        )
        self.assertEqual(
            qualifiers[
                ("people/lead.md", "companies/cobalt.md", "invested_in")
            ],
            "led_round",
        )
        self.assertEqual(
            qualifiers[("people/celia.md", "companies/apex.md", "works_at")],
            "ceo",
        )
        self.assert_hits(
            bundle,
            "Who is on Lumen Grid's advisory board?",
            ["people/asa.md"],
        )
        self.assert_hits(
            bundle,
            "Who led Cobalt Rail's seed round?",
            ["people/lead.md"],
        )
        self.assert_hits(bundle, "Who is Apex Maps' CEO?", [])
        self.assert_hits(bundle, "Who was Apex Maps' CEO?", ["people/celia.md"])

    def test_role_phrases_do_not_bind_nearby_decoys(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "people/vera.md", "Vera Moss", "person", "Person.")
        self.write(bundle, "people/ilya.md", "Ilya Park", "person", "Person.")
        self.write(bundle, "companies/apex.md", "Apex Maps", "company", "Company.")
        self.write(
            bundle,
            "companies/novabyte.md",
            "NovaByte",
            "company",
            "NovaByte's seed roundtable was led by [[Vera Moss]]. "
            "NovaByte's advisory board report includes [[Ilya Park]].",
        )
        self.write(
            bundle,
            "people/celia.md",
            "Celia Ward",
            "person",
            "Celia Ward served as CEO of [[Apex Maps]]'s competitor.",
        )

        self.assertEqual(_triples(bundle), set())

    def test_long_executive_role_extracts_and_normalizes(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "companies/zenith.md", "Zenith", "company", "Company.")
        self.write(
            bundle,
            "people/celia.md",
            "Celia Ward",
            "person",
            "Celia Ward served as chief executive officer of [[Zenith]].",
        )

        edges = relationship_edges(bundle)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].relation, "works_at")
        self.assertEqual(edges[0].qualifier, "ceo")
        self.assert_hits(bundle, "Who is the chief executive officer of Zenith?", [])
        self.assert_hits(
            bundle,
            "Who was the chief executive officer of Zenith?",
            ["people/celia.md"],
        )

    def test_additional_common_relation_forms_extract_and_retrieve(self):
        _, bundle = self.make_bundle()
        people = (
            ("nia", "Nia Patel"),
            ("tess", "Tess Vale"),
            ("lena", "Lena Ortiz"),
            ("mark", "Mark Ivers"),
            ("sana", "Sana Qureshi"),
            ("vera", "Vera Lin"),
        )
        for path, title in people:
            self.write(bundle, f"people/{path}.md", title, "person", "Person.")
        self.write(
            bundle,
            "companies/atlas.md",
            "Atlas Robotics",
            "company",
            "Atlas Robotics raised capital from [[Nia Patel]].",
        )
        self.write(
            bundle,
            "meetings/q3.md",
            "Q3 Review",
            "meeting",
            "The Q3 Review listed [[Tess Vale]] as present.",
        )
        self.write(bundle, "companies/harbor.md", "Harbor AI", "company", "Company.")
        self.write(
            bundle,
            "people/lena.md",
            "Lena Ortiz",
            "person",
            "Lena Ortiz helped found [[Harbor AI]].",
        )
        self.write(
            bundle,
            "people/mark.md",
            "Mark Ivers",
            "person",
            "Mark Ivers was employed by [[Harbor AI]].",
        )
        self.write(
            bundle,
            "people/sana.md",
            "Sana Qureshi",
            "person",
            "Sana Qureshi sat on [[Harbor AI]]'s advisory board.",
        )
        self.write(
            bundle,
            "people/vera.md",
            "Vera Lin",
            "person",
            "Vera Lin led [[Harbor AI]]'s seed round.",
        )

        expected = {
            ("people/nia.md", "companies/atlas.md", "invested_in"),
            ("people/tess.md", "meetings/q3.md", "attended"),
            ("people/lena.md", "companies/harbor.md", "founded"),
            ("people/mark.md", "companies/harbor.md", "works_at"),
            ("people/sana.md", "companies/harbor.md", "advises"),
            ("people/vera.md", "companies/harbor.md", "invested_in"),
        }
        self.assertTrue(expected.issubset(_triples(bundle)))
        for cue, path in (
            ("From whom did Atlas Robotics raise capital?", "people/nia.md"),
            ("Who helped found Harbor AI?", "people/lena.md"),
            ("Who was employed by Harbor AI?", "people/mark.md"),
            ("Who sat on Harbor AI's advisory board?", "people/sana.md"),
            ("Who led Harbor AI's seed round?", "people/vera.md"),
        ):
            with self.subTest(cue=cue):
                self.assert_hits(bundle, cue, [path])

    def test_direct_quote_does_not_create_asserted_relationship(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "companies/quote-co.md", "Quote Co", "company", "Company.")
        self.write(
            bundle,
            "people/ivy.md",
            "Ivy North",
            "person",
            'Ivy North said, "Ivy North founded [[Quote Co]]."',
        )

        self.assertNotIn(("people/ivy.md", "companies/quote-co.md", "founded"), _triples(bundle))

    def test_single_quoted_prompt_does_not_create_asserted_relationship(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "companies/orion.md", "Orion Works", "company", "Company.")
        self.write(
            bundle,
            "people/mira.md",
            "Mira Source",
            "person",
            "The archived prompt was 'Mira Source founded [[Orion Works]].'",
        )

        self.assertNotIn(
            ("people/mira.md", "companies/orion.md", "founded"),
            _triples(bundle),
        )

    def test_named_reporter_and_smart_quote_do_not_create_relationships(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "companies/claim.md", "Claim Co", "company", "Company.")
        self.write(
            bundle,
            "people/ada.md",
            "Ada Vale",
            "person",
            "Ivy North said Ada Vale founded [[Claim Co]]. "
            "Ada Vale wrote, “Ada Vale founded [[Claim Co]].”",
        )

        self.assertNotIn(
            ("people/ada.md", "companies/claim.md", "founded"),
            _triples(bundle),
        )

    def test_linked_reporter_does_not_create_asserted_relationship(self):
        for reporter in (
            "[[Ivy North]] said",
            "[Ivy North](/people/reporter) stated",
        ):
            with self.subTest(reporter=reporter):
                _, bundle = self.make_bundle()
                self.write(
                    bundle,
                    "people/reporter.md",
                    "Ivy North",
                    "person",
                    "Reporter.",
                )
                self.write(
                    bundle,
                    "companies/claim.md",
                    "Claim Co",
                    "company",
                    "Company.",
                )
                self.write(
                    bundle,
                    "people/ada.md",
                    "Ada Vale",
                    "person",
                    f"{reporter} Ada Vale founded [[Claim Co]].",
                )

                self.assertNotIn(
                    ("people/ada.md", "companies/claim.md", "founded"),
                    _triples(bundle),
                )

    def test_nonassertive_markdown_does_not_create_relationships(self):
        cases = (
            "> Ada Vale founded [[Claim Co]].",
            "<!-- Ada Vale founded [[Claim Co]]. -->",
            "~~Ada Vale founded [[Claim Co]].~~",
            "The plaque reads 'Ada Vale founded [[Claim Co]].'",
        )
        for body in cases:
            with self.subTest(body=body):
                _, bundle = self.make_bundle()
                self.write(
                    bundle,
                    "companies/claim.md",
                    "Claim Co",
                    "company",
                    "Company.",
                )
                self.write(
                    bundle,
                    "people/ada.md",
                    "Ada Vale",
                    "person",
                    body,
                )

                self.assertEqual(_triples(bundle), set())
                if body.startswith(">"):
                    self.assertEqual(
                        bundle.notes["people/ada.md"].links,
                        ["companies/claim.md"],
                    )
                elif not body.startswith("The plaque"):
                    self.assertEqual(bundle.notes["people/ada.md"].links, [])

    def test_html_comment_does_not_create_explicit_typed_relationship(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "companies/claim.md", "Claim Co", "company", "Company.")
        self.write(
            bundle,
            "people/ada.md",
            "Ada Vale",
            "person",
            "<!--\n- founded [[Claim Co]]\n-->",
        )

        self.assertEqual(bundle.notes["people/ada.md"].typed_links, [])
        self.assertEqual(_triples(bundle), set())

    def test_neither_nor_does_not_create_relationships(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "companies/claim.md", "Claim Co", "company", "Company.")
        self.write(
            bundle,
            "people/ada.md",
            "Ada Vale",
            "person",
            "Ada Vale neither founded nor advised [[Claim Co]].",
        )

        self.assertEqual(_triples(bundle), set())

    def test_reported_speech_suppression_is_segment_scoped(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "companies/rumor.md", "Rumor", "company", "Company.")
        self.write(bundle, "companies/real.md", "Real", "company", "Company.")
        self.write(
            bundle,
            "people/ivy.md",
            "Ivy",
            "person",
            "A newspaper said Ivy founded [[Rumor]], but Ivy founded [[Real]].",
        )

        triples = _triples(bundle)
        self.assertNotIn(("people/ivy.md", "companies/rumor.md", "founded"), triples)
        self.assertIn(("people/ivy.md", "companies/real.md", "founded"), triples)

    def test_person_object_pronoun_does_not_skip_nearer_person_antecedent(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "companies/acme.md", "Acme", "company", "Company.")
        self.write(bundle, "people/cara.md", "Cara Moss", "person", "Person.")
        self.write(
            bundle,
            "people/ivy.md",
            "Ivy North",
            "person",
            "Ivy North introduced Cara Moss. [[Acme]] employed her.",
        )

        self.assertNotIn(("people/ivy.md", "companies/acme.md", "works_at"), _triples(bundle))

    def test_person_object_pronoun_rejects_unlisted_introduction_verb(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "companies/acme.md", "Acme", "company", "Company.")
        self.write(
            bundle,
            "people/ivy.md",
            "Ivy North",
            "person",
            "Ivy North congratulated Cara Moss. [[Acme]] employed her.",
        )

        self.assertNotIn(("people/ivy.md", "companies/acme.md", "works_at"), _triples(bundle))

    def test_basename_wikilink_extracts_relationship_when_title_differs(self):
        _, bundle = self.make_bundle()
        self.write(
            bundle,
            "companies/acme-labs.md",
            "Registered Entity 42",
            "company",
            "Company.",
        )
        self.write(
            bundle,
            "people/ada.md",
            "Ada",
            "person",
            "Ada founded [[acme-labs]].",
        )

        self.assertEqual(bundle.notes["people/ada.md"].links, ["companies/acme-labs.md"])
        self.assertIn(("people/ada.md", "companies/acme-labs.md", "founded"), _triples(bundle))
        self.assert_hits(bundle, "Who founded acme-labs?", ["people/ada.md"])

    def test_basename_alias_collision_is_ambiguous_for_relationships(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "companies/acme.md", "Root Company", "company", "Company.")
        self.write(
            bundle,
            "companies/beta.md",
            "Beta Company",
            "company",
            "Company.",
            aliases=["acme"],
        )
        self.write(
            bundle,
            "people/ivy.md",
            "Ivy",
            "person",
            "Ivy founded [[acme]].",
        )

        self.assertEqual(bundle.notes["people/ivy.md"].links, [])
        triples = _triples(bundle)
        self.assertNotIn(("people/ivy.md", "companies/acme.md", "founded"), triples)
        self.assertNotIn(("people/ivy.md", "companies/beta.md", "founded"), triples)

    def test_relative_markdown_link_does_not_fall_back_to_bundle_root(self):
        _, bundle = self.make_bundle()
        self.write(
            bundle,
            "companies/root-only.md",
            "Root Only",
            "company",
            "Company.",
        )
        self.write(
            bundle,
            "people/ivy.md",
            "Ivy",
            "person",
            "Ivy founded [Root Only](companies/root-only).",
        )

        self.assertEqual(bundle.notes["people/ivy.md"].links, [])
        self.assertNotIn(("people/ivy.md", "companies/root-only.md", "founded"), _triples(bundle))

    def test_platform_absolute_markdown_paths_do_not_create_links(self):
        _, bundle = self.make_bundle()
        self.write(
            bundle,
            "notes/C:/tmp/target.md",
            "Drive Target",
            "note",
            "Target.",
        )
        self.write(
            bundle,
            "server/share/target.md",
            "UNC Target",
            "note",
            "Target.",
        )
        self.write(
            bundle,
            "notes/source.md",
            "Source",
            "note",
            "[Drive](C:/tmp/target.md) and [UNC](\\\\server\\share\\target.md).",
        )

        self.assertEqual(bundle.notes["notes/source.md"].links, [])
        self.assertEqual(bundle.backlinks, {})

    def test_negated_coordination_preserves_positive_relation_only(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "companies/omni.md", "Omni", "company", "Company.")
        self.write(
            bundle,
            "people/ivy.md",
            "Ivy",
            "person",
            "Ivy founded but did not advise [[Omni]].",
        )

        triples = _triples(bundle)
        self.assertIn(("people/ivy.md", "companies/omni.md", "founded"), triples)
        self.assertNotIn(("people/ivy.md", "companies/omni.md", "advises"), triples)

    def test_comma_but_preserves_positive_gapped_relation(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "companies/neg.md", "NegCo", "company", "Company.")
        self.write(bundle, "companies/pos.md", "PosCo", "company", "Company.")
        self.write(
            bundle,
            "people/person.md",
            "Neg Person",
            "person",
            "Neg Person never founded [[NegCo]], but founded [[PosCo]].",
        )

        self.assertEqual(
            _triples(bundle),
            {("people/person.md", "companies/pos.md", "founded")},
        )

    def test_mixed_three_part_coordination_preserves_positive_relations(self):
        _, bundle = self.make_bundle()
        for company in ("Alpha", "Beta", "Gamma"):
            self.write(
                bundle,
                f"companies/{company.casefold()}.md",
                company,
                "company",
                "Company.",
            )
        self.write(
            bundle,
            "people/ada.md",
            "Ada Vale",
            "person",
            "Ada Vale founded [[Alpha]], did not advise [[Beta]], "
            "and funded [[Gamma]].",
        )

        self.assertEqual(
            _triples(bundle),
            {
                ("people/ada.md", "companies/alpha.md", "founded"),
                ("people/ada.md", "companies/gamma.md", "invested_in"),
            },
        )

    def test_object_pronoun_rejects_nearer_single_name_antecedent(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "people/cara.md", "Cara", "person", "Person.")
        self.write(bundle, "companies/acme.md", "Acme", "company", "Company.")
        self.write(
            bundle,
            "people/ivy.md",
            "Ivy North",
            "person",
            "Ivy North stood beside Cara. [[Acme]] employed her.",
        )

        self.assertNotIn(
            ("people/ivy.md", "companies/acme.md", "works_at"),
            _triples(bundle),
        )

    def test_relation_heading_bullet_members_inherit_relation(self):
        _, bundle = self.make_bundle()
        for person in ("Ari", "Bea"):
            self.write(
                bundle,
                f"people/{person.casefold()}.md",
                person,
                "person",
                "Person.",
            )
        self.write(
            bundle,
            "companies/roster.md",
            "Roster",
            "company",
            "Roster hired:\n- [[Ari]] as engineer\n- [[Bea]] as designer",
        )

        self.assertEqual(
            _triples(bundle),
            {
                ("people/ari.md", "companies/roster.md", "works_at"),
                ("people/bea.md", "companies/roster.md", "works_at"),
            },
        )

    def test_relation_heading_ordered_members_inherit_relation(self):
        _, bundle = self.make_bundle()
        for person in ("Ari", "Bea"):
            self.write(
                bundle,
                f"people/{person.casefold()}.md",
                person,
                "person",
                "Person.",
            )
        self.write(
            bundle,
            "companies/roster.md",
            "Roster",
            "company",
            "Roster hired:\n1. [[Ari]] as engineer\n2. [[Bea]] as designer",
        )

        self.assertEqual(
            _triples(bundle),
            {
                ("people/ari.md", "companies/roster.md", "works_at"),
                ("people/bea.md", "companies/roster.md", "works_at"),
            },
        )

    def test_relation_heading_does_not_bind_an_unrelated_bullet(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "people/ari.md", "Ari", "person", "Person.")
        self.write(
            bundle,
            "companies/roster.md",
            "Roster",
            "company",
            "Roster hired:\n- [[Ari]] declined the offer",
        )

        self.assertEqual(_triples(bundle), set())

    def test_commonmark_reference_link_extracts_relationship(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "companies/acme.md", "Acme", "company", "Company.")
        self.write(
            bundle,
            "people/ada.md",
            "Ada Vale",
            "person",
            "Ada Vale founded [Acme][company].\n\n[company]: /companies/acme",
        )

        self.assertIn(
            ("people/ada.md", "companies/acme.md", "founded"),
            _triples(bundle),
        )

    def test_noncurrent_source_does_not_supply_current_relationship(self):
        for lifecycle in ("deprecated", "superseded"):
            with self.subTest(lifecycle=lifecycle):
                _, bundle = self.make_bundle()
                self.write(bundle, "people/ada.md", "Ada", "person", "Person.")
                self.write(
                    bundle,
                    "companies/legacy.md",
                    "LegacyCo",
                    "company",
                    "LegacyCo hired [[Ada]].",
                    status="deprecated" if lifecycle == "deprecated" else "stable",
                )
                if lifecycle == "superseded":
                    self.write(
                        bundle,
                        "corrections/legacy.md",
                        "LegacyCo correction",
                        "note",
                        "The earlier page is no longer current.",
                        supersedes=["companies/legacy.md"],
                    )

                self.assert_hits(bundle, "Who works at LegacyCo?", [])
                recalled = [
                    note.path
                    for note, _score, _why, _evidence in recall_explain(
                        bundle,
                        None,
                        "Who works at LegacyCo?",
                        k=len(bundle.notes),
                        use_dynamics=False,
                        reactivate=False,
                    )
                ]
                self.assertNotIn("people/ada.md", recalled)

    def test_noncurrent_target_does_not_supply_current_relationship(self):
        for lifecycle in ("deprecated", "superseded"):
            with self.subTest(lifecycle=lifecycle):
                _, bundle = self.make_bundle()
                self.write(
                    bundle,
                    "people/old-ada.md",
                    "Old Ada",
                    "person",
                    "Person.",
                    status="deprecated" if lifecycle == "deprecated" else "stable",
                )
                self.write(
                    bundle,
                    "companies/acme.md",
                    "Acme",
                    "company",
                    "Acme hired [[Old Ada]].",
                )
                if lifecycle == "superseded":
                    self.write(
                        bundle,
                        "people/ada.md",
                        "Ada",
                        "person",
                        "Current person.",
                        supersedes=["people/old-ada.md"],
                    )

                self.assert_hits(bundle, "Who works at Acme?", [])
                self.assertEqual(_triples(bundle), set())

    def test_include_stale_restores_historical_graph_walk(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "people/ada.md", "Ada", "person", "Person.")
        self.write(
            bundle,
            "companies/legacy.md",
            "LegacyCo",
            "company",
            "LegacyCo hired [[Ada]].",
            status="deprecated",
        )

        self.assertNotIn("companies/legacy.md", merged_graph(bundle, None))
        historical = merged_graph(bundle, None, include_stale=True)
        self.assertIn(
            ("people/ada.md", 1.0, "link"),
            historical["companies/legacy.md"],
        )
        recalled = [
            note.path
            for note, _score, _why, _evidence in recall_explain(
                bundle,
                None,
                "LegacyCo",
                k=len(bundle.notes),
                use_dynamics=False,
                reactivate=False,
                include_stale=True,
            )
        ]
        self.assertIn("companies/legacy.md", recalled)

    def test_dynamics_superseded_source_cannot_supply_current_typed_answer(self):
        root, bundle = self.make_bundle()
        self.write(bundle, "people/ada.md", "Ada", "person", "Person.")
        self.write(
            bundle,
            "companies/acme.md",
            "Acme",
            "company",
            "Acme hired [[Ada]].",
        )
        self.write(
            bundle,
            "companies/acme-v2.md",
            "Acme correction",
            "company",
            "Current company record without an employment claim.",
        )
        relationship_edges(bundle)
        dynamics = Dynamics(root)
        dynamics.supersede("companies/acme.md", "companies/acme-v2.md")

        recalled = recall_explain(
            bundle,
            dynamics,
            "Who works at Acme?",
            k=len(bundle.notes),
            use_dynamics=True,
            reactivate=False,
        )

        self.assertNotIn("people/ada.md", [note.path for note, *_rest in recalled])
        self.assertFalse(any(evidence.relation for *_head, evidence in recalled))

    def test_include_stale_restores_historical_typed_answer(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "people/ada.md", "Ada", "person", "Person.")
        self.write(
            bundle,
            "companies/legacy.md",
            "LegacyCo",
            "company",
            "LegacyCo hired [[Ada]].",
            status="deprecated",
        )
        relationship_edges(bundle)

        historical = recall_explain(
            bundle,
            None,
            "Who works at LegacyCo?",
            k=len(bundle.notes),
            use_dynamics=True,
            reactivate=False,
            include_stale=True,
        )

        self.assertEqual(historical[0][0].path, "people/ada.md")
        self.assertEqual(historical[0][3].relation, "works_at")
        self.assertEqual(
            historical[0][3].relation_seed,
            "companies/legacy.md",
        )

    def test_shortcut_reference_link_extracts_and_reloads_relationship(self):
        root, bundle = self.make_bundle()
        self.write(
            bundle,
            "people/ada.md",
            "Ada",
            "person",
            "Ada founded [Acme].\n\n[Acme]: /companies/acme",
        )
        self.assertEqual(bundle.notes["people/ada.md"].links, [])

        self.write(bundle, "companies/acme.md", "Acme", "company", "Company.")

        self.assertEqual(
            bundle.notes["people/ada.md"].links,
            ["companies/acme.md"],
        )
        self.assertIn(
            ("people/ada.md", "companies/acme.md", "founded"),
            _triples(bundle),
        )
        reloaded = Bundle(root)
        self.assertEqual(
            reloaded.notes["people/ada.md"].links,
            ["companies/acme.md"],
        )
        self.assertEqual(_triples(reloaded), _triples(bundle))

    def test_balanced_parentheses_inline_link_extracts_relationship(self):
        _, bundle = self.make_bundle()
        self.write(
            bundle,
            "companies/acme_(labs).md",
            "Acme Labs",
            "company",
            "Company.",
        )
        self.write(
            bundle,
            "people/ada.md",
            "Ada",
            "person",
            "Ada founded [Acme Labs](../companies/acme_(labs).md).",
        )

        self.assertEqual(
            bundle.notes["people/ada.md"].links,
            ["companies/acme_(labs).md"],
        )
        self.assertIn(
            ("people/ada.md", "companies/acme_(labs).md", "founded"),
            _triples(bundle),
        )

    def test_reference_definitions_images_and_escapes_are_not_links(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "companies/acme.md", "Acme", "company", "Company.")
        self.write(
            bundle,
            "people/ada.md",
            "Ada",
            "person",
            "[Acme]: /companies/acme\n\n"
            "![Acme]\n\n"
            "![Company][Acme]\n\n"
            r"\[Acme]",
        )

        self.assertEqual(bundle.notes["people/ada.md"].links, [])
        self.assertEqual(_triples(bundle), set())

    def test_historical_relationship_cache_is_invalidated_after_write(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "people/ada.md", "Ada", "person", "Person.")
        self.write(
            bundle,
            "companies/legacy.md",
            "LegacyCo",
            "company",
            "LegacyCo hired [[Ada]].",
            status="deprecated",
        )
        self.assertEqual(
            len(relationship_edges(bundle, include_stale=True)),
            1,
        )

        self.write(
            bundle,
            "companies/legacy.md",
            "LegacyCo",
            "company",
            "The historical record contains no employment claim.",
            status="deprecated",
        )

        self.assertEqual(relationship_edges(bundle, include_stale=True), ())

    def test_negation_scopes_across_coordinated_relation_verbs(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "people/ada.md", "Ada", "person", "Person.")
        cases = (
            "Acme was not founded, funded, or advised by [[Ada]].",
            "Acme was never founded, funded, or advised by [[Ada]].",
            "Acme was neither founded, funded, nor advised by [[Ada]].",
        )
        for body in cases:
            with self.subTest(body=body):
                self.write(bundle, "companies/acme.md", "Acme", "company", body)
                self.assertEqual(_triples(bundle), set())

    def test_negation_scopes_across_coordinated_link_clauses(self):
        _, bundle = self.make_bundle()
        for path, title in (
            ("ada", "Ada"),
            ("grace", "Grace"),
            ("alan", "Alan"),
        ):
            self.write(bundle, f"people/{path}.md", title, "person", "Person.")
        self.write(
            bundle,
            "companies/acme.md",
            "Acme",
            "company",
            "Acme was not founded by [[Ada]], funded by [[Grace]], "
            "or advised by [[Alan]].",
        )

        self.assertEqual(_triples(bundle), set())

    def test_explicit_subject_ends_prior_clause_negation(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "people/ada.md", "Ada", "person", "Person.")
        self.write(bundle, "people/alan.md", "Alan", "person", "Person.")
        self.write(
            bundle,
            "companies/acme.md",
            "Acme",
            "company",
            "Acme was not founded by [[Ada]], and Acme was advised by [[Alan]].",
        )

        self.assertEqual(
            _triples(bundle),
            {("people/alan.md", "companies/acme.md", "advises")},
        )

    def test_denial_scopes_across_gapped_relation_clauses(self):
        _, bundle = self.make_bundle()
        for path, title in (("ada", "Ada"), ("grace", "Grace")):
            self.write(bundle, f"people/{path}.md", title, "person", "Person.")
        self.write(
            bundle,
            "companies/acme.md",
            "Acme",
            "company",
            "Acme denied being founded by [[Ada]] or funded by [[Grace]].",
        )

        self.assertEqual(_triples(bundle), set())

    def test_alternate_subject_ends_prior_clause_negation(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "people/ada.md", "Ada", "person", "Person.")
        self.write(bundle, "people/grace.md", "Grace", "person", "Person.")
        self.write(
            bundle,
            "companies/acme.md",
            "Acme",
            "company",
            "Acme was not founded by [[Ada]], and [[Grace]] funded Acme.",
        )

        self.assertEqual(
            _triples(bundle),
            {("people/grace.md", "companies/acme.md", "invested_in")},
        )

    def test_contrast_ends_coordinated_negation_scope(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "people/ada.md", "Ada", "person", "Person.")
        self.write(
            bundle,
            "companies/acme.md",
            "Acme",
            "company",
            "Acme was not founded, but funded and advised by [[Ada]].",
        )

        self.assertEqual(
            _triples(bundle),
            {
                ("people/ada.md", "companies/acme.md", "invested_in"),
                ("people/ada.md", "companies/acme.md", "advises"),
            },
        )

    def test_not_only_keeps_coordinated_relations_assertive(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "people/ada.md", "Ada", "person", "Person.")
        self.write(
            bundle,
            "companies/acme.md",
            "Acme",
            "company",
            "Acme was not only founded, but funded and advised by [[Ada]].",
        )

        self.assertEqual(
            _triples(bundle),
            {
                ("people/ada.md", "companies/acme.md", "founded"),
                ("people/ada.md", "companies/acme.md", "invested_in"),
                ("people/ada.md", "companies/acme.md", "advises"),
            },
        )

    def test_attribution_scopes_across_all_coordinated_clauses(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "people/ada.md", "Ada", "person", "Person.")
        self.write(bundle, "people/grace.md", "Grace", "person", "Person.")
        self.write(
            bundle,
            "companies/acme.md",
            "Acme",
            "company",
            "According to a filing, Acme was founded by [[Ada]], "
            "and funded by [[Grace]].",
        )

        self.assertEqual(_triples(bundle), set())
        self.assert_hits(bundle, "Who founded Acme?", [])
        self.assert_hits(bundle, "Who invested in Acme?", [])

    def test_unmatched_measurement_quote_does_not_mask_later_assertion(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "people/ada.md", "Ada", "person", "Person.")
        self.write(
            bundle,
            "companies/acme.md",
            "Acme",
            "company",
            'The original booth was 6" wide.\n\nAcme was founded by [[Ada]].',
        )

        self.assertIn(
            ("people/ada.md", "companies/acme.md", "founded"),
            _triples(bundle),
        )

    def test_unmatched_decade_apostrophe_does_not_mask_later_assertion(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "people/ada.md", "Ada", "person", "Person.")
        self.write(
            bundle,
            "companies/acme.md",
            "Acme",
            "company",
            "Acme grew through the '90s.\n\nAcme was founded by [[Ada]].",
        )

        self.assertIn(
            ("people/ada.md", "companies/acme.md", "founded"),
            _triples(bundle),
        )

    def test_company_note_person_first_role_forms_extract(self):
        _, bundle = self.make_bundle()
        for path, title in (
            ("ada", "Ada"),
            ("bea", "Bea"),
            ("vera", "Vera"),
        ):
            self.write(bundle, f"people/{path}.md", title, "person", "Person.")
        self.write(
            bundle,
            "companies/acme.md",
            "Acme",
            "company",
            "[[Ada]] is the CTO at Acme. "
            "[[Bea]] sat on Acme's advisory board. "
            "[[Vera]] led Acme's seed round.",
        )

        qualifiers = {
            (edge.subject, edge.relation): edge.qualifier
            for edge in relationship_edges(bundle)
        }
        self.assertEqual(qualifiers[("people/ada.md", "works_at")], "cto")
        self.assertEqual(
            qualifiers[("people/bea.md", "advises")],
            "advisory_board",
        )
        self.assertEqual(
            qualifiers[("people/vera.md", "invested_in")],
            "led_round",
        )

    def test_lazy_blockquote_continuation_does_not_assert_relationship(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "people/ada.md", "Ada", "person", "Person.")
        self.write(
            bundle,
            "companies/acme.md",
            "Acme",
            "company",
            "> An article reported:\nAcme was founded by [[Ada]].",
        )

        self.assertEqual(_triples(bundle), set())

    def test_additional_advisory_and_round_query_paraphrases(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "people/ada.md", "Ada", "person", "Person.")
        self.write(
            bundle,
            "companies/acme.md",
            "Acme",
            "company",
            "[[Ada]] served on Acme's advisory board. "
            "[[Ada]] led Acme's round.",
        )

        self.assert_hits(
            bundle,
            "Who serves on Acme's advisory board?",
            ["people/ada.md"],
        )
        self.assert_hits(
            bundle,
            "Who led Acme's round?",
            ["people/ada.md"],
        )

    def test_person_investment_heading_applies_to_list_members(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "companies/acme.md", "Acme", "company", "Company.")
        self.write(bundle, "companies/beta.md", "Beta", "company", "Company.")
        self.write(
            bundle,
            "people/ada.md",
            "Ada",
            "person",
            "Ada invested in:\n- [[Acme]]\n- [[Beta]]",
        )

        self.assertEqual(
            _triples(bundle),
            {
                ("people/ada.md", "companies/acme.md", "invested_in"),
                ("people/ada.md", "companies/beta.md", "invested_in"),
            },
        )

    def test_conditional_relation_statement_does_not_create_edge(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "companies/acme.md", "Acme", "company", "Company.")
        self.write(
            bundle,
            "people/ada.md",
            "Ada",
            "person",
            "If Ada founded [[Acme]], the archive should contain a filing.",
        )

        self.assertEqual(_triples(bundle), set())

    def test_in_bundle_file_symlink_does_not_duplicate_note_identity(self):
        root, bundle = self.make_bundle()
        self.write(
            bundle,
            "people/canonical.md",
            "Canonical",
            "person",
            "Person.",
        )
        os.symlink("canonical.md", os.path.join(root, "people", "alias.md"))
        self.write(
            bundle,
            "companies/acme.md",
            "Acme",
            "company",
            "Acme hired [[Canonical]].",
        )

        reloaded = Bundle(root)

        self.assertEqual(
            sorted(reloaded.notes),
            ["companies/acme.md", "people/canonical.md"],
        )
        self.assertEqual(
            reloaded.notes["companies/acme.md"].links,
            ["people/canonical.md"],
        )
        self.assertIn(
            ("people/canonical.md", "companies/acme.md", "works_at"),
            _triples(reloaded),
        )

    def test_three_role_modified_list_members_all_inherit_hiring_relation(self):
        _, bundle = self.make_bundle()
        for person in ("Ari", "Bea", "Cy"):
            self.write(
                bundle,
                f"people/{person.casefold()}.md",
                person,
                "person",
                "Person.",
            )
        self.write(
            bundle,
            "companies/roster.md",
            "Roster",
            "company",
            "Roster hired [[Ari]] as engineer, "
            "[[Bea]] as designer, and [[Cy]] as analyst.",
        )

        triples = _triples(bundle)
        for person in ("ari", "bea", "cy"):
            with self.subTest(person=person):
                self.assertIn(
                    (f"people/{person}.md", "companies/roster.md", "works_at"),
                    triples,
                )

    def test_parenthetical_role_list_members_inherit_hiring_relation(self):
        _, bundle = self.make_bundle()
        for person in ("Ari", "Bea", "Cy"):
            self.write(
                bundle,
                f"people/{person.casefold()}.md",
                person,
                "person",
                "Person.",
            )
        self.write(
            bundle,
            "companies/roster.md",
            "Roster",
            "company",
            "Roster hired [[Ari]] (engineering), [[Bea]] (design), "
            "and [[Cy]] (research).",
        )

        triples = _triples(bundle)
        for person in ("ari", "bea", "cy"):
            with self.subTest(person=person):
                self.assertIn(
                    (f"people/{person}.md", "companies/roster.md", "works_at"),
                    triples,
                )

    def test_merged_graph_uses_maximum_authored_weight_for_same_pair(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "notes/target.md", "Target", "note", "Target.")
        self.write(
            bundle,
            "notes/source.md",
            "Source",
            "note",
            "- related_to [[Target]] (ambiguous)\n\n"
            "The same note also links plainly to [Target](/notes/target).",
        )

        neighbors = {
            (target, kind): weight
            for target, weight, kind in merged_graph(bundle, None)["notes/source.md"]
        }
        self.assertEqual(neighbors[("notes/target.md", "link")], 1.0)

    def test_repeated_source_title_sentences_scale_better_than_quadratic(self):
        def elapsed_for(link_count: int) -> float:
            _, bundle = self.make_bundle()
            for index in range(link_count):
                self.write(
                    bundle,
                    f"people/person-{index:03d}.md",
                    f"Person {index:03d}",
                    "person",
                    "Person.",
                )
            body = " ".join(
                f"ScaleCo hired [[Person {index:03d}]]."
                for index in range(link_count)
            )
            self.write(bundle, "companies/scaleco.md", "ScaleCo", "company", body)

            start = time.perf_counter()
            edges = relationship_edges(bundle)
            elapsed = time.perf_counter() - start
            self.assertEqual(
                len(
                    [
                        edge
                        for edge in edges
                        if edge.object == "companies/scaleco.md"
                        and edge.relation == "works_at"
                    ]
                ),
                link_count,
            )
            return elapsed

        timings = {
            link_count: min(elapsed_for(link_count) for _ in range(2))
            for link_count in (100, 200, 400, 800)
        }

        self.assertLessEqual(
            timings[800] / timings[100],
            24.0,
            f"relationship_edges timings were {timings!r}",
        )

    def test_repeated_person_identity_sentences_scale_better_than_quadratic(self):
        def elapsed_for(link_count: int) -> float:
            _, bundle = self.make_bundle()
            body = " ".join(
                "Ada founded [Acme](../companies/acme.md)."
                for _ in range(link_count)
            )
            self.write(bundle, "people/ada.md", "Ada", "person", body)
            self.write(bundle, "companies/acme.md", "Acme", "company", "Company.")

            start = time.perf_counter()
            edges = relationship_edges(bundle)
            elapsed = time.perf_counter() - start
            self.assertEqual(
                _triples(bundle),
                {("people/ada.md", "companies/acme.md", "founded")},
            )
            self.assertEqual(len(edges), 1)
            return elapsed

        timings = {
            link_count: min(elapsed_for(link_count) for _ in range(3))
            for link_count in (200, 800)
        }
        self.assertLessEqual(
            timings[800] / timings[200],
            10.0,
            f"relationship_edges timings were {timings!r}",
        )

    def test_unresolved_links_do_not_repeat_sentence_classification(self):
        _, bundle = self.make_bundle()
        unresolved = " ".join(
            f"[[Missing {index:03d}]]" for index in range(500)
        )
        self.write(
            bundle,
            "people/ada.md",
            "Ada",
            "person",
            unresolved + " Ada founded [[Late Target]].",
        )
        self.write(
            bundle,
            "companies/late.md",
            "Late Target",
            "company",
            "Company.",
        )

        with mock.patch.object(
            relationship_module,
            "_link_labels",
            wraps=relationship_module._link_labels,
        ) as classify:
            self.assertEqual(
                _triples(bundle),
                {("people/ada.md", "companies/late.md", "founded")},
            )

        self.assertLessEqual(classify.call_count, 2)


if __name__ == "__main__":
    unittest.main()
