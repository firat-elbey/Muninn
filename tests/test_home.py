"""Verify one home knowledge base with project-specific rooms.

The visible bundle defaults to `~/muninn` and accepts a `MUNINN_HOME`
override. Qualifying project activity creates rooms when needed. Installation
preserves existing instructions and adds at most a two-line pointer. The room
registry keeps local absolute paths in the private sidecar.
"""

import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from muninn import cli, home  # noqa: E402
from muninn.dynamics import Dynamics  # noqa: E402
from muninn.observe import map_note, observe_event  # noqa: E402
from muninn.store import Bundle  # noqa: E402


def _run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        cli.main(list(argv))
    return out.getvalue(), err.getvalue()


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.brain = os.path.join(self.tmp, "muninn")

    def _project(self, name, files=()):
        p = os.path.join(self.tmp, name)
        os.makedirs(p, exist_ok=True)
        for rel in files:
            full = os.path.join(p, rel)
            os.makedirs(os.path.dirname(full) or p, exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write("x = 1\n")
        return p


# -- the brain itself ---------------------------------------------------------

class TestHomeBrain(_Tmp):
    def test_every_generated_home_command_quotes_the_root(self):
        for suffix in ("memory space", "O'Brien", "$(printf altered);memory", "memory`literal``suffix"):
            with self.subTest(suffix=suffix):
                root = os.path.join(self.tmp, suffix)
                home.init_home(root)
                pointer = home.pointer_lines(root)
                command = re.search(r"(`+)(?!`)(muninn --root .*?)\1", pointer).group(2)
                self.assertEqual(shlex.split(command), ["muninn", "--root", root, "prime"])
                self.assertTrue(home.pointer_matches(pointer, root))
                note = Bundle(root).notes["home.md"]
                # The home note lists directories before its executable example.
                command = re.search(r"(`+)(?!`)(muninn --root .*?)\1", note.body).group(2)
                self.assertEqual(shlex.split(command), ["muninn", "--root", root, "prime"])

    def test_home_initialization_refuses_linked_gitignore(self):
        for link in (os.symlink, os.link):
            with self.subTest(link=link.__name__), tempfile.TemporaryDirectory() as area:
                root = os.path.join(area, "brain")
                os.mkdir(root)
                target = os.path.join(area, "outside.txt")
                with open(target, "w", encoding="utf-8") as handle:
                    handle.write("Preserve this file.\n")
                link(target, os.path.join(root, ".gitignore"))
                with self.assertRaises((OSError, ValueError)):
                    home.init_home(root)
                with open(target, encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), "Preserve this file.\n")
                self.assertFalse(os.path.exists(os.path.join(root, "home.md")))

    def test_init_home_builds_the_visible_layout(self):
        self.assertTrue(home.init_home(self.brain))
        for d in ("projects", "threads", "lessons", "style"):
            self.assertTrue(os.path.isdir(os.path.join(self.brain, d)), d)
        # a marked brain is recognizable: hooks key room logic on this
        self.assertTrue(home.is_home(self.brain))
        # an ordinary bundle is NOT a brain
        other = os.path.join(self.tmp, "plain")
        os.makedirs(other)
        self.assertFalse(home.is_home(other))
        # the sidecar stays out of any git history of the brain
        with open(os.path.join(self.brain, ".gitignore"),
                  encoding="utf-8") as fh:
            self.assertIn(".muninn/", fh.read())

    def test_init_home_is_idempotent_and_never_clobbers(self):
        home.init_home(self.brain)
        hub_note = os.path.join(self.brain, "home.md")
        with open(hub_note, "w", encoding="utf-8") as fh:
            fh.write("---\ntype: note\ntitle: mine\n---\n\nuser prose\n")
        self.assertFalse(home.init_home(self.brain))  # already a brain
        with open(hub_note, encoding="utf-8") as fh:
            self.assertIn("user prose", fh.read())

    def test_home_root_respects_the_env_override(self):
        with mock.patch.dict(os.environ, {"MUNINN_HOME": self.brain}):
            self.assertEqual(home.home_root(), self.brain)
        with mock.patch.dict(os.environ, {"HOME": self.tmp}, clear=False):
            os.environ.pop("MUNINN_HOME", None)
            self.assertEqual(home.home_root(),
                             os.path.join(self.tmp, "muninn"))


# -- rooms: organic growth ----------------------------------------------------

