"""Verify multi-cue graph activation, coverage, bounds, and determinism.

The tests cover two-hop retrieval without shared query terms, diverse facet
selection at `k=2`, the lexical floor that protects a strong direct match from
graph-only candidates, and stable output from fixed iterations, sorted
adjacency, and rounded scores.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from muninn.activate import (Cue, activate, cues_from_goals,  # noqa: E402
                             cues_from_query, merged_graph)
from muninn.dynamics import Dynamics  # noqa: E402
from muninn.recall import (context_pack, recall, recall_explain,  # noqa: E402
                           tokens)  # tokens via recall = the re-export
from muninn.store import Bundle  # noqa: E402


class TestTokenization(unittest.TestCase):
    """Identifier subtokens (field-tested on Flask): code-repo knowledge
    speaks in ``teardown_request`` / ``createUrlAdapter`` / ``ctx.py``,
    but agents ask in words. tokens() must emit the parts ALONGSIDE the
    whole identifier: whole-token matches keep their exactness, prose
    queries stop missing identifier-bearing notes entirely."""

    def test_snake_case_emits_parts_and_whole(self):
        toks = tokens("use teardown_request for cleanup")
        self.assertIn("teardown_request", toks)  # exact form still matches
        self.assertIn("teardown", toks)
        self.assertIn("request", toks)

    def test_unicode_terms_keep_complete_words_and_canonical_accents(self):
        self.assertEqual(tokens("東京 李明 José Łukasz"),
                         {"東京", "李明", "josé", "łukasz"})
        self.assertEqual(tokens("Jose\u0301"), tokens("José"))

    def test_non_latin_query_reaches_exact_note(self):
        from muninn.activate import relevance
        from muninn.store import Note
        note = Note(path="tokyo.md", meta={"title": "東京"}, body="Office.")
        self.assertGreater(relevance(tokens("東京"), note), 0)

    def test_camel_case_splits_before_lowercasing(self):
        toks = tokens("call createUrlAdapter() here")
        self.assertLessEqual({"create", "url", "adapter"}, toks)

    def test_dotted_filenames_split(self):
        toks = tokens("the bug is in ctx.py")
        self.assertIn("ctx.py", toks)
        self.assertIn("ctx", toks)

    def test_prose_query_reaches_identifier_body(self):
        from muninn.activate import relevance
        from muninn.store import Note
        note = Note(path="n.md", meta={"title": "DB cleanup"},
                    body="Use teardown_appcontext, not teardown_request: "
                         "background tasks leak connections otherwise.")
        self.assertGreater(relevance(tokens("teardown request leak"), note),
                           0.5)

    def test_stopword_parts_are_not_emitted(self):
        # splitting must not reintroduce stopwords or one-char noise
        toks = tokens("a_the_x value_of")
        self.assertNotIn("the", toks)
        self.assertNotIn("a", toks)
        self.assertNotIn("x", toks)


class TestTwoHop(unittest.TestCase):
    """A note two link-hops from the only lexical hit, sharing no
    vocabulary with the cue, still surfaces: with hop evidence."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        b = Bundle(self.root)
        b.write_note("entry.md", {"title": "Backup policy"},
                     "nightly wal-g basebackup to s3. see [[Bridge note]]")
        b.write_note("bridge.md", {"title": "Bridge note"},
                     "see also [[Target box]]")
        b.write_note("target.md", {"title": "Target box"},
                     "big machine in the basement rack")
        self.b = Bundle(self.root)
        self.d = Dynamics(self.root)

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_two_hop_zero_shared_vocabulary(self):
        cue = "wal-g basebackup"
        # sanity: the target really shares no vocabulary with the cue
        self.assertFalse(tokens(cue) & tokens(
            self.b.notes["target.md"].title + " "
            + self.b.notes["target.md"].body))
        hits = recall_explain(self.b, self.d, cue, k=5, reactivate=False)
        paths = [n.path for n, _s, _w, _e in hits]
        self.assertEqual(paths[0], "entry.md")
        self.assertIn("target.md", paths)
        ev = {n.path: e for n, _s, _w, e in hits}
        self.assertEqual(ev["target.md"].carrier, ("bridge.md", "link"))
        self.assertEqual(ev["target.md"].hop, 2)
        self.assertEqual(ev["target.md"].direct, 0.0)
        why = {n.path: w for n, _s, w, _e in hits}
        self.assertIn("via [[Bridge note]]", why["target.md"])

    def test_flat_ablation_is_no_walk_same_code_path(self):
        hits = recall(self.b, self.d, "wal-g basebackup", k=5,
                      use_dynamics=False, reactivate=False)
        paths = [n.path for n, _s, _w in hits]
        self.assertEqual(paths, ["entry.md"])  # lexical only, no inheritance


