"""Tests for the public persistent source-retrieval workflow."""

from __future__ import annotations

import io
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from muninn import cli, source_retrieval as retrieval
from muninn.persistent_code_search import PersistentCodeSearchIndex
from muninn.source_retrieval import (
    _read_indexed_source,
    ensure_source_index,
    resolve_source_root,
    search_source,
    source_database,
    source_snapshot,
)


class TestSourceRetrievalCLI(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.brain = os.path.join(self.directory, "brain")
        self.source = os.path.join(self.directory, "project")
        os.makedirs(os.path.join(self.source, "ledger"))
        Path(self.source, "ledger", "replay.py").write_text(
            '"""Restore derived state from immutable events."""\n'
            "def replay_ledger(events):\n"
            "    return list(events)\n",
            encoding="utf-8",
        )
        Path(self.source, "session.py").write_text(
            "def open_session():\n    return 'session'\n",
            encoding="utf-8",
        )

    def tearDown(self):
        shutil.rmtree(self.directory)

    def run_cli(self, *arguments):
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            cli.main(["--root", self.brain, *arguments])
        return output.getvalue(), errors.getvalue()

    def test_database_is_private_stable_and_repository_specific(self):
        first = source_database(self.brain, self.source)
        second = source_database(self.brain, self.source)
        other = os.path.join(self.directory, "other")
        os.makedirs(other)

        self.assertEqual(first, second)
        self.assertNotEqual(first, source_database(self.brain, other))
        self.assertIn(os.path.join(".muninn", "source-indexes"), first)

    def test_old_ascii_postings_are_rebuilt_for_unicode_search(self):
        Path(self.source, "tokyo.py").write_text(
            '"""東京 office configuration."""\n', encoding="utf-8")
        index, _ = ensure_source_index(self.brain, self.source)
        index.close()
        with sqlite3.connect(source_database(self.brain, self.source)) as db:
            db.execute("UPDATE metadata SET value = '4' "
                       "WHERE key = 'schema_version'")
        refreshed, rebuilt = ensure_source_index(self.brain, self.source)
        try:
            self.assertTrue(rebuilt)
            self.assertTrue(refreshed.search("東京"))
        finally:
            refreshed.close()

    def test_snapshot_changes_when_a_source_file_changes(self):
        before = source_snapshot(self.source)
        Path(self.source, "session.py").write_text(
            "def open_session():\n    return 'updated-session'\n",
            encoding="utf-8",
        )
        after = source_snapshot(self.source)

        self.assertNotEqual(before, after)

    @unittest.skipUnless(shutil.which("git"), "git is unavailable")
    def test_gitignored_indexed_source_edits_refresh_the_index(self):
        for arguments in (("init", "-q"), ("add", "."),
                          ("-c", "user.name=Test", "-c",
                           "user.email=test@example.invalid", "commit", "-qm",
                           "Initialize source fixture")):
            subprocess.run(["git", "-C", self.source, *arguments], check=True)
        Path(self.source, ".git", "info", "exclude").write_text(
            "ignored.py\nnode_modules/\n", encoding="utf-8")
        ignored = Path(self.source, "ignored.py")
        ignored.write_text("def orionfunction(): pass\n", encoding="utf-8")
        first, _ = ensure_source_index(self.brain, self.source)
        self.assertIn("ignored.py", first.paths)
        first.close()
        before = ignored.stat()
        ignored.write_text("def vegasfunction(): pass\n", encoding="utf-8")
        os.utime(ignored, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
        second, rebuilt = ensure_source_index(self.brain, self.source)
        try:
            self.assertTrue(rebuilt)
            self.assertTrue(second.search("vegasfunction"))
            self.assertFalse(second.search("orionfunction"))
        finally:
            second.close()

        snapshot = source_snapshot(self.source)
        excluded = Path(self.source, "node_modules")
        excluded.mkdir()
        (excluded / "ignored.py").write_text("def vendorfunction(): pass\n",
                                             encoding="utf-8")
        self.assertEqual(snapshot, source_snapshot(self.source))
        ignored.unlink()
        third, rebuilt = ensure_source_index(self.brain, self.source)
        try:
            self.assertTrue(rebuilt)
            self.assertNotIn("ignored.py", third.paths)
        finally:
            third.close()

    def test_git_runner_fails_soft_on_errors_and_nonzero_status(self):
        with mock.patch(
            "muninn.source_retrieval.subprocess.run",
            side_effect=OSError("missing"),
        ):
            self.assertIsNone(retrieval._git(self.source, "status"))
        with mock.patch(
            "muninn.source_retrieval.subprocess.run",
            side_effect=retrieval.subprocess.TimeoutExpired("git", 10),
        ):
            self.assertIsNone(retrieval._git(self.source, "status"))
        completed = retrieval.subprocess.CompletedProcess(
            ["git"],
            1,
            stdout=b"ignored",
        )
        with mock.patch(
            "muninn.source_retrieval.subprocess.run",
            return_value=completed,
        ):
            self.assertIsNone(retrieval._git(self.source, "status"))

    def test_git_snapshot_hashes_changed_and_untracked_source(self):
        outputs = [
            b"abc123\n",
            b"session.py\0deleted.py\0",
            b"ledger/replay.py\0",
        ]
        with mock.patch("muninn.source_retrieval._git", side_effect=outputs):
            first = retrieval._git_snapshot(self.source)
        Path(self.source, "session.py").write_text(
            "def open_session():\n    return 'changed'\n",
            encoding="utf-8",
        )
        with mock.patch("muninn.source_retrieval._git", side_effect=outputs):
            second = retrieval._git_snapshot(self.source)

        self.assertTrue(first.startswith("git:"))
        self.assertNotEqual(first, second)

    def test_git_snapshot_falls_back_when_required_queries_fail(self):
        with mock.patch("muninn.source_retrieval._git", return_value=None):
            self.assertIsNone(retrieval._git_snapshot(self.source))
        with mock.patch(
            "muninn.source_retrieval._git",
            side_effect=[b"head", None, b""],
        ):
            self.assertIsNone(retrieval._git_snapshot(self.source))

    def test_relevant_file_fails_soft_when_metadata_cannot_be_read(self):
        path = mock.Mock()
        path.suffix = ".py"
        path.is_symlink.return_value = False
        path.is_file.return_value = True
        path.stat.side_effect = OSError("unreadable")

        self.assertFalse(retrieval._relevant_file(path))
        with mock.patch(
            "muninn.source_retrieval._git",
            side_effect=[b"head", b"", None],
        ):
            self.assertIsNone(retrieval._git_snapshot(self.source))

    def test_changed_file_hash_marks_unreadable_and_excluded_paths(self):
        excluded = hashlib.sha256()
        retrieval._hash_changed_file(excluded, self.source, b"missing.py")
        expected_excluded = hashlib.sha256()
        expected_excluded.update(b"missing.py\0absent-or-excluded\0")
        self.assertEqual(excluded.digest(), expected_excluded.digest())

        digest = hashlib.sha256()
        with mock.patch.object(Path, "read_bytes", side_effect=OSError):
            retrieval._hash_changed_file(digest, self.source, b"session.py")
        expected = hashlib.sha256()
        expected.update(b"session.py\0unreadable\0\0")
        self.assertEqual(digest.digest(), expected.digest())

    def test_resolve_source_root_rejects_a_file_and_missing_path(self):
        with self.assertRaises(NotADirectoryError):
            resolve_source_root(Path(self.source, "session.py"))
        with self.assertRaises(NotADirectoryError):
            resolve_source_root(Path(self.source, "missing"))

    def test_source_index_command_builds_an_atomic_private_index(self):
        output, errors = self.run_cli("source", "index", self.source)

        self.assertIn("indexed 2 source files", output)
        self.assertEqual(errors, "")
        database = source_database(self.brain, self.source)
        self.assertTrue(os.path.isfile(database))
        self.assertFalse(any(
            name.startswith(".code-index-")
            for name in os.listdir(os.path.dirname(database))
        ))

    def test_source_search_builds_then_reuses_the_current_index(self):
        first_output, first_errors = self.run_cli(
            "source", "search", "immutable ledger state", "--path", self.source,
        )
        second_output, second_errors = self.run_cli(
            "source", "search", "immutable ledger state", "--path", self.source,
        )

        self.assertIn("`ledger/replay.py`", first_output)
        self.assertIn("Restore derived state", first_output)
        self.assertIn("source index refreshed: 2 files", first_errors)
        self.assertEqual(second_output, first_output)
        self.assertEqual(second_errors, "")

    def test_source_search_refreshes_after_a_source_change(self):
        self.run_cli(
            "source", "search", "session", "--path", self.source,
        )
        Path(self.source, "new.py").write_text(
            "def create_audit_record():\n    return 'audit'\n",
            encoding="utf-8",
        )

        output, errors = self.run_cli(
            "source", "search", "create audit record", "--path", self.source,
        )

        self.assertIn("`new.py`", output)
        self.assertIn("source index refreshed: 3 files", errors)

    def test_no_refresh_uses_the_existing_snapshot(self):
        index, rebuilt = ensure_source_index(self.brain, self.source)
        index.close()
        self.assertTrue(rebuilt)
        Path(self.source, "new.py").write_text("def new(): pass\n", encoding="utf-8")

        stale, rebuilt = ensure_source_index(
            self.brain,
            self.source,
            refresh=False,
        )
        try:
            self.assertFalse(rebuilt)
            self.assertNotIn("new.py", stale.paths)
        finally:
            stale.close()

    def test_json_output_reports_ranks_and_source_lines(self):
        output, errors = self.run_cli(
            "source",
            "search",
            "replay ledger",
            "--path",
            self.source,
            "--json",
        )
        result = json.loads(output)

        self.assertEqual(errors, "")
        self.assertEqual(result["mode"], "hybrid")
        self.assertTrue(result["index_rebuilt"])
        self.assertEqual(result["hits"][0]["path"], "ledger/replay.py")
        self.assertIn("bm25f", result["hits"][0]["arms"])
        self.assertGreaterEqual(result["hits"][0]["start_line"], 1)

    def test_empty_result_and_small_budget_have_explicit_outputs(self):
        empty, _errors = self.run_cli(
            "source", "search", "zyxwvutsrq", "--path", self.source,
        )
        small, small_errors = self.run_cli(
            "source", "search", "ledger", "--path", self.source,
            "--budget", "1",
        )

        self.assertIn("No source file matched", empty)
        self.assertEqual(small, "")
        self.assertIn("No source excerpt fit", small_errors)

    def test_source_stdout_respects_small_and_large_byte_budgets(self):
        for budget in (1, 2, 10, 30, 100):
            for query in ("ledger", "unmatchedzyxwv"):
                with self.subTest(budget=budget, query=query):
                    output, _ = self.run_cli(
                        "source", "search", query, "--path", self.source,
                        "--budget", str(budget))
                    self.assertLessEqual(len(output.encode("utf-8")), budget * 4)
                    result = search_source(self.brain, self.source, query,
                                           budget=budget)
                    self.assertLessEqual(result.pack.estimated_tokens, budget)

    def test_search_rejects_an_empty_query(self):
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            search_source(self.brain, self.source, "   ")

    def test_indexed_paths_cannot_escape_the_source_root(self):
        outside = Path(self.directory, "outside.py")
        outside.write_text("secret = 'outside'\n", encoding="utf-8")

        self.assertIsNone(_read_indexed_source(self.source, "../outside.py"))

    def test_indexed_source_rejects_links_missing_files_and_large_files(self):
        linked = Path(self.source, "linked.py")
        try:
            linked.symlink_to(Path(self.source, "session.py"))
        except OSError as error:
            self.skipTest(f"symbolic links are unavailable: {error}")
        self.assertIsNone(_read_indexed_source(self.source, "linked.py"))
        self.assertIsNone(_read_indexed_source(self.source, "missing.py"))

        large = Path(self.source, "large.py")
        large.write_bytes(b"x" * (retrieval.MAX_SOURCE_BYTES + 1))
        self.assertIsNone(_read_indexed_source(self.source, "large.py"))

    def test_indexed_source_fails_soft_on_a_read_error(self):
        with mock.patch.object(Path, "read_bytes", side_effect=OSError):
            self.assertIsNone(_read_indexed_source(self.source, "session.py"))

    def test_corrupt_index_is_rebuilt(self):
        database = source_database(self.brain, self.source)
        os.makedirs(os.path.dirname(database), exist_ok=True)
        Path(database).write_text("not sqlite", encoding="utf-8")

        index, rebuilt = ensure_source_index(self.brain, self.source)
        try:
            self.assertTrue(rebuilt)
            self.assertEqual(index.document_count, 2)
        finally:
            index.close()

    def test_index_with_the_wrong_source_root_is_rebuilt(self):
        database = source_database(self.brain, self.source)
        wrong = os.path.join(self.directory, "wrong")
        os.makedirs(wrong)
        index = PersistentCodeSearchIndex.build_records(
            [],
            database,
            source_root=wrong,
            source_snapshot=source_snapshot(self.source),
        )
        index.close()

        current, rebuilt = ensure_source_index(self.brain, self.source)
        try:
            self.assertTrue(rebuilt)
            self.assertEqual(current.metadata["source_root"],
                             os.path.realpath(self.source))
        finally:
            current.close()

    def test_force_rebuild_replaces_a_valid_index(self):
        first, _rebuilt = ensure_source_index(self.brain, self.source)
        first.close()
        forced, rebuilt = ensure_source_index(
            self.brain,
            self.source,
            force=True,
        )
        try:
            self.assertTrue(rebuilt)
            self.assertEqual(forced.metadata["source_snapshot"],
                             source_snapshot(self.source))
        finally:
            forced.close()

    def test_persistent_build_accepts_a_source_snapshot(self):
        database = os.path.join(self.directory, "direct.sqlite")
        index = PersistentCodeSearchIndex.build(
            self.source,
            database,
            source_snapshot="fixed-snapshot",
        )
        try:
            self.assertEqual(index.metadata["source_snapshot"], "fixed-snapshot")
            self.assertEqual(index.metadata["source_root"],
                             os.path.realpath(self.source))
        finally:
            index.close()

    def test_machine_parser_rejects_nonpositive_controls(self):
        for option in ("--budget", "--limit"):
            with self.subTest(option=option), self.assertRaises(SystemExit):
                self.run_cli(
                    "source", "search", "ledger", "--path", self.source,
                    option, "0",
                )

    def test_cli_reports_source_index_and_search_errors_without_tracebacks(self):
        missing = os.path.join(self.source, "missing")
        with self.assertRaisesRegex(SystemExit, "muninn source index"):
            self.run_cli("source", "index", missing)
        with self.assertRaisesRegex(SystemExit, "muninn source search"):
            self.run_cli("source", "search", "query", "--path", missing)

    def test_stale_index_omits_a_file_removed_during_no_refresh_search(self):
        index, _rebuilt = ensure_source_index(self.brain, self.source)
        index.close()
        Path(self.source, "ledger", "replay.py").unlink()

        result = search_source(
            self.brain,
            self.source,
            "immutable ledger state",
            refresh=False,
        )

        self.assertIn("ledger/replay.py", [hit.path for hit in result.hits])
        self.assertIn("ledger/replay.py", result.pack.omitted_paths)


if __name__ == "__main__":
    unittest.main()