class TestRooms(_Tmp):
    def setUp(self):
        super().setUp()
        home.init_home(self.brain)

    def test_first_activity_registers_a_room_organically(self):
        proj = self._project("webapp")
        self.assertIsNone(home.room_for(self.brain, proj))  # not yet known
        room = home.room_for(self.brain, proj, create=True)
        self.assertEqual(room, "projects/webapp")
        # stable across calls: same project, same room, forever
        self.assertEqual(home.room_for(self.brain, proj), room)
        self.assertTrue(os.path.isdir(os.path.join(self.brain, room)))

    def test_same_basename_projects_get_distinct_rooms(self):
        a = self._project(os.path.join("clients", "acme"))
        b = self._project(os.path.join("personal", "acme"))
        ra = home.room_for(self.brain, a, create=True)
        rb = home.room_for(self.brain, b, create=True)
        self.assertEqual(ra, "projects/acme")
        self.assertNotEqual(ra, rb)
        self.assertTrue(rb.startswith("projects/acme-"))
        # both remain stable after the collision resolution
        self.assertEqual(home.room_for(self.brain, a), ra)
        self.assertEqual(home.room_for(self.brain, b), rb)

    def test_the_brain_is_never_its_own_room(self):
        self.assertIsNone(home.room_for(self.brain, self.brain, create=True))
        sub = os.path.join(self.brain, "threads")
        self.assertIsNone(home.room_for(self.brain, sub, create=True))

    def test_registry_lives_in_the_private_sidecar(self):
        proj = self._project("secretclient")
        home.room_for(self.brain, proj, create=True)
        reg = os.path.join(self.brain, ".muninn", "rooms.json")
        self.assertTrue(os.path.isfile(reg))
        with open(reg, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertIn(os.path.realpath(proj), data["rooms"])
        # and NOT in any visible note (local paths must not travel)
        b = Bundle(self.brain)
        for note in b.notes.values():
            self.assertNotIn(os.path.realpath(proj), note.body)
            self.assertNotIn(os.path.realpath(proj),
                             json.dumps(note.meta, default=str))


# -- adopt: the two-line pointer, never an override ---------------------------

class TestAdopt(_Tmp):
    def test_existing_pointer_to_another_home_is_not_reported_as_wired(self):
        project = self._project("existing-project")
        canonical = os.path.join(project, "AGENTS.md")
        original = home.pointer_lines(os.path.join(self.tmp, "other-home"))
        with open(canonical, "w", encoding="utf-8") as handle:
            handle.write(original)
        room, status = home.adopt(self.brain, project)
        self.assertIsNone(room)
        self.assertEqual(status, "conflicting pointer")
        self.assertNotIn(os.path.realpath(project), home.rooms(self.brain))
        with open(canonical, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), original)

    def setUp(self):
        super().setUp()
        home.init_home(self.brain)

    def test_adopt_appends_two_lines_to_an_existing_agents_md(self):
        proj = self._project("api")
        agents = os.path.join(proj, "AGENTS.md")
        with open(agents, "w", encoding="utf-8") as fh:
            fh.write("# Our project\n\nHouse rules live here.\n")
        room, verb = home.adopt(self.brain, proj)
        self.assertEqual(room, "projects/api")
        self.assertEqual(verb, "appended to")
        with open(agents, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("House rules live here.", text)   # nothing overridden
        added = text.split("House rules live here.\n", 1)[1]
        self.assertLessEqual(
            len([ln for ln in added.splitlines() if ln.strip()]), 2,
            "adopt must append at most two content lines")
        self.assertIn(home.POINTER_MARK, text)
        self.assertIn(self.brain, text)

    def test_adopt_creates_agents_md_when_missing_and_is_idempotent(self):
        proj = self._project("cli-tool")
        room, verb = home.adopt(self.brain, proj)
        self.assertEqual(verb, "wrote")
        agents = os.path.join(proj, "AGENTS.md")
        with open(agents, encoding="utf-8") as fh:
            first = fh.read()
        room2, verb2 = home.adopt(self.brain, proj)
        self.assertEqual((room, verb2), (room2, "already wired"))
        with open(agents, encoding="utf-8") as fh:
            self.assertEqual(first, fh.read())

    def test_adopt_via_cli(self):
        proj = self._project("svc")
        out, _ = _run_cli("adopt", proj, "--brain", self.brain)
        self.assertIn("projects/svc", out)
        self.assertTrue(os.path.isfile(os.path.join(proj, "AGENTS.md")))


# -- room-aware mapping: the duplicate-filename disambiguation ----------------

class TestRoomAwareMapping(_Tmp):
    def setUp(self):
        super().setUp()
        home.init_home(self.brain)
        self.b = Bundle(self.brain)
        # two rooms, both derived from a file spelled src/app.py
        for room in ("projects/alpha", "projects/beta"):
            self.b.write_note(f"{room}/extracted/app.md",
                              {"type": "code", "title": f"{room} app",
                               "resource": "src/app.py"},
                              "the app module")

    def test_prefer_picks_the_right_room(self):
        hit_a = map_note(self.b, "src/app.py", prefer="projects/alpha")
        hit_b = map_note(self.b, "src/app.py", prefer="projects/beta")
        self.assertEqual(hit_a, "projects/alpha/extracted/app.md")
        self.assertEqual(hit_b, "projects/beta/extracted/app.md")

    def test_without_prefer_the_old_deterministic_rule_holds(self):
        self.assertEqual(map_note(self.b, "src/app.py"),
                         "projects/alpha/extracted/app.md")

    def test_prefer_falls_back_globally_when_the_room_lacks_the_note(self):
        self.assertEqual(
            map_note(self.b, "src/app.py", prefer="projects/gamma"),
            "projects/alpha/extracted/app.md")

    def test_observe_event_threads_prefer_through_to_the_ledger(self):
        d = Dynamics(self.brain)
        path = observe_event(self.b, d, "touch", "src/app.py",
                             prefer="projects/beta")
        self.assertEqual(path, "projects/beta/extracted/app.md")
        self.assertIn("projects/beta/extracted/app.md", d.entries)
        self.assertNotIn("projects/alpha/extracted/app.md", d.entries)


# -- install --home: one command, everything wired, nothing clobbered ---------

class TestInstallHome(_Tmp):
    def _home_env(self):
        fake = os.path.join(self.tmp, "userhome")
        os.makedirs(fake, exist_ok=True)
        return fake

    def test_install_home_builds_brain_and_canonical_and_slots(self):
        fake = self._home_env()
        with mock.patch.dict(os.environ, {"HOME": fake}):
            out, _ = _run_cli("install", "--home", "--brain", self.brain)
        self.assertTrue(home.is_home(self.brain))
        canonical = os.path.join(fake, "AGENTS.md")
        self.assertTrue(os.path.isfile(canonical))
        with open(canonical, encoding="utf-8") as fh:
            self.assertIn("# Muninn agent protocol", fh.read())
        # every harness's HOME slot resolves to the canonical file
        for slot in (os.path.join(fake, ".claude", "CLAUDE.md"),
                     os.path.join(fake, ".codex", "AGENTS.md"),
                     os.path.join(fake, ".gemini", "GEMINI.md"),
                     os.path.join(fake, ".grok", "AGENTS.md")):
            self.assertTrue(os.path.lexists(slot), slot)
        self.assertIn("hooks", out)  # the paste-ready hook config is printed

    def test_install_home_appends_pointer_to_an_existing_canonical(self):
        fake = self._home_env()
        canonical = os.path.join(fake, "AGENTS.md")
        with open(canonical, "w", encoding="utf-8") as fh:
            fh.write("# Me\n\nMy personality and style.\n")
        with mock.patch.dict(os.environ, {"HOME": fake}):
            _run_cli("install", "--home", "--brain", self.brain)
        with open(canonical, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("My personality and style.", text)  # never overridden
        self.assertIn(home.POINTER_MARK, text)
        added = text.split("My personality and style.\n", 1)[1]
        self.assertLessEqual(
            len([ln for ln in added.splitlines() if ln.strip()]), 2,
            "an existing canonical gets the two-line pointer, not the "
            "full protocol")

    def test_install_home_reruns_are_idempotent(self):
        fake = self._home_env()
        with mock.patch.dict(os.environ, {"HOME": fake}):
            _run_cli("install", "--home", "--brain", self.brain)
            canonical = os.path.join(fake, "AGENTS.md")
            with open(canonical, encoding="utf-8") as fh:
                first = fh.read()
            _run_cli("install", "--home", "--brain", self.brain)
            with open(canonical, encoding="utf-8") as fh:
                self.assertEqual(first, fh.read())


# -- the hook grows rooms organically -----------------------------------------

class TestHookGrowsRooms(_Tmp):
    def test_post_tool_in_a_new_project_registers_its_room(self):
        home.init_home(self.brain)
        proj = self._project("fresh", files=("src/app.py",))
        # the room is the file's PROJECT (its git toplevel), not the
        # subfolder the file happens to live in
        subprocess.run(["git", "init", "-q", proj], capture_output=True)
        payload = json.dumps({"session_id": "s1", "tool_name": "Read",
                              "tool_input": {"file_path":
                                             os.path.join(proj, "src",
                                                          "app.py")}})
        with mock.patch("sys.stdin", io.StringIO(payload)):
            _run_cli("--root", self.brain, "hook", "post-tool")
        self.assertEqual(home.room_for(self.brain, proj), "projects/fresh")

    def test_post_tool_on_a_plain_bundle_never_registers_rooms(self):
        os.makedirs(self.brain)
        Bundle(self.brain).generate_index()
        Dynamics(self.brain)._save()
        proj = self._project("fresh", files=("src/app.py",))
        payload = json.dumps({"session_id": "s1", "tool_name": "Read",
                              "tool_input": {"file_path":
                                             os.path.join(proj, "src",
                                                          "app.py")}})
        with mock.patch("sys.stdin", io.StringIO(payload)):
            _run_cli("--root", self.brain, "hook", "post-tool")
        self.assertFalse(os.path.exists(
            os.path.join(self.brain, ".muninn", "rooms.json")))


# -- doctor --home ------------------------------------------------------------

class TestHubEdges(_Tmp):
    def test_adopt_without_a_brain_fails_with_the_fix(self):
        proj = self._project("p")
        with self.assertRaises(SystemExit) as cm:
            _run_cli("adopt", proj, "--brain", os.path.join(self.tmp, "no"))
        self.assertEqual(cm.exception.code, 1)

    def test_adopting_the_brain_itself_fails_loud(self):
        home.init_home(self.brain)
        with self.assertRaises(SystemExit):
            _run_cli("adopt", self.brain, "--brain", self.brain)

    def test_adopt_rerun_reports_already_wired(self):
        home.init_home(self.brain)
        proj = self._project("p")
        _run_cli("adopt", proj, "--brain", self.brain)
        out, _ = _run_cli("adopt", proj, "--brain", self.brain)
        self.assertIn("already wired", out)

    def test_install_home_leaves_a_pointered_canonical_alone(self):
        fake = os.path.join(self.tmp, "userhome")
        os.makedirs(fake)
        canonical = os.path.join(fake, "AGENTS.md")
        with open(canonical, "w", encoding="utf-8") as fh:
            fh.write("# Me\n\nprose\n\n"
                     + home.pointer_lines(self.brain))
        with mock.patch.dict(os.environ, {"HOME": fake}):
            _run_cli("install", "--home", "--brain", self.brain)
        with open(canonical, encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual(text.count(home.POINTER_MARK), 1)

    def test_refused_adopt_leaves_no_pointer_behind(self):
        home.init_home(self.brain)
        room, verb = home.adopt(self.brain, self.brain)
        self.assertEqual((room, verb), (None, "not adopted"))
        # the refusal must not have appended a self-pointing AGENTS.md
        agents = os.path.join(self.brain, "AGENTS.md")
        if os.path.exists(agents):
            with open(agents, encoding="utf-8") as fh:
                self.assertNotIn(home.POINTER_MARK, fh.read())

    def test_ambient_touches_only_mint_rooms_for_git_projects(self):
        home.init_home(self.brain)
        plain = self._project("downloads")   # not a repo: no room
        self.assertIsNone(home.room_for(self.brain, plain,
                                        create=True, ambient=True))
        repo = self._project("realproj")
        subprocess.run(["git", "init", "-q", repo], capture_output=True)
        self.assertEqual(home.room_for(self.brain, repo,
                                       create=True, ambient=True),
                         "projects/realproj")
        # explicit adopt may still room any folder
        self.assertEqual(home.room_for(self.brain, plain, create=True),
                         "projects/downloads")

    def test_a_corrupt_registry_never_hands_a_room_to_another_project(self):
        home.init_home(self.brain)
        a = self._project(os.path.join("clients", "web"))
        self.assertEqual(home.room_for(self.brain, a, create=True),
                         "projects/web")
        reg = os.path.join(self.brain, ".muninn", "rooms.json")
        with open(reg, "w", encoding="utf-8") as fh:
            fh.write('{"version": 1, "rooms": {"/cl')  # torn write
        # the corrupt file is quarantined, not half-trusted
        self.assertEqual(home.rooms(self.brain), {})
        self.assertTrue(os.path.isfile(reg + ".corrupt"))
        # a DIFFERENT project with the same basename cannot inherit the
        # existing populated room dir: the disk check forces a suffix
        b = self._project(os.path.join("personal", "web"))
        rb = home.room_for(self.brain, b, create=True)
        self.assertNotEqual(rb, "projects/web")
        self.assertTrue(rb.startswith("projects/web-"))

    def test_doctor_home_flags_a_missing_layout_dir(self):
        fake = os.path.join(self.tmp, "userhome")
        os.makedirs(fake)
        with mock.patch.dict(os.environ, {"HOME": fake}):
            _run_cli("install", "--home", "--brain", self.brain)
            shutil.rmtree(os.path.join(self.brain, "threads"))
            with self.assertRaises(SystemExit):
                _run_cli("doctor", "--home", "--brain", self.brain)


class TestDoctorHome(_Tmp):
    def test_doctor_home_passes_on_a_wired_brain(self):
        fake = os.path.join(self.tmp, "userhome")
        os.makedirs(fake)
        with mock.patch.dict(os.environ, {"HOME": fake}):
            _run_cli("install", "--home", "--brain", self.brain)
            out, _ = _run_cli("doctor", "--home", "--brain", self.brain)
        self.assertIn("home configuration: valid", out)

    def test_doctor_home_fails_loud_when_no_brain_exists(self):
        with self.assertRaises(SystemExit) as cm:
            _run_cli("doctor", "--home", "--brain", self.brain)
        self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
