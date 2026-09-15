"""Verify safe full-protocol discovery through two-line home pointers."""

import io
import os
import re
import shlex
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from muninn import cli, home, skill


def legacy_pointer(root, room=None):
    where = f" (room: {room})" if room else ""
    return (
        "<!-- muninn:home: this workspace is organized by a muninn "
        "home knowledge base -->\n"
        f"Start every session with {home._code_span(f'muninn --root {shlex.quote(root)} prime')}: "
        f"knowledge, threads, and lessons live in {root}{where}; log "
        "reactions with `muninn feedback`, milestones with `muninn journal`.\n"
    )


class TestHomePointerProtocol(unittest.TestCase):
    def test_new_pointer_routes_to_complete_rooted_protocol_in_two_lines(self):
        root = "/disposable/memory"
        pointer = home.pointer_lines(root)
        self.assertEqual(len(pointer.splitlines()), 2)
        self.assertIn("complete protocol", pointer)
        self.assertIn("muninn --root /disposable/memory skill", pointer)
        self.assertTrue(home.pointer_matches(pointer, root))
        self.assertTrue(home.pointer_routes_protocol(pointer, root))

    def test_legacy_pointer_still_matches_but_needs_upgrade(self):
        root = "/disposable/memory"
        pointer = legacy_pointer(root)
        self.assertTrue(home.pointer_matches(pointer, root))
        self.assertFalse(home.pointer_routes_protocol(pointer, root))
        self.assertEqual(home.refresh_pointer(pointer, root), home.pointer_lines(root))

    def test_pointer_command_retrieves_the_complete_current_skill(self):
        root = "/disposable/memory space"
        commands = re.findall(r"(`+)(?!`)(muninn --root .*?)\1", home.pointer_lines(root))
        command = next(shlex.split(text) for _, text in commands
                       if shlex.split(text)[-1] == "skill")
        output = io.StringIO()
        with redirect_stdout(output):
            cli.main(command[1:])
        self.assertEqual(output.getvalue(), skill.render(root) + "\n")

    def test_upgrade_preserves_surrounding_personal_prose_and_room(self):
        root = "/disposable/memory"
        before = "# Personal instructions\n\nPreserve my files.\n\n"
        after = "\n## Team preferences\n\nUse complete sentences.\n"
        text = before + legacy_pointer(root, "projects/example") + after
        expected = before + home.pointer_lines(root, "projects/example") + after
        self.assertEqual(home.refresh_pointer(text, root), expected)
        self.assertTrue(home.pointer_routes_protocol(expected, root))

    def test_refresh_is_idempotent(self):
        root = "/disposable/memory"
        text = "Personal prose.\n" + home.pointer_lines(root) + "End."
        self.assertEqual(home.refresh_pointer(text, root), text)

    def test_equivalent_root_spelling_is_safely_refreshed(self):
        root = "/disposable/memory"
        alias = "/disposable/unused/../memory"
        pointer = legacy_pointer(alias)
        self.assertTrue(home.pointer_matches(pointer, root))
        self.assertEqual(home.refresh_pointer(pointer, root), home.pointer_lines(root))

    def test_refresh_preserves_line_endings_and_missing_terminal_newline(self):
        root = "/disposable/memory"
        for ending in ("\n", "\r\n"):
            for terminal in (True, False):
                with self.subTest(ending=repr(ending), terminal=terminal):
                    old = legacy_pointer(root).replace("\n", ending)
                    new = home.pointer_lines(root).replace("\n", ending)
                    if not terminal:
                        old, new = old.rstrip("\r\n"), new.rstrip("\r\n")
                    self.assertEqual(home.refresh_pointer("Personal." + ending + old, root),
                                     "Personal." + ending + new)

    def test_missing_damaged_conflicting_or_extended_pointer_is_not_refreshed(self):
        root = "/disposable/memory"
        good = legacy_pointer(root)
        cases = [
            "Personal prose only.\n",
            good.splitlines()[0] + "\n",
            good.replace("home knowledge base -->", "home knowledge base"),
            good.replace(" prime`:", " prime:", 1),
            legacy_pointer("/disposable/other"),
            good + good,
            good.rstrip("\n") + " Personal content on the managed line.\n",
            good.replace("knowledge, threads", "changed knowledge, threads"),
            good.replace("; log ", " (room: ); log "),
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertIsNone(home.refresh_pointer(text, root))
                self.assertFalse(home.pointer_routes_protocol(text, root))

    def test_unrelated_skill_command_does_not_make_legacy_pointer_current(self):
        root = "/disposable/memory"
        text = legacy_pointer(root) + "\nRun `muninn --root /disposable/memory skill`.\n"
        self.assertFalse(home.pointer_routes_protocol(text, root))

    def test_route_rejects_another_root_or_altered_instruction(self):
        root = "/disposable/memory"
        pointer = home.pointer_lines(root)
        self.assertFalse(home.pointer_routes_protocol(pointer, "/disposable/other"))
        altered = pointer.replace("/disposable/memory skill", "/disposable/other skill")
        self.assertFalse(home.pointer_routes_protocol(altered, root))
        self.assertIsNone(home.refresh_pointer(altered, root))

    def test_all_rooted_commands_quote_unusual_roots(self):
        for root in ("/disposable/memory space", "/disposable/O'Brien",
                     "/disposable/$(touch NEVER)", "/disposable/one`two``three"):
            with self.subTest(root=root):
                pointer = home.pointer_lines(root)
                commands = re.findall(r"(`+)(?!`)(muninn --root .*?)\1", pointer)
                self.assertEqual([shlex.split(command) for _, command in commands],
                                 [["muninn", "--root", root, "prime"],
                                  ["muninn", "--root", root, "skill"]])
                self.assertTrue(home.pointer_routes_protocol(pointer, root))
                self.assertEqual(home.refresh_pointer(legacy_pointer(root), root), pointer)


class TestAdoptPointerUpgrade(unittest.TestCase):
    def setUp(self):
        area = tempfile.TemporaryDirectory()
        self.addCleanup(area.cleanup)
        self.area = Path(area.name)
        self.brain = str(self.area / "memory")
        home.init_home(self.brain)

    def test_readoption_refreshes_legacy_pointer_and_preserves_personal_bytes(self):
        project = self.area / "project"
        project.mkdir()
        room = home.room_for(self.brain, str(project), create=True)
        before, after = "# Personal rules\r\n\r\nKeep my prose.\r\n", "\r\nKeep this ending."
        old = before + legacy_pointer(self.brain, room).replace("\n", "\r\n") + after
        expected = before + home.pointer_lines(self.brain, room).replace("\n", "\r\n") + after
        instructions = project / "AGENTS.md"
        instructions.write_bytes(old.encode())
        self.assertEqual(home.adopt(self.brain, str(project)), (room, "refreshed"))
        self.assertEqual(instructions.read_bytes(), expected.encode())
        self.assertTrue(home.pointer_routes_protocol(instructions.read_text(), self.brain))
        self.assertEqual(home.adopt(self.brain, str(project)), (room, "already wired"))
        self.assertEqual(instructions.read_bytes(), expected.encode())

    def test_readoption_refuses_damaged_or_conflicting_pointers_without_registration(self):
        legacy = legacy_pointer(self.brain)
        cases = [
            legacy.replace("home knowledge base -->", "home knowledge base"),
            legacy_pointer(str(self.area / "other-memory")),
            legacy + legacy,
            legacy.rstrip("\n") + " My personal addition.\n",
        ]
        for number, original in enumerate(cases):
            with self.subTest(number=number):
                project = self.area / f"project-{number}"
                project.mkdir()
                instructions = project / "AGENTS.md"
                instructions.write_text(original)
                self.assertEqual(home.adopt(self.brain, str(project)),
                                 (None, "conflicting pointer"))
                self.assertEqual(instructions.read_text(), original)
                self.assertNotIn(str(project.resolve()), home.rooms(self.brain))

    def test_readoption_does_not_refresh_linked_instruction_files(self):
        for linker in (os.symlink, os.link):
            with self.subTest(linker=linker.__name__):
                project = self.area / linker.__name__
                project.mkdir()
                target = self.area / f"{linker.__name__}-personal.md"
                original = legacy_pointer(self.brain)
                target.write_text(original)
                instructions = project / "AGENTS.md"
                linker(target, instructions)
                self.assertEqual(home.adopt(self.brain, str(project)),
                                 (None, "conflicting pointer"))
                self.assertEqual(target.read_text(), original)
                self.assertEqual(instructions.read_text(), original)
                self.assertNotIn(str(project.resolve()), home.rooms(self.brain))


class TestSetupPointerUpgrade(unittest.TestCase):
    def setUp(self):
        area = tempfile.TemporaryDirectory()
        self.addCleanup(area.cleanup)
        self.area = Path(area.name)
        self.fake = self.area / "user home"
        self.fake.mkdir()
        self.brain = str(self.fake / "memory")
        self.canonical = self.fake / "AGENTS.md"
        environment = mock.patch.dict(os.environ, {"HOME": str(self.fake)})
        environment.start()
        self.addCleanup(environment.stop)

    def run_cli(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        status = 0
        with redirect_stdout(output), redirect_stderr(errors):
            try:
                cli.main(list(arguments))
            except SystemExit as error:
                status = error.code
        return status, output.getvalue(), errors.getvalue()

    def test_setup_upgrades_crlf_pointer_and_preserves_all_personal_bytes(self):
        prefix = "# Personal rules\r\n\r\nKeep my prose.\r\n"
        suffix = "\r\n## Personal suffix\r\nKeep this final line."
        old = prefix + legacy_pointer(self.brain).replace("\n", "\r\n") + suffix
        expected = prefix + home.pointer_lines(self.brain).replace("\n", "\r\n") + suffix
        self.canonical.write_bytes(old.encode())
        status, output, errors = self.run_cli("setup", "--brain", self.brain)
        self.assertEqual(status, 0, (output, errors))
        self.assertEqual(self.canonical.read_bytes(), expected.encode())
        self.assertEqual(self.run_cli("doctor", "--home", "--brain", self.brain)[0], 0)
        self.assertEqual(self.run_cli("setup", "--brain", self.brain)[0], 0)
        self.assertEqual(self.canonical.read_bytes(), expected.encode())

    def test_setup_refuses_extended_pointer_before_creating_any_configuration(self):
        original = ("Keep my prose.\n" + legacy_pointer(self.brain).rstrip("\n")
                    + " Preserve this personal addition.\n")
        self.canonical.write_text(original)
        status, output, errors = self.run_cli("setup", "--brain", self.brain)
        self.assertNotEqual(status, 0, (output, errors))
        self.assertEqual(self.canonical.read_text(), original)
        self.assertEqual(sorted(path.name for path in self.fake.iterdir()), ["AGENTS.md"])
        self.assertIn("pointer", str(status))


if __name__ == "__main__":
    unittest.main()
