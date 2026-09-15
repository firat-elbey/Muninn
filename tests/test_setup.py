"""Verify installation, configuration preservation, and removal.

Setup creates the home knowledge base and connects each supported agent client.
It installs lifecycle hooks and the Muninn skill while preserving user-owned
configuration. Repeated execution is idempotent. The first modification keeps
one exact backup, and invalid configuration remains unchanged.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from muninn import cli, home  # noqa: E402


def _run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        cli.main(list(argv))
    return out.getvalue(), err.getvalue()


def _run_cli_status(*argv):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with redirect_stdout(out), redirect_stderr(err):
        try:
            cli.main(list(argv))
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class _FakeHome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.fake = os.path.join(self.tmp, "userhome")
        os.makedirs(self.fake)
        self.brain = os.path.join(self.tmp, "brain")
        self.settings = os.path.join(self.fake, ".claude", "settings.json")
        self.codex_hooks = os.path.join(self.fake, ".codex", "hooks.json")
        self.grok_hooks = os.path.join(
            self.fake, ".grok", "hooks", "muninn.json"
        )
        self.env = mock.patch.dict(os.environ, {"HOME": self.fake})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _setup(self):
        return _run_cli("setup", "--brain", self.brain)

    def _settings(self):
        with open(self.settings, encoding="utf-8") as fh:
            return json.load(fh)

    def _json(self, path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)


class TestSetupFreshMachine(_FakeHome):
    def test_one_command_wires_everything(self):
        out, _ = self._setup()
        # the brain
        self.assertTrue(home.is_home(self.brain))
        # every harness slot
        for slot in (".claude/CLAUDE.md", ".codex/AGENTS.md",
                     ".gemini/GEMINI.md", ".grok/AGENTS.md"):
            self.assertTrue(
                os.path.lexists(os.path.join(self.fake, slot)), slot)
        # hooks WRITTEN, all three events, pointing at the brain
        data = self._settings()
        for event in ("SessionStart", "PostToolUse", "SessionEnd"):
            cmds = json.dumps(data["hooks"][event])
            self.assertIn("muninn", cmds, event)
            self.assertIn(self.brain, cmds, event)
        for path in (self.codex_hooks, self.grok_hooks):
            hooks = self._json(path)["hooks"]
            self.assertEqual(set(hooks),
                             {"SessionStart", "PostToolUse", "SessionEnd"})
            self.assertIn(self.brain, json.dumps(hooks))
        grok = self._json(self.grok_hooks)
        self.assertIn("read_file", json.dumps(grok["hooks"]["PostToolUse"]))
        self.assertEqual(grok["hooks"], data["hooks"])
        # the skill
        self.assertTrue(os.path.isfile(os.path.join(
            self.fake, ".claude", "skills", "muninn", "SKILL.md")))
        self.assertIn("Setup is complete.", out)

    def test_rerun_is_a_byte_identical_no_op(self):
        self._setup()
        paths = (self.settings, self.codex_hooks, self.grok_hooks)
        first = {}
        for path in paths:
            with open(path, encoding="utf-8") as fh:
                first[path] = fh.read()
        out, _ = self._setup()
        for path in paths:
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(first[path], fh.read())
        self.assertIn("already wired", out)


class TestSetupNeverClobbers(_FakeHome):
    def test_user_settings_and_hooks_survive(self):
        os.makedirs(os.path.dirname(self.settings))
        user = {"model": "opus",
                "hooks": {"SessionStart": [
                    {"hooks": [{"type": "command",
                                "command": "echo my-own-thing"}]}]}}
        original = json.dumps(user, separators=(",", ":"))
        with open(self.settings, "w", encoding="utf-8") as fh:
            fh.write(original)
        self._setup()
        data = self._settings()
        self.assertEqual(data["model"], "opus")           # untouched
        starts = json.dumps(data["hooks"]["SessionStart"])
        self.assertIn("my-own-thing", starts)             # kept
        self.assertIn("muninn", starts)                   # added beside it
        # and the pre-muninn state is backed up exactly once
        self.assertTrue(os.path.isfile(self.settings + ".muninn-bak"))
        with open(self.settings + ".muninn-bak", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), original)

    def test_a_moved_brain_replaces_stale_entries_not_duplicates(self):
        self._setup()
        other = os.path.join(self.tmp, "elsewhere")
        _run_cli("setup", "--brain", other)
        data = self._settings()
        for event in ("SessionStart", "PostToolUse", "SessionEnd"):
            entries = data["hooks"][event]
            ours = [e for e in entries if "muninn" in json.dumps(e)]
            self.assertEqual(len(ours), 1, event)  # replaced, not stacked
            self.assertIn(other, json.dumps(ours[0]))
            self.assertNotIn(self.brain, json.dumps(ours[0]))

    def test_codex_and_grok_user_hooks_survive_with_exact_backups(self):
        for path in (self.codex_hooks, self.grok_hooks):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            original = ('{"hooks":{"SessionStart":[{"hooks":'
                        '[{"type":"command","command":"echo user"}]}]}}')
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(original)
            self._setup()
            text = json.dumps(self._json(path))
            self.assertIn("echo user", text)
            self.assertIn("muninn", text)
            with open(path + ".muninn-bak", encoding="utf-8") as fh:
                self.assertEqual(fh.read(), original)
            self._setup()
            with open(path + ".muninn-bak", encoding="utf-8") as fh:
                self.assertEqual(fh.read(), original)

    def test_invalid_codex_and_grok_json_remains_unchanged(self):
        for path in (self.codex_hooks, self.grok_hooks):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{ invalid")
        code, out, _ = _run_cli_status("setup", "--brain", self.brain)
        self.assertEqual(code, 1)
        for path in (self.codex_hooks, self.grok_hooks):
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "{ invalid")
            self.assertFalse(os.path.exists(path + ".muninn-bak"))
        self.assertIn("left untouched", out)

    def test_unparseable_settings_are_refused_untouched(self):
        os.makedirs(os.path.dirname(self.settings))
        with open(self.settings, "w", encoding="utf-8") as fh:
            fh.write("{ not json,,, ")
        code, out, _ = _run_cli_status("setup", "--brain", self.brain)
        self.assertEqual(code, 1)
        self.assertIn("left untouched", out)
        with open(self.settings, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "{ not json,,, ")  # byte for byte
        # everything else still got set up
        self.assertTrue(home.is_home(self.brain))


class TestSetupReportsConflicts(_FakeHome):
    def test_project_copy_reports_damaged_child_markers_as_incomplete(self):
        project = os.path.join(self.tmp, "project")
        os.mkdir(project)
        child = os.path.join(project, "CLAUDE.md")
        with open(child, "w", encoding="utf-8") as handle:
            handle.write("Preserve this prose.\n" + cli.PROTOCOL_BEGIN)
        code, output, _ = _run_cli_status("--root", self.brain, "install", project, "--copy")
        self.assertEqual(code, 1)
        self.assertIn("incomplete", output)
        with open(child, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "Preserve this prose.\n" + cli.PROTOCOL_BEGIN)

    def test_stale_home_pointer_refuses_root_change_without_writes(self):
        canonical = os.path.join(self.fake, "AGENTS.md")
        with open(canonical, "w", encoding="utf-8") as handle:
            handle.write("Preserve my instructions.\n" + home.pointer_lines(self.brain))
        other = os.path.join(self.tmp, "other-brain")
        with open(canonical, encoding="utf-8") as handle:
            before = handle.read()
        code, _out, _err = _run_cli_status("setup", "--brain", other)
        self.assertNotEqual(code, 0)
        self.assertIn("pointer", str(code))
        self.assertFalse(os.path.exists(other))
        self.assertFalse(os.path.exists(self.settings))
        with open(canonical, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), before)

    def test_doctor_rejects_a_pointer_to_another_home(self):
        self._setup()
        canonical = os.path.join(self.fake, "AGENTS.md")
        with open(canonical, "w", encoding="utf-8") as handle:
            handle.write(home.pointer_lines(os.path.join(self.tmp, "other")))
        code, output, _ = _run_cli_status("doctor", "--home", "--brain", self.brain)
        self.assertEqual(code, 1)
        self.assertIn("home configuration: invalid", output)

    def test_doctor_rejects_managed_commands_selecting_another_root(self):
        self._setup()
        canonical = os.path.join(self.fake, "AGENTS.md")
        other = os.path.join(self.tmp, "other")
        samples = (cli._managed_block(other), cli._managed_block(self.brain).replace(
            f"--root {self.brain} pack", f"--root {other} pack"))
        for text in samples:
            with self.subTest(text=text):
                with open(canonical, "w", encoding="utf-8") as handle:
                    handle.write(text)
                code, output, _ = _run_cli_status("doctor", "--home", "--brain", self.brain)
                self.assertEqual(code, 1)
                self.assertIn("home configuration: invalid", output)

    def test_existing_instructions_support_roots_with_backticks(self):
        self.brain = os.path.join(self.tmp, "memory`literal``suffix")
        canonical = os.path.join(self.fake, "AGENTS.md")
        with open(canonical, "w", encoding="utf-8") as handle:
            handle.write("Preserve my instructions.\n")
        for _ in range(2):
            code, output, _ = _run_cli_status("setup", "--brain", self.brain)
            self.assertEqual(code, 0, output)
            code, output, _ = _run_cli_status("doctor", "--home", "--brain", self.brain)
            self.assertEqual(code, 0, output)
        with open(canonical, encoding="utf-8") as handle:
            self.assertTrue(handle.read().startswith("Preserve my instructions.\n"))

    def test_invalid_canonical_location_fails_before_creating_configuration(self):
        for canonical in (os.path.join(self.fake, "missing", "AGENTS.md"),
                          self.fake):
            with self.subTest(canonical=canonical):
                code, _out, _err = _run_cli_status(
                    "setup", "--brain", self.brain, "--from", canonical)
                self.assertNotEqual(code, 0)
                self.assertIn("canonical", str(code))
                self.assertFalse(os.path.exists(self.brain))
                self.assertFalse(os.path.exists(self.settings))

    def test_damaged_markers_remain_unchanged_and_fail_setup(self):
        begin, end = cli.PROTOCOL_BEGIN, cli.PROTOCOL_END
        for text in (begin, end, begin + end + begin,
                     end + begin, begin + cli.PROTOCOL_HEADING):
            with self.subTest(text=text):
                canonical = os.path.join(self.fake, "AGENTS.md")
                with open(canonical, "w", encoding="utf-8") as fh:
                    fh.write(text)
                code, out, _ = _run_cli_status(
                    "setup", "--brain", self.brain)
                self.assertEqual(code, 1)
                self.assertIn("Setup is incomplete", out)
                self.assertNotIn("Setup is complete", out)
                self.assertNotIn("Refreshed the managed protocol", out)
                with open(canonical, encoding="utf-8") as fh:
                    self.assertEqual(fh.read(), text)

    def test_doctor_rejects_damaged_and_empty_managed_protocol(self):
        self._setup()
        begin, end = cli.PROTOCOL_BEGIN, cli.PROTOCOL_END
        samples = (begin, end, begin + end, begin + end + begin,
                   end + cli.PROTOCOL_HEADING + begin,
                   begin + "\n" + cli.PROTOCOL_HEADING + "\n" + end,
                   begin + "\n" + home.pointer_lines(self.brain))
        for text in samples:
            with self.subTest(text=text):
                canonical = os.path.join(self.fake, "AGENTS.md")
                with open(canonical, "w", encoding="utf-8") as fh:
                    fh.write(text)
                code, out, _ = _run_cli_status(
                    "doctor", "--home", "--brain", self.brain)
                self.assertEqual(code, 1)
                self.assertIn("home configuration: invalid", out)

    def test_divergent_slots_fail_setup_without_replacement(self):
        regular = os.path.join(self.fake, ".codex", "AGENTS.md")
        os.makedirs(os.path.dirname(regular))
        with open(regular, "w", encoding="utf-8") as fh:
            fh.write("Keep these personal instructions.\n")
        target = os.path.join(self.fake, "other.md")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("Keep the separate instructions.\n")
        link = os.path.join(self.fake, ".grok", "AGENTS.md")
        os.makedirs(os.path.dirname(link))
        os.symlink(target, link)
        code, out, _ = _run_cli_status("setup", "--brain", self.brain)
        self.assertEqual(code, 1)
        self.assertIn("Setup is incomplete", out)
        self.assertNotIn("every agent brand", out)
        self.assertNotIn("Setup is complete", out)
        with open(regular, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "Keep these personal instructions.\n")
        self.assertEqual(os.readlink(link), target)

    def test_identical_copies_remain_valid_on_repeat_setup(self):
        self._setup()
        canonical = os.path.join(self.fake, "AGENTS.md")
        for slot in cli.GLOBAL_SLOTS:
            path = os.path.expanduser(slot)
            os.unlink(path)
            shutil.copyfile(canonical, path)
        code, out, _ = _run_cli_status("setup", "--brain", self.brain)
        self.assertEqual(code, 0)
        self.assertIn("Setup is complete", out)
        self.assertNotIn("every agent brand", out)

    def test_project_install_preserves_damaged_canonical_markers(self):
        project = os.path.join(self.tmp, "project")
        os.makedirs(project)
        path = os.path.join(project, "AGENTS.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(cli.PROTOCOL_BEGIN)
        code, out, _ = _run_cli_status(
            "--root", self.brain, "install", project)
        self.assertEqual(code, 1)
        self.assertIn("damaged", out)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), cli.PROTOCOL_BEGIN)

    def test_protocol_detection_retains_valid_and_legacy_content(self):
        for text in (cli._managed_block(self.brain),
                     cli.PROTOCOL_HEADING + "\nUse the local knowledge base.",
                     cli.LEGACY_PROTOCOL_HEADING + "\nUse the knowledge."):
            with self.subTest(text=text):
                self.assertTrue(cli._contains_protocol(text))


class TestDryRunAndUninstall(_FakeHome):
    def test_dry_run_prints_the_manifest_and_touches_nothing(self):
        out, _ = _run_cli("setup", "--brain", self.brain, "--dry-run")
        self.assertIn("Muninn setup would modify only these paths", out)
        self.assertIn("No path was changed.", out)
        self.assertFalse(os.path.exists(self.brain))
        self.assertFalse(os.path.exists(self.settings))
        self.assertFalse(os.path.exists(self.codex_hooks))
        self.assertFalse(os.path.exists(self.grok_hooks))

    def test_uninstall_removes_exactly_what_setup_added(self):
        os.makedirs(os.path.dirname(self.settings))
        with open(self.settings, "w", encoding="utf-8") as fh:
            json.dump({"model": "opus", "hooks": {"SessionStart": [
                {"hooks": [{"type": "command",
                            "command": "echo my-own-thing"}]}]}}, fh)
        self._setup()
        for path in (self.codex_hooks, self.grok_hooks):
            data = self._json(path)
            data["hooks"]["SessionStart"].append(
                {"hooks": [{"type": "command", "command": "echo user"}]}
            )
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        out, _ = _run_cli("uninstall", "--brain", self.brain)
        data = self._settings()
        self.assertEqual(data["model"], "opus")
        s = json.dumps(data)
        self.assertIn("my-own-thing", s)      # the user's hook survives
        self.assertNotIn("muninn", s)         # ours are gone
        self.assertFalse(os.path.exists(os.path.join(
            self.fake, ".claude", "skills", "muninn")))
        for slot in (".claude/CLAUDE.md", ".codex/AGENTS.md",
                     ".gemini/GEMINI.md", ".grok/AGENTS.md"):
            self.assertFalse(
                os.path.lexists(os.path.join(self.fake, slot)), slot)
        for path in (self.codex_hooks, self.grok_hooks):
            text = json.dumps(self._json(path))
            self.assertIn("echo user", text)
            self.assertNotIn("muninn", text)
        # The command preserves the knowledge base.
        self.assertTrue(home.is_home(self.brain))
        self.assertIn("Knowledge was preserved", out)

    def test_uninstall_twice_is_a_clean_no_op(self):
        self._setup()
        _run_cli("uninstall", "--brain", self.brain)
        out, _ = _run_cli("uninstall", "--brain", self.brain)
        self.assertIn("nothing to remove", out)


class TestDoctorSeesTheWires(_FakeHome):
    def test_doctor_reports_hooks_and_skill(self):
        self._setup()
        out, _ = _run_cli("doctor", "--home", "--brain", self.brain)
        self.assertIn("claude hooks: written", out)
        self.assertIn("codex hooks: written", out)
        self.assertIn("grok hooks: written", out)
        self.assertIn("claude skill: installed", out)
        self.assertIn("home configuration: valid", out)

    def test_doctor_fails_when_a_global_slot_is_missing(self):
        self._setup()
        slot = os.path.join(self.fake, ".codex", "AGENTS.md")
        os.unlink(slot)
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            cli.main(["doctor", "--home", "--brain", self.brain])
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("~/.codex/AGENTS.md: missing", out.getvalue())
        self.assertIn("home configuration: invalid", out.getvalue())

    def test_doctor_fails_when_a_setup_hook_is_missing(self):
        self._setup()
        os.unlink(self.codex_hooks)
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            cli.main(["doctor", "--home", "--brain", self.brain])
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("codex hooks: not written or stale", out.getvalue())
        self.assertIn("home configuration: invalid", out.getvalue())


if __name__ == "__main__":
    unittest.main()