class TestFacetCoverage(unittest.TestCase):
    """At k=2 the pack must cover both query facets, not spend the second
    slot on a near-duplicate of the first pick."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        b = Bundle(self.root)
        b.write_note("a1.md", {"title": "Postgres port config"},
                     "postgres port 7433 for prod")
        b.write_note("a2.md", {"title": "Postgres notes"},
                     "postgres port 7433 discussion")  # near-duplicate of a1
        b.write_note("b1.md", {"title": "Backup schedule"},
                     "nightly backup at 2am")
        self.b = Bundle(self.root)
        self.d = Dynamics(self.root)

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_coverage_beats_near_duplicates_at_k2(self):
        cue = "postgres port; backup schedule"
        amap = activate(self.b, self.d, cues_from_query(cue))
        # by score alone the near-duplicate would take the second slot
        self.assertGreater(amap.scores["a2.md"], amap.scores["b1.md"])
        pack = context_pack(self.b, self.d, cue, budget=600, k=2,
                            mode="muninn-walk", reactivate=False)
        focus = pack.split("## Focus", 1)[1]
        self.assertIn("### Postgres port config (a1.md)", focus)
        self.assertIn("### Backup schedule (b1.md)", focus)
        self.assertNotIn("### Postgres notes (a2.md)", focus)


class TestBoundedness(unittest.TestCase):
    """Graph support reorders near-peers; it can never bury or manufacture
    a top hit. A graph-only note with maximal support (many feeders,
    heavy use, pinned, goal-aligned) still never outranks the strongest
    lexical hit, which has zero graph support."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        b = Bundle(self.root)
        b.write_note("hero.md", {"title": "Redis eviction policy"},
                     "maxmemory eviction lru tuning")  # no links, no usage
        for i in range(6):
            b.write_note(f"f{i}.md", {"title": f"Cache note {i}"},
                         "eviction tips. [[Ghost node]]")
        b.write_note("ghost.md", {"title": "Ghost node"},
                     "completely unrelated words here")
        self.b = Bundle(self.root)
        self.d = Dynamics(self.root)
        for _ in range(10):  # ghost is heavily used...
            self.d.touch("ghost.md", session="s1")
        for i in range(6):   # ...co-used with every feeder...
            self.d.touch(f"f{i}.md", session="s1")
        self.d.pin("ghost.md")                      # ...pinned...
        self.d.goal("ghost node cleanup", 1.0)      # ...and goal-aligned

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_strong_lexical_hit_never_outranked_by_graph_only_note(self):
        hits = recall_explain(self.b, self.d, "redis eviction policy",
                              k=10, reactivate=False)
        scores = {n.path: s for n, s, _w, _e in hits}
        ev = {n.path: e for n, _s, _w, e in hits}
        self.assertEqual(hits[0][0].path, "hero.md")
        self.assertIn("ghost.md", scores)           # surfaced (reorder...)
        self.assertEqual(ev["ghost.md"].direct, 0.0)
        self.assertEqual(ev["hero.md"].walk, 0.0)   # hero has NO graph support
        self.assertGreater(scores["hero.md"], scores["ghost.md"])  # ...never gate

    def test_graph_support_never_buries_a_stronger_lexical_hit(self):
        """A weak direct hit riding heavy walk inflow must stay below a
        2x-stronger lexical hit with zero graph support: the orphan floor
        applies ONLY to zero-lexical notes (review-panel regression)."""
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        b = Bundle(root)
        b.write_note("a.md", {"title": "alpha bravo charlie delta"},
                     "hub note. see [[weak note]]")     # top hit, feeds c
        b.write_note("b.md", {"title": "plain"},
                     "alpha bravo mentioned in passing")  # stronger lexical
        b.write_note("c.md", {"title": "weak note"},
                     "alpha only, plus heavy graph inflow")  # weaker lexical
        b = Bundle(root)
        d = Dynamics(root)
        hits = recall_explain(b, d, "alpha bravo charlie delta", k=5,
                              reactivate=False)
        scores = {n.path: s for n, s, _w, _e in hits}
        ev = {n.path: e for n, _s, _w, e in hits}
        self.assertGreater(ev["c.md"].walk, 0.0)     # c really gets inflow
        self.assertGreater(ev["b.md"].direct, ev["c.md"].direct)
        self.assertLessEqual(ev["c.md"].gain, 0.5)   # capped, saturating
        self.assertGreater(scores["b.md"], scores["c.md"])  # never buried


