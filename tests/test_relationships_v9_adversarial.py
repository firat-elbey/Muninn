"""Regression gates added by independent candidate audits."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from muninn import relationships as relationship_module
from muninn import store
from muninn.activate import superseded_paths
from muninn.dynamics import Dynamics
from muninn.relationships import (
    parse_relationship_query,
    relationship_edges,
    relationship_hits,
)
from muninn.store import Bundle, Note, render_note
from muninn.volunteer import volunteer_pack


class _CountingText(str):
    """Count indexed and iterative character reads."""

    def __new__(cls, value: str):
        instance = super().__new__(cls, value)
        instance.reads = 0
        instance.integer_reads = 0
        instance.slice_reads = 0
        return instance

    def __contains__(self, value: object) -> bool:
        self.reads += len(self)
        return super().__contains__(value)

    def __getitem__(self, key):
        self.reads += 1
        if isinstance(key, int):
            self.integer_reads += 1
        else:
            self.slice_reads += 1
        return super().__getitem__(key)

    def __iter__(self):
        for index in range(len(self)):
            self.reads += 1
            self.integer_reads += 1
            yield super().__getitem__(index)


class TestCandidateAuditRegressions(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.bundle = Bundle(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(
        self,
        path: str,
        title: str,
        note_type: str,
        body: str,
        **meta: object,
    ) -> None:
        self.bundle.write_note(
            path,
            {"title": title, "type": note_type, **meta},
            body,
        )

    def edges(self) -> set[tuple[str, str, str, str]]:
        return {
            (edge.subject, edge.object, edge.relation, edge.qualifier)
            for edge in relationship_edges(self.bundle)
        }

    def test_questions_modals_and_trailing_attribution_are_not_evidence(self) -> None:
        self.write("companies/acme.md", "Acme Bank", "company", "Company.")
        self.write(
            "people/samir.md",
            "Samir Patel",
            "person",
            "Samir Patel invested in [[Acme Bank]], according to an "
            "unverified partner note. Did Samir Patel advise [[Acme Bank]]? "
            "Samir Patel may have founded [[Acme Bank]].",
        )

        self.assertEqual(self.edges(), set())

    def test_present_tense_attribution_is_not_evidence(self) -> None:
        self.write("companies/nacre.md", "Nacre Robotics", "company", "Company.")
        self.write(
            "people/casey.md",
            "Casey Voss",
            "person",
            "The board memo says Casey Voss founded [[Nacre Robotics]].",
        )

        self.assertEqual(self.edges(), set())
        self.assertEqual(
            relationship_hits(self.bundle, "Who founded Nacre Robotics?"),
            (),
        )

    def test_passive_attribution_is_not_evidence(self) -> None:
        self.write("companies/nacre.md", "Nacre Robotics", "company", "Company.")
        variants = (
            "It is reported that Casey Voss founded [[Nacre Robotics]].",
            "It is said that Casey Voss founded [[Nacre Robotics]].",
            "It is stated that Casey Voss founded [[Nacre Robotics]].",
            "It is written that Casey Voss founded [[Nacre Robotics]].",
            "It has been reported that Casey Voss founded [[Nacre Robotics]].",
            "It is believed that Casey Voss founded [[Nacre Robotics]].",
            (
                "It was reported in the filing that Casey Voss founded "
                "[[Nacre Robotics]]."
            ),
            "It was reported in 2019 that Casey Voss founded [[Nacre Robotics]].",
            "The filing indicates that Casey Voss founded [[Nacre Robotics]].",
            "The article asserts that Casey Voss founded [[Nacre Robotics]].",
            "The memo notes that Casey Voss founded [[Nacre Robotics]].",
        )
        for index, body in enumerate(variants):
            self.write(
                f"people/casey-{index}.md",
                "Casey Voss",
                "person",
                body,
            )
        self.assertEqual(self.edges(), set())

    def test_attributed_wrote_check_and_fact_check_prose_are_not_evidence(
        self,
    ) -> None:
        self.write("companies/acme.md", "Acme", "company", "Company.")
        variants = (
            "Reuters wrote that Dana wrote [[Acme]] a check.",
            "Reuters wrote in a fact-check that Dana wrote [[Acme]] a check.",
            "A fact-check said Dana founded [[Acme]].",
            "A Reuters fact-check said Dana founded [[Acme]].",
        )
        for body in variants:
            with self.subTest(body=body):
                self.write("people/dana.md", "Dana", "person", body)
                self.assertEqual(self.edges(), set())

        self.assertEqual(
            relationship_hits(self.bundle, "Who invested in Acme?"),
            (),
        )
        self.assertEqual(
            relationship_hits(self.bundle, "Who founded Acme?"),
            (),
        )

    def test_punctuated_passive_attribution_is_not_evidence(self) -> None:
        self.write("companies/beta.md", "Beta Works", "company", "Company.")
        self.write("companies/acme.md", "Acme", "company", "Company.")
        self.write(
            "people/dana.md",
            "Dana Fox",
            "person",
            "It was reported by Reuters, citing two sources, that Dana Fox "
            "founded [[Beta Works]].",
        )

        self.assertEqual(self.edges(), set())

        for body in (
            (
                "It was reported by Reuters, citing two sources,  \n"
                "that Dana Fox founded [[Beta Works]]."
            ),
            (
                "It was reported by Reuters,\n"
                "citing two sources, that Dana Fox founded [[Beta Works]]."
            ),
        ):
            with self.subTest(body=body):
                self.write(
                    "people/dana.md",
                    "Dana Fox",
                    "person",
                    body,
                )
                self.assertEqual(self.edges(), set())

        self.write(
            "people/dana.md",
            "Dana Fox",
            "person",
            "It was reported by Reuters, citing two sources, that Dana Fox "
            "did not found [[Beta Works]], but Dana Fox founded [[Acme]].",
        )
        self.assertEqual(
            self.edges(),
            {("people/dana.md", "companies/acme.md", "founded", "")},
        )

    def test_sentence_initial_hedges_scope_over_the_following_clause(self) -> None:
        self.write("companies/acme.md", "Acme", "company", "Company.")
        variants = (
            "Reportedly, Dana Fox founded [[Acme]].",
            "Allegedly, Dana Fox founded [[Acme]].",
            "Supposedly, Dana Fox founded [[Acme]].",
            "Purportedly, Dana Fox founded [[Acme]].",
            "If true, Dana Fox founded [[Acme]].",
            "Speculation aside, Dana Fox founded [[Acme]].",
            "It is disputed, and Dana Fox founded [[Acme]].",
        )
        for body in variants:
            with self.subTest(body=body):
                self.write("people/dana.md", "Dana Fox", "person", body)
                self.assertEqual(self.edges(), set())

        self.write("people/dana.md", "Dana Fox", "person", "Person.")
        self.write(
            "companies/acme.md",
            "Acme",
            "company",
            "Reportedly, Acme was founded by [[Dana Fox]].",
        )
        self.assertEqual(self.edges(), set())

    def test_markdown_leaders_do_not_bypass_nonassertive_gates(self) -> None:
        self.write("companies/acme.md", "Acme", "company", "Company.")
        variants = (
            "- According to a filing, Dana Fox founded [[Acme]].",
            "* It is said that Dana Fox founded [[Acme]].",
            "+ Had Dana Fox founded [[Acme]], the record would show it.",
            "## Suppose Dana Fox founded [[Acme]].",
            "**According to a filing,** Dana Fox founded [[Acme]].",
            "*Had Dana Fox founded [[Acme]], the record would show it.*",
        )
        for body in variants:
            with self.subTest(body=body):
                self.write("people/dana.md", "Dana Fox", "person", body)
                self.assertEqual(self.edges(), set())

    def test_soft_line_break_preserves_attribution_scope(self) -> None:
        self.write("companies/nacre.md", "Nacre Robotics", "company", "Company.")
        self.write(
            "people/casey.md",
            "Casey Voss",
            "person",
            "The board memo says\n"
            "Casey Voss founded [[Nacre Robotics]].",
        )
        self.write(
            "people/riley.md",
            "Riley Ames",
            "person",
            "Riley Ames\n"
            "founded [[Nacre Robotics]].",
        )
        self.write(
            "people/hard-break.md",
            "Hard Break",
            "person",
            "The board memo says  \n"
            "Hard Break founded [[Nacre Robotics]].",
        )
        self.assertEqual(
            self.edges(),
            {("people/riley.md", "companies/nacre.md", "founded", "")},
        )

    def test_hard_break_ends_completed_assertion_scope(self) -> None:
        self.write("companies/nacre.md", "Nacre Robotics", "company", "Company.")
        self.write("companies/acme.md", "Acme Bank", "company", "Company.")
        cases = {
            "second-attribution": (
                "Casey Voss advised [[Nacre Robotics]]  \n"
                "According to a filing, Casey Voss founded [[Acme Bank]]."
            ),
            "first-attribution": (
                "The board memo says Casey Voss founded [[Nacre Robotics]]  \n"
                "Casey Voss founded [[Acme Bank]]."
            ),
            "negation": (
                "Casey Voss did not found [[Nacre Robotics]]  \n"
                "Casey Voss founded [[Acme Bank]]."
            ),
            "modal": (
                "Casey Voss may have founded [[Nacre Robotics]]  \n"
                "Casey Voss founded [[Acme Bank]]."
            ),
        }
        for name, body in cases.items():
            self.write(f"people/{name}.md", "Casey Voss", "person", body)
        expected = {
            (
                "people/second-attribution.md",
                "companies/nacre.md",
                "advises",
                "",
            ),
            *(
                (f"people/{name}.md", "companies/acme.md", "founded", "")
                for name in ("first-attribution", "negation", "modal")
            ),
        }
        self.assertEqual(self.edges(), expected)

    def test_hard_break_preserves_an_incomplete_relation_scope(self) -> None:
        self.write("companies/lineco.md", "LineCo", "company", "Company.")
        self.write(
            "people/riley.md",
            "Riley Ames",
            "person",
            "Riley Ames founded  \n[[LineCo]].",
        )

        self.assertEqual(
            self.edges(),
            {("people/riley.md", "companies/lineco.md", "founded", "")},
        )
        self.assertEqual(
            [hit.path for hit in relationship_hits(self.bundle, "Who founded LineCo?")],
            ["people/riley.md"],
        )

    def test_inverted_counterfactual_is_not_evidence(self) -> None:
        self.write("companies/beta.md", "Beta Works", "company", "Company.")
        self.write(
            "people/dana.md",
            "Dana Fox",
            "person",
            "Had Dana Fox founded [[Beta Works]], the cap table would show it.",
        )
        self.assertEqual(self.edges(), set())
        self.assertEqual(relationship_hits(self.bundle, "Who founded Beta Works?"), ())

    def test_prefixed_inverted_counterfactual_is_not_evidence(self) -> None:
        self.write("companies/beta.md", "Beta Works", "company", "Company.")
        self.write(
            "people/dana.md",
            "Dana Fox",
            "person",
            "Even had Dana Fox founded [[Beta Works]], the cap table would show it.",
        )

        self.assertEqual(self.edges(), set())

    def test_linked_person_blocks_subject_pronoun_binding(self) -> None:
        self.write("people/cara.md", "Cara Moss", "person", "Person.")
        self.write("companies/northwind.md", "Northwind", "company", "Company.")
        self.write(
            "people/ivy.md",
            "Ivy North",
            "person",
            "Ivy North introduced [[Cara Moss]]. She advised [[Northwind]].",
        )
        self.assertEqual(self.edges(), set())

    def test_single_name_introduction_blocks_subject_pronoun_binding(self) -> None:
        self.write("people/cara.md", "Cara", "person", "Person.")
        self.write("companies/northwind.md", "Northwind", "company", "Company.")
        self.write(
            "people/ivy.md",
            "Ivy North",
            "person",
            "Ivy North introduced Cara. She advised [[Northwind]].",
        )

        self.assertEqual(self.edges(), set())

        self.write(
            "people/ivy.md",
            "Ivy North",
            "person",
            "Ivy North praised Cara. She advised [[Northwind]].",
        )
        self.assertEqual(self.edges(), set())

    def test_causal_subclauses_do_not_negate_the_preceding_assertion(self) -> None:
        self.write("companies/beta.md", "Beta Works", "company", "Company.")
        variants = (
            "Dana Fox founded [[Beta Works]] because she did not trust incumbents.",
            "Dana Fox founded [[Beta Works]] so she could advise another team.",
        )
        for body in variants:
            with self.subTest(body=body):
                self.write("people/dana.md", "Dana Fox", "person", body)
                self.assertEqual(
                    self.edges(),
                    {("people/dana.md", "companies/beta.md", "founded", "")},
                )

    def test_explicit_hypotheticals_are_not_relationship_evidence(self) -> None:
        self.write("companies/beta.md", "Beta Works", "company", "Company.")
        self.write(
            "people/dana.md",
            "Dana Fox",
            "person",
            "Suppose Dana Fox founded [[Beta Works]].",
        )

        self.assertEqual(self.edges(), set())

    def test_temporal_adverbs_preserve_pronoun_and_segment_relations(self) -> None:
        self.write("companies/acme.md", "Acme", "company", "Company.")
        self.write("companies/beta.md", "Beta", "company", "Company.")
        self.write(
            "people/alice.md",
            "Alice Smith",
            "person",
            "Alice Smith is an experienced investor. "
            "She also invested in [[Acme]]. "
            "Alice Smith founded [[Acme]] and later invested in [[Beta]].",
        )

        self.assertEqual(
            self.edges(),
            {
                ("people/alice.md", "companies/acme.md", "founded", ""),
                ("people/alice.md", "companies/acme.md", "invested_in", ""),
                ("people/alice.md", "companies/beta.md", "invested_in", ""),
            },
        )

    def test_subject_pronoun_analysis_scans_each_antecedent_once(self) -> None:
        self.write("companies/acme.md", "Acme", "company", "Company.")
        prior_links = " ".join("[[Acme]]" for _ in range(160))
        advised_links = ", ".join("[[Acme]]" for _ in range(160))
        body = f"Ivy North reviewed {prior_links}. She advised {advised_links}."
        self.write("people/ivy.md", "Ivy North", "person", body)
        original = relationship_module._all_markdown_links
        scanned = 0

        def counted(text: str):
            nonlocal scanned
            scanned += len(text)
            return original(text)

        with mock.patch.object(
            relationship_module,
            "_all_markdown_links",
            counted,
        ):
            relationship_edges(self.bundle)

        self.assertLess(scanned, len(body) * 20)

    def test_repeated_irrelevant_links_before_a_pronoun_scale_linearly(self) -> None:
        self.write("companies/acme.md", "Acme", "company", "Company.")
        body = (
            "Ivy North reviewed "
            + " ".join("[[Acme]]" for _ in range(1_600))
            + ". She advised [[Acme]]."
        )
        self.write("people/ivy.md", "Ivy North", "person", body)

        started = time.perf_counter()
        edges = relationship_edges(self.bundle)
        elapsed = time.perf_counter() - started

        self.assertEqual(
            [(edge.subject, edge.object, edge.relation) for edge in edges],
            [("people/ivy.md", "companies/acme.md", "advises")],
        )
        self.assertLess(elapsed, 2.0)

    def test_table_context_memory_is_linear_in_link_count(self) -> None:
        count = 800
        for index in range(count):
            self.write(
                f"people/person-{index:04d}.md",
                f"Person {index:04d}",
                "person",
                "Person.",
            )
        rows = "\n".join(
            f"| [[Person {index:04d}]] | attendee |"
            for index in range(count)
        )
        self.write(
            "meetings/summit.md",
            "Summit",
            "meeting",
            "| Name | Role |\n| --- | --- |\n" + rows,
        )
        original = relationship_module._infer_relations
        context_characters = 0

        def measured(source, target, context):
            nonlocal context_characters
            context_characters += len(context.local)
            return original(source, target, context)

        started = time.perf_counter()
        with mock.patch.object(
            relationship_module,
            "_infer_relations",
            measured,
        ):
            relationship_edges(self.bundle)
        elapsed = time.perf_counter() - started

        self.assertLessEqual(
            context_characters,
            count * (relationship_module._MAX_RELATION_CONTEXT_CHARS + 32),
        )
        self.assertLess(elapsed, 3.0)

    def test_adjacent_duplicate_endpoints_are_classified_once(self) -> None:
        self.write("companies/qorp.md", "Qorp", "company", "Company.")
        body = "Ari advised [[Qorp]] " + " ".join(
            "[[Qorp]]" for _ in range(4_000)
        )
        self.write("people/ari.md", "Ari", "person", body)

        original = relationship_module._infer_relations
        calls = 0

        def counted(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original(*args, **kwargs)

        started = time.perf_counter()
        with mock.patch.object(
            relationship_module,
            "_infer_relations",
            counted,
        ):
            edges = relationship_edges(self.bundle)
        elapsed = time.perf_counter() - started

        self.assertEqual(
            [(edge.subject, edge.object, edge.relation) for edge in edges],
            [("people/ari.md", "companies/qorp.md", "advises")],
        )
        self.assertEqual(calls, 1)
        self.assertLess(elapsed, 2.0)

    def test_repeated_endpoint_across_a_line_break_keeps_the_assertion(self) -> None:
        self.write("companies/acme.md", "Acme", "company", "Company.")
        variants = (
            "See [[Acme]]\n[[Acme]] employed Ada Lovelace.",
            "## [[Acme]]\n\n[[Acme]] employed Ada Lovelace.",
        )
        for body in variants:
            with self.subTest(body=body):
                self.write(
                    "people/ada.md",
                    "Ada Lovelace",
                    "person",
                    body,
                )
                self.assertEqual(
                    self.edges(),
                    {
                        (
                            "people/ada.md",
                            "companies/acme.md",
                            "works_at",
                            "",
                        )
                    },
                )

    def test_emphasis_does_not_hide_asserted_relationships(self) -> None:
        self.write("companies/acme.md", "Acme", "company", "Company.")
        variants = (
            "Ada Lovelace **founded** [[Acme]].",
            "**Ada Lovelace** founded [[Acme]].",
            "Ada Lovelace founded **[[Acme]]**.",
            "*Ada Lovelace founded [[Acme]].*",
        )
        for body in variants:
            with self.subTest(body=body):
                self.write(
                    "people/ada.md",
                    "Ada Lovelace",
                    "person",
                    body,
                )
                self.assertEqual(
                    self.edges(),
                    {
                        (
                            "people/ada.md",
                            "companies/acme.md",
                            "founded",
                            "",
                        )
                    },
                )

    def test_intraword_underscore_does_not_create_a_hedge(self) -> None:
        self.write("companies/acme.md", "Acme", "company", "Company.")
        self.write(
            "people/ada.md",
            "Ada Lovelace",
            "person",
            "Ada Lovelace founded [[Acme]] in an un_certain_ climate.",
        )

        self.assertEqual(
            self.edges(),
            {("people/ada.md", "companies/acme.md", "founded", "")},
        )

    def test_nested_bracket_link_scan_has_a_linear_read_bound(self) -> None:
        prefix = "[" * 2_000
        text = _CountingText(prefix + "[Acme](acme.md)" + "]" * 2_000)

        links = store._markdown_links(text)

        self.assertEqual(
            [(link.destination, link.label) for link in links],
            [("acme.md", "Acme")],
        )
        self.assertLess(text.reads, len(text) * 30)

    def test_malformed_image_link_scan_has_a_linear_read_bound(self) -> None:
        for fragment, suffix in (("![", ""), ("![x](", ""), ("![x](", ")")):
            with self.subTest(fragment=fragment, suffix=suffix):
                small = _CountingText(fragment * 1_000 + suffix)
                large = _CountingText(fragment * 2_000 + suffix)

                small_masked = store._mask_wikilink_metadata(small)
                large_masked = store._mask_wikilink_metadata(large)

                self.assertEqual(len(small_masked), len(small))
                self.assertEqual(len(large_masked), len(large))
                self.assertGreater(small.integer_reads, 0)
                self.assertLess(large.integer_reads, small.integer_reads * 3)
                self.assertLess(large.integer_reads, len(large) * 30)

    def test_unmatched_smart_quotes_have_a_linear_read_bound(self) -> None:
        text = _CountingText("“quote " * 2_000)

        masked = relationship_module._mask_direct_quotes(text)

        self.assertEqual(masked, text)
        self.assertLess(text.reads, len(text) * 20)

    def test_backslashes_do_not_escape_unicode_smart_quote_closers(self) -> None:
        self.write("companies/acme.md", "Acme", "company", "Company.")
        variants = (
            r"“Dana founded [[Acme]].\”",
            r"‘Dana founded [[Acme]].\’",
        )
        for body in variants:
            with self.subTest(body=body):
                self.write("people/dana.md", "Dana", "person", body)
                self.assertEqual(self.edges(), set())

    def test_note_preserves_the_original_positional_constructor(self) -> None:
        plain = ["companies/acme.md"]
        typed = [
            {
                "target": "companies/acme.md",
                "relation": "founded",
                "confidence_word": "extracted",
                "weight": 1.0,
            }
        ]

        note = Note(
            "people/ada.md",
            {"title": "Ada Lovelace", "type": "person"},
            "Body.",
            ["companies/acme.md"],
            typed,
            plain,
        )

        self.assertEqual(note.typed_links, typed)
        self.assertEqual(note.plain_links, plain)

        target = Note(
            "companies/acme.md",
            {"title": "Acme", "type": "company"},
            "Company.",
        )
        self.bundle.notes = {note.path: note, target.path: target}
        self.assertEqual(
            [
                (edge.subject, edge.object, edge.relation)
                for edge in relationship_edges(self.bundle)
            ],
            [("people/ada.md", "companies/acme.md", "founded")],
        )

    def test_empty_confidence_parentheses_are_not_a_typed_link(self) -> None:
        self.write("companies/acme.md", "Acme", "company", "Company.")
        self.write(
            "people/ada.md",
            "Ada Lovelace",
            "person",
            "- founded [[Acme]] ()",
        )

        note = self.bundle.notes["people/ada.md"]
        self.assertEqual(note.typed_links, [])
        self.assertEqual(note.plain_links, ["companies/acme.md"])

    def test_conditional_and_epistemic_leads_are_not_evidence(self) -> None:
        self.write("companies/acme.md", "Acme", "company", "Company.")
        variants = (
            "If the report is true, Dana Fox founded [[Acme]].",
            "Perhaps Dana Fox founded [[Acme]].",
            "Reportedly, in 2020, Dana Fox founded [[Acme]].",
        )
        for body in variants:
            with self.subTest(body=body):
                self.write("people/dana.md", "Dana Fox", "person", body)
                self.assertEqual(self.edges(), set())

    def test_purpose_clause_hedge_does_not_negate_a_completed_relation(
        self,
    ) -> None:
        self.write("companies/acme.md", "Acme", "company", "Company.")
        self.write(
            "people/dana.md",
            "Dana Fox",
            "person",
            "Dana Fox founded [[Acme]] to possibly reduce costs.",
        )

        self.assertEqual(
            self.edges(),
            {("people/dana.md", "companies/acme.md", "founded", "")},
        )

    def test_block_markers_precede_unpaired_emphasis_normalization(self) -> None:
        self.write("companies/acme.md", "Acme", "company", "Company.")
        for body in (
            "- _Dana works at [[Acme]]",
            "## _Dana works at [[Acme]]",
        ):
            with self.subTest(body=body):
                self.write("people/dana.md", "Dana", "person", body)
                self.assertEqual(
                    self.edges(),
                    {
                        (
                            "people/dana.md",
                            "companies/acme.md",
                            "works_at",
                            "",
                        )
                    },
                )

    def test_emphasis_wrapped_block_markers_do_not_bypass_assertion_gates(
        self,
    ) -> None:
        self.write("companies/acme.md", "Acme", "company", "Company.")
        variants = (
            "*- Reuters reported that Dana Fox founded [[Acme]].",
            "*_Reuters reported that Dana Fox founded [[Acme]].",
            "**- It is rumoured that Dana Fox founded [[Acme]].**",
            "*- Had Dana Fox worked at [[Acme]], she would have known.*",
            "*_Had Dana Fox worked at [[Acme]], she would have known.",
            "_## Reuters reported that Dana Fox founded [[Acme]]._",
        )
        for body in variants:
            with self.subTest(body=body):
                self.write("people/dana.md", "Dana Fox", "person", body)
                self.assertEqual(self.edges(), set())

    def test_alternating_prefix_normalization_has_a_linear_read_bound(self) -> None:
        small = _CountingText("*- " * 1_000 + "Reuters reported the claim.")
        large = _CountingText("*- " * 2_000 + "Reuters reported the claim.")

        small_result = relationship_module._strip_leading_markdown_prose_syntax(
            small
        )
        large_result = relationship_module._strip_leading_markdown_prose_syntax(
            large
        )

        self.assertEqual(small_result, "Reuters reported the claim.")
        self.assertEqual(large_result, "Reuters reported the claim.")
        self.assertGreater(small.integer_reads, 0)
        self.assertLess(large.integer_reads, small.integer_reads * 3)
        self.assertLess(large.integer_reads, len(large) * 12)

    def test_repeated_block_markers_have_a_linear_read_bound(self) -> None:
        small = _CountingText("- " * 1_000 + "Dana founded Acme.")
        large = _CountingText("- " * 2_000 + "Dana founded Acme.")

        small_result = relationship_module._strip_leading_markdown_block_markers(
            small
        )
        large_result = relationship_module._strip_leading_markdown_block_markers(
            large
        )

        self.assertEqual(small_result, "Dana founded Acme.")
        self.assertEqual(large_result, "Dana founded Acme.")
        self.assertGreater(small.integer_reads, 0)
        self.assertLess(large.integer_reads, small.integer_reads * 3)
        self.assertLess(large.integer_reads, len(large) * 12)

    def test_classification_prose_does_not_repeat_full_emphasis_scans(self) -> None:
        original = relationship_module._strip_emphasis_delimiters
        with mock.patch.object(
            relationship_module,
            "_strip_emphasis_delimiters",
            wraps=original,
        ) as strip_emphasis:
            result = relationship_module._classification_prose(
                "*- " * 1_000 + "Reuters reported the claim."
            )

        self.assertEqual(result, "Reuters reported the claim.")
        self.assertEqual(strip_emphasis.call_count, 1)

    def test_mixed_emphasis_normalization_preserves_nonmarker_whitespace(self) -> None:
        self.assertEqual(
            relationship_module._classification_prose(
                " _*_\t\tDana founded Acme."
            ),
            " \t\tDana founded Acme.",
        )

    def test_colon_labeled_pronoun_binds_to_the_source(self) -> None:
        self.write("companies/acme.md", "Acme", "company", "Company.")
        self.write(
            "people/dana.md",
            "Dana Fox",
            "person",
            "Dana Fox: she founded [[Acme]].",
        )

        self.assertEqual(
            self.edges(),
            {("people/dana.md", "companies/acme.md", "founded", "")},
        )

    def test_semicolon_ends_attribution_before_an_independent_clause(self) -> None:
        self.write("companies/rumor.md", "Rumor", "company", "Company.")
        self.write("companies/real.md", "Real", "company", "Company.")
        self.write(
            "people/dana.md",
            "Dana Fox",
            "person",
            "A newspaper said Dana Fox founded [[Rumor]]; "
            "Dana Fox founded [[Real]].",
        )

        self.assertEqual(
            self.edges(),
            {("people/dana.md", "companies/real.md", "founded", "")},
        )

    def test_invisible_comments_do_not_change_bounded_context_semantics(self) -> None:
        self.write("companies/acme.md", "Acme", "company", "Company.")
        self.write(
            "people/dana.md",
            "Dana Fox",
            "person",
            "Dana Fox founded <!--" + ("x" * 488) + "--> [[Acme]].",
        )
        self.assertEqual(
            self.edges(),
            {("people/dana.md", "companies/acme.md", "founded", "")},
        )

        self.write(
            "people/dana.md",
            "Dana Fox",
            "person",
            "Allegedly <!--" + ("x" * 480) + "--> Dana Fox founded [[Acme]].",
        )
        self.assertEqual(self.edges(), set())

    def test_then_departure_marks_hiring_evidence_as_past(self) -> None:
        self.write("companies/acme.md", "Acme", "company", "Company.")
        self.write(
            "people/dana.md",
            "Dana Fox",
            "person",
            "Dana Fox was hired by [[Acme]] in 2021, then left in 2022.",
        )

        edge = relationship_edges(self.bundle)[0]
        self.assertEqual(edge.temporal, "past")
        self.assertEqual(
            relationship_hits(self.bundle, "Where does Dana Fox work?"),
            (),
        )
        self.assertEqual(
            [
                hit.path
                for hit in relationship_hits(
                    self.bundle,
                    "Where did Dana Fox work?",
                )
            ],
            ["companies/acme.md"],
        )

    def test_query_vocabulary_has_matching_assertion_vocabulary(self) -> None:
        cases = (
            (
                "company",
                "Dana Fox co-created [[Target]].",
                "Who co-created Target?",
                "founded",
            ),
            (
                "company",
                "Dana Fox wrote [[Target]] a check.",
                "Who wrote Target a check?",
                "invested_in",
            ),
            (
                "company",
                "Dana Fox gave [[Target]] strategic direction.",
                "Who gave Target strategic direction?",
                "advises",
            ),
            (
                "company",
                "Dana Fox has worked at [[Target]].",
                "Where has Dana Fox worked?",
                "works_at",
            ),
            (
                "meeting",
                "Dana Fox was present at [[Target]].",
                "Who was present at Target?",
                "attended",
            ),
        )
        for target_type, body, cue, relation in cases:
            with (
                self.subTest(body=body),
                tempfile.TemporaryDirectory() as root,
            ):
                bundle = Bundle(root)
                bundle.write_note(
                    "targets/target.md",
                    {"title": "Target", "type": target_type},
                    "Target.",
                )
                bundle.write_note(
                    "people/dana.md",
                    {"title": "Dana Fox", "type": "person"},
                    body,
                )
                edges = relationship_edges(bundle)
                self.assertEqual(
                    [(edge.relation, edge.object) for edge in edges],
                    [(relation, "targets/target.md")],
                )
                self.assertEqual(
                    [hit.path for hit in relationship_hits(bundle, cue)],
                    [
                        "targets/target.md"
                        if cue.startswith("Where")
                        else "people/dana.md"
                    ],
                )

    def test_location_name_does_not_block_a_subject_pronoun(self) -> None:
        self.write("companies/acme.md", "Acme", "company", "Company.")
        self.write(
            "people/dana.md",
            "Dana Fox",
            "person",
            "Dana Fox moved to London. She founded [[Acme]].",
        )

        self.assertEqual(
            self.edges(),
            {("people/dana.md", "companies/acme.md", "founded", "")},
        )

    def test_source_identity_pattern_is_built_once_per_note(self) -> None:
        self.write("companies/acme.md", "Acme", "company", "Company.")
        body = " ".join("Dana founded [[Acme]]." for _ in range(100))
        self.write(
            "people/dana.md",
            "Dana",
            "person",
            body,
            aliases=[f"Dana Alias {index:03d}" for index in range(100)],
        )
        original = relationship_module._build_source_identity_pattern
        calls = 0

        def counted(note):
            nonlocal calls
            calls += 1
            return original(note)

        with mock.patch.object(
            relationship_module,
            "_build_source_identity_pattern",
            counted,
        ):
            relationship_edges(self.bundle)

        self.assertEqual(calls, 1)

    def test_relationship_cache_invalidation_clears_note_identity_patterns(
        self,
    ) -> None:
        self.write("companies/acme.md", "Acme", "company", "Company.")
        self.write(
            "people/founder.md",
            "Dana",
            "person",
            "Dana founded [[Acme]].",
        )
        self.assertEqual(
            self.edges(),
            {("people/founder.md", "companies/acme.md", "founded", "")},
        )

        founder = self.bundle.notes["people/founder.md"]
        founder.meta["title"] = "Eve"
        founder.body = "Eve founded [[Acme]]."
        self.bundle._invalidate_relationship_caches()

        self.assertEqual(
            self.edges(),
            {("people/founder.md", "companies/acme.md", "founded", "")},
        )

    def test_nearest_marked_segment_uses_logarithmic_index_reads(self) -> None:
        class CountingList(list):
            def __init__(self, values):
                super().__init__(values)
                self.reads = 0

            def __getitem__(self, index):
                self.reads += 1
                return super().__getitem__(index)

        marked = CountingList(range(0, 200_000, 2))

        self.assertEqual(
            relationship_module._nearest_marked_segment(marked, 99_999),
            99_998,
        )
        self.assertLess(marked.reads, 50)

    def test_current_employment_query_excludes_explicit_past_employment(self) -> None:
        self.write("companies/old.md", "OldCo", "company", "Company.")
        self.write("companies/new.md", "NewCo", "company", "Company.")
        self.write(
            "people/eric.md",
            "Eric Vale",
            "person",
            "Eric Vale worked at [[OldCo]]. Eric Vale works at [[NewCo]].",
        )
        self.assertEqual(
            [
                hit.path
                for hit in relationship_hits(
                    self.bundle,
                    "Where has Eric Vale worked?",
                )
            ],
            ["companies/old.md", "companies/new.md"],
        )
        self.assertEqual(
            [
                hit.path
                for hit in relationship_hits(
                    self.bundle,
                    "Where does Eric Vale work?",
                )
            ],
            ["companies/new.md"],
        )

    def test_past_perfect_employment_is_not_current(self) -> None:
        self.write("companies/acme.md", "Acme", "company", "Company.")
        self.write(
            "people/dana.md",
            "Dana",
            "person",
            "Dana had worked at [[Acme]].",
        )

        edge = relationship_edges(self.bundle)[0]
        self.assertEqual(edge.temporal, "past")
        self.assertEqual(
            relationship_hits(self.bundle, "Where does Dana work?"),
            (),
        )
        self.assertEqual(
            [
                hit.path
                for hit in relationship_hits(
                    self.bundle,
                    "Where did Dana work?",
                )
            ],
            ["companies/acme.md"],
        )

    def test_typed_employment_does_not_hide_inferred_past_evidence(self) -> None:
        self.write("companies/acme.md", "Acme", "company", "Company.")
        self.write(
            "people/dana.md",
            "Dana",
            "person",
            "- works_at [[Acme]] (high)\n\nDana worked at [[Acme]].",
        )

        self.assertEqual(
            {
                edge.temporal
                for edge in relationship_edges(self.bundle)
                if edge.relation == "works_at"
            },
            {"past"},
        )
        self.assertEqual(
            relationship_hits(self.bundle, "Where does Dana work?"),
            (),
        )
        self.assertEqual(
            [
                hit.path
                for hit in relationship_hits(
                    self.bundle,
                    "Where did Dana work?",
                )
            ],
            ["companies/acme.md"],
        )

    def test_current_inbound_employment_query_excludes_former_employees(self) -> None:
        self.write("companies/old.md", "OldCo", "company", "Company.")
        self.write("people/alice.md", "Alice Vale", "person", "Person.")
        self.write("people/bob.md", "Bob Stone", "person", "Person.")
        self.write(
            "companies/old.md",
            "OldCo",
            "company",
            "OldCo employed [[Alice Vale]]. OldCo employs [[Bob Stone]].",
        )

        self.assertEqual(
            [hit.path for hit in relationship_hits(self.bundle, "Who does OldCo employ?")],
            ["people/bob.md"],
        )
        self.assertEqual(
            [hit.path for hit in relationship_hits(self.bundle, "Who did OldCo employ?")],
            ["people/alice.md", "people/bob.md"],
        )

    def test_current_executive_query_excludes_former_officeholders(self) -> None:
        self.write("companies/acme.md", "Acme", "company", "Company.")
        self.write("people/alice.md", "Alice Vale", "person", "Person.")
        self.write("people/bob.md", "Bob Stone", "person", "Person.")
        self.write(
            "companies/acme.md",
            "Acme",
            "company",
            "Acme's CEO was [[Alice Vale]]. Acme's CEO is [[Bob Stone]].",
        )

        self.assertEqual(
            [
                hit.path
                for hit in relationship_hits(self.bundle, "Who is the CEO of Acme?")
            ],
            ["people/bob.md"],
        )
        self.assertEqual(
            [
                hit.path
                for hit in relationship_hits(self.bundle, "Who was the CEO of Acme?")
            ],
            ["people/alice.md"],
        )
    def test_past_employment_queries_exclude_explicit_current_edges(self) -> None:
        self.write("companies/acme.md", "Acme", "company", "Company.")
        self.write("companies/beta.md", "Beta", "company", "Company.")
        self.write(
            "people/alice.md",
            "Alice Vale",
            "person",
            "Alice Vale works at [[Acme]]. Alice Vale worked at [[Beta]].",
        )
        self.write(
            "people/bob.md",
            "Bob Stone",
            "person",
            "Bob Stone works at [[Acme]].",
        )
        self.write(
            "people/cara.md",
            "Cara Moss",
            "person",
            "Cara Moss worked at [[Acme]].",
        )

        self.assertEqual(
            [
                hit.path
                for hit in relationship_hits(
                    self.bundle,
                    "Where was Alice Vale employed?",
                )
            ],
            ["companies/beta.md"],
        )
        self.assertEqual(
            [
                hit.path
                for hit in relationship_hits(
                    self.bundle,
                    "Who used to work at Acme?",
                )
            ],
            ["people/cara.md"],
        )

        for cue in (
            "Who currently works at Acme?",
            "Who presently works at Acme?",
            "Name the people who work at Acme.",
            "Who is on Acme's staff?",
        ):
            with self.subTest(cue=cue):
                self.assertEqual(
                    [hit.path for hit in relationship_hits(self.bundle, cue)],
                    ["people/alice.md", "people/bob.md"],
                )

    def test_past_only_queries_require_explicit_past_evidence(self) -> None:
        self.write("companies/acme.md", "Acme", "company", "Company.")
        self.write(
            "people/bob.md",
            "Bob Stone",
            "person",
            "Bob Stone joined [[Acme]] in 2024.",
        )

        self.assertEqual(
            relationship_hits(self.bundle, "Who used to work at Acme?"),
            (),
        )
        self.assertEqual(
            [
                hit.path
                for hit in relationship_hits(
                    self.bundle,
                    "Who currently works at Acme?",
                )
            ],
            ["people/bob.md"],
        )

    def test_past_progressive_query_is_not_classified_as_current(self) -> None:
        self.write("companies/acme.md", "Acme", "company", "Company.")
        self.write(
            "people/alice.md",
            "Alice Vale",
            "person",
            "Alice Vale worked at [[Acme]].",
        )
        self.write(
            "people/bob.md",
            "Bob Stone",
            "person",
            "Bob Stone works at [[Acme]].",
        )

        request = relationship_module.parse_relationship_query(
            self.bundle,
            "Who was working at Acme?",
        )
        self.assertIsNotNone(request)
        self.assertEqual(request.temporal, "past")
        self.assertEqual(
            [
                hit.path
                for hit in relationship_hits(
                    self.bundle,
                    "Who was working at Acme?",
                )
            ],
            ["people/alice.md"],
        )
        self.assertEqual(
            relationship_module.parse_relationship_query(
                self.bundle,
                "Where was Alice Vale working?",
            ).temporal,
            "past",
        )
        self.assertEqual(
            relationship_module.parse_relationship_query(
                self.bundle,
                "Where is Bob Stone working?",
            ).temporal,
            "current",
        )

    def test_source_carries_into_coordinated_joined_clause(self) -> None:
        self.write("companies/new.md", "NewCo", "company", "Company.")
        self.write("companies/other.md", "OtherCo", "company", "Company.")
        self.write(
            "people/eric.md",
            "Eric Vale",
            "person",
            "Eric Vale left [[NewCo]] in 2022 and joined [[OtherCo]].",
        )

        self.assertEqual(
            self.edges(),
            {("people/eric.md", "companies/other.md", "works_at", "")},
        )

        self.write(
            "people/eric.md",
            "Eric Vale",
            "person",
            "Eric Vale said Dana left a job and joined [[OtherCo]].",
        )
        self.assertEqual(self.edges(), set())

    def test_employment_tense_ignores_an_unrelated_clause(self) -> None:
        self.write("companies/old.md", "OldCo", "company", "Company.")
        self.write(
            "people/alice.md",
            "Alice Vale",
            "person",
            "Alice Vale worked at [[OldCo]] because it is the oldest firm in town.",
        )

        edges = relationship_edges(self.bundle)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].temporal, "past")
        self.assertEqual(
            relationship_hits(self.bundle, "Where does Alice Vale work?"),
            (),
        )

    def test_hiring_event_does_not_imply_that_employment_ended(self) -> None:
        self.write("companies/new.md", "NewCo", "company", "Company.")
        self.write(
            "people/eric.md",
            "Eric Vale",
            "person",
            "Eric Vale was hired by [[NewCo]] in 2021.",
        )

        edges = relationship_edges(self.bundle)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].temporal, "")
        self.assertEqual(
            [
                hit.path
                for hit in relationship_hits(self.bundle, "Where does Eric Vale work?")
            ],
            ["companies/new.md"],
        )

    def test_explicit_departure_marks_a_hiring_record_as_past(self) -> None:
        self.write("companies/new.md", "NewCo", "company", "Company.")
        self.write(
            "people/eric.md",
            "Eric Vale",
            "person",
            "Eric Vale was hired by [[NewCo]] in 2021 and left in 2022.",
        )

        edges = relationship_edges(self.bundle)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].temporal, "past")
        self.assertEqual(
            relationship_hits(self.bundle, "Where does Eric Vale work?"),
            (),
        )
        self.assertEqual(
            [
                hit.path
                for hit in relationship_hits(
                    self.bundle,
                    "Where has Eric Vale worked?",
                )
            ],
            ["companies/new.md"],
        )

        self.write(
            "people/eric.md",
            "Eric Vale",
            "person",
            "Eric Vale was hired by [[NewCo]] in 2021 and "
            "left his apartment in 2022.",
        )
        edges = relationship_edges(self.bundle)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].temporal, "")

    def test_departure_qualifiers_mark_a_hiring_record_as_past(self) -> None:
        self.write("companies/acme.md", "Acme", "company", "Company.")
        variants = (
            (
                "Dana was hired by [[Acme]] in 2021, then left the company "
                "after a dispute."
            ),
            (
                "Dana was hired by [[Acme]] in 2021, then left after the "
                "acquisition."
            ),
            "Dana was hired by [[Acme]] in 2021, then left in late 2022.",
        )
        for body in variants:
            with self.subTest(body=body):
                self.write("people/dana.md", "Dana", "person", body)
                edges = relationship_edges(self.bundle)
                self.assertEqual(len(edges), 1)
                self.assertEqual(edges[0].temporal, "past")
                self.assertEqual(
                    relationship_hits(self.bundle, "Where does Dana work?"),
                    (),
                )

    def test_unrelated_trip_departure_does_not_end_employment(self) -> None:
        self.write("companies/acme.md", "Acme", "company", "Company.")
        self.write(
            "people/alice.md",
            "Alice Vale",
            "person",
            "Alice Vale works at [[Acme]] and left on a trip in 2022.",
        )

        edges = relationship_edges(self.bundle)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].temporal, "current")
        self.assertEqual(
            [
                hit.path
                for hit in relationship_hits(
                    self.bundle,
                    "Where does Alice Vale work?",
                )
            ],
            ["companies/acme.md"],
        )

    def test_outbound_employment_queries_accept_temporal_adverbs(self) -> None:
        self.write("companies/acme.md", "Acme", "company", "Company.")
        self.write("companies/beta.md", "Beta", "company", "Company.")
        self.write(
            "people/alice.md",
            "Alice Vale",
            "person",
            "Alice Vale works at [[Acme]]. Alice Vale worked at [[Beta]].",
        )

        cases = {
            "Where does Alice Vale currently work?": ["companies/acme.md"],
            "Where is Alice Vale presently employed?": ["companies/acme.md"],
            "Where did Alice Vale previously work?": ["companies/beta.md"],
            "Where was Alice Vale formerly employed?": ["companies/beta.md"],
        }
        for cue, expected in cases.items():
            with self.subTest(cue=cue):
                self.assertEqual(
                    [hit.path for hit in relationship_hits(self.bundle, cue)],
                    expected,
                )

    def test_relation_heading_lists_do_not_rescan_the_remaining_tail(self) -> None:
        count = 2_000
        text = "Acme's founders include:\n" + "".join(
            f"- [[Person {index}]]\n" for index in range(count)
        )

        class CountingList(list):
            sliced_items = 0

            def __getitem__(self, key):
                if isinstance(key, slice):
                    self.sliced_items += len(range(*key.indices(len(self))))
                return super().__getitem__(key)

        occurrences = CountingList(
            (match.start(), match.end(), f"people/{index}.md")
            for index, match in enumerate(
                relationship_module._wikilink_matches(text)
            )
        )
        self.write("companies/acme.md", "Acme", "company", "Company.")

        contexts = relationship_module._relation_heading_list_contexts(
            text,
            occurrences,
            self.bundle.notes["companies/acme.md"],
        )

        self.assertEqual(len(contexts), count)
        self.assertLessEqual(occurrences.sliced_items, count)

    def test_employment_adverbs_and_coordinated_tenses_are_extracted(self) -> None:
        for company in ("Current", "Old", "Former", "Legacy", "Next"):
            self.write(
                f"companies/{company.casefold()}.md",
                company,
                "company",
                "Company.",
            )
        self.write(
            "people/alice.md",
            "Alice Vale",
            "person",
            "Alice Vale currently works at [[Current]]. "
            "Alice Vale previously worked at [[Old]]. "
            "Alice Vale formerly worked at [[Former]]. "
            "Alice Vale used to work at [[Legacy]]. "
            "Alice Vale worked at [[Old]] and works at [[Next]].",
        )

        temporal = {
            edge.object: edge.temporal
            for edge in relationship_edges(self.bundle)
            if edge.subject == "people/alice.md"
        }
        self.assertEqual(
            temporal,
            {
                "companies/current.md": "current",
                "companies/old.md": "past",
                "companies/former.md": "past",
                "companies/legacy.md": "past",
                "companies/next.md": "current",
            },
        )

    def test_backslash_hard_break_preserves_an_incomplete_relation(self) -> None:
        self.write("companies/lineco.md", "LineCo", "company", "Company.")
        self.write(
            "people/riley.md",
            "Riley Ames",
            "person",
            "Riley Ames founded \\\n[[LineCo]].",
        )

        self.assertEqual(
            self.edges(),
            {("people/riley.md", "companies/lineco.md", "founded", "")},
        )

    def test_contrast_clause_pronoun_preserves_a_new_assertion(self) -> None:
        self.write("companies/cinder.md", "Cinder API", "company", "Company.")
        self.write(
            "people/arden.md",
            "Arden Vale",
            "person",
            "Arden Vale did not found [[Cinder API]], but she advised "
            "[[Cinder API]].",
        )

        self.assertEqual(
            self.edges(),
            {("people/arden.md", "companies/cinder.md", "advises", "")},
        )

    def test_contrast_clause_pronoun_rejects_an_alternate_person(self) -> None:
        self.write("companies/cinder.md", "Cinder API", "company", "Company.")
        self.write("people/casey.md", "Casey Voss", "person", "Person.")
        self.write(
            "people/arden.md",
            "Arden Vale",
            "person",
            "Arden Vale introduced [[Casey Voss]], but she advised "
            "[[Cinder API]].",
        )

        self.assertEqual(self.edges(), set())

    def test_bounded_subject_pronoun_requires_an_uncontested_antecedent(self) -> None:
        self.write("companies/northwind.md", "Northwind", "company", "Company.")
        self.write(
            "people/leila.md",
            "Leila Rao",
            "person",
            "Leila Rao is an experienced operator. She advised [[Northwind]].",
        )
        self.write(
            "people/ivy.md",
            "Ivy North",
            "person",
            "Ivy North introduced Cara Moss. She advised [[Northwind]].",
        )

        self.assertIn(
            ("people/leila.md", "companies/northwind.md", "advises", ""),
            self.edges(),
        )
        self.assertNotIn(
            ("people/ivy.md", "companies/northwind.md", "advises", ""),
            self.edges(),
        )

    def test_object_centered_headings_create_qualified_edges(self) -> None:
        self.write("people/mira.md", "Mira Soto", "person", "Person.")
        self.write("people/nia.md", "Nia Vale", "person", "Person.")
        self.write(
            "companies/quill.md",
            "Quill Labs",
            "company",
            "Current advisers to Quill Labs:\n- [[Mira Soto]]",
        )
        self.write(
            "companies/round.md",
            "Round Works",
            "company",
            "Lead investors in Round Works' seed round:\n- [[Nia Vale]]",
        )

        self.assertIn(
            ("people/mira.md", "companies/quill.md", "advises", ""),
            self.edges(),
        )
        self.assertIn(
            ("people/nia.md", "companies/round.md", "invested_in", "led_round"),
            self.edges(),
        )

    def test_qualified_role_word_orders_are_supported(self) -> None:
        for path, title in (
            ("mira", "Mira Soto"),
            ("nia", "Nia Vale"),
            ("omar", "Omar Reed"),
            ("zoe", "Zoe Park"),
        ):
            self.write(f"people/{path}.md", title, "person", "Person.")
        self.write(
            "companies/role.md",
            "Role Co",
            "company",
            "The president of Role Co is [[Mira Soto]].",
        )
        self.write("companies/board.md", "Board Co", "company", "Company.")
        self.write(
            "people/nia.md",
            "Nia Vale",
            "person",
            "Nia Vale is a member of [[Board Co]]' advisory board.",
        )
        self.write("companies/round-a.md", "Round A", "company", "Company.")
        self.write(
            "people/omar.md",
            "Omar Reed",
            "person",
            "Omar Reed led the seed round for [[Round A]].",
        )
        self.write(
            "companies/round-b.md",
            "Round B",
            "company",
            "The seed round for Round B was led by [[Zoe Park]].",
        )

        expected = {
            ("people/mira.md", "companies/role.md", "works_at", "president"),
            ("people/nia.md", "companies/board.md", "advises", "advisory_board"),
            ("people/omar.md", "companies/round-a.md", "invested_in", "led_round"),
            ("people/zoe.md", "companies/round-b.md", "invested_in", "led_round"),
        }
        self.assertTrue(expected.issubset(self.edges()))

    def test_coordination_is_endpoint_anchored(self) -> None:
        self.write("companies/acme.md", "Acme", "company", "Company.")
        self.write(
            "people/ada.md",
            "Ada Lovelace",
            "person",
            "Ada Lovelace left the company she founded and later joined [[Acme]].",
        )
        self.write(
            "people/grace.md",
            "Grace Hopper",
            "person",
            "Grace Hopper founded but did not advise [[Acme]].",
        )

        edges = self.edges()
        self.assertFalse(any(edge[0] == "people/ada.md" for edge in edges))
        self.assertIn(
            ("people/grace.md", "companies/acme.md", "founded", ""),
            edges,
        )
        self.assertNotIn(
            ("people/grace.md", "companies/acme.md", "advises", ""),
            edges,
        )

    def test_current_identity_outranks_a_superseded_duplicate(self) -> None:
        self.write("companies/acme-old.md", "Acme", "company", "Old.")
        self.write(
            "companies/acme.md",
            "Acme",
            "company",
            "Current.",
            supersedes=["companies/acme-old.md"],
        )
        self.write(
            "people/ivy.md",
            "Ivy",
            "person",
            "Ivy works at [[Acme]].",
        )

        request = parse_relationship_query(self.bundle, "Who works at Acme?")
        self.assertIsNotNone(request)
        self.assertEqual(request.seed, "companies/acme.md")
        self.assertEqual(
            [hit.path for hit in relationship_hits(self.bundle, "Who works at Acme?")],
            ["people/ivy.md"],
        )
        self.assertIn("### Acme (companies/acme.md)", volunteer_pack(
            self.bundle,
            None,
            "Status on Acme.",
        ))

    def test_supersession_paths_are_canonical_in_files_and_memory(self) -> None:
        self.write("companies/legacy.md", "Legacy", "company", "Company.")
        self.write(
            "corrections/current.md",
            "Correction",
            "note",
            "Corrected.",
            supersedes=["./companies/legacy.md"],
        )
        dynamics = Dynamics(self.temp.name)
        dynamics.supersede("./companies/legacy.md", "corrections/current.md")

        self.assertEqual(
            self.bundle.superseded_by(),
            {"companies/legacy.md": "corrections/current.md"},
        )
        self.assertEqual(
            superseded_paths(self.bundle, dynamics),
            {"companies/legacy.md"},
        )
        self.assertIn("companies/legacy.md", dynamics.entries)
        self.assertNotIn("./companies/legacy.md", dynamics.entries)

    def test_external_file_alias_does_not_duplicate_a_mounted_note(self) -> None:
        external = tempfile.TemporaryDirectory()
        self.addCleanup(external.cleanup)
        canonical = os.path.join(external.name, "canonical.md")
        with open(canonical, "w", encoding="utf-8") as handle:
            handle.write(render_note(
                {"title": "Canonical", "type": "company"},
                "Company.",
            ))
        os.symlink(external.name, os.path.join(self.temp.name, "mounted"))
        os.symlink(canonical, os.path.join(self.temp.name, "alias.md"))
        os.makedirs(os.path.join(self.temp.name, "people"), exist_ok=True)
        with open(
            os.path.join(self.temp.name, "people", "ada.md"),
            "w",
            encoding="utf-8",
        ) as handle:
            handle.write(render_note(
                {"title": "Ada", "type": "person"},
                "Ada works at [[Canonical]].",
            ))

        bundle = Bundle(self.temp.name)
        self.assertEqual(
            sorted(bundle.notes),
            ["mounted/canonical.md", "people/ada.md"],
        )
        self.assertEqual(
            bundle.notes["people/ada.md"].links,
            ["mounted/canonical.md"],
        )

    def test_physical_alias_paths_resolve_to_the_surviving_note(self) -> None:
        external = tempfile.TemporaryDirectory()
        self.addCleanup(external.cleanup)
        canonical = os.path.join(external.name, "canonical.md")
        with open(canonical, "w", encoding="utf-8") as handle:
            handle.write(render_note(
                {"title": "Canonical", "type": "company"},
                "Company.",
            ))
        os.symlink(external.name, os.path.join(self.temp.name, "mounted"))
        os.symlink(canonical, os.path.join(self.temp.name, "alias.md"))
        with open(
            os.path.join(self.temp.name, "reference.md"),
            "w",
            encoding="utf-8",
        ) as handle:
            handle.write(render_note(
                {"title": "Reference", "type": "note"},
                "[Alias](alias.md)",
            ))

        bundle = Bundle(self.temp.name)
        self.assertEqual(
            bundle.notes["reference.md"].links,
            ["mounted/canonical.md"],
        )

    def test_hard_link_paths_resolve_to_the_surviving_note(self) -> None:
        os.makedirs(os.path.join(self.temp.name, "notes"), exist_ok=True)
        canonical = os.path.join(self.temp.name, "notes", "canonical.md")
        with open(canonical, "w", encoding="utf-8") as handle:
            handle.write(render_note(
                {"title": "Canonical", "type": "company"},
                "Company.",
            ))
        os.link(canonical, os.path.join(self.temp.name, "alias.md"))
        with open(
            os.path.join(self.temp.name, "reference.md"),
            "w",
            encoding="utf-8",
        ) as handle:
            handle.write(render_note(
                {"title": "Reference", "type": "note"},
                "[Canonical](notes/canonical.md)",
            ))

        bundle = Bundle(self.temp.name)
        surviving = next(
            path for path in bundle.notes if bundle.notes[path].title == "Canonical"
        )
        self.assertEqual(bundle.notes["reference.md"].links, [surviving])

    def test_object_pronoun_scan_volume_is_linear(self) -> None:
        count = 200
        body = "Ada Lovelace wrote this note. " + " ".join(
            "[Acme](../companies/acme.md) employed her."
            for _ in range(count)
        )
        bundle = Bundle(os.path.join(self.temp.name, "absent"))
        bundle.notes = {
            "people/ada.md": Note(
                "people/ada.md",
                {"title": "Ada Lovelace", "type": "person"},
                body,
            ),
            "companies/acme.md": Note(
                "companies/acme.md",
                {"title": "Acme", "type": "company"},
                "Company.",
            ),
        }
        original = store._all_markdown_links
        scanned = 0

        def counted(text: str):
            nonlocal scanned
            scanned += len(text)
            return original(text)

        with mock.patch.object(
            relationship_module,
            "_all_markdown_links",
            counted,
        ):
            edges = relationship_edges(bundle)

        self.assertEqual(len(edges), 1)
        self.assertLess(scanned, len(body) * 12)


class TestCommonMarkScannerAuditRegressions(unittest.TestCase):
    @staticmethod
    def destinations(text: str) -> list[str]:
        return [link.destination for link in store._all_markdown_links(text)]

    def test_inline_link_forms_match_the_registered_oracle(self) -> None:
        cases = {
            "[x](docs/a.md)": ["docs/a.md"],
            "[x](<docs/a file.md>)": ["docs/a file.md"],
            "[x](docs/a(b)c.md)": ["docs/a(b)c.md"],
            "[x](docs/a.md \"Title\")": ["docs/a.md"],
            "[x](docs/a.md 'Title')": ["docs/a.md"],
            "[x](docs/a.md (Title))": ["docs/a.md"],
            "[x](docs/a.md (line\nbreak))": ["docs/a.md"],
            r"[x](docs/a\)b.md)": ["docs/a)b.md"],
            "[outer [inner]](docs/nested.md)": ["docs/nested.md"],
            "[x](<>)": [""],
            "[x](<a<b>)": [],
            "[x](docs/a.md": [],
            "[x](docs/a(b.md)": [],
            "[x](docs/a.md title)": [],
            "[x](docs/a.md \"unterminated)": [],
            "![x](docs/image.md)": [],
            r"\[x](docs/escaped.md)": [],
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.destinations(text), expected)

    def test_blockquote_links_remain_graph_edges(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Bundle(directory)
            bundle.write_note(
                "companies/lineco.md",
                {"title": "LineCo", "type": "company"},
                "Company.",
            )
            bundle.write_note(
                "people/riley.md",
                {"title": "Riley Ames", "type": "person"},
                "> Riley Ames founded [LineCo](../companies/lineco.md)",
            )

            self.assertEqual(
                self.destinations("> [LineCo](companies/lineco.md)\n"),
                ["companies/lineco.md"],
            )
            self.assertEqual(
                bundle.notes["people/riley.md"].links,
                ["companies/lineco.md"],
            )
            self.assertEqual(
                bundle.backlinks["companies/lineco.md"],
                ["people/riley.md"],
            )
            self.assertEqual(relationship_edges(bundle), ())

    def test_reference_link_forms_match_the_registered_oracle(self) -> None:
        cases = {
            "[x][id]\n\n[id]: /docs/a.md": ["/docs/a.md"],
            "[x][]\n\n[x]: /docs/a.md": ["/docs/a.md"],
            "[x]\n\n[x]: /docs/a.md": ["/docs/a.md"],
            "[x][id]\n\n[id]:\n  /docs/a.md\n  \"Title\"\n": ["/docs/a.md"],
            "[x][id]\n\n[id]: <docs/a file.md> 'Title'": ["docs/a file.md"],
            "[x][missing]": [],
            "![x][id]\n\n[id]: /docs/image.md": [],
            "[x](docs/inline.md)\n\n[x]: docs/reference.md": ["docs/inline.md"],
            r"\[x][id]" + "\n\n[id]: /docs/a.md": ["/docs/a.md"],
            "[multi\nline][id]\n\n[id]: /docs/multiline.md": [
                "/docs/multiline.md"
            ],
            "[x][multi\nline]\n\n[multi line]: /docs/label.md": [
                "/docs/label.md"
            ],
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.destinations(text), expected)

    def test_invalid_reference_definitions_are_rejected(self) -> None:
        invalid = (
            "[id]:",
            "[id]: <a<b>",
            "[id]: docs/a(b.md",
            "[id]: docs/a).md",
            "[id]: docs/a.md trailing",
            "[id]: docs/a.md \"unterminated",
        )
        for text in invalid:
            with self.subTest(text=text):
                self.assertEqual(store._reference_definitions(text), ())

    def test_reference_normalization_and_first_definition_wins(self) -> None:
        text = (
            "[A&amp;B][Mixed   Label]\n\n"
            "[mixed label]: docs/first.md\n"
            "[MIXED LABEL]: docs/second.md\n"
        )
        self.assertEqual(self.destinations(text), ["docs/first.md"])
        self.assertEqual(store._markdown_unescape(r"a\)b&amp;c"), "a)b&c")
        self.assertEqual(store._markdown_unescape("a&copy;b"), "a©b")
        self.assertEqual(store._markdown_unescape("a&copyb"), "a&copyb")
        self.assertEqual(store._markdown_unescape("a&copy.b"), "a&copy.b")

    def test_reference_labels_preserve_escapes_and_entities(self) -> None:
        mismatches = (
            "[x][a&amp;b]\n\n[a&b]: docs/entity.md",
            r"[x][a\!b]" + "\n\n[a!b]: docs/escape.md",
            "[x][a&#65;b]\n\n[aAb]: docs/numeric.md",
        )
        matches = (
            "[x][a&amp;b]\n\n[a&amp;b]: docs/entity.md",
            r"[x][a\!b]" + "\n\n" + r"[a\!b]: docs/escape.md",
        )
        for text in mismatches:
            with self.subTest(kind="mismatch", text=text):
                self.assertEqual(self.destinations(text), [])
        for text in matches:
            with self.subTest(kind="match", text=text):
                self.assertEqual(len(self.destinations(text)), 1)

    def test_low_level_scanner_rejects_malformed_boundaries(self) -> None:
        self.assertEqual(store._line_records(""), [])
        self.assertIsNone(store._reference_destination("<unterminated"))
        self.assertEqual(
            store._reference_destination(r"docs/a\)b.md"),
            (12, "docs/a)b.md"),
        )
        self.assertIsNone(store._reference_destination("(" * 33 + "x" + ")" * 33))
        self.assertIsNone(store._reference_destination("docs/a).md"))
        for value in ("", "Title", " x", " \"", " invalid title"):
            with self.subTest(title=value):
                self.assertFalse(store._definition_title(value))
        self.assertIsNone(store._link_label_end("[unterminated", 0))
        self.assertEqual(store._link_label_end("[line\nbreak]", 0), 11)
        self.assertEqual(store._link_label_end(r"[a\]b]", 0), 5)
        self.assertIsNone(store._title_end("line\nbreak", 0, '"'))
        self.assertIsNone(store._title_end("unterminated", 0, '"'))

    def test_inline_titles_and_images_respect_complete_ranges(self) -> None:
        malformed = (
            "[x](<line\nbreak>)",
            "[x](<unterminated)",
            "[x](docs/a.md (unterminated)",
            "[x](docs/a.md invalid)",
            "[x](docs/a.md \"title\" trailing)",
            "[unterminated\n\n[id]: docs/a.md",
        )
        for text in malformed:
            with self.subTest(text=text):
                self.assertEqual(self.destinations(text), [])
        image_text = (
            r"\![escaped](docs/escaped.md) "
            "![inline](docs/image.md) ![full][img] ![broken\n\n"
            "[img]: docs/reference-image.md\n"
        )
        ranges = store._image_link_ranges(image_text)
        self.assertEqual(len(ranges), 2)
        self.assertEqual(self.destinations(image_text), ["docs/escaped.md"])
        self.assertEqual(store._reference_destinations(
            "[one]: docs/first.md\n[one]: docs/second.md\n"
        ), {"one": "docs/first.md"})

    def test_code_mask_handles_unclosed_and_complete_spans(self) -> None:
        text = "plain `closed` text and ``unterminated"
        masked = store._mask_markdown_code(text)
        self.assertNotIn("closed", masked)
        self.assertIn("unterminated", masked)

    def test_code_and_comments_do_not_create_links_or_definitions(self) -> None:
        text = (
            "    [indented](docs/indented.md)\n\n"
            "```markdown\n"
            "[fenced][target]\n"
            "[target]: docs/fenced.md\n"
            "```\n\n"
            "<!-- [comment][hidden]\n[hidden]: docs/comment.md -->\n"
        )
        self.assertEqual(self.destinations(text), [])

    def test_html_blocks_do_not_create_links_or_definitions(self) -> None:
        blocks = (
            "<div>\n[x](docs/div.md)\n</div>\n",
            "<script>\n[x](docs/script.md)\n</script>\n",
            "<pre>\n[x](docs/pre.md)\n</pre>\n",
            "<style>\n[x](docs/style.md)\n</style>\n",
            "<textarea>\n[x](docs/textarea.md)\n</textarea>\n",
            "<?processing\n[x](docs/pi.md)\n?>\n",
            "<!DECLARATION\n[x](docs/declaration.md)\n>\n",
            "<![CDATA[\n[x](docs/cdata.md)\n]]>\n",
            "<x-widget>\n[x](docs/custom.md)\n</x-widget>\n",
            "- <div>\n  [x](docs/list-div.md)\n  </div>\n",
            "1. <x-widget>\n   [x](docs/list-custom.md)\n   </x-widget>\n",
            "- item\n    <div>\n    [x](docs/indented-list.md)\n    </div>\n",
            (
                "  - item\n\n    <x-widget>\n    [x](docs/nested-list.md)\n"
                "    </x-widget>\n"
            ),
        )
        for text in blocks:
            with self.subTest(text=text):
                self.assertEqual(self.destinations(text), [])
        hidden_definition = (
            "<div>\n[id]: docs/hidden.md\n</div>\n\n[x][id]\n"
        )
        self.assertEqual(self.destinations(hidden_definition), [])
        self.assertEqual(
            self.destinations("<div>raw</div>\n\n[x](docs/visible.md)\n"),
            ["docs/visible.md"],
        )
        self.assertEqual(
            self.destinations(
                "- item\n  <x-widget>\n  [x](docs/inline-custom.md)\n"
                "  </x-widget>\n"
            ),
            ["docs/inline-custom.md"],
        )

    def test_list_block_constructs_do_not_open_paragraphs_before_html(self) -> None:
        cases = (
            "- # Heading\n  <x-widget>\n  [x](docs/heading.md)\n  </x-widget>\n",
            "- > quotation\n  <x-widget>\n  [x](docs/quote.md)\n  </x-widget>\n",
            "-\n  <x-widget>\n  [x](docs/empty.md)\n  </x-widget>\n",
            "- item\n  ***\n  <x-widget>\n  [x](docs/thematic.md)\n  </x-widget>\n",
            "- item\n  ===\n  <x-widget>\n  [x](docs/setext.md)\n  </x-widget>\n",
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(self.destinations(text), [])

    def test_html_blocks_follow_list_container_boundaries(self) -> None:
        self.assertEqual(
            self.destinations(
                "- Profile\n"
                "<x-archive>\n"
                "[x](docs/raw.md)\n"
                "</x-archive>\n"
            ),
            ["docs/raw.md"],
        )
        self.assertEqual(
            self.destinations(
                "- Profile\n"
                "    <div>\n"
                "[x](docs/container-ended.md)\n"
                "    </div>\n"
            ),
            ["docs/container-ended.md"],
        )

    def test_non_commonmark_line_separators_do_not_break_scanner_views(self) -> None:
        self.assertEqual(
            self.destinations("`a\x0cb`\n[See](docs/target.md)"),
            ["docs/target.md"],
        )

    def test_masked_code_does_not_become_html_indentation(self) -> None:
        text = "1. item\n`code`<div>[Target](target.md)"
        self.assertEqual(self.destinations(text), ["target.md"])

    def test_angle_prescan_is_monotonic_for_unclosed_openers(self) -> None:
        text = "`" + "<" * 10_000 + "`"
        with mock.patch.object(
            store,
            "_html_span_end",
            wraps=store._html_span_end,
        ) as parser:
            store._mask_markdown_code(text)
        self.assertLessEqual(parser.call_count, 1)

    def test_whitespace_only_wikilinks_are_rejected(self) -> None:
        text = "[[good]] and [[ ]] and [[\t]] and [[a|display]] and [[b#section]]"
        self.assertEqual(
            [match.group(1) for match in store._WIKILINK.finditer(text)],
            ["good", "a", "b"],
        )

    def test_indented_code_does_not_create_bundle_links(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            bundle = Bundle(root)
            bundle.write_note(
                "target.md",
                {"title": "Target", "type": "note"},
                "Target.",
            )
            bundle.write_note(
                "source.md",
                {"title": "Source", "type": "note"},
                "    [Target](target.md)\n",
            )
            self.assertEqual(bundle.notes["source.md"].links, [])

    def test_multiline_inline_link_text_is_parsed(self) -> None:
        self.assertEqual(
            self.destinations("[two\nlines](docs/multiline.md)"),
            ["docs/multiline.md"],
        )

    def test_commonmark_block_and_inline_precedence(self) -> None:
        cases = {
            "Foo\n[bar]: /baz\n\n[bar]\n": [],
            "[foo]\n\n> [foo]: /url\n": ["/url"],
            "[foo]: <>\n\n[foo]\n": [""],
            "[link]()\n": [""],
            "[foo](not a link)\n\n[foo]: /url1\n": ["/url1"],
            "[foo <bar attr=\"](baz)\">\n": [],
            "[foo <bar attr=\"][ref]\">\n\n[ref]: /uri\n": [],
            "[![moon](moon.jpg)][ref]\n\n[ref]: /uri\n": ["/uri"],
            "[[*foo* bar]]\n\n[*foo* bar]: /url \"title\"\n": ["/url"],
            "[link](   /uri\n  \"title\"  )\n": ["/uri"],
            "![[[foo](uri1)](uri2)](uri3)\n": [],
            "[foo<https://example.com/?search=](uri)>\n": [
                "https://example.com/?search=](uri)"
            ],
            "[foo [bar](/uri)][ref]\n\n[ref]: /uri\n": ["/uri", "/uri"],
            "[foo *bar [baz][ref]*][ref]\n\n[ref]: /uri\n": [
                "/uri",
                "/uri",
            ],
            "[foo<https://example.com/?search=][ref]>\n\n[ref]: /uri\n": [
                "https://example.com/?search=][ref]"
            ],
            "<https://foo.bar.`baz>`\n": ["https://foo.bar.`baz"],
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.destinations(text), expected)

    def test_reference_definition_continuations_are_parsed(self) -> None:
        cases = {
            "   [foo]:\n      /url\n           'the title'\n\n[foo]\n": [
                "/url"
            ],
            "[foo]: /url '\ntitle\nline1\nline2\n'\n\n[foo]\n": [
                "/url"
            ],
            r"[Foo*bar\]]:my_(url) 'title (with parens)'" + "\n\n" +
            r"[Foo*bar\]]" + "\n": ["my_(url)"],
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.destinations(text), expected)

    def test_markdown_paths_decode_before_security_checks(self) -> None:
        note = store.Note("docs/source.md", {}, "")
        self.assertEqual(
            store.Bundle._markdown_target_path(note, "target%20file.md"),
            "docs/target file.md",
        )
        self.assertEqual(
            store.Bundle._markdown_target_path(note, "name%23part.md#section"),
            "docs/name#part.md",
        )
        for destination in (
            "%2E%2E/%2E%2E/secret.md",
            "https%3A%2F%2Fexample.com/a.md",
            "mailto%3Aada@example.com",
            "%5Cserver%5Cshare.md",
            "bad%00name.md",
            "mailto:ada@example.com",
            "urn:isbn:9780143127741",
            "tel:+15551234567",
            "data:text/plain,example",
        ):
            with self.subTest(destination=destination):
                self.assertIsNone(
                    store.Bundle._markdown_target_path(note, destination)
                )

        self.assertEqual(
            store.Bundle._markdown_target_path(note, "folder/name:part.md"),
            "docs/folder/name:part.md",
        )
        self.assertEqual(
            store.Bundle._markdown_target_path(note, "./mailto:local.md"),
            "docs/mailto:local.md",
        )

    def test_uri_links_never_enter_the_internal_note_graph(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Bundle(directory)
            bundle.write_note(
                "mailto:ada@example.com.md",
                {"title": "Misleading local path", "type": "note"},
                "Local note.",
            )
            bundle.write_note(
                "source.md",
                {"title": "Source", "type": "note"},
                "Contact <ada@example.com>. Also "
                "[mail](mailto:ada@example.com).",
            )

            self.assertEqual(bundle.notes["source.md"].links, [])
            self.assertNotIn("mailto:ada@example.com.md", bundle.backlinks)

    def test_absolute_uri_schemes_remain_external_when_paths_exist(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Bundle(directory)
            for path in (
                "cid:target.md",
                "sftp:target.md",
                "cid:explicitly-relative.md",
                "sftp:explicitly-relative.md",
            ):
                bundle.write_note(
                    path,
                    {"title": path, "type": "note"},
                    "Local note.",
                )
            bundle.write_note(
                "source.md",
                {"title": "Source", "type": "note"},
                "[CID](cid:target.md) [SFTP](sftp:target.md) "
                "[Local CID](./cid:explicitly-relative.md) "
                "[Local SFTP](./sftp:explicitly-relative.md)",
            )

            self.assertEqual(
                bundle.notes["source.md"].links,
                [
                    "cid:explicitly-relative.md",
                    "sftp:explicitly-relative.md",
                ],
            )
            self.assertNotIn("cid:target.md", bundle.backlinks)
            self.assertNotIn("sftp:target.md", bundle.backlinks)
            self.assertEqual(
                bundle.backlinks["cid:explicitly-relative.md"],
                ["source.md"],
            )
            self.assertEqual(
                bundle.backlinks["sftp:explicitly-relative.md"],
                ["source.md"],
            )

    def test_unregistered_absolute_uri_syntax_remains_external(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Bundle(directory)
            bundle.write_note(
                "docs:target.md",
                {"title": "Target", "type": "note"},
                "Local note.",
            )
            bundle.write_note(
                "mailto:ada@example.com.md",
                {"title": "Misleading mail path", "type": "note"},
                "Local note.",
            )
            bundle.write_note(
                "source.md",
                {"title": "Source", "type": "note"},
                "[Target](docs:target.md) "
                "[Mail](mailto:ada@example.com.md)",
            )

            self.assertEqual(bundle.notes["source.md"].links, [])
            self.assertNotIn("docs:target.md", bundle.backlinks)
            self.assertNotIn("mailto:ada@example.com.md", bundle.backlinks)

    def test_explicit_relative_colon_path_forward_reference_refreshes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Bundle(directory)
            bundle.write_note(
                "source.md",
                {"title": "Source", "type": "note"},
                "[Target](./docs:target.md)",
            )
            self.assertEqual(bundle.notes["source.md"].links, [])

            bundle.write_note(
                "docs:target.md",
                {"title": "Target", "type": "note"},
                "Local note.",
            )

            self.assertEqual(
                bundle.notes["source.md"].links,
                ["docs:target.md"],
            )
            self.assertEqual(bundle.backlinks["docs:target.md"], ["source.md"])

    def test_inline_link_metadata_does_not_create_a_wikilink_edge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Bundle(directory)
            bundle.write_note(
                "companies/acme.md",
                {"title": "Acme", "type": "company"},
                "Company.",
            )
            bundle.write_note(
                "other.md",
                {"title": "Other", "type": "note"},
                "Other note.",
            )
            bundle.write_note(
                "source.md",
                {"title": "Source", "type": "note"},
                '[ordinary](other.md "metadata [[Acme]]")',
            )

            self.assertEqual(bundle.notes["source.md"].links, ["other.md"])
            self.assertNotIn("companies/acme.md", bundle.backlinks)
            self.assertEqual(bundle.backlinks["other.md"], ["source.md"])

    def test_escaped_wikilinks_never_enter_graph_or_relationship_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Bundle(directory)
            bundle.write_note(
                "people/ari.md",
                {"title": "Ari", "type": "person"},
                r"Ari advised \[[Qorp]].",
            )
            bundle.write_note(
                "companies/qorp.md",
                {"title": "Qorp", "type": "company"},
                "Company.",
            )

            self.assertEqual(bundle.notes["people/ari.md"].links, [])
            self.assertNotIn("companies/qorp.md", bundle.backlinks)
            self.assertEqual(relationship_edges(bundle), ())

    def test_html_block_typed_links_do_not_bypass_the_link_graph(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Bundle(directory)
            bundle.write_note(
                "companies/acme.md",
                {"title": "Acme", "type": "company"},
                "Company.",
            )
            bundle.write_note(
                "people/dana.md",
                {"title": "Dana", "type": "person"},
                "<div>\n- works_at [[Acme]] (high)\n</div>",
            )

            note = bundle.notes["people/dana.md"]
            self.assertEqual(note.links, [])
            self.assertEqual(note.typed_links, [])
            self.assertNotIn("companies/acme.md", bundle.backlinks)
            self.assertEqual(relationship_edges(bundle), ())

    def test_html_images_and_reference_metadata_are_not_graph_assertions(self) -> None:
        variants = (
            "<div>\nDana works at [[Acme]].\n</div>",
            "![Dana works at [[Acme]]](portrait.png)",
            "[ref]: /unused (Dana works at [[Acme]])",
        )
        for body in variants:
            with self.subTest(body=body), tempfile.TemporaryDirectory() as directory:
                bundle = Bundle(directory)
                bundle.write_note(
                    "companies/acme.md",
                    {"title": "Acme", "type": "company"},
                    "Company.",
                )
                bundle.write_note(
                    "people/dana.md",
                    {"title": "Dana", "type": "person"},
                    body,
                )

                self.assertEqual(bundle.notes["people/dana.md"].links, [])
                self.assertNotIn("companies/acme.md", bundle.backlinks)
                self.assertEqual(relationship_edges(bundle), ())

    def test_destinationless_image_like_text_retains_wikilinks(self) -> None:
        cases = (
            ("![[Acme]]", True, False),
            ("![Dana works at [[Acme]]]", True, True),
            ("![Dana works at [[Acme]]](portrait.png)", False, False),
        )
        for body, linked, related in cases:
            with self.subTest(body=body), tempfile.TemporaryDirectory() as directory:
                bundle = Bundle(directory)
                bundle.write_note(
                    "companies/acme.md",
                    {"title": "Acme", "type": "company"},
                    "Company.",
                )
                bundle.write_note(
                    "people/dana.md",
                    {"title": "Dana", "type": "person"},
                    body,
                )

                note = bundle.notes["people/dana.md"]
                self.assertEqual(note.links, ["companies/acme.md"] if linked else [])
                self.assertEqual(
                    bundle.backlinks.get("companies/acme.md", []),
                    ["people/dana.md"] if linked else [],
                )
                self.assertEqual(
                    {
                        (edge.subject, edge.object, edge.relation)
                        for edge in relationship_edges(bundle)
                    },
                    {
                        (
                            "people/dana.md",
                            "companies/acme.md",
                            "works_at",
                        )
                    }
                    if related
                    else set(),
                )

    def test_reference_scanning_remains_bounded_at_ten_thousand_links(self) -> None:
        count = 10_000
        text = "[id]: target.md\n\n" + " ".join(
            "[x][id]" for _ in range(count)
        )
        started = time.perf_counter()
        links = store._all_markdown_links(text)
        elapsed = time.perf_counter() - started
        self.assertEqual(len(links), count)
        self.assertLess(elapsed, 2.0)

    def test_mixed_image_and_link_scanning_remains_bounded(self) -> None:
        count = 10_000
        text = " ".join(
            f"![image](image-{index}.png) [x](docs/{index}.md)"
            for index in range(count)
        )
        started = time.perf_counter()
        links = store._all_markdown_links(text)
        elapsed = time.perf_counter() - started
        self.assertEqual(len(links), count)
        self.assertLess(elapsed, 2.0)

    def test_many_short_destinations_have_a_linear_read_bound(self) -> None:
        small = _CountingText(" ".join("[x](a)" for _ in range(1_000)))
        large = _CountingText(" ".join("[x](a)" for _ in range(2_000)))

        small_links = store._markdown_links(small)
        large_links = store._markdown_links(large)

        self.assertEqual(len(small_links), 1_000)
        self.assertEqual(len(large_links), 2_000)
        self.assertGreater(small.integer_reads, 0)
        self.assertLess(large.integer_reads, small.integer_reads * 3)
        self.assertLess(large.integer_reads, len(large) * 35)

    def test_malformed_outer_destination_preserves_nested_link(self) -> None:
        links = store._all_markdown_links("[[x](a)](b")

        self.assertEqual(
            [(link.destination, link.label) for link in links],
            [("a", "x")],
        )

    def test_decreasing_nested_destination_queries_remain_linear(self) -> None:
        small = _CountingText(" ".join("[[x](a)](b" for _ in range(1_000)))
        large = _CountingText(" ".join("[[x](a)](b" for _ in range(2_000)))

        small_links = store._all_markdown_links(small)
        large_links = store._all_markdown_links(large)

        self.assertEqual(len(small_links), 1_000)
        self.assertEqual(len(large_links), 2_000)
        self.assertLess(large.integer_reads, small.integer_reads * 3)
        self.assertLess(large.integer_reads, len(large) * 45)


if __name__ == "__main__":
    unittest.main()
