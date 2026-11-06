"""Frequency-aware lexical retrieval and rank-fusion tests."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from muninn.lexical import (
    BM25FIndex,
    LexicalDocument,
    diverse_protected_rank_fusion,
    protected_rank_fusion,
    reciprocal_rank_fusion,
)


class TestBM25F(unittest.TestCase):
    def test_rare_identifier_outweighs_common_prose(self):
        index = BM25FIndex([
            LexicalDocument("target", {
                "path": "src/session_store.py",
                "body": "session state calls reconcile_ledger",
            }),
            LexicalDocument("common-a", {
                "path": "docs/session.md",
                "body": "session state session state session state",
            }),
            LexicalDocument("common-b", {
                "path": "tests/test_session.py",
                "body": "session state test",
            }),
        ])
        hits = index.search("session state reconcile ledger")
        self.assertEqual(hits[0].key, "target")
        self.assertIn("reconcile", dict(hits[0].terms))

    def test_path_field_can_resolve_an_exact_filename(self):
        index = BM25FIndex([
            LexicalDocument("src/observe.py", {
                "path": "src observe.py", "body": "record file use"}),
            LexicalDocument("docs/observe.md", {
                "path": "docs observe.md", "body": "observe a process"}),
        ])
        self.assertEqual(index.search("src observe.py")[0].key,
                         "src/observe.py")

    def test_length_normalization_prefers_concentrated_evidence(self):
        index = BM25FIndex([
            LexicalDocument("short", {
                "body": "ledger replay repairs state"}),
            LexicalDocument("long", {
                "body": "ledger " + "unrelated " * 200}),
        ])
        self.assertEqual(index.search("ledger")[0].key, "short")

    def test_empty_index_and_unknown_query_return_no_hits(self):
        self.assertEqual(BM25FIndex([]).search("ledger"), [])
        index = BM25FIndex([LexicalDocument("a", {"body": "ledger"})])
        self.assertEqual(index.search("unmentioned"), [])


class TestReciprocalRankFusion(unittest.TestCase):
    def test_consensus_beats_a_single_first_place(self):
        fused = reciprocal_rank_fusion([
            ["single", "consensus"],
            ["consensus", "other"],
        ])
        self.assertEqual(fused[0][0], "consensus")

    def test_duplicate_in_one_ranking_counts_once(self):
        once = reciprocal_rank_fusion([["a", "b"]])
        duplicate = reciprocal_rank_fusion([["a", "a", "b"]])
        self.assertEqual(once, duplicate)

    def test_invalid_parameters_fail_explicitly(self):
        with self.assertRaises(ValueError):
            reciprocal_rank_fusion([["a"]], rank_constant=-1)
        with self.assertRaises(ValueError):
            reciprocal_rank_fusion([["a"]], weights=[1.0, 2.0])


class TestProtectedRankFusion(unittest.TestCase):
    def test_primary_prefix_retains_its_order(self):
        fused = protected_rank_fusion(
            ["primary-a", "primary-b", "other"],
            [
                ["primary-a", "primary-b", "other"],
                ["other", "secondary"],
            ],
            protected=2,
        )

        self.assertEqual(
            [key for key, _score in fused[:3]],
            ["primary-a", "primary-b", "other"],
        )

    def test_duplicate_primary_keys_do_not_consume_prefix_slots(self):
        fused = protected_rank_fusion(
            ["a", "a", "b", "c"],
            [["c", "b", "a"]],
            protected=2,
        )

        self.assertEqual([key for key, _score in fused[:2]], ["a", "b"])

    def test_limit_and_invalid_prefix_are_explicit(self):
        self.assertEqual(
            len(protected_rank_fusion(
                ["a"], [["a", "b"]], protected=1, limit=1)),
            1,
        )
        with self.assertRaises(ValueError):
            protected_rank_fusion(["a"], [["a"]], protected=-1)

    def test_diverse_prefix_uses_novel_items_from_each_source(self):
        fused = diverse_protected_rank_fusion(
            [
                (["semantic-a", "shared", "semantic-b"], 2),
                (["shared", "structural-a", "structural-b"], 1),
            ],
            [
                ["semantic-a", "shared", "semantic-b"],
                ["shared", "structural-a", "structural-b"],
            ],
        )

        self.assertEqual(
            [key for key, _score in fused[:3]],
            ["semantic-a", "shared", "structural-a"],
        )

    def test_diverse_prefix_rejects_a_negative_source_count(self):
        with self.assertRaises(ValueError):
            diverse_protected_rank_fusion(
                [(["a"], -1)],
                [["a"]],
            )


if __name__ == "__main__":
    unittest.main()