class TestPackDeterminism(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        b = Bundle(self.root)
        b.write_note("a.md", {"title": "Alpha service"},
                     "alpha talks to [[Beta service]] over grpc")
        b.write_note("b.md", {"title": "Beta service"},
                     "beta stores blobs; restarts nightly")
        b.write_note("c.md", {"title": "Gamma runbook"},
                     "gamma drains traffic before deploys")
        self.b = Bundle(self.root)
        d = Dynamics(self.root)
        d.touch("a.md", session="s1")
        d.touch("c.md", session="s1")
        d.consolidate()

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_same_state_same_pack(self):
        cue = "alpha grpc; gamma deploys"
        packs = [context_pack(Bundle(self.root), Dynamics(self.root), cue,
                              budget=400, k=3, mode="muninn-walk",
                              reactivate=False)
                 for _ in range(3)]
        self.assertEqual(packs[0], packs[1])
        self.assertEqual(packs[1], packs[2])
        self.assertIn("why loaded", packs[0])


class TestTypedEdgeWeights(unittest.TestCase):
    """Typed links (SPEC §1) carry their confidence weight into the walk:
    extracted 1.0 / inferred 0.5 / ambiguous 0.2. Plain wikilinks stay at
    1.0; when several authored signals connect one pair, the heaviest
    wins; determinism holds with typed edges in the graph."""

    def _root(self, notes):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        b = Bundle(root)
        for path, meta, body in notes:
            b.write_note(path, meta, body)
        return root

    def test_merged_graph_typed_weights_plain_stays_1(self):
        root = self._root([
            ("hub.md", {"title": "Hub"},
             "- feeds [[Left note]] (extracted)\n"
             "- feeds [[Right note]] (ambiguous)\n"
             "- guesses [[Mid note]] (inferred)\n"
             "prose mention of [[Plain note]]\n"
             "- see [[Listy note]] for details"),  # trailing text -> prose
            ("left.md", {"title": "Left note"}, "x"),
            ("right.md", {"title": "Right note"}, "x"),
            ("mid.md", {"title": "Mid note"}, "x"),
            ("plain.md", {"title": "Plain note"}, "x"),
            ("listy.md", {"title": "Listy note"}, "x"),
        ])
        adj = merged_graph(Bundle(root), Dynamics(root))
        self.assertEqual(adj["hub.md"], [
            ("left.md", 1.0, "link"),
            ("listy.md", 1.0, "link"),
            ("mid.md", 0.5, "link"),
            ("plain.md", 1.0, "link"),
            ("right.md", 0.2, "link"),
        ])
        for n, w, k in adj["hub.md"]:  # symmetric at the same weight
            self.assertIn(("hub.md", w, k), adj[n])

    def test_typed_and_plain_on_one_pair_take_the_max(self):
        root = self._root([
            ("a.md", {"title": "A note"}, "- guesses [[B note]] (ambiguous)"),
            ("b.md", {"title": "B note"}, "see [[A note]] in prose"),
            ("c.md", {"title": "C note"}, "- guesses [[D note]] (ambiguous)"),
            ("d.md", {"title": "D note"}, "- refines [[C note]] (inferred)"),
        ])
        adj = merged_graph(Bundle(root), Dynamics(root))
        # plain prose back-link (1.0) outweighs the ambiguous typed edge
        self.assertIn(("b.md", 1.0, "link"), adj["a.md"])
        # typed both ways: the heavier confidence wins
        self.assertIn(("c.md", 0.5, "link"), adj["d.md"])

    def test_ambiguous_carries_less_activation_than_extracted(self):
        """Between otherwise-identical walk-only neighbors of the one
        lexical hit, the ambiguous edge carries measurably less than the
        extracted one: downweighted, never gated."""
        root = self._root([
            ("hub.md", {"title": "Backup policy"},
             "nightly wal-g basebackup to s3\n"
             "- feeds [[Left note]] (extracted)\n"
             "- feeds [[Right note]] (ambiguous)"),
            ("left.md", {"title": "Left note"}, "identical filler body"),
            ("right.md", {"title": "Right note"}, "identical filler body"),
        ])
        amap = activate(Bundle(root), Dynamics(root),
                        [Cue("wal-g basebackup s3")])
        evl = amap.evidence["left.md"]
        evr = amap.evidence["right.md"]
        self.assertEqual((evl.direct, evr.direct), (0.0, 0.0))  # walk-only
        self.assertEqual(evl.carrier, ("hub.md", "link"))
        self.assertEqual(evr.carrier, ("hub.md", "link"))
        self.assertGreater(evl.walk, evr.walk)
        self.assertGreater(amap.scores["right.md"], 0.0)
        self.assertLess(amap.scores["right.md"],
                        0.5 * amap.scores["left.md"])

    def test_confidence_word_flips_topk_ordering(self):
        """Two bundles identical except ONE confidence word: 'extracted'
        lifts the linked note above a weak direct hit, 'ambiguous' drops
        it below: the edge weight alone changes top-k order."""
        def order(conf):
            root = self._root([
                ("hub.md", {"title": "Backup policy"},
                 "nightly wal-g basebackup to s3\n"
                 f"- feeds [[Payload box]] ({conf})\n"
                 "- feeds [[Ballast box]] (extracted)"),
                ("payload.md", {"title": "Payload box"}, "quiet target"),
                ("ballast.md", {"title": "Ballast box"}, "quiet target"),
                ("distractor.md", {"title": "Digest note"},
                 "nightly digest email"),
            ])
            hits = recall_explain(Bundle(root), Dynamics(root),
                                  "wal-g basebackup s3 nightly", k=4,
                                  reactivate=False)
            return [n.path for n, _s, _w, _e in hits]
        ex, am = order("extracted"), order("ambiguous")
        self.assertEqual(ex[0], "hub.md")
        self.assertEqual(am[0], "hub.md")
        self.assertLess(ex.index("payload.md"), ex.index("distractor.md"))
        self.assertGreater(am.index("payload.md"), am.index("distractor.md"))

    def test_determinism_and_couse_merge_with_typed_edges(self):
        root = self._root([
            ("a.md", {"title": "Alpha note"},
             "alpha walks\n- guesses [[Beta note]] (ambiguous)"),
            ("b.md", {"title": "Beta note"}, "beta rests"),
        ])
        d = Dynamics(root)
        d.touch("a.md", session="s1")  # co-use pair a|b at min(1, 1/3)
        d.touch("b.md", session="s1")
        one = merged_graph(Bundle(root), Dynamics(root))
        two = merged_graph(Bundle(root), Dynamics(root))
        self.assertEqual(one, two)
        # the learned edge (1/3) outweighs the ambiguous typed edge (0.2)
        (nbr, w, kind), = one["a.md"]
        self.assertEqual((nbr, kind), ("b.md", "co-use"))
        self.assertAlmostEqual(w, 1 / 3)
        maps = [activate(Bundle(root), Dynamics(root),
                         cues_from_query("alpha walks; beta rests"))
                for _ in range(2)]
        self.assertEqual(maps[0].scores, maps[1].scores)


class TestCueBuilders(unittest.TestCase):
    def test_cues_from_query_facets(self):
        q = 'restore the "backup target" and postgres port 7433'
        cues = cues_from_query(q)
        self.assertEqual(cues[0].origin, "query")  # the full query leads
        origins = {c.origin for c in cues}
        self.assertIn("quote", origins)
        self.assertIn("clause", origins)
        texts = [c.text for c in cues if c.origin == "quote"]
        self.assertIn("backup target", texts)
        # deterministic and dedup'd by token set
        self.assertEqual([c.text for c in cues],
                         [c.text for c in cues_from_query(q)])
        sets = [frozenset(tokens(c.text)) for c in cues]
        self.assertEqual(len(sets), len(set(sets)))

    def test_cues_from_goals(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        d = Dynamics(root)
        d.goal("ship the migration", 0.9)
        d.goal("archive old stuff", 0.4)
        cues = cues_from_goals(d)
        self.assertEqual([(c.text, c.weight, c.origin) for c in cues],
                         [("archive old stuff", 0.4, "goal"),
                          ("ship the migration", 0.9, "goal")])
        self.assertEqual(cues_from_goals(None), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
