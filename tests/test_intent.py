"""Verify expiring, branch-specific work announcements.

Declaring an intent refreshes it, `done` retires it, and an unrefreshed intent
expires at read time without changing replay. Primed packs mark active intents
whose paths overlap the current changes. An intent never locks a path.
"""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from muninn import cli  # noqa: E402
from muninn.dynamics import INTENT_TTL, Dynamics  # noqa: E402
from muninn.observe import (current_branch, inflight_section,  # noqa: E402
                            observe_event, _paths_overlap)
from muninn.store import Bundle  # noqa: E402

GIT = shutil.which("git")


def _git(wd, *args):
    subprocess.run(["git", "-C", wd, *args], check=True,
                   capture_output=True, text=True)


class TestIntentEvents(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_declare_refresh_done(self):
        d = Dynamics(self.root)
        d.intent("agent/x", paths=["src/a.py"], goal="fix a")
        self.assertIn("agent/x", d.active_intents())
        i = d.active_intents()["agent/x"]
        self.assertEqual(i["paths"], ["src/a.py"])
        self.assertEqual(i["goal"], "fix a")
        self.assertAlmostEqual(i["expires"] - i["ts"], INTENT_TTL, delta=1)
        # re-declaring the same branch REFRESHES (heartbeat), not duplicates
        d.intent("agent/x", paths=["src/b.py"], goal="fix b")
        self.assertEqual(len(d.intents), 1)
        self.assertEqual(d.active_intents()["agent/x"]["goal"], "fix b")
        d.intent_done("agent/x")
        self.assertEqual(d.active_intents(), {})

    def test_ttl_expiry_is_read_time_only(self):
        d = Dynamics(self.root)
        d.intent("agent/x", ttl=3600)
        now = time.time()
        self.assertIn("agent/x", d.active_intents(now))
        self.assertNotIn("agent/x", d.active_intents(now + 3601))
        # stored state never mutates with the clock (replay determinism)
        self.assertIn("agent/x", d.intents)

    def test_replay_rebuilds_intents(self):
        d = Dynamics(self.root)
        d.intent("agent/x", paths=["a.py"], goal="fix")
        d.intent("agent/y", goal="docs")
        d.intent_done("agent/y")
        os.remove(d.state_path)  # force a full ledger replay
        fresh = Dynamics(self.root)
        self.assertEqual(set(fresh.intents), {"agent/x"})
        self.assertEqual(fresh.intents["agent/x"]["paths"], ["a.py"])
        # and the warm-cache path carries them too
        warm = Dynamics(self.root)
        self.assertEqual(set(warm.intents), {"agent/x"})

    def test_sense_waist_refuses_intent(self):
        # SPEC: no sense can invent new ledger shapes: intents are a
        # DELIBERATE announcement (CLI/library), never an ambient one
        kb = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, kb, ignore_errors=True)
        Bundle(kb).write_note("a.md", {"title": "A"}, "a")
        d = Dynamics(kb)
        self.assertIsNone(observe_event(Bundle(kb), d, "intent", "a.md"))
        self.assertEqual(d.intents, {})

    def test_malformed_intent_events_are_harmless(self):
        d = Dynamics(self.root)
        d._apply({"kind": "intent"})                       # no branch
        d._apply({"kind": "intent", "branch": "  "})       # blank branch
        d._apply({"kind": "intent", "branch": "b", "ttl": "soon",
                  "paths": "not-a-list"})                  # junk fields
        self.assertEqual(d.intents["b"]["paths"], [])
        self.assertAlmostEqual(d.intents["b"]["expires"] - d.intents["b"]["ts"],
                               INTENT_TTL, delta=1)
        self.assertEqual(len(d.intents), 1)


class TestInflightSection(unittest.TestCase):
    def setUp(self):
        self.top = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.top, ignore_errors=True)
        self.kb = os.path.join(self.top, "kb")
        os.makedirs(self.kb)
        Bundle(self.kb).write_note("store.md", {"title": "Store"}, "the store")

    def test_empty_without_intents(self):
        self.assertEqual(
            inflight_section(Bundle(self.kb), Dynamics(self.kb),
                             workdir=self.top), "")

    def test_renders_and_bounds(self):
        d = Dynamics(self.kb)
        for n in range(10):
            d.intent(f"agent/{n}", goal=f"job {n}")
        out = inflight_section(Bundle(self.kb), d, workdir=self.top)
        self.assertIn("## In flight", out)
        self.assertIn("... and 2 more", out)  # INFLIGHT_SHOWN = 8
        self.assertIn("awareness, not locks", out)

    @unittest.skipUnless(GIT, "git not installed")
    def test_overlap_flag_against_changed_files(self):
        repo = os.path.join(self.top, "repo")
        os.makedirs(os.path.join(repo, "src"))
        _git(self.top, "init", "-q", "repo")
        _git(repo, "checkout", "-q", "-b", "me/branch")
        with open(os.path.join(repo, "src", "store.py"), "w") as fh:
            fh.write("x")
        d = Dynamics(self.kb)
        d.intent("other/branch", paths=["src/store.py"], goal="atomic writes")
        d.intent("elsewhere/branch", paths=["docs/README.md"], goal="docs")
        out = inflight_section(Bundle(self.kb), d, workdir=repo)
        lines = {ln.split("`")[1]: ln for ln in out.splitlines()
                 if ln.startswith("- `")}
        self.assertIn("overlaps your working set: src/store.py",
                      lines["other/branch"])
        self.assertNotIn("overlaps", lines["elsewhere/branch"])

    @unittest.skipUnless(GIT, "git not installed")
    def test_own_branch_is_labelled_not_warned(self):
        repo = os.path.join(self.top, "repo")
        _git(self.top, "init", "-q", "repo")
        _git(repo, "checkout", "-q", "-b", "me/branch")
        with open(os.path.join(repo, "f.py"), "w") as fh:
            fh.write("x")
        d = Dynamics(self.kb)
        d.intent("me/branch", paths=["f.py"], goal="my own work")
        out = inflight_section(Bundle(self.kb), d, workdir=repo)
        self.assertIn("current branch announcement", out)
        self.assertNotIn("overlaps your working set", out)


