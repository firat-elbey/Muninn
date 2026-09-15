"""Verify idempotent ledger synchronization through a dedicated Git reference.

The ledger union is conflict-free and timestamp-ordered. Two consumers that
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
        writer = Dynamics(self.a)
        writer._apply({"kind": "touch", "note": "a.md", "ts": 2})
        writer._apply({"kind": "outcome", "valence": 1, "ts": 1})
        writer._save()
        self.assertGreater(writer.entries["a.md"]["captured"], 0)
        sync(self.a, push=False)
        self.assertEqual(Dynamics(self.a).entries["a.md"]["captured"], 0)
        writer.touch("b.md")
        self.assertEqual(Dynamics(self.a).entries["a.md"]["captured"], 0)

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
