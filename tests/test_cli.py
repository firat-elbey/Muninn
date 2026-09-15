"""CLI-level tests: demo end-to-end, import, build soft-fail."""

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

from muninn import cli  # noqa: E402
from muninn.demo import run_demo  # noqa: E402
from muninn.store import Bundle


class TestDemo(unittest.TestCase):
    def test_demo_runs_end_to_end(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            run_demo(root)
        out = buf.getvalue()
        self.assertIn("Retrieve before observed use", out)
        self.assertIn("Retrieve again with the same files and query", out)
        self.assertIn("Context pack", out)
        self.assertIn("aligned with active goal", out)
        self.assertIn("Available next commands", out)
        # the used-together note surfaces only once usage memory exists
        before, after = out.split("Retrieve again with the same files", 1)
        self.assertNotIn("infra/wal-g.md  (", before)
        self.assertIn("infra/wal-g.md  (", after)


class TestHelpLanguage(unittest.TestCase):
    def test_all_command_help_excludes_informal_release_language(self):
        commands = [
            "demo", "init", "add", "build", "source", "extract", "enrich",
            "import", "viz", "skill", "export-graph", "touch", "outcome",
            "pin", "supersede", "recall", "pack", "volunteer", "goal",
            "intent", "sync", "install", "setup", "uninstall", "adopt",
            "style", "doctor", "feedback", "evolve", "journal",
            "import-transcripts", "lesson", "review", "prime", "observe",
            "hook", "consolidate", "stats", "index",
        ]
        prohibited = (
            "one-minute narrated",
            "throwaway",
            "deep-code",
            "zero LLM",
            "YOUR agent",
            "see the knowledge",
            "brain-first",
            "win/failure",
            "what matters now",
            "distributed team brain",
            "THE one command",
        )
        for command in (None, *commands):
            with self.subTest(command=command):
                out = io.StringIO()
                argv = ["--help"] if command is None else [command, "--help"]
                with redirect_stdout(out), self.assertRaises(SystemExit) as error:
                    cli.main(argv)
                self.assertEqual(error.exception.code, 0)
                for phrase in prohibited:
                    self.assertNotIn(phrase, out.getvalue())
                self.assertNotIn(chr(0x2014), out.getvalue())


class TestPackOutputBudget(unittest.TestCase):
    def test_stdout_including_newlines_stays_within_budget(self):
        with tempfile.TemporaryDirectory() as root:
            Bundle(root).write_note(
                "memory.md", {"type": "concept", "title": "Memory"},
                "Memory keeps a recorded decision.\n" * 30)
            for budget in (0, 1, 2, 10, 40, 100):
                for compact in (False, True):
                    with self.subTest(budget=budget, compact=compact):
                        out = io.StringIO()
                        argv = ["--root", root, "pack", "memory", "--budget",
                                str(budget)] + (["--compact"] if compact else [])
                        with redirect_stdout(out):
                            cli.main(argv)
                self.assertLessEqual(len(out.getvalue()), budget * 4)


class TestInitWriteBoundary(unittest.TestCase):
    def test_init_refuses_linked_gitignore_without_changing_target(self):
        for link in (os.symlink, os.link):
            with self.subTest(link=link.__name__), tempfile.TemporaryDirectory() as area:
                root = os.path.join(area, "brain")
                os.mkdir(root)
                target = os.path.join(area, "outside.txt")
                with open(target, "w", encoding="utf-8") as handle:
                    handle.write("Preserve this file.\n")
                link(target, os.path.join(root, ".gitignore"))
                with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as error:
                    cli.main(["--root", root, "init"])
                self.assertIn(".gitignore", str(error.exception))
                with open(target, encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), "Preserve this file.\n")
                self.assertFalse(os.path.exists(os.path.join(root, "index.md")))

    def test_init_preserves_regular_gitignore_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, ".gitignore")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("local/")
            for _ in range(2):
                with redirect_stdout(io.StringIO()):
                    cli.main(["--root", root, "init"])
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "local/\n.muninn/\n")


