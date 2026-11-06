"""Adversarial regressions for the Muninn v5 relationship snapshot."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from muninn.activate import merged_graph
from muninn.relationships import relationship_edges, relationship_hits
from muninn.store import Bundle


def _typed_view(typed_links: list[dict]) -> tuple[tuple[tuple[str, object], ...], ...]:
    return tuple(tuple(sorted(item.items())) for item in typed_links)


def _graph_view(bundle: Bundle) -> dict[str, tuple[tuple[str, float, str], ...]]:
    return {
        path: tuple(neighbors)
        for path, neighbors in sorted(merged_graph(bundle, None).items())
    }


def _bundle_view(bundle: Bundle) -> dict[str, object]:
    return {
        "links": {
            path: tuple(note.links)
            for path, note in sorted(bundle.notes.items())
        },
        "typed_links": {
            path: _typed_view(note.typed_links)
            for path, note in sorted(bundle.notes.items())
        },
        "backlinks": {
            path: tuple(sources)
            for path, sources in sorted(bundle.backlinks.items())
        },
        "merged_graph": _graph_view(bundle),
    }


def _triples(bundle: Bundle) -> set[tuple[str, str, str]]:
    return {
        (edge.subject, edge.object, edge.relation)
        for edge in relationship_edges(bundle)
    }


class V5AdversarialRelationshipTests(unittest.TestCase):
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
    ) -> None:
        meta: dict[str, object] = {"title": title, "type": note_type}
        if aliases is not None:
            meta["aliases"] = aliases
        bundle.write_note(path, meta, body)

    def test_late_typed_link_live_and_reload_views_are_equivalent(self):
        root, bundle = self.make_bundle()
        self.write(
            bundle,
            "people/early.md",
            "Early Employee",
            "person",
            "# Connections\n\n- works_at [[Late Company]] (extracted)",
        )
        self.write(
            bundle,
            "companies/late-company.md",
            "Late Company",
            "company",
            "Late Company was written after the typed-link source.",
        )

        expected = ("people/early.md", "companies/late-company.md", "works_at")
        self.assertIn(expected, _triples(bundle))
        self.assertEqual(
            bundle.notes["people/early.md"].links,
            ["companies/late-company.md"],
        )
        self.assertEqual(
            bundle.notes["people/early.md"].typed_links[0]["target"],
            "companies/late-company.md",
        )
        self.assertEqual(
            bundle.backlinks.get("companies/late-company.md"),
            ["people/early.md"],
        )
        self.assertIn(
            ("companies/late-company.md", 1.0, "link"),
            merged_graph(bundle, None)["people/early.md"],
        )
        self.assertEqual(_bundle_view(bundle), _bundle_view(Bundle(root)))

    def test_target_title_rename_removes_stale_typed_link_views(self):
        root, bundle = self.make_bundle()
        self.write(
            bundle,
            "companies/company-001.md",
            "Acme",
            "company",
            "Old title.",
        )
        self.write(
            bundle,
            "people/employee.md",
            "Employee",
            "person",
            "# Connections\n\n- works_at [[Acme]] (extracted)",
        )
        self.assertIn(
            ("people/employee.md", "companies/company-001.md", "works_at"),
            _triples(bundle),
        )

        self.write(
            bundle,
            "companies/company-001.md",
            "Acme Renamed",
            "company",
            "The same file now has a different title.",
        )

        self.assertNotIn(
            ("people/employee.md", "companies/company-001.md", "works_at"),
            _triples(bundle),
        )
        self.assertEqual(bundle.notes["people/employee.md"].links, [])
        self.assertEqual(bundle.notes["people/employee.md"].typed_links, [])
        self.assertNotIn("companies/company-001.md", bundle.backlinks)
        self.assertEqual(_bundle_view(bundle), _bundle_view(Bundle(root)))

    def test_duplicate_title_is_ambiguous_independent_of_write_order(self):
        def build(paths: tuple[str, str]) -> tuple[str, Bundle]:
            root, bundle = self.make_bundle()
            self.write(
                bundle,
                "people/source.md",
                "Source",
                "person",
                "# Connections\n\n- works_at [[Twin]] (extracted)",
            )
            for path in paths:
                self.write(bundle, path, "Twin", "company", "Duplicate title.")
            return root, bundle

        for paths in (
            ("companies/a.md", "companies/z.md"),
            ("companies/z.md", "companies/a.md"),
        ):
            with self.subTest(paths=paths):
                root, bundle = build(paths)
                self.assertEqual(bundle.notes["people/source.md"].links, [])
                self.assertEqual(bundle.notes["people/source.md"].typed_links, [])
                self.assertFalse(
                    any(
                        edge.subject == "people/source.md"
                        for edge in relationship_edges(bundle)
                    )
                )
                self.assertEqual(_bundle_view(bundle), _bundle_view(Bundle(root)))

    def test_one_endpoint_can_carry_founded_funded_and_advised(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "people/ada.md", "Ada Lovelace", "person", "Person.")
        self.write(
            bundle,
            "companies/multi.md",
            "Multi",
            "company",
            "Multi was founded, funded, and advised by [Ada](/people/ada).",
        )

        triples = _triples(bundle)
        self.assertIn(("people/ada.md", "companies/multi.md", "founded"), triples)
        self.assertIn(
            ("people/ada.md", "companies/multi.md", "invested_in"),
            triples,
        )
        self.assertIn(("people/ada.md", "companies/multi.md", "advises"), triples)

    def test_coordinated_employment_links_keep_role_modifiers(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "people/ada.md", "Ada", "person", "Person.")
        self.write(bundle, "people/bob.md", "Bob", "person", "Person.")
        self.write(
            bundle,
            "companies/roster.md",
            "Roster",
            "company",
            "Roster employed [Ada](/people/ada) as an engineer and "
            "[Bob](/people/bob) as a designer.",
        )

        triples = _triples(bundle)
        self.assertIn(("people/ada.md", "companies/roster.md", "works_at"), triples)
        self.assertIn(("people/bob.md", "companies/roster.md", "works_at"), triples)

    def test_no_question_that_preserves_affirmative_relation(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "companies/acme.md", "Acme", "company", "Company.")
        self.write(
            bundle,
            "people/founder.md",
            "Certain Founder",
            "person",
            "There is no question that Certain Founder founded "
            "[Acme](/companies/acme).",
        )

        self.assertIn(
            ("people/founder.md", "companies/acme.md", "founded"),
            _triples(bundle),
        )

    def test_denial_does_not_suppress_later_asserted_but_clause(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "companies/beta.md", "Beta", "company", "Company.")
        self.write(bundle, "companies/acme.md", "Acme", "company", "Company.")
        self.write(
            bundle,
            "people/ada.md",
            "Ada Lovelace",
            "person",
            "Ada Lovelace denied founding [Beta](/companies/beta) but founded "
            "[Acme](/companies/acme).",
        )

        triples = _triples(bundle)
        self.assertNotIn(("people/ada.md", "companies/beta.md", "founded"), triples)
        self.assertIn(("people/ada.md", "companies/acme.md", "founded"), triples)

    def test_commonmark_angle_destination_and_optional_title_resolve(self):
        _, bundle = self.make_bundle()
        self.write(
            bundle,
            "companies/angle-labs.md",
            "Angle Labs",
            "company",
            "Company.",
        )
        self.write(
            bundle,
            "companies/title-labs.md",
            "Title Labs",
            "company",
            "Company.",
        )
        self.write(
            bundle,
            "people/angle-founder.md",
            "Angle Founder",
            "person",
            "Angle Founder founded [Angle Labs](</companies/angle-labs>).",
        )
        self.write(
            bundle,
            "people/title-founder.md",
            "Title Founder",
            "person",
            'Title Founder founded [Title Labs](/companies/title-labs "page").',
        )

        triples = _triples(bundle)
        self.assertIn(
            ("people/angle-founder.md", "companies/angle-labs.md", "founded"),
            triples,
        )
        self.assertIn(
            ("people/title-founder.md", "companies/title-labs.md", "founded"),
            triples,
        )
        self.assertEqual(
            bundle.notes["people/angle-founder.md"].links,
            ["companies/angle-labs.md"],
        )
        self.assertEqual(
            bundle.notes["people/title-founder.md"].links,
            ["companies/title-labs.md"],
        )
        self.assertEqual(
            bundle.backlinks["companies/angle-labs.md"],
            ["people/angle-founder.md"],
        )

    def test_fenced_code_does_not_create_relationship_edges(self):
        _, bundle = self.make_bundle()
        self.write(
            bundle,
            "companies/code-labs.md",
            "Code Labs",
            "company",
            "Company.",
        )
        self.write(
            bundle,
            "people/coder.md",
            "Coder",
            "person",
            "```markdown\n"
            "Coder founded [Code Labs](/companies/code-labs).\n"
            "```\n",
        )

        self.assertNotIn(
            ("people/coder.md", "companies/code-labs.md", "founded"),
            _triples(bundle),
        )
        self.assertEqual(bundle.notes["people/coder.md"].links, [])
        self.assertNotIn("companies/code-labs.md", bundle.backlinks)
        self.assertNotIn(
            ("companies/code-labs.md", 1.0, "link"),
            merged_graph(bundle, None).get("people/coder.md", []),
        )

    def test_markdown_and_wikilink_pronoun_binding_are_consistent(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "companies/acme.md", "Acme", "company", "Company.")
        self.write(
            bundle,
            "people/ada.md",
            "Ada Lovelace",
            "person",
            "Person.",
            aliases=["Ada"],
        )
        self.write(
            bundle,
            "companies/markdown-co.md",
            "Markdown Co",
            "company",
            "Markdown Co worked with [Acme](/companies/acme). "
            "It hired [Ada](/people/ada).",
        )
        self.write(
            bundle,
            "companies/wiki-co.md",
            "Wiki Co",
            "company",
            "Wiki Co worked with [[Acme]]. It hired [[Ada]].",
        )

        triples = _triples(bundle)
        markdown_result = (
            "people/ada.md",
            "companies/markdown-co.md",
            "works_at",
        ) in triples
        wikilink_result = (
            "people/ada.md",
            "companies/wiki-co.md",
            "works_at",
        ) in triples
        self.assertEqual(markdown_result, wikilink_result)

    def test_plural_possessive_apostrophe_blocks_false_employment(self):
        _, bundle = self.make_bundle()
        self.write(
            bundle,
            "companies/acme-labs.md",
            "Acme Labs",
            "company",
            "Company.",
        )
        self.write(
            bundle,
            "people/nora.md",
            "Nora",
            "person",
            "Nora worked at [Acme Labs](/companies/acme-labs)' booth.",
        )

        self.assertNotIn(
            ("people/nora.md", "companies/acme-labs.md", "works_at"),
            _triples(bundle),
        )

    def test_who_has_acme_hired_returns_hired_people(self):
        _, bundle = self.make_bundle()
        self.write(bundle, "people/ada.md", "Ada", "person", "Person.")
        self.write(
            bundle,
            "companies/acme.md",
            "Acme",
            "company",
            "Acme hired [Ada](/people/ada).",
        )

        self.assertEqual(
            [hit.path for hit in relationship_hits(bundle, "Who has Acme hired?")],
            ["people/ada.md"],
        )

    def test_reported_speech_is_not_asserted_by_said_or_according_to(self):
        _, bundle = self.make_bundle()
        self.write(
            bundle,
            "companies/hearsay-labs.md",
            "Hearsay Labs",
            "company",
            "Company.",
        )
        self.write(
            bundle,
            "people/rhea.md",
            "Rhea Vale",
            "person",
            "A newspaper said Rhea Vale founded "
            "[Hearsay Labs](/companies/hearsay-labs).",
        )
        self.write(
            bundle,
            "people/mika.md",
            "Mika Orlo",
            "person",
            "According to an analyst note, Mika Orlo worked at "
            "[Hearsay Labs](/companies/hearsay-labs).",
        )

        triples = _triples(bundle)
        self.assertNotIn(
            ("people/rhea.md", "companies/hearsay-labs.md", "founded"),
            triples,
        )
        self.assertNotIn(
            ("people/mika.md", "companies/hearsay-labs.md", "works_at"),
            triples,
        )

    def test_initial_unanchored_it_does_not_bind_company_or_meeting_source(self):
        _, bundle = self.make_bundle()
        self.write(
            bundle,
            "people/kai.md",
            "Kai Rowan",
            "person",
            "Person.",
            aliases=["Kai"],
        )
        self.write(
            bundle,
            "companies/unanchored.md",
            "Unanchored Company",
            "company",
            "It employed [Kai](/people/kai).",
        )
        self.write(
            bundle,
            "meetings/unanchored.md",
            "Unanchored Meeting",
            "meeting",
            "It was attended by [Kai](/people/kai).",
        )

        triples = _triples(bundle)
        self.assertNotIn(
            ("people/kai.md", "companies/unanchored.md", "works_at"),
            triples,
        )
        self.assertNotIn(
            ("people/kai.md", "meetings/unanchored.md", "attended"),
            triples,
        )

    def test_one_note_relationship_extraction_scales_better_than_quadratic(self):
        def elapsed_for(link_count: int) -> float:
            _, bundle = self.make_bundle()
            for index in range(link_count):
                title = f"Person {index:03d}"
                self.write(
                    bundle,
                    f"people/person-{index:03d}.md",
                    title,
                    "person",
                    "Person.",
                )
            body = " ".join(
                f"ScaleCo hired [Person {index:03d}]"
                f"(/people/person-{index:03d})."
                for index in range(link_count)
            )
            self.write(
                bundle,
                "companies/scaleco.md",
                "ScaleCo",
                "company",
                body,
            )

            start = time.perf_counter()
            edges = relationship_edges(bundle)
            elapsed = time.perf_counter() - start
            hires = [
                edge
                for edge in edges
                if edge.object == "companies/scaleco.md"
                and edge.relation == "works_at"
            ]
            self.assertEqual(len(hires), link_count)
            return elapsed

        timings = {
            link_count: min(elapsed_for(link_count) for _ in range(3))
            for link_count in (100, 200, 400)
        }
        self.assertLessEqual(
            timings[400] / timings[100],
            12.0,
            f"relationship_edges timings were {timings!r}",
        )
        self.assertLessEqual(
            timings[400] / timings[200],
            6.5,
            f"relationship_edges timings were {timings!r}",
        )


if __name__ == "__main__":
    unittest.main()
