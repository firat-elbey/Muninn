"""Verify corrective lessons and path-specific guards.

The command writes a pinned lesson and records the preceding negative outcome.
Guard matching follows the path-suffix rules and rejects ambiguous bare file
names. Served lessons create session recall events for later review. The agent
must provide the lesson because Muninn does not read conversations.
"""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from muninn import cli, review  # noqa: E402
from muninn.dynamics import Dynamics  # noqa: E402
from muninn.observe import lessons_section  # noqa: E402
from muninn.store import Bundle, Note  # noqa: E402

GIT = shutil.which("git")


def _git(wd, *args):
    subprocess.run(["git", "-C", wd, *args], check=True,
                   capture_output=True, text=True)


class TestLessonVerb(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _run(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--root", self.root, *argv])
        return buf.getvalue()

    def test_lesson_writes_pinned_guard_note_and_outcome(self):
        Bundle(self.root).write_note("styles.md", {"title": "Styles"}, "css")
        d = Dynamics(self.root)
        d.touch("styles.md")  # what the agent was editing when it burned
        out = self._run("lesson", "Shared CSS edits break dark mode",
                        "--guards", "ui/styles, ui/theme.css",
                        "--body", "Theme tokens feed BOTH modes; verify "
                                  "dark mode after any shared style edit.")
        self.assertIn("lesson recorded", out)
        b = Bundle(self.root)
        lesson = next(n for n in b.notes.values()
                      if n.meta.get("type") == "lesson")
        self.assertEqual(lesson.guards(), ["ui/styles", "ui/theme.css"])
        self.assertTrue(lesson.meta.get("pinned"))
        self.assertIn("Theme tokens", lesson.body)
        # the burn marked its precursors (capture window)
        d = Dynamics(self.root)
        self.assertGreater(d.entries["styles.md"]["captured"], 0)

    def test_lesson_list_and_note_guards_accessor(self):
        self._run("lesson", "Never skip listed tasks",
                  "--body", "Report every task done or explicitly not.")
        listing = self._run("lesson")
        self.assertIn("Never skip listed tasks", listing)
        self.assertIn("(no guards: every session)", listing)
        n = Note(path="x.md", meta={"guards": "one.py"}, body="")
        self.assertEqual(n.guards(), ["one.py"])  # scalar tolerated
        self.assertEqual(Note(path="y.md", meta={}, body="").guards(), [])


@unittest.skipUnless(GIT, "git not installed")
class TestLessonsInPrime(unittest.TestCase):
    def setUp(self):
        self.top = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.top, ignore_errors=True)
        self.kb = os.path.join(self.top, "kb")
        os.makedirs(self.kb)
        b = Bundle(self.kb)
        b.write_note("lessons/dark-mode.md",
                     {"title": "Shared CSS edits break dark mode",
                      "type": "lesson", "pinned": True,
                      "guards": ["ui/styles", "ui/theme.css"]},
                     "Theme tokens feed BOTH modes; verify dark mode.")
        b.write_note("lessons/tasks.md",
                     {"title": "Never silently skip tasks",
                      "type": "lesson", "pinned": True, "guards": []},
                     "Report every requested task as done or explicitly "
                     "not done: never drop one quietly.")
        self.b = Bundle(self.kb)
        self.repo = os.path.join(self.top, "repo")
        os.makedirs(os.path.join(self.repo, "ui", "styles"))
        _git(self.top, "init", "-q", "repo")

    def test_guarded_lesson_fires_on_overlapping_change(self):
        with open(os.path.join(self.repo, "ui", "styles", "app.css"),
                  "w") as fh:
            fh.write("body {}")
        d = Dynamics(self.kb)
        out = lessons_section(self.b, d, workdir=self.repo, session="s1")
        self.assertIn("## Lessons for this change", out)
        self.assertIn("Shared CSS edits break dark mode", out)
        self.assertIn("Theme tokens feed BOTH modes", out)  # full body
        self.assertIn("guards: ui/styles", out)
        # guardless (process) lessons ride along every session
        self.assertIn("Never silently skip tasks", out)
        # served lessons are recall events: the reflection loop will
        # know whether they get USED
        m = review.session_metrics(d.ledger_path, "s1")
        self.assertIn("lessons/dark-mode.md", m["served"])

    def test_no_overlap_serves_only_guardless_lessons(self):
        with open(os.path.join(self.repo, "backend.py"), "w") as fh:
            fh.write("x = 1")
        out = lessons_section(self.b, Dynamics(self.kb),
                              workdir=self.repo, session="s2")
        self.assertNotIn("dark mode", out)
        self.assertIn("Never silently skip tasks", out)

    def test_no_lessons_no_section(self):
        for p in list(self.b.notes):
            os.remove(os.path.join(self.kb, p))
        b = Bundle(self.kb)
        self.assertEqual(
            lessons_section(b, Dynamics(self.kb), workdir=self.repo), "")

    def test_prime_cli_carries_the_section(self):
        with open(os.path.join(self.repo, "ui", "styles", "app.css"),
                  "w") as fh:
            fh.write("body {}")
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--root", self.kb, "prime", "--cwd", self.repo,
                      "--budget", "300"])
        out = buf.getvalue()
        self.assertIn("## Lessons for this change", out)
        self.assertIn("dark mode", out)


if __name__ == "__main__":
    unittest.main()