class TestAuthoredSecretBoundary(unittest.TestCase):
    def test_add_scrubs_free_text_without_changing_note_identity(self):
        secret = "sk-" + "SYNTHETIC" * 5
        with tempfile.TemporaryDirectory() as root:
            with redirect_stdout(io.StringIO()):
                cli.main(["--root", root, "add", "authored.md", "--title", secret,
                          "--description", secret, "--tags", "safe," + secret,
                          "--body", "Preserve this decision. " + secret])
            note = Bundle(root).notes["authored.md"]
            self.assertNotIn(secret, str(note.meta) + note.body)
            self.assertIn("Preserve this decision.", note.body)
            self.assertIn("[scrubbed]", note.body)

    def test_lesson_scrubs_title_body_and_derived_filename(self):
        secret = "sk-" + "SYNTHETIC" * 5
        with tempfile.TemporaryDirectory() as root:
            with redirect_stdout(io.StringIO()):
                cli.main(["--root", root, "lesson", secret, "--body", secret])
            for path, note in Bundle(root).notes.items():
                self.assertNotIn(secret, str(note.meta) + note.body)
                self.assertNotIn(secret.lower(), path)


class TestInputValidation(unittest.TestCase):
    def test_nonfinite_numbers_fail_before_writes(self):
        for value in ("nan", "inf", "-inf", "1e999"):
            commands = (("outcome", value), ("goal", "Review", "--weight=" + value),
                        ("lesson", "Review", "--valence=" + value),
                        ("intent", "Review", "--branch", "test", "--ttl=" + value))
            for arguments in commands:
                with self.subTest(arguments=arguments), tempfile.TemporaryDirectory() as root:
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                            self.assertRaises(SystemExit) as error:
                        cli.main(["--root", root, *arguments])
                    self.assertNotEqual(error.exception.code, 0)
                    self.assertFalse(os.path.exists(os.path.join(root, ".muninn", "ledger.jsonl")))
                    self.assertFalse(Bundle(root).notes)

    def test_empty_required_note_fields_fail_before_writes(self):
        for field in ("--title", "--type"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as root:
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    cli.main(["--root", root, "add", "note.md", "--title", "Valid",
                              field, " ", "--body", "A decision."])
                self.assertFalse(Bundle(root).notes)


class TestImportCmd(unittest.TestCase):
    def test_malformed_graph_reports_failure_before_writing_notes(self):
        for content in ("null", "[]", "not json", '{"nodes": null}',
                        '{"nodes": [], "links": 42}'):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as root:
                path = os.path.join(root, "bad.json")
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(content)
                with self.assertRaises(SystemExit) as error:
                    cli.main(["--root", root, "import", path])
                self.assertIn("muninn import:", str(error.exception))
                self.assertEqual(Bundle(root).notes, {})

    def test_import_writes_notes(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        graph = {"nodes": [{"id": "c1", "label": "Cache Strategy",
                            "file_type": "concept",
                            "rationale": "we cache to cut latency"}],
                 "links": []}
        gp = os.path.join(root, "graph.json")
        with open(gp, "w", encoding="utf-8") as fh:
            json.dump(graph, fh)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--root", root, "import", gp])
        self.assertIn("imported 1 notes", buf.getvalue())
        note = os.path.join(root, "imported", "cache-strategy.md")
        self.assertTrue(os.path.exists(note))
        with open(note, encoding="utf-8") as fh:
            self.assertIn("we cache to cut latency", fh.read())


class TestBuildCmd(unittest.TestCase):
    def test_build_soft_fails_when_graphify_missing(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        buf = io.StringIO()
        with mock.patch("muninn.cli.shutil.which", return_value=None), \
                redirect_stdout(buf):
            cli.main(["--root", root, "build", root])
        out = buf.getvalue()
        self.assertIn("graphify not found", out)
        self.assertIn("pip install graphifyy", out)
        self.assertIn("muninn import <graph.json>", out)


def _import_notes(root, nodes):
    """Populate a bundle with concept notes via `muninn import`."""
    gp = os.path.join(root, "g.json")
    with open(gp, "w", encoding="utf-8") as fh:
        json.dump({"nodes": nodes, "links": []}, fh)
    with redirect_stdout(io.StringIO()):
        cli.main(["--root", root, "import", gp])


class TestPackNoIndex(unittest.TestCase):
    def test_no_index_omits_index_keeps_focus(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        _import_notes(root, [
            {"id": "c1", "label": "Alpha", "file_type": "concept", "rationale": "the alpha note on caching"},
            {"id": "c2", "label": "Beta", "file_type": "concept", "rationale": "beta note on caching latency"}])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--root", root, "pack", "caching", "--no-reactivate"])
        self.assertIn("## Compact index", buf.getvalue())
        self.assertIn("## Focus", buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--root", root, "pack", "caching", "--no-index", "--no-reactivate"])
        out = buf.getvalue()
        self.assertNotIn("## Compact index", out)
        self.assertIn("## Focus", out)


class TestWalkModeReachable(unittest.TestCase):
    def test_muninn_walk_mode_reachable_from_cli(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        _import_notes(root, [
            {"id": "c1", "label": "Cache", "file_type": "concept", "rationale": "we cache to cut api latency"}])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--root", root, "pack", "cache latency", "--mode", "muninn-walk", "--no-reactivate"])
        self.assertIn("## Focus", buf.getvalue())


class TestMaxCodeNotes(unittest.TestCase):
    def test_max_code_notes_caps_and_names_flag(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        nodes = [{"id": f"f{i}", "label": f"func_{i}", "file_type": "function",
                  "source_file": f"m{i}.py"} for i in range(4)]
        gp = os.path.join(root, "g.json")
        with open(gp, "w", encoding="utf-8") as fh:
            json.dump({"nodes": nodes, "links": []}, fh)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--root", root, "import", gp, "--include-code", "--max-code-notes", "2"])
        out = buf.getvalue()
        self.assertIn("cap of 2 stub notes reached", out)
        self.assertIn("skipped 2 further code nodes", out)
        self.assertIn("--max-code-notes", out)        # the message names the flag
        code_dir = os.path.join(root, "imported", "code")
        written = len([f for f in os.listdir(code_dir) if f.endswith(".md")]) if os.path.isdir(code_dir) else 0
        self.assertEqual(written, 2)


class TestAutoViz(unittest.TestCase):
    """Commands that grow the bundle refresh .muninn/graph.html: 'see
    what you just built' costs zero extra commands, stays bounded, and
    never fails the build."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.gp = os.path.join(self.root, "g.json")
        with open(self.gp, "w", encoding="utf-8") as fh:
            json.dump({"nodes": [
                {"id": "a", "label": "Alpha", "file_type": "concept"},
                {"id": "b", "label": "Beta", "file_type": "concept"}],
                "edges": [{"source": "a", "target": "b",
                           "relation": "refines",
                           "confidence": "EXTRACTED"}]}, fh)

    def _run(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--root", self.root, *argv])
        return buf.getvalue()

    def test_import_refreshes_the_graph(self):
        out = self._run("import", self.gp)
        self.assertIn("graph refreshed", out)
        self.assertTrue(os.path.isfile(
            os.path.join(self.root, ".muninn", "graph.html")))

    def test_auto_viz_is_bounded_with_a_hint(self):
        with mock.patch.object(cli, "VIZ_AUTO_CAP", 1):
            out = self._run("import", self.gp)
        self.assertIn("auto-viz skips", out)
        self.assertFalse(os.path.exists(
            os.path.join(self.root, ".muninn", "graph.html")))

    def test_a_viz_failure_never_fails_the_command(self):
        with mock.patch("muninn.cli.viz.write_html",
                        side_effect=OSError("disk full")):
            err = io.StringIO()
            with redirect_stderr(err):
                out = self._run("import", self.gp)
        self.assertIn("imported 2 notes", out)      # the command succeeded
        self.assertIn("viz refresh skipped", err.getvalue())


class TestBuildEnvScrub(unittest.TestCase):
    def test_cloud_keys_stripped_from_graphify_env(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        captured = {}

        def fake_run(argv, env=None, **kw):
            captured["env"] = env

            class R:
                returncode = 1  # non-zero -> cmd_build bails right after the call
            return R()

        with mock.patch("muninn.cli.shutil.which", return_value="/usr/bin/graphify"), \
                mock.patch("muninn.cli.subprocess.run", side_effect=fake_run), \
                mock.patch.dict(os.environ, {"GEMINI_API_KEY": "SECRET",
                                             "OPENAI_API_KEY": "SECRET2",
                                             "AWS_ACCESS_KEY_ID": "SECRET3",
                                             "AWS_SECRET_ACCESS_KEY": "SECRET4",
                                             "AZURE_OPENAI_API_KEY": "SECRET5",
                                             "DEEPSEEK_API_KEY": "SECRET6"}), \
                redirect_stdout(io.StringIO()):
            cli.main(["--root", root, "build", root])
        env = captured.get("env", {})
        # every remote-capable backend graphify has grown stays stripped
        for var in ("GEMINI_API_KEY", "OPENAI_API_KEY", "AWS_ACCESS_KEY_ID",
                    "AWS_SECRET_ACCESS_KEY", "AZURE_OPENAI_API_KEY",
                    "DEEPSEEK_API_KEY"):
            self.assertNotIn(var, env)                 # keys never reach the child
        self.assertIn("GRAPHIFY_OUT", env)             # but the intended var is set
        self.assertIn("PATH", env)                     # and non-cloud env is preserved


if __name__ == "__main__":
    unittest.main(verbosity=2)
