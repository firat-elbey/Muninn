"""The public style-repository protocol and its cross-agent wiring."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from muninn import cli, home, style  # noqa: E402
from muninn.dynamics import Dynamics  # noqa: E402
from muninn.evolve import evolve_once  # noqa: E402
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
        home.init_home(self.brain)

    def _style_repo(self, name="stylerepo"):
        repo = os.path.join(self.tmp, name)
        os.makedirs(repo)
        with open(os.path.join(repo, "core.md"), "w", encoding="utf-8") as fh:
            fh.write("Use complete sentences. Remove words that add no meaning.\n")
        with open(os.path.join(repo, "email.md"), "w", encoding="utf-8") as fh:
            fh.write("---\ntitle: Email voice\ndescription: short, warm, "
                     "no corporate filler\n---\n\nOne idea per email.\n")
        with open(os.path.join(repo, "blog.md"), "w", encoding="utf-8") as fh:
            fh.write("# Blog voice\n\nOpen with the point, never a "
                     "throat-clear.\n")
        os.makedirs(os.path.join(repo, "code"))
        with open(os.path.join(repo, "code", "reviews.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("Terse, kind, one must-fix per review.\n")
        os.makedirs(os.path.join(repo, "templates"))
        with open(os.path.join(repo, "templates", "brief.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("# Brief\n\nState the decision.\n")
        manifest = {
            "version": 1,
            "core": "core.md",
            "guides": [
                {"id": "email", "path": "email.md", "title": "Email voice",
                 "description": "Short, warm, and free of filler.",
                 "applies_to": ["email"]},
                {"id": "blog", "path": "blog.md", "title": "Blog voice",
                 "description": "State the point before the context.",
                 "applies_to": ["articles", "posts"]},
                {"id": "code-review", "path": "code/reviews.md",
                 "title": "Code reviews",
                 "description": "State one required correction at a time.",
                 "applies_to": ["code review"]},
            ],
            "templates": [
                {"id": "project-brief", "path": "templates/brief.md",
                 "title": "Project brief",
                 "description": "A concise decision request.",
                 "guide": "email"},
            ],
        }
        self._write_manifest(repo, manifest)
        return repo

    @staticmethod
    def _manifest(repo):
        with open(os.path.join(repo, style.MANIFEST), encoding="utf-8") as fh:
            return json.load(fh)

    @staticmethod
    def _write_manifest(repo, manifest):
        with open(os.path.join(repo, style.MANIFEST), "w",
                  encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
            fh.write("\n")


# -- the router: rendered from the repo's own structure -----------------------

class TestRouter(_Tmp):
    def test_router_embeds_core_and_lists_manifest_guides(self):
        repo = self._style_repo()
        text = style.render_router(repo, self.brain)
        self.assertIn("Use complete sentences. Remove words that add no meaning.",
                      text)
        self.assertIn("email", text)
        self.assertIn("Email voice", text)
        self.assertIn("Short, warm", text)
        self.assertIn("Blog voice", text)
        self.assertIn("code/reviews", text)
        self.assertIn("project-brief", text)
        self.assertIn("templates/brief.md", text)
        # every referenced path is absolute: the block loads from ANY cwd
        for line in text.splitlines():
            if "→" in line:
                self.assertTrue(os.path.isabs(line.rsplit("→", 1)[1].strip()),
                                line)
        # the feedback reflex rides the router: agents are told to log
        self.assertIn("muninn", text)
        self.assertIn("feedback", text)

    def test_unlisted_markdown_does_not_change_the_router(self):
        repo = self._style_repo()
        before = style.render_router(repo, self.brain)
        with open(os.path.join(repo, "unlisted.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("This file is not part of the contract.\n")
        self.assertEqual(style.render_router(repo, self.brain), before)

    def test_router_is_deterministic_and_fails_above_the_limit(self):
        repo = self._style_repo()
        manifest = self._manifest(repo)
        for i in range(style.MAX_ROUTES):
            with open(os.path.join(repo, f"g{i:02d}.md"), "w",
                      encoding="utf-8") as fh:
                fh.write(f"guide {i}\n")
            manifest["guides"].append({
                "id": f"g{i:02d}", "path": f"g{i:02d}.md",
                "title": f"Guide {i}", "description": "A neutral guide.",
                "applies_to": [f"type {i}"],
            })
        self._write_manifest(repo, manifest)
        with self.assertRaises(style.ContractError):
            style.render_router(repo, self.brain)

    def test_contract_hash_changes_when_routed_content_changes(self):
        repo = self._style_repo()
        first = style.load_contract(repo).content_hash
        with open(os.path.join(repo, "email.md"), "a", encoding="utf-8") as fh:
            fh.write("Prefer direct requests.\n")
        second = style.load_contract(repo).content_hash
        self.assertNotEqual(first, second)

    def test_manifest_paths_cannot_escape_the_repo(self):
        repo = self._style_repo()
        manifest = self._manifest(repo)
        manifest["core"] = "../outside.md"
        self._write_manifest(repo, manifest)
        with self.assertRaises(style.ContractError):
            style.load_contract(repo)

    def test_manifest_rejects_duplicate_ids_and_unknown_template_guides(self):
        repo = self._style_repo()
        manifest = self._manifest(repo)
        manifest["guides"][1]["id"] = "email"
        self._write_manifest(repo, manifest)
        with self.assertRaises(style.ContractError):
            style.load_contract(repo)

        second = self._style_repo("second")
        manifest = self._manifest(second)
        manifest["templates"] = [{
            "id": "brief", "path": "email.md", "title": "Brief",
            "description": "A short brief.", "guide": "missing",
        }]
        self._write_manifest(second, manifest)
        with self.assertRaises(style.ContractError):
            style.load_contract(second)

    def test_missing_manifest_fails_with_a_specific_error(self):
        repo = self._style_repo()
        os.remove(os.path.join(repo, style.MANIFEST))
        with self.assertRaisesRegex(style.ContractError, style.MANIFEST):
            style.load_contract(repo)

    def test_core_cannot_contain_the_managed_end_marker(self):
        repo = self._style_repo()
        with open(os.path.join(repo, "core.md"), "w", encoding="utf-8") as fh:
            fh.write(style.STYLE_END)
        with self.assertRaises(style.ContractError):
            style.load_contract(repo)


# -- adopt: mount + wire, never clobber ---------------------------------------

class TestStyleAdopt(_Tmp):
    def _fake_home(self):
        fake = os.path.join(self.tmp, "userhome")
        os.makedirs(fake, exist_ok=True)
        return fake

    def test_adopt_mounts_the_repo_as_style(self):
        repo = self._style_repo()
        fake = self._fake_home()
        with mock.patch.dict(os.environ, {"HOME": fake}):
            out, _ = _run_cli("style", "adopt", repo, "--brain", self.brain)
        mount = os.path.join(self.brain, "style")
        self.assertTrue(os.path.islink(mount))
        self.assertEqual(os.path.realpath(mount), os.path.realpath(repo))
        self.assertIn("mounted", out)
        # the guides are now bundle notes: recall can serve them
        b = Bundle(self.brain)
        self.assertIn("style/email.md", b.notes)
        self.assertEqual(b.notes["style/email.md"].title, "Email voice")

    def test_adopt_relocates_legacy_learning_into_the_private_overlay(self):
        repo = self._style_repo()
        fake = self._fake_home()
        learned = os.path.join(self.brain, "style", "learned")
        os.makedirs(learned)
        with open(os.path.join(learned, "email.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("- keep it short\n")
        with mock.patch.dict(os.environ, {"HOME": fake}):
            _run_cli("style", "adopt", repo, "--brain", self.brain)
        moved = os.path.join(self.brain, "style-learned", "email.md")
        self.assertTrue(os.path.isfile(moved))
        self.assertFalse(os.path.exists(os.path.join(repo, "learned")))
        with open(moved, encoding="utf-8") as fh:
            self.assertIn("keep it short", fh.read())

    def test_adopt_refuses_to_clobber_a_populated_style_dir(self):
        repo = self._style_repo()
        fake = self._fake_home()
        mine = os.path.join(self.brain, "style", "mine.md")
        with open(mine, "w", encoding="utf-8") as fh:
            fh.write("hand-written\n")
        with mock.patch.dict(os.environ, {"HOME": fake}):
            with self.assertRaises(SystemExit) as cm:
                _run_cli("style", "adopt", repo, "--brain", self.brain)
        self.assertEqual(cm.exception.code, 1)
        with open(mine, encoding="utf-8") as fh:  # untouched
            self.assertEqual(fh.read(), "hand-written\n")

    def test_adopt_wires_the_canonical_file_without_touching_prose(self):
        repo = self._style_repo()
        fake = self._fake_home()
        canonical = os.path.join(fake, "AGENTS.md")
        with open(canonical, "w", encoding="utf-8") as fh:
            fh.write("# Me\n\nMy own words.\n")
        with mock.patch.dict(os.environ, {"HOME": fake}):
            _run_cli("style", "adopt", repo, "--brain", self.brain)
        with open(canonical, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("My own words.", text)
        self.assertIn(style.STYLE_BEGIN, text)
        self.assertIn("Email voice", text)
        self.assertIn("Use complete sentences", text)
        # re-run refreshes the block in place: no growth, no duplicates
        with mock.patch.dict(os.environ, {"HOME": fake}):
            _run_cli("style", "adopt", repo, "--brain", self.brain)
        with open(canonical, encoding="utf-8") as fh:
            self.assertEqual(fh.read().count(style.STYLE_BEGIN), 1)

    def test_adopt_emits_the_claude_skill(self):
        repo = self._style_repo()
        fake = self._fake_home()
        with mock.patch.dict(os.environ, {"HOME": fake}):
            _run_cli("style", "adopt", repo, "--brain", self.brain)
        skill_md = os.path.join(fake, ".claude", "skills", "style",
                                "SKILL.md")
        self.assertTrue(os.path.isfile(skill_md))
        with open(skill_md, encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\n"))
        self.assertIn("name: style", text)
        self.assertIn("Email voice", text)
        canonical = os.path.join(fake, "AGENTS.md")
        self.assertEqual(style.current_block(canonical),
                         style.render_router(repo, self.brain).strip())
        self.assertIn(style.current_block(canonical), text)

    def test_adopt_records_revision_and_content_hashes(self):
        repo = self._style_repo()
        subprocess.run(["git", "-C", repo, "init", "-q"], check=True)
        subprocess.run(["git", "-C", repo, "add", "."], check=True)
        subprocess.run(["git", "-C", repo, "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit",
                        "-qm", "Initial style"], check=True)
        fake = self._fake_home()
        with mock.patch.dict(os.environ, {"HOME": fake}):
            _run_cli("style", "adopt", repo, "--brain", self.brain)
        record = style.adoption_record(self.brain)
        self.assertEqual(record["version"], 2)
        self.assertEqual(record["revision"]["commit"],
                         subprocess.check_output(
                             ["git", "-C", repo, "rev-parse", "HEAD"],
                             text=True).strip())
        self.assertEqual(record["content_hash"],
                         style.load_contract(repo).content_hash)
        self.assertTrue(record["router_hash"])

    def test_style_status_reports_the_mount(self):
        repo = self._style_repo()
        fake = self._fake_home()
        with mock.patch.dict(os.environ, {"HOME": fake}):
            _run_cli("style", "adopt", repo, "--brain", self.brain)
            out, _ = _run_cli("style", "--brain", self.brain)
        self.assertIn(os.path.realpath(repo), out)

    def test_refresh_recreates_the_learned_overlay_for_an_existing_mount(self):
        repo = self._style_repo()
        fake = self._fake_home()
        overlay = os.path.join(self.brain, style.LEARNED_OVERLAY)
        with mock.patch.dict(os.environ, {"HOME": fake}):
            _run_cli("install", "--home", "--brain", self.brain)
            _run_cli("style", "adopt", repo, "--brain", self.brain)
            os.rmdir(overlay)
            _run_cli("style", "refresh", "--brain", self.brain)
            self.assertTrue(os.path.isdir(overlay))
            out, _ = _run_cli("doctor", "--home", "--brain", self.brain)
        self.assertIn("home configuration: valid", out)

    def test_style_status_without_a_mount_says_how_to_get_one(self):
        out, _ = _run_cli("style", "--brain", self.brain)
        self.assertIn("style adopt", out)


# -- the evolution loop remains separate from the mounted repo ----------------

class TestEvolveIntoMountedRepo(_Tmp):
    def test_promoted_law_lands_in_the_separate_overlay(self):
        repo = self._style_repo()
        fake = os.path.join(self.tmp, "userhome")
        os.makedirs(fake)
        with mock.patch.dict(os.environ, {"HOME": fake}):
            _run_cli("style", "adopt", repo, "--brain", self.brain)
        d = Dynamics(self.brain)
        for i, sess in enumerate(("s1", "s1", "s2")):
            d.feedback(f"cut the intro paragraph shorter {i}",
                       domain="email", polarity=-1, session=sess)
        b = Bundle(self.brain)
        r = evolve_once(b, d)
        self.assertEqual(len(r["promoted"]), 1)
        law = os.path.join(self.brain, "style-learned", "email.md")
        self.assertTrue(os.path.isfile(law),
                        "promoted law must land in the private overlay")
        self.assertFalse(os.path.exists(os.path.join(repo, "learned")))


# -- the bundle follows the mount ---------------------------------------------

class TestBundleFollowsMounts(_Tmp):
    def test_notes_behind_a_symlinked_dir_are_loaded(self):
        outside = os.path.join(self.tmp, "elsewhere")
        os.makedirs(outside)
        with open(os.path.join(outside, "g.md"), "w", encoding="utf-8") as fh:
            fh.write("---\ntitle: Mounted\n---\n\nbody\n")
        os.symlink(outside, os.path.join(self.brain, "mounted"))
        self.assertIn("mounted/g.md", Bundle(self.brain).notes)

    def test_a_symlink_cycle_cannot_hang_the_load(self):
        os.symlink(self.brain, os.path.join(self.brain, "loop"))
        b = Bundle(self.brain)  # must terminate
        self.assertNotIn("loop/loop/home.md", b.notes)

    def test_an_in_bundle_alias_never_renames_canonical_note_paths(self):
        # a user's convenience symlink inside the bundle (alias -> zettel)
        # must not shadow the real dir: every ledger entry, pin, and link
        # is keyed to the canonical spelling
        z = os.path.join(self.brain, "zettel")
        os.makedirs(z)
        with open(os.path.join(z, "foo.md"), "w", encoding="utf-8") as fh:
            fh.write("---\ntitle: Foo\n---\n\nbody\n")
        os.symlink(z, os.path.join(self.brain, "alias"))
        b = Bundle(self.brain)
        self.assertIn("zettel/foo.md", b.notes)
        self.assertNotIn("alias/foo.md", b.notes)


# -- doctor knows about style ---------------------------------------------------

class TestDoctorStyle(_Tmp):
    def test_doctor_ignores_unlisted_files_but_flags_routed_content_drift(self):
        repo = self._style_repo()
        fake = os.path.join(self.tmp, "userhome")
        os.makedirs(fake)
        with mock.patch.dict(os.environ, {"HOME": fake}):
            _run_cli("install", "--home", "--brain", self.brain)
            _run_cli("style", "adopt", repo, "--brain", self.brain)
            out, _ = _run_cli("doctor", "--home", "--brain", self.brain)
            self.assertIn("home configuration: valid", out)
            with open(os.path.join(repo, "talks.md"), "w",
                      encoding="utf-8") as fh:
                fh.write("# Talk abstracts\n\nno buzzwords\n")
            out, _ = _run_cli("doctor", "--home", "--brain", self.brain)
            self.assertIn("home configuration: valid", out)
            with open(os.path.join(repo, "email.md"), "a",
                      encoding="utf-8") as fh:
                fh.write("Ask directly.\n")
            with self.assertRaises(SystemExit):
                _run_cli("doctor", "--home", "--brain", self.brain)

    def _remote_pair(self, prefix):
        remote = os.path.join(self.tmp, f"{prefix}.git")
        seed = self._style_repo(f"{prefix}-seed")
        subprocess.run(["git", "init", "--bare", "-q", remote], check=True)
        subprocess.run(["git", "-C", seed, "init", "-q"], check=True)
        ident = ["-c", "user.name=Test", "-c",
                 "user.email=test@example.invalid"]
        subprocess.run(["git", "-C", seed, "add", "."], check=True)
        subprocess.run(["git", "-C", seed, *ident, "commit", "-qm", "one"],
                       check=True)
        subprocess.run(["git", "-C", seed, "remote", "add", "origin", remote],
                       check=True)
        subprocess.run(["git", "-C", seed, "push", "-qu", "origin", "HEAD"],
                       check=True)
        repo = os.path.join(self.tmp, f"{prefix}-clone")
        subprocess.run(["git", "clone", "-q", remote, repo], check=True)
        return seed, repo, ident

    def test_doctor_flags_a_branch_behind_its_local_upstream(self):
        seed, repo, ident = self._remote_pair("behind")
        fake = os.path.join(self.tmp, "userhome")
        os.makedirs(fake)
        with mock.patch.dict(os.environ, {"HOME": fake}):
            _run_cli("install", "--home", "--brain", self.brain)
            _run_cli("style", "adopt", repo, "--brain", self.brain)
            with open(os.path.join(seed, "email.md"), "a",
                      encoding="utf-8") as fh:
                fh.write("A remote change.\n")
            subprocess.run(["git", "-C", seed, "add", "."], check=True)
            subprocess.run(["git", "-C", seed, *ident, "commit", "-qm", "two"],
                           check=True)
            subprocess.run(["git", "-C", seed, "push", "-q"], check=True)
            subprocess.run(["git", "-C", repo, "fetch", "-q"], check=True)
            with self.assertRaises(SystemExit):
                _run_cli("doctor", "--home", "--brain", self.brain)

    def test_refresh_pull_fast_forwards_and_rewires(self):
        seed, repo, ident = self._remote_pair("refresh")
        fake = os.path.join(self.tmp, "refresh-home")
        os.makedirs(fake)
        with mock.patch.dict(os.environ, {"HOME": fake}):
            _run_cli("install", "--home", "--brain", self.brain)
            _run_cli("style", "adopt", repo, "--brain", self.brain)
            with open(os.path.join(seed, "core.md"), "w",
                      encoding="utf-8") as fh:
                fh.write("Use complete sentences. Prefer concrete nouns.\n")
            subprocess.run(["git", "-C", seed, "add", "."], check=True)
            subprocess.run(["git", "-C", seed, *ident, "commit", "-qm", "two"],
                           check=True)
            subprocess.run(["git", "-C", seed, "push", "-q"], check=True)
            _run_cli("style", "refresh", "--pull", "--brain", self.brain)
            with open(os.path.join(fake, "AGENTS.md"), encoding="utf-8") as fh:
                self.assertIn("Prefer concrete nouns.", fh.read())


# -- explicit error cases ----------------------------------------------------

class TestStyleEdges(_Tmp):
    def test_adopt_without_a_brain_fails_with_the_fix(self):
        repo = self._style_repo()
        empty = os.path.join(self.tmp, "nobrain")
        with self.assertRaises(SystemExit) as cm:
            _run_cli("style", "adopt", repo, "--brain", empty)
        self.assertEqual(cm.exception.code, 1)

    def test_adopt_without_a_repo_argument_fails_loud(self):
        with self.assertRaises(SystemExit):
            _run_cli("style", "adopt", "--brain", self.brain)

    def test_adopt_without_a_manifest_fails_loud(self):
        empty = os.path.join(self.tmp, "noguides")
        os.makedirs(empty)
        with self.assertRaises(SystemExit):
            _run_cli("style", "adopt", empty, "--brain", self.brain)

    def test_adopt_refuses_to_switch_a_mounted_repo_silently(self):
        fake = os.path.join(self.tmp, "userhome")
        os.makedirs(fake)
        first, second = self._style_repo("one"), self._style_repo("two")
        with mock.patch.dict(os.environ, {"HOME": fake}):
            _run_cli("style", "adopt", first, "--brain", self.brain)
            with self.assertRaises(SystemExit):
                _run_cli("style", "adopt", second, "--brain", self.brain)
        self.assertEqual(os.path.realpath(os.path.join(self.brain, "style")),
                         os.path.realpath(first))

    def test_unknown_action_fails_loud(self):
        with self.assertRaises(SystemExit):
            _run_cli("style", "banana", "--brain", self.brain)

    def test_legacy_relocation_never_overwrites_overlay_law(self):
        repo = self._style_repo()
        overlay = os.path.join(self.brain, "style-learned")
        with open(os.path.join(overlay, "email.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("current law\n")
        learned = os.path.join(self.brain, "style", "learned")
        os.makedirs(learned)
        with open(os.path.join(learned, "email.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("brain law\n")
        style.mount(self.brain, repo)
        with open(os.path.join(overlay, "email.md"),
                  encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "current law\n")
        with open(os.path.join(overlay, "email-migrated.md"),
                  encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "brain law\n")

    def test_mount_survives_platforms_without_symlinks(self):
        repo = self._style_repo()
        with mock.patch("os.symlink", side_effect=OSError("no links here")):
            verb = style.mount(self.brain, repo)
        self.assertIn("symlinks unavailable", verb)
        # the sidecar record still routes everything
        self.assertEqual(style.mounted_repo(self.brain),
                         os.path.realpath(repo))
        router = style.render_router(repo, self.brain)
        self.assertIn(os.path.abspath(repo), router)  # paths go to the repo

    def test_current_block_is_none_without_markers_or_file(self):
        p = os.path.join(self.tmp, "plain.md")
        self.assertIsNone(style.current_block(p))  # no file
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("just prose\n")
        self.assertIsNone(style.current_block(p))  # no markers

    def test_wire_block_refuses_damaged_markers_untouched(self):
        # (a) END before BEGIN must not crash; (b) an orphaned BEGIN must
        # not be "repaired" by appending: the second refresh would then
        # swallow every byte of user prose between them
        for bad in (style.STYLE_END + "\nprose\n" + style.STYLE_BEGIN + "\n",
                    style.STYLE_BEGIN + "\nprose the user wrote\n"):
            p = os.path.join(self.tmp, "broken.md")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(bad)
            with self.assertRaises(style.MountError):
                style.wire_block(p, "router")
            with open(p, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), bad)  # untouched, byte for byte

    def test_ambiguous_style_markers_preserve_every_byte(self):
        for markers in ("BBEE", "BEBE", "EBE", "BEE", "BBE"):
            with self.subTest(markers=markers):
                text = "\nUser-owned content.\n".join(
                    style.STYLE_BEGIN if marker == "B" else style.STYLE_END
                    for marker in markers)
                path = os.path.join(self.tmp, "ambiguous-style.md")
                with open(path, "w", encoding="utf-8") as stream:
                    stream.write(text)
                self.assertIsNone(style.current_block(path))
                with self.assertRaises(style.MountError):
                    style.wire_block(path, "Replacement content.")
                with open(path, encoding="utf-8") as stream:
                    self.assertEqual(stream.read(), text)

    def test_ambiguous_protocol_markers_preserve_every_byte(self):
        for markers in ("BBEE", "BEBE", "EBE", "BEE", "BBE", "B", "E", "EB"):
            with self.subTest(markers=markers):
                text = "\nUser-owned content.\n".join(
                    cli.PROTOCOL_BEGIN if marker == "B" else cli.PROTOCOL_END
                    for marker in markers)
                path = os.path.join(self.tmp, "ambiguous-protocol.md")
                with open(path, "w", encoding="utf-8") as stream:
                    stream.write(text)
                result = cli._write_copy(path, self.brain)
                self.assertIn("left untouched", result)
                with open(path, encoding="utf-8") as stream:
                    self.assertEqual(stream.read(), text)

    def test_router_neutralizes_marker_syntax_in_guide_content(self):
        repo = self._style_repo("hostile")
        manifest = self._manifest(repo)
        manifest["guides"][0]["title"] = f"x {style.STYLE_END} y"
        self._write_manifest(repo, manifest)
        with self.assertRaises(style.ContractError):
            style.render_router(repo, self.brain)

    def test_doctor_flags_a_mount_whose_router_was_never_wired(self):
        repo = self._style_repo()
        fake = os.path.join(self.tmp, "userhome")
        os.makedirs(fake)
        with mock.patch.dict(os.environ, {"HOME": fake}):
            _run_cli("install", "--home", "--brain", self.brain)
            style.mount(self.brain, repo)  # mounted, but never wired
            with self.assertRaises(SystemExit):
                _run_cli("doctor", "--home", "--brain", self.brain)


if __name__ == "__main__":
    unittest.main()