class TestPathsOverlap(unittest.TestCase):
    def test_equal_and_boundary_suffix(self):
        self.assertTrue(_paths_overlap("src/store.py", "src/store.py"))
        self.assertTrue(_paths_overlap("src/store.py", "repo/src/store.py"))
        self.assertTrue(_paths_overlap("repo/src/store.py", "src/store.py"))
        self.assertFalse(_paths_overlap("src/store.py", "src/restore.py"))
        self.assertFalse(_paths_overlap("", "a.py"))

    def test_bare_filename_never_soaks_up_the_tree(self):
        # map_note discipline: a claim on "README.md" means the root one :
        # it must not flag every README in every directory
        self.assertTrue(_paths_overlap("README.md", "README.md"))
        self.assertFalse(_paths_overlap("README.md", "docs/README.md"))
        self.assertFalse(_paths_overlap("vendor/f.py", "f.py"))


@unittest.skipUnless(GIT, "git not installed")
class TestCurrentBranch(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        _git(self.repo, "init", "-q")

    def test_unborn_branch_is_detected(self):
        # a fresh clone/init has no commits: rev-parse fails there, but
        # the branch name is exactly the coordination key we need
        _git(self.repo, "checkout", "-q", "-b", "agent/fresh")
        self.assertEqual(current_branch(self.repo), "agent/fresh")

    def test_detached_head_is_not_a_branch(self):
        _git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t",
             "commit", "-q", "--allow-empty", "-m", "x")
        _git(self.repo, "checkout", "-q", "--detach")
        self.assertEqual(current_branch(self.repo), "")

    def test_not_a_repo_is_empty(self):
        bare = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, bare, ignore_errors=True)
        self.assertEqual(current_branch(bare), "")


class TestIntentCli(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _run(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--root", self.root, *argv])
        return buf.getvalue()

    def test_announce_list_done(self):
        out = self._run("intent", "ship the fix", "--branch", "agent/z",
                        "--paths", "a.py, b.py", "--ttl", "4h")
        self.assertIn("intent announced: agent/z", out)
        listing = self._run("intent")
        self.assertIn("agent/z", listing)
        self.assertIn("ship the fix", listing)
        self.assertIn("a.py", listing)
        out = self._run("intent", "--done", "--branch", "agent/z")
        self.assertIn("intent retired: agent/z", out)
        self.assertIn("no active intents", self._run("intent"))

    def test_done_on_unknown_branch_fails_loud(self):
        with self.assertRaises(SystemExit) as cm:
            self._run("intent", "--done", "--branch", "never/announced")
        self.assertEqual(cm.exception.code, 1)

    def test_bad_ttl_fails_loud(self):
        with self.assertRaises(SystemExit):
            self._run("intent", "x", "--branch", "b", "--ttl", "soonish")

    def test_prime_appends_inflight_section(self):
        Bundle(self.root).write_note("a.md", {"title": "A"}, "alpha note")
        self._run("intent", "reshape the store", "--branch", "other/work",
                  "--paths", "src/store.py")
        out = self._run("prime", "--cwd", self.root)
        self.assertIn("# Context pack", out)
        self.assertIn("## In flight", out)
        self.assertIn("other/work", out)
        # ... and stays absent when nothing is announced
        self._run("intent", "--done", "--branch", "other/work")
        self.assertNotIn("## In flight", self._run("prime", "--cwd", self.root))

    def test_hook_session_start_lands_inflight_in_the_session(self):
        Bundle(self.root).write_note("a.md", {"title": "A"}, "alpha note")
        Dynamics(self.root).intent("other/work", paths=["src/store.py"],
                                   goal="reshape the store")
        buf = io.StringIO()
        payload = '{"session_id": "s-hook-1"}'
        with mock.patch("sys.stdin", io.StringIO(payload)), \
                redirect_stdout(buf):
            cli.main(["--root", self.root, "hook", "session-start"])
        out = buf.getvalue()
        self.assertIn("# Context pack", out)
        self.assertIn("## In flight", out)
        self.assertIn("other/work", out)


if __name__ == "__main__":
    unittest.main()
