"""Verify idempotent ledger synchronization through a dedicated Git reference.

The ledger union preserves distinct events and their causal order. Consumers that
share only a remote converge through fetch, union, replay, and push. The
operation does not modify a working branch and retries a concurrent push.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from muninn import cli  # noqa: E402
from muninn.dynamics import Dynamics  # noqa: E402
from muninn.sync import (SYNC_REF, SyncError, _commit_union,  # noqa: E402
                         _git as sync_git, sync, union_ledgers)

GIT = shutil.which("git")


def _git(wd, *args):
    r = subprocess.run(["git", "-C", wd, *args], capture_output=True,
                       text=True)
    return r.returncode, r.stdout.strip()


class TestUnionLedgers(unittest.TestCase):
    def test_union_dedups_and_orders_by_ts(self):
        a = '{"kind":"touch","note":"a.md","ts":2}\n'
        b = ('{"kind":"touch","note":"b.md","ts":1}\n'
             '{"kind":"touch","note":"a.md","ts":2}\n')  # duplicate of a's
        merged = union_ledgers(a, b)
        lines = merged.splitlines()
        self.assertEqual(len(lines), 2)  # exact duplicate collapsed
        self.assertEqual(json.loads(lines[0])["note"], "b.md")  # ts order

    def test_legacy_predecessor_preserves_an_identified_chain(self):
        legacy = '{"kind":"touch","note":"amber.md","ts":999}'
        first = json.dumps({"kind": "touch", "note": "quartz.md",
                            "sync_clock": 10, "sync_id": "a" * 32})
        second = json.dumps({"kind": "outcome", "valence": 1,
                             "sync_clock": 11, "sync_id": "b" * 32})
        history = "\n".join((legacy, first, second, ""))
        self.assertEqual(union_ledgers(history), history)
        self.assertEqual(union_ledgers(second, history), history)

    def test_one_identified_event_with_different_json_spacing_counts_once(self):
        event = {"kind": "touch", "note": "quartz.md",
                 "sync_clock": 1, "sync_id": "a" * 32}
        compact = json.dumps(event, separators=(",", ":"))
        spaced = json.dumps(event, sort_keys=True)
        self.assertEqual(union_ledgers(compact, spaced), union_ledgers(spaced, compact))
        self.assertEqual(json.loads(union_ledgers(compact, spaced)), event)

    def test_conflicting_histories_raise_without_inventing_an_order(self):
        first = json.dumps({"kind": "touch", "note": "quartz.md",
                            "sync_clock": 1, "sync_id": "a" * 32})
        other = first.replace("quartz.md", "amber.md")
        with self.assertRaisesRegex(SyncError, "one ledger event ID"):
            union_ledgers(first, other)
        with self.assertRaisesRegex(SyncError, "event order"):
            union_ledgers("a\nb\n", "b\na\n")

    def test_union_is_idempotent_and_commutative(self):
        a = '{"ts":1,"kind":"touch","note":"x.md"}\n'
        b = '{"ts":2,"kind":"outcome","valence":0.9}\n'
        self.assertEqual(union_ledgers(a, a), union_ledgers(a))
        self.assertEqual(union_ledgers(a, b), union_ledgers(b, a))
        self.assertEqual(union_ledgers(), "")
        # unparseable / ts-less lines survive (unknown events are preserved)
        junk = "not json at all\n"
        self.assertIn("not json at all", union_ledgers(a, junk))


class TestSyncPlumbingFailures(unittest.TestCase):
    def test_git_helper_swallows_exec_failure(self):
        with mock.patch("muninn.sync.subprocess.run",
                        side_effect=OSError("no git binary")):
            self.assertEqual(sync_git("/anywhere", "status"), (1, ""))

    @unittest.skipUnless(GIT, "git not installed")
    def test_commit_union_outside_a_repo_raises(self):
        bare = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, bare, ignore_errors=True)
        with self.assertRaisesRegex(SyncError, "hash-object failed"):
            _commit_union(bare, '{"ts":1}\n', None)

    @unittest.skipUnless(GIT, "git not installed")
    def test_commit_union_surfaces_mktree_and_commit_failures(self):
        repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        subprocess.run(["git", "-C", repo, "init", "-q"], check=True)
        real = sync_git

        def fail(step):
            def run(r, *args, data=None):
                if step in args:  # commit-tree rides behind -c identity flags
                    return 1, ""
                return real(r, *args, data=data)
            return run

        with mock.patch("muninn.sync._git", side_effect=fail("mktree")):
            with self.assertRaisesRegex(SyncError, "mktree failed"):
                _commit_union(repo, '{"ts":1}\n', None)
        with mock.patch("muninn.sync._git", side_effect=fail("commit-tree")):
            with self.assertRaisesRegex(SyncError, "commit-tree failed"):
                _commit_union(repo, '{"ts":1}\n', None)


@unittest.skipUnless(GIT, "git not installed")
class TestSyncEndToEnd(unittest.TestCase):
    def setUp(self):
        self.top = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.top, ignore_errors=True)
        self.remote = os.path.join(self.top, "remote.git")
        _git(self.top, "init", "-q", "--bare", "remote.git")
        self.a = self._agent("A")
        self.b = self._agent("B")

    def _agent(self, name):
        root = os.path.join(self.top, name)
        os.makedirs(root)
        _git(self.top, "init", "-q", name)
        _git(root, "remote", "add", "origin", self.remote)
        return root

    def test_not_a_repo_fails_loud(self):
        bare = os.path.join(self.top, "no-repo")
        os.makedirs(bare)
        with self.assertRaisesRegex(SyncError, "not a git repository"):
            sync(bare)
        # a root that does not exist at all gets its own message, not a
        # misleading "not a git repository"
        with self.assertRaisesRegex(SyncError, "does not exist"):
            sync(os.path.join(self.top, "never-made"))

    def test_two_agents_converge_via_the_remote(self):
        Dynamics(self.a).touch("pg.md")
        Dynamics(self.b).touch("redis.md")
        r1 = sync(self.a)  # first contact: nothing to pull, pushes
        self.assertEqual(r1["pulled"], 0)
        self.assertTrue(r1["pushed"])
        r2 = sync(self.b)  # B learns A's event, publishes the union
        self.assertEqual(r2["pulled"], 1)
        self.assertIn("pg.md", Dynamics(self.b).entries)
        r3 = sync(self.a)  # A learns B's event back
        self.assertEqual(r3["pulled"], 1)
        self.assertIn("redis.md", Dynamics(self.a).entries)
        # convergence: both sidecars now replay the identical union
        self.assertEqual(r3["events"], 2)
        self.assertEqual(set(Dynamics(self.a).entries),
                         set(Dynamics(self.b).entries))

    def test_intents_travel(self):
        Dynamics(self.a).intent("agent-a/migrate", paths=["db/schema.sql"],
                                goal="migrate the db")
        sync(self.a)
        sync(self.b)
        self.assertIn("agent-a/migrate", Dynamics(self.b).active_intents())

    def test_sync_never_touches_working_branches(self):
        Dynamics(self.a).touch("pg.md")
        sync(self.a)
        code, out = _git(self.remote, "for-each-ref", "--format=%(refname)")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [SYNC_REF])

    def test_empty_ledgers_push_no_ref_noise(self):
        # a fresh bundle with zero events has nothing to say: syncing must
        # not publish an empty ledger commit just to say so
        r = sync(self.a)
        self.assertTrue(r["pushed"])  # vacuously in sync
        self.assertEqual(r["events"], 0)
        code, out = _git(self.remote, "for-each-ref")
        self.assertEqual(out, "")

    def test_pull_only_publishes_nothing(self):
        Dynamics(self.a).touch("pg.md")
        r = sync(self.a, push=False)
        self.assertFalse(r["pushed"])
        code, out = _git(self.remote, "for-each-ref")
        self.assertEqual(out, "")

    def test_distinct_simultaneous_events_survive_repeated_sync(self):
        with mock.patch("muninn.dynamics.time.time", return_value=112.5):
            for root in (self.a, self.b):
                Dynamics(root).touch("quartz.md")
                Dynamics(root).touch("quartz.md")
        self.assertEqual(sync(self.a)["events"], 2)
        self.assertEqual(sync(self.b)["pulled"], 2)
        self.assertEqual(sync(self.a)["pulled"], 2)
        self.assertEqual(sync(self.b)["pulled"], 0)
        for root in (self.a, self.b):
            memory = Dynamics(root)
            self.assertEqual(memory.entries["quartz.md"]["recurrence"], 4)
            with open(memory.ledger_path, encoding="utf-8") as ledger:
                self.assertEqual(len({line for line in ledger}), 4)

    def test_append_after_pull_preserves_causality_despite_clock_rollback(self):
        with mock.patch("muninn.dynamics.time.time", return_value=200):
            first = Dynamics(self.a)
            first.pin("quartz.md", True)
            first.pin("quartz.md", False)
            first.touch("quartz.md")
        sync(self.a)
        sync(self.b, push=False)
        with mock.patch("muninn.dynamics.time.time", return_value=100):
            second = Dynamics(self.b)
            second.pin("quartz.md", True)
            second.outcome(1)
        sync(self.b)
        sync(self.a)
        for root in (self.a, self.b):
            memory = Dynamics(root)
            self.assertTrue(memory.entries["quartz.md"]["pinned"])
            self.assertGreater(memory.entries["quartz.md"]["captured"], 0)
            memory.replay()
            self.assertGreater(memory.entries["quartz.md"]["captured"], 0)
            self.assertEqual(sync(root)["pulled"], 0)

    def test_legacy_multiplicity_order_and_mixed_append(self):
        touch = '{"kind":"touch","note":"quartz.md","ts":3}\n'
        outcome = '{"kind":"outcome","valence":1,"ts":2}\n'
        for root, raw in ((self.a, touch + touch + outcome), (self.b, touch)):
            directory = os.path.join(root, ".muninn")
            os.mkdir(directory)
            with open(os.path.join(directory, "ledger.jsonl"), "w") as ledger:
                ledger.write(raw)
        self.assertEqual(sync(self.a)["events"], 3)
        self.assertEqual(sync(self.b)["pulled"], 2)
        with mock.patch("muninn.dynamics.time.time", return_value=1):
            Dynamics(self.b).touch("amber.md")
        sync(self.b)
        self.assertEqual(sync(self.a)["events"], 4)
        for root in (self.a, self.b):
            memory = Dynamics(root)
            self.assertEqual(memory.entries["quartz.md"]["recurrence"], 2)
            self.assertGreater(memory.entries["quartz.md"]["captured"], 0)
            self.assertEqual(memory.entries["amber.md"]["captured"], 0)
            self.assertEqual(sync(root)["pulled"], 0)

    def test_older_writer_appends_between_identified_events(self):
        with mock.patch("muninn.dynamics.time.time", return_value=20):
            Dynamics(self.a).touch("amber.md")
        sync(self.a)
        sync(self.b, push=False)
        with open(Dynamics(self.b).ledger_path, "a", encoding="utf-8") as ledger:
            ledger.write('{"kind":"touch","note":"quartz.md","ts":1000}\n')
        with mock.patch("muninn.dynamics.time.time", return_value=5):
            Dynamics(self.b).outcome(1)
        self.assertEqual(sync(self.b)["events"], 3)
        self.assertEqual(sync(self.a)["pulled"], 2)
        for root in (self.a, self.b):
            memory = Dynamics(root)
            self.assertGreater(memory.entries["quartz.md"]["captured"], 0)
            self.assertGreater(memory.entries["amber.md"]["captured"], 0)
            self.assertEqual(sync(root)["pulled"], 0)

    def test_local_append_during_fetch_survives_union_and_replay(self):
        import muninn.sync as msync
        Dynamics(self.a).touch("remote.md")
        sync(self.a)
        Dynamics(self.b).touch("initial.md")
        real = msync._git

        def append_during_fetch(repo, *args, data=None):
            if args[0] == "fetch":
                Dynamics(self.b).touch("concurrent.md")
            return real(repo, *args, data=data)

        with mock.patch.object(msync, "_git", side_effect=append_during_fetch):
            result = sync(self.b, push=False)
        self.assertEqual(result["events"], 3)
        self.assertEqual(result["pulled"], 1)
        self.assertEqual(set(Dynamics(self.b).entries),
                         {"remote.md", "initial.md", "concurrent.md"})

    def test_stale_writer_cannot_cache_pre_union_event_order(self):
        with mock.patch("muninn.dynamics.time.time", return_value=100):
            writer = Dynamics(self.a)
            writer.touch("quartz.md")
        with mock.patch("muninn.dynamics.time.time", return_value=200):
            Dynamics(self.b).outcome(1)
        sync(self.b)
        sync(self.a, push=False)
        captured = Dynamics(self.a).entries["quartz.md"]["captured"]
        self.assertGreater(captured, 0)
        writer.touch("amber.md")
        self.assertEqual(Dynamics(self.a).entries["quartz.md"]["captured"], captured)

    def test_stale_writers_issue_clocks_after_concurrent_appends(self):
        from concurrent.futures import ThreadPoolExecutor

        writers = [Dynamics(self.a) for _ in range(8)]
        with mock.patch("muninn.dynamics.time.time", return_value=100):
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda writer: writer.touch("amber.md"), writers))
        memory = Dynamics(self.a)
        with open(memory.ledger_path, encoding="utf-8") as ledger:
            events = [json.loads(line) for line in ledger]
        self.assertEqual(len({e["sync_id"] for e in events}), 8)
        self.assertEqual([e["sync_clock"] for e in events], list(range(100000, 100008)))
        self.assertEqual(memory.entries["amber.md"]["recurrence"], 8)

    def test_append_preserves_a_legacy_last_record_without_a_newline(self):
        directory = os.path.join(self.a, ".muninn")
        os.mkdir(directory)
        with open(os.path.join(directory, "ledger.jsonl"), "w") as ledger:
            ledger.write('{"kind":"touch","note":"amber.md","ts":2}')
        Dynamics(self.a).touch("quartz.md")
        self.assertEqual(sync(self.a)["events"], 2)
        self.assertEqual(sync(self.b)["pulled"], 2)
        self.assertEqual(set(Dynamics(self.b).entries), {"amber.md", "quartz.md"})

    def test_old_fetch_reference_does_not_hide_remote_history(self):
        Dynamics(self.a).touch("amber.md")
        sync(self.a)
        self.assertEqual(_git(self.b, "fetch", "origin", SYNC_REF + ":refs/muninn/incoming")[0], 0)
        self.assertEqual(sync(self.b, push=False)["pulled"], 1)

    def test_lost_push_race_reunions_and_converges(self):
        # the race: B fetches BEFORE A pushes (simulated by blinding B's
        # first fetch), so B builds a parentless commit, its push is
        # rejected as non-fast-forward, and the retry loop must re-fetch,
        # re-union, and land: convergence, not failure.
        import muninn.sync as msync
        Dynamics(self.a).touch("pg.md")
        Dynamics(self.b).touch("redis.md")
        sync(self.a)
        real, state = msync._git, {"blinded": False}

        def race(repo, *args, data=None):
            if args and args[0] == "fetch" and not state["blinded"]:
                state["blinded"] = True  # B "fetched" pre-push: ref absent
                return 1, ""
            return real(repo, *args, data=data)

        with mock.patch.object(msync, "_git", side_effect=race):
            r = msync.sync(self.b)
        self.assertTrue(state["blinded"])
        self.assertTrue(r["pushed"])
        self.assertEqual(r["events"], 2)
        # the retry's re-union also landed A's event LOCALLY, not just on
        # the remote
        self.assertIn("pg.md", Dynamics(self.b).entries)
        self.assertEqual(sync(self.a)["events"], 2)

    def test_cli_summary_line(self):
        Dynamics(self.a).touch("pg.md")
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--root", self.a, "sync"])
        out = buf.getvalue()
        self.assertIn("synced:", out)
        self.assertIn("pushed", out)

    def test_offline_push_raises_but_keeps_local_union(self):
        _git(self.a, "remote", "set-url", "origin",
             os.path.join(self.top, "gone.git"))
        Dynamics(self.a).touch("pg.md")
        with self.assertRaises(SyncError):
            sync(self.a)
        self.assertIn("pg.md", Dynamics(self.a).entries)  # nothing lost


if __name__ == "__main__":
    unittest.main()
