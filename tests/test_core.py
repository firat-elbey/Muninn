"""Core tests: store parsing, dynamics physics, recall behavior, packs."""

import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from muninn.dynamics import FLOOR, PIN_FLOOR, Dynamics  # noqa: E402
from muninn.recall import context_pack, estimate_tokens, recall  # noqa: E402
from muninn.store import Bundle, parse_frontmatter, render_frontmatter  # noqa: E402


class TestStore(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.b = Bundle(self.root)

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_frontmatter_roundtrip(self):
        meta = {"type": "fact", "title": "A B", "tags": ["x", "y"],
                "pinned": True, "importance": 0.7}
        text = render_frontmatter(meta) + "\n\nbody"
        parsed, body = parse_frontmatter(text)
        self.assertEqual(parsed["type"], "fact")
        self.assertEqual(parsed["tags"], ["x", "y"])
        self.assertIs(parsed["pinned"], True)
        self.assertEqual(parsed["importance"], 0.7)
        self.assertEqual(body.strip(), "body")

    def test_no_frontmatter_is_permissive(self):
        meta, body = parse_frontmatter("just text")
        self.assertEqual(meta, {})
        self.assertEqual(body, "just text")

    def test_leading_hr_not_treated_as_frontmatter(self):
        # a note that OPENS with a '---' horizontal rule (no frontmatter intent) must
        # not have the text up to the next '---' silently swallowed (empty-keys guard)
        meta, body = parse_frontmatter("---\nfirst paragraph\n---\nsecond paragraph\n")
        self.assertEqual(meta, {})
        self.assertIn("first paragraph", body)
        self.assertIn("second paragraph", body)

    def test_wikilink_resolution_and_backlinks(self):
        self.b.write_note("a.md", {"title": "Alpha"}, "links to [[Beta]]")
        self.b.write_note("b.md", {"title": "Beta"}, "plain")
        b2 = Bundle(self.root)
        self.assertEqual(b2.notes["a.md"].links, ["b.md"])
        self.assertEqual(b2.backlinks["b.md"], ["a.md"])

    def test_supersedes_mapping(self):
        self.b.write_note("old.md", {"title": "Old"}, "x")
        self.b.write_note("new.md", {"title": "New", "supersedes": ["old.md"]}, "y")
        b2 = Bundle(self.root)
        self.assertEqual(b2.superseded_by(), {"old.md": "new.md"})

    def test_index_generation(self):
        self.b.write_note("t/a.md", {"title": "A", "description": "da"}, "x")
        text = Bundle(self.root).generate_index()
        self.assertIn("* [A](/t/a.md) - da", text)
        self.assertIn('okf_version: "0.2"', text)

    def test_index_generation_rejects_a_symbolic_link_destination(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        target = os.path.join(outside, "index.md")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("unchanged\n")
        try:
            os.symlink(target, os.path.join(self.root, "index.md"))
        except OSError as error:
            self.skipTest(f"symbolic links unavailable: {error}")

        with self.assertRaisesRegex(ValueError, "symbolic link"):
            Bundle(self.root).generate_index()
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "unchanged\n")

    def test_bundle_loading_enforces_each_resource_limit(self):
        cases = (
            (
                {"large.md": "x" * 11},
                {"max_note_bytes": 10},
                "note exceeds 10 bytes",
            ),
            (
                {"a.md": "a", "b.md": "b"},
                {"max_notes": 1},
                "exceeds 1 notes",
            ),
            (
                {"a.md": "aaaa", "b.md": "bbbb"},
                {"max_bundle_bytes": 7},
                "exceeds 7 note bytes",
            ),
            (
                {"a/note.md": "a"},
                {"max_directories": 1},
                "exceeds 1 directories",
            ),
            (
                {"a/b/note.md": "a"},
                {"max_depth": 1},
                "exceeds directory depth 1",
            ),
            (
                {"a.md": "a", "b.txt": "b"},
                {"max_entries": 1},
                "exceeds 1 filesystem entries",
            ),
        )
        for files, limits, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as root:
                for relative, content in files.items():
                    full = os.path.join(root, relative)
                    os.makedirs(os.path.dirname(full), exist_ok=True)
                    with open(full, "w", encoding="utf-8") as handle:
                        handle.write(content)
                with self.assertRaisesRegex(ValueError, message):
                    Bundle(root, **limits)

        with self.assertRaisesRegex(ValueError, "bundle limit must be positive"):
            Bundle(self.root, max_notes=0)

    def test_bundle_prunes_literal_muninnignore_paths(self):
        with open(
            os.path.join(self.root, ".muninnignore"), "w", encoding="utf-8"
        ) as fh:
            fh.write("cache/\nignored.md\narchive\n")
        for path in (
            "cache/generated.md",
            "ignored.md",
            "archive/kept.md",
            "notes/cache/kept.md",
            "notes/kept.md",
        ):
            full = os.path.join(self.root, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(f"# {path}\n")

        loaded = Bundle(self.root)

        self.assertEqual(
            set(loaded.notes),
            {"archive/kept.md", "notes/cache/kept.md", "notes/kept.md"},
        )

    def test_gitignore_does_not_hide_knowledge(self):
        self.b.write_note("notes/kept.md", {"title": "Kept"}, "knowledge")
        with open(os.path.join(self.root, ".gitignore"), "w", encoding="utf-8") as fh:
            fh.write("notes/\n")

        self.assertIn("notes/kept.md", Bundle(self.root).notes)

    def test_write_note_rejects_a_muninnignored_destination(self):
        with open(
            os.path.join(self.root, ".muninnignore"), "w", encoding="utf-8"
        ) as fh:
            fh.write("generated/\nignored.md\n")
        bundle = Bundle(self.root)

        with self.assertRaisesRegex(ValueError, "excluded by .muninnignore"):
            bundle.write_note(
                "generated/note.md", {"title": "Hidden"}, "must not be written"
            )
        with self.assertRaisesRegex(ValueError, "excluded by .muninnignore"):
            bundle.write_note(
                "ignored.md", {"title": "Hidden"}, "must not be written"
            )

        self.assertFalse(os.path.exists(os.path.join(self.root, "generated/note.md")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "ignored.md")))

    def test_write_note_refreshes_exclusions_and_normalizes_safe_paths(self):
        bundle = Bundle(self.root)
        with open(
            os.path.join(self.root, ".muninnignore"), "w", encoding="utf-8"
        ) as fh:
            fh.write("generated/\n")

        with self.assertRaisesRegex(ValueError, "excluded by .muninnignore"):
            bundle.write_note(
                "generated/note.md", {"title": "Hidden"}, "must not be written"
            )
        self.assertEqual(
            bundle.write_note("./note.md", {"title": "Root"}, "content"),
            "note.md",
        )
        self.assertEqual(
            bundle.write_note(
                "notes//note.md", {"title": "Nested"}, "content"
            ),
            "notes/note.md",
        )
        with self.assertRaisesRegex(ValueError, "escapes the bundle"):
            bundle.write_note("", {"title": "Invalid"}, "content")

    def test_write_note_rejects_paths_that_the_loader_cannot_read(self):
        bundle = Bundle(self.root)
        for path in (".hidden/note.md", "plain.txt", "index.md", "log.md"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                bundle.write_note(path, {"title": "Invalid"}, "content")

        for path in (
            ".hidden/note.md",
            "plain.txt",
            "index.md",
            "log.md",
        ):
            self.assertFalse(os.path.lexists(os.path.join(self.root, path)))

    def test_write_note_rejects_symbolic_link_destinations(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        outside_file = os.path.join(outside, "outside.md")
        with open(outside_file, "w", encoding="utf-8") as fh:
            fh.write("unchanged\n")
        try:
            os.symlink(outside_file, os.path.join(self.root, "linked.md"))
            os.symlink(outside, os.path.join(self.root, "mounted"))
        except OSError as exc:
            self.skipTest(f"symbolic links unavailable: {exc}")

        bundle = Bundle(self.root)
        self.assertNotIn("linked.md", bundle.notes)
        self.assertIn("mounted/outside.md", bundle.notes)
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            bundle.write_note("linked.md", {"title": "Linked"}, "changed")
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            bundle.write_note(
                "mounted/created.md", {"title": "Mounted"}, "created"
            )
        with open(outside_file, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "unchanged\n")
        self.assertFalse(os.path.exists(os.path.join(outside, "created.md")))

    def test_write_note_rejects_filesystem_aliases_to_excluded_paths(self):
        real = os.path.join(self.root, "real")
        os.makedirs(real)
        try:
            os.symlink(real, os.path.join(self.root, "alias"))
        except OSError as exc:
            self.skipTest(f"symbolic links unavailable: {exc}")
        with open(
            os.path.join(self.root, ".muninnignore"), "w", encoding="utf-8"
        ) as fh:
            fh.write("real/\n")

        with self.assertRaisesRegex(ValueError, "symbolic link"):
            Bundle(self.root).write_note(
                "alias/hidden.md", {"title": "Hidden"}, "hidden"
            )

        upper = os.path.join(self.root, "Notes")
        lower = os.path.join(self.root, "notes")
        os.makedirs(upper)
        if os.path.lexists(lower) and os.path.samefile(upper, lower):
            with open(
                os.path.join(self.root, ".muninnignore"),
                "w",
                encoding="utf-8",
            ) as fh:
                fh.write("Notes/\n")
            with self.assertRaisesRegex(ValueError, "excluded by .muninnignore"):
                Bundle(self.root).write_note(
                    "notes/hidden.md", {"title": "Hidden"}, "hidden"
                )
            self.assertFalse(os.path.exists(os.path.join(upper, "hidden.md")))

    def test_write_note_rejects_windows_drive_and_unc_paths(self):
        bundle = Bundle(self.root)
        for path in ("C:outside.md", "C:\\outside.md", "\\\\server\\share\\note.md"):
            with self.subTest(path=path), self.assertRaisesRegex(
                ValueError, "escapes the bundle"
            ):
                bundle.write_note(path, {"title": "Outside"}, "content")

    def test_write_note_fails_closed_without_descriptor_writes(self):
        bundle = Bundle(self.root)
        with mock.patch(
            "muninn.store._descriptor_writes_supported", return_value=False
        ), self.assertRaisesRegex(OSError, "race-safe bundle writes"):
            bundle.write_note(
                "fallback/note.md", {"title": "Fallback"}, "content"
            )
        self.assertFalse(os.path.lexists(os.path.join(self.root, "fallback")))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFOs unavailable")
    def test_write_note_rejects_a_fifo_without_blocking(self):
        os.mkfifo(os.path.join(self.root, "blocked.md"))
        bundle = Bundle(self.root)
        self.assertNotIn("blocked.md", bundle.notes)

        with self.assertRaisesRegex(ValueError, "not a regular file"):
            bundle.write_note("blocked.md", {"title": "Blocked"}, "content")

    @unittest.skipUnless(hasattr(os, "link"), "hard links unavailable")
    def test_write_note_rejects_a_multiply_linked_file(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        outside_file = os.path.join(outside, "outside.md")
        with open(outside_file, "w", encoding="utf-8") as fh:
            fh.write("unchanged\n")
        try:
            os.link(outside_file, os.path.join(self.root, "linked.md"))
        except OSError as exc:
            self.skipTest(f"hard links unavailable: {exc}")

        with self.assertRaisesRegex(ValueError, "multiple hard links"):
            Bundle(self.root).write_note(
                "linked.md", {"title": "Linked"}, "changed"
            )
        with open(outside_file, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "unchanged\n")

    @unittest.skipUnless(
        hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.open in os.supports_dir_fd,
        "descriptor-relative writes unavailable",
    )
    def test_write_note_does_not_follow_a_parent_replaced_during_open(self):
        parent = os.path.join(self.root, "parent")
        original_parent = os.path.join(self.root, "original-parent")
        os.makedirs(parent)
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        real_replace = os.replace
        replaced = False

        def racing_replace(
            source,
            destination,
            *,
            src_dir_fd=None,
            dst_dir_fd=None,
        ):
            nonlocal replaced
            if not replaced:
                os.rename(parent, original_parent)
                os.symlink(outside, parent)
                replaced = True
            return real_replace(
                source,
                destination,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )

        with mock.patch("muninn.store.os.replace", side_effect=racing_replace):
            with self.assertRaises(ValueError):
                Bundle(self.root).write_note(
                    "parent/note.md", {"title": "Note"}, "content"
                )

        self.assertTrue(replaced)
        self.assertFalse(os.path.exists(os.path.join(outside, "note.md")))

    @unittest.skipUnless(hasattr(os, "link"), "hard links unavailable")
    def test_atomic_write_does_not_modify_a_late_hard_link(self):
        bundle = Bundle(self.root)
        bundle.write_note("note.md", {"title": "Old"}, "old content")
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        outside_alias = os.path.join(outside, "old-note.md")
        real_replace = os.replace
        linked = False

        def linking_replace(
            source,
            destination,
            *,
            src_dir_fd=None,
            dst_dir_fd=None,
        ):
            nonlocal linked
            if not linked:
                os.link(os.path.join(self.root, "note.md"), outside_alias)
                linked = True
            return real_replace(
                source,
                destination,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )

        with mock.patch("muninn.store.os.replace", side_effect=linking_replace):
            bundle.write_note("note.md", {"title": "New"}, "new content")

        self.assertTrue(linked)
        with open(outside_alias, encoding="utf-8") as fh:
            self.assertIn("old content", fh.read())
        with open(os.path.join(self.root, "note.md"), encoding="utf-8") as fh:
            self.assertIn("new content", fh.read())
        self.assertFalse(
            os.path.samefile(outside_alias, os.path.join(self.root, "note.md"))
        )

    @unittest.skipUnless(
        hasattr(os, "link") and os.link in os.supports_dir_fd,
        "descriptor-relative hard links unavailable",
    )
    def test_atomic_write_finishes_before_a_late_hard_link(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        outside_alias = os.path.join(outside, "temporary.md")
        real_replace = os.replace
        linked = False

        def linking_replace(
            source,
            target,
            *,
            src_dir_fd=None,
            dst_dir_fd=None,
        ):
            nonlocal linked
            if not linked:
                os.link(
                    source,
                    outside_alias,
                    src_dir_fd=src_dir_fd,
                    follow_symlinks=False,
                )
                linked = True
            return real_replace(
                source,
                target,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )

        with mock.patch(
            "muninn.store.os.replace",
            side_effect=linking_replace,
        ):
            Bundle(self.root).write_note(
                "note.md", {"title": "New"}, "new content"
            )

        self.assertTrue(linked)
        destination = os.path.join(self.root, "note.md")
        self.assertTrue(os.path.samefile(outside_alias, destination))
        with open(destination, encoding="utf-8") as handle:
            self.assertIn("new content", handle.read())

    def test_atomic_write_keeps_the_new_file_when_directory_sync_fails(self):
        bundle = Bundle(self.root)
        bundle.write_note("note.md", {"title": "Old"}, "old content")
        real_fsync = os.fsync

        def failing_directory_sync(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError("directory sync failed")
            return real_fsync(descriptor)

        with mock.patch(
            "muninn.store.os.fsync",
            side_effect=failing_directory_sync,
        ), self.assertRaisesRegex(OSError, "directory sync failed"):
            bundle.write_note("note.md", {"title": "New"}, "new content")

        with open(os.path.join(self.root, "note.md"), encoding="utf-8") as handle:
            self.assertIn("new content", handle.read())

    def test_atomic_write_preserves_a_concurrent_replacement(self):
        bundle = Bundle(self.root)
        bundle.write_note("note.md", {"title": "Old"}, "old content")
        concurrent = os.path.join(self.root, "concurrent.tmp")
        with open(concurrent, "w", encoding="utf-8") as handle:
            handle.write("concurrent content\n")
        real_replace = os.replace
        real_stat = os.stat
        committed = False
        raced = False

        def marking_replace(*args, **kwargs):
            nonlocal committed
            result = real_replace(*args, **kwargs)
            committed = True
            return result

        def racing_stat(path, *args, **kwargs):
            nonlocal raced
            if committed and path == "note.md" and kwargs.get("dir_fd") and not raced:
                real_replace(concurrent, os.path.join(self.root, "note.md"))
                raced = True
            return real_stat(path, *args, **kwargs)

        with (
            mock.patch(
                "muninn.store._descriptor_writes_supported",
                return_value=True,
            ),
            mock.patch("muninn.store.os.replace", side_effect=marking_replace),
            mock.patch("muninn.store.os.stat", side_effect=racing_stat),
            self.assertRaisesRegex(ValueError, "changed during write"),
        ):
            bundle.write_note("note.md", {"title": "New"}, "new content")

        self.assertTrue(raced)
        with open(os.path.join(self.root, "note.md"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "concurrent content\n")

    def test_muninnignore_rejects_nonliteral_entries(self):
        path = os.path.join(self.root, ".muninnignore")
        for value in (
            "*.md",
            "!notes/kept.md",
            "/absolute/",
            "notes//bad/",
            "notes/../bad/",
            "notes\\bad/",
        ):
            with self.subTest(value=value):
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(value + "\n")
                with self.assertRaisesRegex(ValueError, "invalid .muninnignore"):
                    Bundle(self.root)

    def test_muninnignore_is_bounded_and_must_be_a_regular_file(self):
        path = os.path.join(self.root, ".muninnignore")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("x" * 65_537)
        with self.assertRaisesRegex(ValueError, "exceeds 65536 bytes"):
            Bundle(self.root)
        os.unlink(path)

        outside = os.path.join(self.root, "ignore-source")
        with open(outside, "w", encoding="utf-8") as fh:
            fh.write("notes/\n")
        try:
            os.symlink(outside, path)
        except OSError as exc:
            self.skipTest(f"file symlinks unavailable: {exc}")
        with self.assertRaisesRegex(ValueError, "regular file"):
            Bundle(self.root)

    def test_muninnignore_does_not_follow_a_link_swapped_during_open(self):
        path = os.path.join(self.root, ".muninnignore")
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        outside_policy = os.path.join(outside, "policy")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("original/\n")
        with open(outside_policy, "w", encoding="utf-8") as handle:
            handle.write("outside/\n")
        real_open = os.open
        swapped = False

        def racing_open(target, flags, *args, **kwargs):
            nonlocal swapped
            if target == path and not swapped:
                os.unlink(path)
                os.symlink(outside_policy, path)
                swapped = True
            return real_open(target, flags, *args, **kwargs)

        with mock.patch(
            "muninn.store.os.open",
            side_effect=racing_open,
        ), self.assertRaisesRegex(ValueError, "changed during read"):
            Bundle(self.root)
        self.assertTrue(swapped)

    def test_muninnignore_accepts_utf8_bom_comments_and_blank_lines(self):
        path = os.path.join(self.root, ".muninnignore")
        with open(path, "wb") as fh:
            fh.write("\ufeff# generated content\n\nnotes/\n".encode("utf-8"))
        os.makedirs(os.path.join(self.root, "notes"))
        with open(
            os.path.join(self.root, "notes", "ignored.md"),
            "w",
            encoding="utf-8",
        ) as fh:
            fh.write("# ignored\n")

        self.assertEqual(Bundle(self.root).notes, {})

    def test_muninnignore_rejects_non_utf8_content(self):
        path = os.path.join(self.root, ".muninnignore")
        with open(path, "wb") as fh:
            fh.write(b"notes/\n\xff")

        with self.assertRaisesRegex(ValueError, "must be UTF-8"):
            Bundle(self.root)

    def test_muninnignore_can_prune_a_mounted_directory(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        with open(os.path.join(outside, "mounted.md"), "w", encoding="utf-8") as fh:
            fh.write("# mounted\n")
        try:
            os.symlink(outside, os.path.join(self.root, "mounted"))
        except OSError as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")
        with open(
            os.path.join(self.root, ".muninnignore"), "w", encoding="utf-8"
        ) as fh:
            fh.write("mounted/\n")

        self.assertNotIn("mounted/mounted.md", Bundle(self.root).notes)


class TestDynamics(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.d = Dynamics(self.root)

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_touch_strengthens(self):
        self.d.touch("a.md")
        s1 = self.d.strength("a.md")
        for _ in range(5):
            self.d.touch("a.md")
        self.assertGreater(self.d.strength("a.md"), s1)

    def test_capture_boosts_recent_window(self):
        self.d.touch("pre.md")
        self.d.outcome(-0.9, why="incident")
        self.assertGreater(self.d.entries["pre.md"]["captured"], 0)
        # weak outcomes do not capture
        self.d.touch("weak.md")
        self.d.outcome(-0.2)
        self.assertEqual(self.d.entries["weak.md"]["captured"], 0.0)

    def test_ltd_on_supersede(self):
        self.d.touch("old.md")
        self.d.touch("new.md")
        before = self.d.strength("old.md")
        self.d.supersede("old.md", "new.md")
        self.assertLess(self.d.strength("old.md"), before)

    def test_pin_floor_survives_decay(self):
        self.d.touch("policy.md")
        self.d.pin("policy.md")
        for _ in range(60):
            self.d.consolidate()
        self.assertGreaterEqual(self.d.strength("policy.md"), PIN_FLOOR)

    def test_unused_go_dormant(self):
        self.d.touch("fad.md")
        for _ in range(80):
            self.d.consolidate()
        self.assertLess(self.d.strength("fad.md"), FLOOR)
        self.assertTrue(self.d.is_dormant("fad.md"))

    def test_ledger_replay_rebuilds_state(self):
        self.d.touch("a.md")
        self.d.touch("a.md")
        self.d.supersede("a.md", "b.md")
        self.d.outcome(-0.8)
        self.d.consolidate()
        snapshot = {k: dict(v) for k, v in self.d.entries.items()}
        os.remove(self.d.state_path)
        d2 = Dynamics(self.root)
        self.assertEqual(snapshot, d2.entries)


class TestRecall(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        b = Bundle(self.root)
        b.write_note("new.md", {"title": "DB port", "type": "fact",
                                "supersedes": ["old.md"]},
                     "postgres listens on 7433. see [[DB host]]")
        b.write_note("old.md", {"title": "DB port old", "type": "fact"},
                     "postgres listens on 5432")
        b.write_note("host.md", {"title": "DB host", "type": "fact"},
                     "primary at 192.0.2.10")
        self.b = Bundle(self.root)
        self.d = Dynamics(self.root)

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_superseded_excluded(self):
        hits = recall(self.b, self.d, "postgres port", reactivate=False)
        paths = [n.path for n, _s, _w in hits]
        self.assertIn("new.md", paths)
        self.assertNotIn("old.md", paths)

    def test_include_stale_flag(self):
        hits = recall(self.b, self.d, "postgres port", reactivate=False,
                      include_stale=True)
        self.assertIn("old.md", [n.path for n, _s, _w in hits])

    def test_spreading_activation_reaches_neighbor(self):
        hits = recall(self.b, self.d, "postgres 7433", reactivate=False, k=5)
        self.assertIn("host.md", [n.path for n, _s, _w in hits])

    def test_dynamics_reorder(self):
        # make host.md much stronger; a tie-ish cue should now favor it
        for _ in range(9):
            self.d.touch("host.md")
        flat = recall(self.b, None, "db", use_dynamics=False, reactivate=False)
        dyn = recall(self.b, self.d, "db", reactivate=False)
        self.assertEqual(dyn[0][0].path, "host.md")
        self.assertIsNotNone(flat)

    def test_pack_budget_respected(self):
        pack = context_pack(self.b, self.d, "postgres port", budget=200,
                            reactivate=False)
        self.assertLessEqual(estimate_tokens(pack), 260)  # small overshoot ok
        self.assertIn("why loaded", pack)

    def test_recall_reactivates(self):
        before = self.d.strength("new.md")
        recall(self.b, self.d, "postgres port")  # reactivate=True default
        self.assertGreater(self.d.strength("new.md"), before)

    def test_pack_survives_read_only_recall_metadata(self):
        with mock.patch.object(
                self.d, "touch", side_effect=PermissionError("read-only")):
            pack = context_pack(self.b, self.d, "postgres port")
        self.assertIn("DB port", pack)


class TestReviewRegressions(unittest.TestCase):
    """Regression tests for the pre-release review-panel findings."""

    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_replay_resets_coactivation(self):
        d = Dynamics(self.root)
        d.touch("a.md", session="s1")
        d.touch("b.md", session="s1")
        before = dict(d.coact)
        d.replay()
        d.replay()
        self.assertEqual(d.coact, before)

    def test_state_json_corruption_recovers_via_replay(self):
        d = Dynamics(self.root)
        d.touch("a.md")
        for garbage in ("null", "[]", '{"entries": 5}', '{"entr'):
            with open(d.state_path, "w") as fh:
                fh.write(garbage)
            d2 = Dynamics(self.root)
            self.assertEqual(d2.entries["a.md"]["recurrence"], 1, garbage)

    def test_stale_state_rebuilds_from_ledger(self):
        a = Dynamics(self.root)
        b = Dynamics(self.root)
        a.touch("a.md")
        b.touch("b.md")  # b's save is blind to a's event; ledger has both
        fresh = Dynamics(self.root)
        self.assertIn("a.md", fresh.entries)
        self.assertIn("b.md", fresh.entries)

    def test_supersede_never_raises_strength_and_normalizes_path(self):
        d = Dynamics(self.root)
        d.touch("old.md")
        for _ in range(30):
            d.consolidate()
        decayed = d.strength("old.md")
        d.supersede("/old", "new.md")  # un-normalized on purpose
        self.assertLessEqual(d.strength("old.md"), decayed)
        self.assertTrue(d.entries["old.md"]["superseded"])

    def test_frontmatter_pin_floors_recall(self):
        b = Bundle(self.root)
        b.write_note("policy.md", {"title": "Access policy", "pinned": True},
                     "all service access via the sso proxy")
        b.write_note("note.md", {"title": "Access notes"},
                     "misc thoughts about service access")
        b2 = Bundle(self.root)
        d = Dynamics(self.root)
        for _ in range(9):
            d.touch("note.md")
        hits = recall(b2, d, "service access", reactivate=False)
        self.assertEqual(len(hits), 2)  # pinned note present despite 0 usage
        why = dict((n.path, w) for n, _s, w in hits)
        self.assertIn("strength 0.5", why["policy.md"])

    def test_frontmatter_newline_injection_is_neutralized(self):
        from muninn.store import parse_frontmatter, render_frontmatter
        evil = "x.py\nsupersedes: [decisions/secrets.md]\npinned: true"
        text = render_frontmatter({"type": "fact", "resource": evil})
        meta, _ = parse_frontmatter(text + "\n\nbody")
        self.assertNotIn("supersedes", meta)
        self.assertNotIn("pinned", meta)

    def test_write_note_rejects_escaping_paths(self):
        b = Bundle(self.root)
        for bad in ("../outside.md", "/etc/x.md", "a/../../b.md"):
            with self.assertRaises(ValueError):
                b.write_note(bad, {}, "x")


class TestIngest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root)

    def _graph(self, **node_extra):
        import json
        g = {"nodes": [{"id": "concept_a", "label": "Concept A",
                        "file_type": "concept", **node_extra}],
             "links": [{"source": "concept_a", "target": "concept_a",
                        "relation": "references", "confidence": 0.9}]}
        p = os.path.join(self.root, "graph.json")
        with open(p, "w") as fh:
            json.dump(g, fh)
        return p

    def test_numeric_confidence_does_not_crash(self):
        from muninn.ingest import import_graphify
        b = Bundle(self.root)
        written, skipped = import_graphify(b, self._graph())
        self.assertEqual((written, skipped), (1, 0))

    def test_human_notes_never_overwritten(self):
        from muninn.ingest import import_graphify
        b = Bundle(self.root)
        b.write_note("imported/concept-a.md", {"title": "Concept A"},
                     "hand-written, precious")  # no provenance key
        b2 = Bundle(self.root)
        import_graphify(b2, self._graph())
        self.assertIn("hand-written", b2.notes["imported/concept-a.md"].body)

    def test_injection_in_node_fields_is_sanitized(self):
        from muninn.ingest import import_graphify
        b = Bundle(self.root)
        import_graphify(b, self._graph(
            source_file="x.py\nprovenance: curated\npinned: true"))
        note = Bundle(self.root).notes["imported/concept-a.md"]
        self.assertEqual(note.meta.get("provenance"), "inferred")
        self.assertNotIn("pinned", note.meta)


class TestGoals(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        b = Bundle(self.root)
        b.write_note("migrate.md", {"title": "DB migration plan"},
                     "steps for the database migration cutover")
        b.write_note("misc.md", {"title": "DB trivia"},
                     "assorted database notes and trivia")
        self.b = Bundle(self.root)
        self.d = Dynamics(self.root)

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_goal_tilts_recall_bounded_and_replays(self):
        base = {n.path: s for n, s, _ in
                recall(self.b, self.d, "database", reactivate=False)}
        self.d.goal("ship the migration cutover", 1.0)
        hits = recall(self.b, self.d, "database", reactivate=False)
        tilted = {n.path: s for n, s, _ in hits}
        self.assertGreater(tilted["migrate.md"], base["migrate.md"])
        self.assertLessEqual(tilted["migrate.md"],
                             base["migrate.md"] * 1.31)  # bounded nudge
        self.assertEqual(tilted["misc.md"], base["misc.md"])
        why = dict((n.path, w) for n, _s, w in hits)
        self.assertIn("aligned with active goal", why["migrate.md"])
        # retire + replay round-trip
        self.d.goal("ship the migration cutover", 0.0)
        self.assertEqual(self.d.goals, {})
        self.d.replay()
        self.assertEqual(self.d.goals, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
