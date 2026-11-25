"""Verify observation intake, ambient decay, and the hook privacy boundary.

The agent adapters may pass only a session identifier and file path beyond
intake. Prompt text, credentials, transcript paths, assistant messages, and
tool output must leave no trace in the sidecar.
"""

import io
import json
import os
import shlex
import shutil
import sys
import tempfile
import unittest
import zipfile
import zipimport
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from muninn import cli  # noqa: E402
from muninn.dynamics import Dynamics  # noqa: E402
from muninn.observe import (  # noqa: E402
    DEBOUNCE,
    SESSION_WINDOW,
    hook_config,
    map_note,
    observe_event,
)
from muninn.store import Bundle  # noqa: E402

T0 = 1_800_000_000.0

DECOYS = {
    "prompt": "DECOY-PROMPT-TEXT",
    "api_key": "sk-DECOY-KEY-123",
    "transcript_path": "/tmp/DECOY-TRANSCRIPT.jsonl",
    "cwd": "/DECOY/CWD",
    "hook_event_name": "PostToolUse",
    "tool_response": {"stdout": "DECOY-TOOL-OUTPUT"},
}


def ledger(root):
    path = os.path.join(root, ".muninn", "ledger.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(x) for x in fh if x.strip()]


class SenseCase(unittest.TestCase):
    """A tiny bundle (one plain note + one imported note with a
    resource: pointer) under a fake clock shared by intake decisions
    and event timestamps."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        b = Bundle(self.root)
        b.write_note("notes/db-port.md", {"title": "DB port"},
                     "postgres listens on 7433")
        b.write_note("imported/api-server.md",
                     {"title": "API server",
                      "resource": "src/api/server.py"},
                     "the api server entrypoint")
        self.b = Bundle(self.root)
        self.d = Dynamics(self.root)
        self.t = T0
        p = mock.patch("time.time", new=lambda: self.t)
        p.start()
        self.addCleanup(p.stop)


class TestMapping(SenseCase):
    def test_path_to_note_mapping(self):
        # (a) exact bundle-relative membership, ./-relative, and absolute
        self.assertEqual(map_note(self.b, "notes/db-port.md"),
                         "notes/db-port.md")
        self.assertEqual(map_note(self.b, "./notes/db-port.md"),
                         "notes/db-port.md")
        self.assertEqual(
            map_note(self.b, os.path.join(self.root, "notes", "db-port.md")),
            "notes/db-port.md")
        # (b) the resource index: exact key, and an absolute path ending
        # with the key (hooks always send absolute paths)
        self.assertEqual(map_note(self.b, "src/api/server.py"),
                         "imported/api-server.md")
        self.assertEqual(map_note(self.b, "/home/" + "me/repo/src/api/server.py"),
                         "imported/api-server.md")
        # unmappable
        self.assertIsNone(map_note(self.b, "/etc/passwd"))
        self.assertIsNone(map_note(self.b, ""))
        self.assertIsNone(map_note(self.b, None))


class TestIntake(SenseCase):
    def test_session_window_rollover(self):
        observe_event(self.b, self.d, "touch", "notes/db-port.md")
        self.t = T0 + 120  # two minutes later: same window
        observe_event(self.b, self.d, "encode", "imported/api-server.md")
        self.t = T0 + 120 + SESSION_WINDOW + 1  # window expired: new id
        observe_event(self.b, self.d, "encode", "notes/db-port.md")
        evs = [e for e in ledger(self.root)
               if e["kind"] in ("touch", "encode")]
        self.assertEqual(len(evs), 3)
        self.assertEqual(evs[0]["session"], evs[1]["session"])
        self.assertNotEqual(evs[1]["session"], evs[2]["session"])
        # sharing a window wired the two notes used together
        self.assertIn("imported/api-server.md|notes/db-port.md",
                      self.d.coact)

    def test_debounce_collapses_repeats(self):
        self.assertIsNotNone(
            observe_event(self.b, self.d, "touch", "notes/db-port.md"))
        self.t = T0 + 30  # same (note, kind) inside the window: collapsed
        self.assertIsNone(
            observe_event(self.b, self.d, "touch", "notes/db-port.md"))
        # a different kind on the same note is NOT collapsed
        self.assertIsNotNone(
            observe_event(self.b, self.d, "encode", "notes/db-port.md"))
        self.t = T0 + DEBOUNCE + 1  # window passed: lands again
        self.assertIsNotNone(
            observe_event(self.b, self.d, "touch", "notes/db-port.md"))
        touches = [e for e in ledger(self.root)
                   if e["kind"] == "touch" and e["note"] == "notes/db-port.md"]
        self.assertEqual(len(touches), 2)

    def test_consolidate_if_due_fires_once_and_caps_at_3(self):
        self.d.touch("notes/db-port.md")  # first event anchors the clock
        self.assertEqual(self.d.consolidate_if_due(), 0)  # same day
        self.t = T0 + 86400 + 5
        self.assertEqual(self.d.consolidate_if_due(), 1)  # one day, one tick
        self.assertEqual(self.d.consolidate_if_due(), 0)  # fires ONCE
        self.t = T0 + 40 * 86400  # a vacation: capped, no decay avalanche
        self.assertEqual(self.d.consolidate_if_due(), 3)
        self.assertEqual(
            sum(1 for e in ledger(self.root) if e["kind"] == "consolidate"),
            4)
        # the anchor is derived state: a replay rebuilds it from the ledger
        os.remove(os.path.join(self.root, ".muninn", "state.json"))
        self.assertEqual(Dynamics(self.root).consolidate_if_due(), 0)

    def test_observe_intake_runs_the_due_check(self):
        self.d.touch("notes/db-port.md")
        self.t = T0 + 2 * 86400 + 5
        observe_event(self.b, self.d, "touch", "imported/api-server.md")
        kinds = [e["kind"] for e in ledger(self.root)]
        self.assertEqual(kinds.count("consolidate"), 2)
        self.assertEqual(kinds[-1], "touch")  # decay precedes the event

    def test_observe_json_stdin(self):
        payload = {"kind": "touch", "note": "notes/db-port.md",
                   "session": "j1"}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            cli.main(["--root", self.root, "observe", "--json"])
        evs = [e for e in ledger(self.root) if e["kind"] == "touch"]
        self.assertEqual(evs[0]["note"], "notes/db-port.md")
        self.assertEqual(evs[0]["session"], "j1")

    def test_unmappable_path_is_silently_dropped(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            cli.main(["--root", self.root, "observe", "--kind", "touch",
                      "--note", "/definitely/not/ours.py"])
        self.assertEqual(out.getvalue(), "")  # exit 0, not a word
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(ledger(self.root), [])
        # the hook flavor of the same: an unmappable tool path never errors
        payload = {"session_id": "s1", "tool_name": "Read",
                   "tool_input": {"file_path": "/definitely/not/ours.py"}}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), \
                redirect_stdout(out), redirect_stderr(err):
            cli.main(["--root", self.root, "hook", "post-tool"])
        self.assertEqual(ledger(self.root), [])


class TestHook(SenseCase):
    def setUp(self):
        super().setUp()
        # HERMETIC CWD: hooks harvest the situation from os.getcwd() :
        # branch, changed files, and the LAST COMMIT SUBJECT all enter
        # the session-begin cue and land in the ledger. Run from the
        # muninn repo itself, a commit message containing a scan word
        # (e.g. "transcript import") makes the privacy byte-scan flake.
        # A neutral non-repo cwd keeps the cue environment-free, so the
        # scan tests exactly what it should: hook-JSON leakage only.
        self._oldcwd = os.getcwd()
        neutral = tempfile.mkdtemp(prefix="hookcwd-")
        os.chdir(neutral)
        self.addCleanup(os.chdir, self._oldcwd)
        self.addCleanup(shutil.rmtree, neutral, ignore_errors=True)

    def test_privacy_only_session_id_and_file_path_land(self):
        payload = dict(DECOYS)
        payload["session_id"] = "sess-priv"
        payload["tool_name"] = "Edit"
        payload["tool_input"] = {
            "file_path": os.path.join(self.root, "notes", "db-port.md"),
            "old_string": "DECOY-OLD", "new_string": "DECOY-NEW",
            "content": "DECOY-CONTENT", "command": "DECOY-CMD"}
        # every stdin-consuming hook event gets the decoy-stuffed payload
        stuffed = json.dumps(payload)
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(stuffed)):
            cli.main(["--root", self.root, "hook", "post-tool"])
        with mock.patch("sys.stdin", io.StringIO(stuffed)), \
                redirect_stdout(out):
            cli.main(["--root", self.root, "hook", "session-start"])
        with mock.patch("sys.stdin", io.StringIO(stuffed)):
            cli.main(["--root", self.root, "hook", "session-end"])
        evs = ledger(self.root)
        self.assertTrue(any(
            e["kind"] == "encode" and e["note"] == "notes/db-port.md"
            and e.get("session") == "sess-priv" for e in evs))
        # NOTHING else from the hook JSON may reach ANY disk artifact :
        # scan every byte under the bundle root for every decoy
        blob = ""
        for dirpath, _dns, fns in os.walk(self.root):
            for fn in fns:
                with open(os.path.join(dirpath, fn),
                          encoding="utf-8", errors="replace") as fh:
                    blob += fh.read()
        for decoy in ("DECOY", "sk-DECOY-KEY-123", "api_key", "prompt",
                      "transcript", "PostToolUse", "old_string", "content",
                      "command", "tool_response", "Edit", "SessionStart"):
            self.assertNotIn(decoy, blob)

    def test_grok_hook_fields_map_without_retaining_payload_content(self):
        path = os.path.join(self.root, "notes", "db-port.md")
        common = {
            "sessionId": "grok-session",
            "prompt": "GROK-DECOY-PROMPT",
            "lastAssistantMessage": "GROK-DECOY-RESPONSE",
            "toolOutput": "GROK-DECOY-OUTPUT",
        }
        read = dict(common, toolName="read_file",
                    toolInput={"target_file": path,
                               "content": "GROK-DECOY-CONTENT"})
        with mock.patch("sys.stdin", io.StringIO(json.dumps(read))):
            cli.main(["--root", self.root, "hook", "post-tool"])
        self.t += DEBOUNCE + 1
        edit = dict(common, toolName="search_replace",
                    toolInput={"file_path": path,
                               "old_string": "GROK-DECOY-OLD",
                               "new_string": "GROK-DECOY-NEW"})
        with mock.patch("sys.stdin", io.StringIO(json.dumps(edit))):
            cli.main(["--root", self.root, "hook", "post-tool"])
        events = [e for e in ledger(self.root)
                  if e["kind"] in ("touch", "encode")]
        self.assertEqual(
            [(e["kind"], e.get("session")) for e in events],
            [("touch", "grok-session"), ("encode", "grok-session")],
        )
        blob = ""
        for dirpath, _dns, filenames in os.walk(self.root):
            for filename in filenames:
                with open(os.path.join(dirpath, filename),
                          encoding="utf-8", errors="replace") as fh:
                    blob += fh.read()
        for decoy in ("GROK-DECOY", "lastAssistantMessage", "toolOutput",
                      "search_replace", "read_file"):
            self.assertNotIn(decoy, blob)

    def test_record_only_session_start_does_not_claim_context_delivery(self):
        self.d.touch("notes/db-port.md", session="warm")
        payload = {"sessionId": "grok-start", "hookEventName": "session_start"}
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), \
                redirect_stdout(out):
            cli.main(["--root", self.root, "hook", "session-start"])
        self.assertEqual(out.getvalue(), "")
        events = ledger(self.root)
        self.assertIn(("session-begin", "grok-start"),
                      [(e["kind"], e.get("session")) for e in events])
        self.assertFalse(any(e["kind"] == "recall" and
                             e.get("session") == "grok-start"
                             for e in events))

    def test_session_start_end_to_end(self):
        # warm usage so the situation ("recent" cues) can recall something
        self.d.touch("notes/db-port.md", session="warm")
        payload = dict(DECOYS, hook_event_name="SessionStart",
                       session_id="sess-start-1")
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), \
                redirect_stdout(out):
            cli.main(["--root", self.root, "hook", "session-start"])
        text = out.getvalue()
        self.assertIn("Context pack", text)  # the pack reached stdout
        self.assertIn("Compact index", text)
        evs = ledger(self.root)
        self.assertIn(("session-begin", "sess-start-1"),
                      [(e["kind"], e.get("session")) for e in evs])
        recalls = [e for e in evs if e["kind"] == "recall"]
        self.assertTrue(recalls)  # …and the recalls carry the hook session
        self.assertTrue(all(e.get("session") == "sess-start-1"
                            for e in recalls))

    def test_plain_recall_never_wires_even_under_env_session(self):
        # SPEC: only an EXPLICIT session ties recalls to co-use. A plain
        # query with MUNINN_SESSION exported must stay read-only.
        self.d.touch("notes/db-port.md", session="w1")
        self.d.touch("imported/api-server.md", session="w2")
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"MUNINN_SESSION": "env-s"}), \
                redirect_stdout(out):
            cli.main(["--root", self.root, "pack", "postgres api server"])
        recalls = [e for e in ledger(self.root) if e["kind"] == "recall"]
        self.assertTrue(recalls)
        self.assertTrue(all("session" not in e for e in recalls))
        self.assertEqual(Dynamics(self.root).coact, {})

    def test_prime_session_wires_what_was_primed_together(self):
        # two touches in DIFFERENT sessions: no co-use yet
        self.d.touch("notes/db-port.md", session="w1")
        self.d.touch("imported/api-server.md", session="w2")
        self.assertEqual(self.d.coact, {})
        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(["--root", self.root, "prime", "--session", "agent-1",
                      "--cwd", self.root])
        evs = ledger(self.root)
        recalls = [e for e in evs if e["kind"] == "recall"]
        self.assertTrue(recalls)
        self.assertTrue(all(e["session"] == "agent-1" for e in recalls))
        # both notes were primed together -> the pair is wired for the walk
        self.assertIn("imported/api-server.md|notes/db-port.md",
                      Dynamics(self.root).coact)

    def test_broader_tool_and_path_envelopes_map_without_leaking_payload_fields(self):
        path = os.path.join(self.root, "notes", "db-port.md")
        envelopes = [
            ("touch", {
                **DECOYS,
                "session_id": "env-s1",
                "tool_call": {"name": "view_file", "arguments": {"path": path}},
            }),
            ("encode", {
                **DECOYS,
                "session_id": "env-s1",
                "toolCall": {"name": "edit_file", "args": {"Path": path}},
            }),
            ("encode", {
                **DECOYS,
                "session_id": "env-s2",
                "tool_name": "MultiEdit",
                "tool_input": {"path": path},
            }),
            ("encode", {
                **DECOYS,
                "session_id": "env-s3",
                "tool_name": "replace",
                "tool_input": {"file_path": path},
            }),
            ("encode", {
                **DECOYS,
                "session_id": "env-s4",
                "tool_name": "write_file",
                "tool_input": {"Path": path},
            }),
        ]
        for expected_kind, payload in envelopes:
            self.t += DEBOUNCE + 1
            with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
                cli.main(["--root", self.root, "hook", "post-tool"])
        kinds = [(e["kind"], e.get("session")) for e in ledger(self.root)
                 if e["kind"] in ("touch", "encode")]
        self.assertEqual(kinds, [
            ("touch", "env-s1"),
            ("encode", "env-s1"),
            ("encode", "env-s2"),
            ("encode", "env-s3"),
            ("encode", "env-s4"),
        ])
        blob = ""
        for dirpath, _dns, fns in os.walk(self.root):
            for fn in fns:
                with open(os.path.join(dirpath, fn),
                          encoding="utf-8", errors="replace") as fh:
                    blob += fh.read()
        for decoy in ("DECOY", "view_file", "edit_file", "MultiEdit",
                      "write_file", "toolCall", "tool_call"):
            self.assertNotIn(decoy, blob)

    def test_gemini_adapter_config_and_structured_json_hook_output(self):
        cfg = json.loads(hook_config(self.root, adapter="gemini"))
        self.assertEqual(set(cfg["hooks"]),
                         {"SessionStart", "AfterTool", "SessionEnd"})
        self.assertIn("--adapter gemini",
                      json.dumps(cfg["hooks"]["SessionStart"]))
        self.d.touch("notes/db-port.md", session="warm")
        start_out = io.StringIO()
        payload = json.dumps({"session_id": "gem-s1", "hook_event_name": "SessionStart"})
        with mock.patch("sys.stdin", io.StringIO(payload)), \
                redirect_stdout(start_out):
            cli.main(["--root", self.root, "hook", "session-start",
                      "--adapter", "gemini"])
        parsed = json.loads(start_out.getvalue())
        self.assertEqual(
            parsed["hookSpecificOutput"]["hookEventName"], "SessionStart"
        )
        self.assertIn(
            "Context pack", parsed["hookSpecificOutput"]["additionalContext"]
        )
        for event in ("post-tool", "session-end"):
            buf = io.StringIO()
            with mock.patch("sys.stdin", io.StringIO(payload)), \
                    redirect_stdout(buf):
                cli.main(["--root", self.root, "hook", event,
                          "--adapter", "gemini"])
            self.assertEqual(json.loads(buf.getvalue()), {})

    def test_user_prompt_and_post_compact_hooks_stay_read_only_and_deduplicate(self):
        self.d.touch("notes/db-port.md", session="warm")

        def snapshot_root():
            snap = {}
            for dirpath, _dns, fns in os.walk(self.root):
                for fn in fns:
                    full = os.path.join(dirpath, fn)
                    with open(full, "rb") as fh:
                        snap[os.path.relpath(full, self.root)] = fh.read()
            return snap

        before = snapshot_root()
        dedupe_dir = tempfile.mkdtemp(prefix="muninn-dedupe-")
        self.addCleanup(shutil.rmtree, dedupe_dir, ignore_errors=True)
        with mock.patch.dict(os.environ, {"MUNINN_DEDUPE_DIR": dedupe_dir}):
            p1 = dict(DECOYS, session_id="up-1",
                      prompt="check postgres port 7433")
            out1 = io.StringIO()
            with mock.patch("sys.stdin", io.StringIO(json.dumps(p1))), \
                    redirect_stdout(out1):
                cli.main(["--root", self.root, "hook", "user-prompt"])
            self.assertIn("DB port", out1.getvalue())
            self.assertIn("7433", out1.getvalue())
            self.assertNotIn("Compact index", out1.getvalue())

            # A follow-up prompt in the same session recalling identical excerpts is suppressed.
            p2 = dict(DECOYS, session_id="up-1",
                      prompt="verify postgres port 7433 setting")
            out2 = io.StringIO()
            with mock.patch("sys.stdin", io.StringIO(json.dumps(p2))), \
                    redirect_stdout(out2):
                cli.main(["--root", self.root, "hook", "user-prompt"])
            self.assertEqual(out2.getvalue(), "")

            # A different session receives the pack cleanly.
            p3 = dict(DECOYS, session_id="up-2",
                      prompt="verify postgres port 7433 setting")
            out3 = io.StringIO()
            with mock.patch("sys.stdin", io.StringIO(json.dumps(p3))), \
                    redirect_stdout(out3):
                cli.main(["--root", self.root, "hook", "user-prompt"])
            self.assertIn("7433", out3.getvalue())

            # Unmatched prompts stay silent instead of emitting boilerplate.
            p4 = dict(DECOYS, session_id="up-2",
                      prompt="quantum crystallography")
            out4 = io.StringIO()
            with mock.patch("sys.stdin", io.StringIO(json.dumps(p4))), \
                    redirect_stdout(out4):
                cli.main(["--root", self.root, "hook", "user-prompt"])
            self.assertEqual(out4.getvalue(), "")

            # Gemini adapter wraps BeforeAgent context as JSON and emits {} when unmatched.
            gem_hit = io.StringIO()
            with mock.patch("sys.stdin", io.StringIO(json.dumps(
                dict(DECOYS, session_id="up-gem", prompt="postgres port 7433")
            ))), redirect_stdout(gem_hit):
                cli.main(["--root", self.root, "hook", "user-prompt",
                          "--adapter", "gemini"])
            gem_data = json.loads(gem_hit.getvalue())
            self.assertEqual(
                gem_data["hookSpecificOutput"]["hookEventName"], "BeforeAgent"
            )
            self.assertIn(
                "7433", gem_data["hookSpecificOutput"]["additionalContext"]
            )
            gem_dup = io.StringIO()
            with mock.patch("sys.stdin", io.StringIO(json.dumps(
                dict(DECOYS, session_id="up-gem", prompt="postgres port 7433")
            ))), redirect_stdout(gem_dup):
                cli.main(["--root", self.root, "hook", "user-prompt",
                          "--adapter", "gemini"])
            self.assertEqual(json.loads(gem_dup.getvalue()), {})

            # Post-compact re-emits the situation pack without recording recalls or session-begin.
            compact_out = io.StringIO()
            with mock.patch("sys.stdin", io.StringIO(json.dumps(
                dict(DECOYS, session_id="up-1")
            ))), redirect_stdout(compact_out):
                cli.main(["--root", self.root, "hook", "post-compact"])
            self.assertIn("Context pack", compact_out.getvalue())

            # Neither the bundle nor the dedupe cache may retain prompt text or decoys.
            self.assertEqual(snapshot_root(), before)
            cache_blob = ""
            for dirpath, _dns, fns in os.walk(dedupe_dir):
                for fn in fns:
                    with open(os.path.join(dirpath, fn),
                              encoding="utf-8", errors="replace") as fh:
                        cache_blob += fh.read()
            for secret in ("DECOY", "postgres", "7433", "up-1", "up-2", "up-gem"):
                self.assertNotIn(secret, cache_blob)


class TestStandaloneHookConfig(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.archive = os.path.join(self.directory.name, "Muninn user's executable.pyz")
        with zipfile.ZipFile(self.archive, "w") as archive:
            archive.writestr("__main__.py", "")
            archive.writestr("muninn/observe.py", "")
        self.loader = zipimport.zipimporter(self.archive)

    def commands(self, adapter="claude"):
        config = json.loads(hook_config("/tmp/knowledge user's notes", adapter=adapter))
        return [shlex.split(handler["command"])
                for groups in config["hooks"].values()
                for group in groups for handler in group["hooks"]]

    def test_running_archive_overrides_missing_or_shadowed_path_for_every_adapter(self):
        interpreter = "/tmp/Python user's runtime/bin/python3"
        with mock.patch("muninn.observe.__loader__", self.loader), \
                mock.patch.object(sys.modules["__main__"], "__loader__", self.loader), \
                mock.patch("sys.argv", [self.archive, "setup"]), \
                mock.patch("sys.executable", interpreter):
            for found in (None, "/different/muninn"):
                with mock.patch("shutil.which", return_value=found):
                    for adapter in ("claude", "codex", "gemini", "grok"):
                        with self.subTest(found=found, adapter=adapter):
                            commands = self.commands(adapter)
                            self.assertEqual(len(commands), 3)
                            for command in commands:
                                self.assertEqual(command[:6], [
                                    interpreter, "-I", self.archive, "--root",
                                    "/tmp/knowledge user's notes", "hook"])

    def test_archive_argument_alone_does_not_override_normal_entrypoints(self):
        with mock.patch("sys.argv", [self.archive, "setup"]), \
                mock.patch("shutil.which", return_value="/normal/muninn"):
            self.assertTrue(all(command[0] == "/normal/muninn" for command in self.commands()))

    def test_imported_archive_does_not_replace_a_different_main_program(self):
        with mock.patch("muninn.observe.__loader__", self.loader), \
                mock.patch("sys.argv", [self.archive, "setup"]), \
                mock.patch("shutil.which", return_value=None):
            self.assertTrue(all(command[0] == "muninn" for command in self.commands()))

    def test_nonarchive_main_argument_retains_normal_lookup(self):
        with mock.patch("muninn.observe.__loader__", self.loader), \
                mock.patch.object(sys.modules["__main__"], "__loader__", self.loader), \
                mock.patch("sys.argv", ["other-program", "setup"]), \
                mock.patch("shutil.which", return_value="/normal/muninn"):
            self.assertTrue(all(command[0] == "/normal/muninn" for command in self.commands()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
