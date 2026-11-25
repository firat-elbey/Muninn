"""Verify opt-in onboarding memory discovery, synthesis handshake, and watermarks."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from muninn import bootstrap, cli, home
from muninn.dynamics import Dynamics
from muninn.journal import add_episode
from muninn.store import Bundle


def _run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with redirect_stdout(out), redirect_stderr(err):
        try:
            cli.main(list(argv))
        except SystemExit as exc:
            code = int(exc.code) if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


class TestBootstrap(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.fake_home = os.path.join(self.tmp, "userhome")
        os.makedirs(self.fake_home, exist_ok=True)
        self.brain = os.path.join(self.tmp, "brain")
        home.init_home(self.brain)
        self.env = mock.patch.dict(os.environ, {"HOME": self.fake_home})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _write_claude_archive(self, project: str, sessions: dict[str, list[dict]]) -> str:
        folder = os.path.join(self.fake_home, ".claude", "projects", project)
        os.makedirs(folder, exist_ok=True)
        for name, events in sessions.items():
            path = os.path.join(folder, f"{name}.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                fh.writelines(json.dumps(event) + "\n" for event in events)
        return folder

    def test_discover_is_metadata_only_and_rejects_symlinks(self) -> None:
        synth_sk = "sk-" + ("SYNTHETIC" * 3)
        secret_text = f"PRIVATE-BODY-MARKER {synth_sk}"
        archive = self._write_claude_archive("billing-service", {
            "s1": [
                {"type": "user", "timestamp": "2026-11-20T10:00:00Z",
                 "message": {"content": secret_text}},
            ],
            "s2": [
                {"type": "user", "timestamp": "2026-11-21T10:00:00Z",
                 "message": {"content": "Check invoice retry backoff"}},
            ],
        })
        # Symlinked files and directories inside or beside archives must be ignored.
        outside_dir = os.path.join(self.tmp, "outside-secret")
        os.makedirs(outside_dir, exist_ok=True)
        outside_file = os.path.join(outside_dir, "escape.jsonl")
        with open(outside_file, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"role": "user", "content": "ESCAPED-CONTENT"}) + "\n")
        os.symlink(outside_file, os.path.join(archive, "linked.jsonl"))
        os.symlink(outside_dir, os.path.join(self.fake_home, ".claude", "projects", "linked-proj"))

        # Codex year/month/day rollout layout and Gemini chats layout.
        codex_day = os.path.join(self.fake_home, ".codex", "sessions", "2026", "11", "24")
        os.makedirs(codex_day, exist_ok=True)
        with open(os.path.join(codex_day, "rollout-1.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "type": "response_item",
                "payload": {"role": "user", "content": [{"type": "input_text", "text": secret_text}]},
            }) + "\n")
        gemini_chats = os.path.join(self.fake_home, ".gemini", "tmp", "search-ui", "chats")
        os.makedirs(gemini_chats, exist_ok=True)
        with open(os.path.join(gemini_chats, "c1.json"), "w", encoding="utf-8") as fh:
            json.dump({"messages": [{"type": "user", "content": secret_text}]}, fh)

        real_open = open

        def guarded_open(file, *args, **kwargs):
            if str(file).endswith((".jsonl", ".json")):
                raise AssertionError(f"discover must not open transcript body: {file}")
            return real_open(file, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=guarded_open):
            found = bootstrap.discover_archives()
        self.assertEqual(len(found), 3)
        # Only the 2 real files in billing-service count; symlinked file is ignored.
        by_slug = {item["project_slug"]: item["sessions"] for item in found}
        self.assertEqual(by_slug["billing-service"], 2)
        self.assertNotIn("linked-proj", by_slug)

        code, out, _err = _run_cli("--root", self.brain, "bootstrap")
        self.assertEqual(code, 0)
        self.assertIn("billing-service", out)
        self.assertNotIn("PRIVATE-BODY-MARKER", out)
        self.assertNotIn(synth_sk, out)
        self.assertFalse(os.path.exists(os.path.join(self.brain, ".muninn", "bootstrap.json")))

        # Passing a symlinked --source directly returns nothing.
        self.assertEqual(bootstrap.discover_archives(sources=[os.path.join(archive, "linked.jsonl")]), [])

    def test_request_requires_source_and_scrubs_structured_credentials(self) -> None:
        code, out, err = _run_cli("--root", self.brain, "bootstrap", "--request")
        self.assertNotEqual(code, 0)
        self.assertIn("--source", err + out)

        synth_ghr = "ghr_" + ("A1b2C3d4" * 3)
        archive = self._write_claude_archive("payments-api", {
            "sess-a": [
                {"type": "summary", "summary": "Migrate webhook retry queue"},
                {"type": "user", "timestamp": "2026-11-22T09:00:00Z",
                 "message": {"content": [
                     {"type": "text",
                      "text": (
                          f'Check {{"api_key": "JSON-SECRET-VAL"}} and Authorization: Bearer '
                          f"BEARERSECRET1234567890 and https://admin:URLPASS99@db.internal/app "
                          f"and {synth_ghr}"
                      )},
                     {"type": "tool_result", "content": "TOOL-OUTPUT-MUST-NOT-APPEAR"},
                 ]}},
                {"type": "assistant",
                 "message": {"content": "ASSISTANT-CODE-DUMP-MUST-NOT-APPEAR"}},
            ],
        })
        code, req_out, err = _run_cli(
            "--root", self.brain, "bootstrap", "--source", archive, "--request"
        )
        self.assertEqual(code, 0, err)
        self.assertIn("S001", req_out)
        self.assertIn("Migrate webhook retry queue", req_out)
        for secret in ("JSON-SECRET-VAL", "BEARERSECRET1234567890", "URLPASS99", synth_ghr,
                       "TOOL-OUTPUT-MUST-NOT-APPEAR", "ASSISTANT-CODE-DUMP-MUST-NOT-APPEAR"):
            self.assertNotIn(secret, req_out)

    def test_apply_protects_user_notes_avoids_room_collision_and_validates_guards(self) -> None:
        archive = self._write_claude_archive("payments-api", {
            "sess-1": [
                {"type": "summary", "summary": "Webhook retry idempotency"},
                {"type": "user", "timestamp": "2026-11-23T12:00:00Z",
                 "message": {"content": "Ensure idempotency keys expire after 48 hours."}},
            ],
        })
        b = Bundle(self.brain)
        d = Dynamics(self.brain)
        # User-authored note WITHOUT explicit provenance key, plus case-colliding curated lesson,
        # plus journaled thread head (no provenance key). None of these may be overwritten.
        b.write_note(
            "notes/payments-api/Idempotency.md",
            {"type": "note", "title": "User policy"},
            "User-written note without provenance key.",
        )
        b.write_note(
            "lessons/Keep-Curated-Guard.md",
            {"type": "lesson", "title": "Keep curated guard", "provenance": "curated", "pinned": True},
            "Curated lesson body.",
        )
        add_episode(b, d, "payments-api", "Manual episode", state="CURRENT: manual journal head.")

        code, req_out, _err = _run_cli(
            "--root", self.brain, "bootstrap", "--source", archive, "--request"
        )
        self.assertEqual(code, 0)
        digest = re.search(r'"digest":\s*"([0-9a-f]{64})"', req_out).group(1)

        # Reject bad digest, unknown handle, extra fields, guardless lessons, and invalid types.
        bad_payloads = [
            {"digest": "0" * 64, "notes": [], "threads": [], "lessons": []},
            {"digest": digest, "notes": [{
                "slug": "arch", "project": "payments-api",
                "title": "Arch", "summary": "Body", "sources": ["S999"],
            }], "threads": [], "lessons": []},
            {"digest": digest, "extra_field": True, "notes": [], "threads": [], "lessons": []},
            {"digest": digest, "notes": [], "threads": [], "lessons": [{
                "title": "Guardless lesson", "body": "Body", "guards": [], "sources": ["S001"],
            }]},
            {"digest": digest, "notes": "not-a-list", "threads": [], "lessons": []},
        ]
        for bad_payload in bad_payloads:
            bad_path = os.path.join(self.tmp, "bad.json")
            with open(bad_path, "w", encoding="utf-8") as fh:
                json.dump(bad_payload, fh)
            bad_code, bad_out, bad_err = _run_cli(
                "--root", self.brain, "bootstrap", "--source", archive, "--apply", bad_path
            )
            self.assertNotEqual(bad_code, 0)
            self.assertTrue(bad_out or bad_err)

        valid_payload = {
            "digest": digest,
            "notes": [
                {
                    "slug": "idempotency",
                    "project": "payments-api",
                    "title": "Attempted case-colliding overwrite",
                    "summary": "Must be skipped because user note exists.",
                    "sources": ["S001"],
                },
                {
                    "slug": "../index",
                    "project": "../payments-api",
                    "title": "Index",
                    "summary": "Reserved slug and traversal characters are sanitized safely.",
                    "sources": ["S001"],
                },
            ],
            "threads": [
                {
                    "slug": "payments-api",
                    "title": "Payments API",
                    "state": "Attempted overwrite of manual journal head.",
                    "episode": "Configured 48-hour idempotency expiry.",
                    "sources": ["S001"],
                },
            ],
            "lessons": [
                {
                    "title": "keep curated guard",
                    "body": "Attempted case-colliding overwrite of curated lesson.",
                    "guards": ["src/payments"],
                    "sources": ["S001"],
                },
                {
                    "title": "Verify webhook idempotency TTL in migrations",
                    "body": "Check TTL index bounds whenever migration files change.",
                    "guards": ["migrations/"],
                    "sources": ["S001"],
                },
            ],
        }
        resp_path = os.path.join(self.tmp, "response.json")
        with open(resp_path, "w", encoding="utf-8") as fh:
            json.dump(valid_payload, fh)

        apply_code, apply_out, apply_err = _run_cli(
            "--root", self.brain, "bootstrap", "--source", archive, "--apply", resp_path
        )
        self.assertEqual(apply_code, 0, apply_err)
        self.assertIn("1 note(s)", apply_out)
        self.assertIn("1 thread(s)", apply_out)
        self.assertIn("1 lesson(s)", apply_out)
        self.assertIn("3 curated preserved", apply_out)

        reloaded = Bundle(self.brain)
        # User-authored note, curated lesson, and manual journal head all remained intact.
        self.assertIn("User-written note", reloaded.notes["notes/payments-api/Idempotency.md"].body)
        self.assertEqual(
            reloaded.notes["lessons/Keep-Curated-Guard.md"].body.strip(),
            "Curated lesson body.",
        )
        self.assertEqual(
            reloaded.notes["threads/payments-api/thread.md"].body.strip(),
            "CURRENT: manual journal head.",
        )
        # Unregistered project landed under notes/<proj>/ so future home.room_for gets clean projects/<proj>.
        self.assertIn("notes/payments-api/index-note.md", reloaded.notes)
        real_repo = os.path.join(self.tmp, "payments-api")
        os.makedirs(real_repo, exist_ok=True)
        self.assertEqual(home.room_for(self.brain, real_repo, create=True), "projects/payments-api")

        # Watermark file has mode 0600 and contains no raw paths or prompt text.
        wm_path = os.path.join(self.brain, ".muninn", "bootstrap.json")
        self.assertEqual(stat.S_IMODE(os.stat(wm_path).st_mode), 0o600)
        with open(wm_path, encoding="utf-8") as fh:
            wm_blob = fh.read()
        self.assertNotIn("payments-api", wm_blob)
        self.assertNotIn("48 hours", wm_blob)

        code2, req_out2, _ = _run_cli(
            "--root", self.brain, "bootstrap", "--source", archive, "--request"
        )
        self.assertEqual(code2, 0)
        self.assertIn("no new or changed transcripts", req_out2.lower())

    def test_deterministic_mode_codex_rollout_and_plain_bundle_fenced_apply(self) -> None:
        gemini_dir = os.path.join(self.fake_home, ".gemini", "tmp", "search-app", "chats")
        os.makedirs(gemini_dir, exist_ok=True)
        chat_file = os.path.join(gemini_dir, "session-1.json")
        with open(chat_file, "w", encoding="utf-8") as fh:
            json.dump({
                "messages": [
                    {"type": "user", "timestamp": "2026-11-24T08:00:00Z",
                     "content": "Add BM25 ranking test coverage with api_key=SECRET123"},
                    {"type": "gemini", "content": "Assistant reply ignored"},
                ]
            }, fh)

        code, out, err = _run_cli(
            "--root", self.brain, "bootstrap", "--source", gemini_dir, "--deterministic"
        )
        self.assertEqual(code, 0, err)
        self.assertIn("imported 1", out)
        reloaded = Bundle(self.brain)
        episodes = [p for p in reloaded.notes if p.startswith("threads/")]
        self.assertEqual(len(episodes), 1)
        self.assertNotIn("SECRET123", reloaded.notes[episodes[0]].body)

        # Second run skips unchanged watermarked file; --force reprocesses it.
        code2, out2, _ = _run_cli(
            "--root", self.brain, "bootstrap", "--source", gemini_dir, "--deterministic"
        )
        self.assertEqual(code2, 0)
        self.assertIn("imported 0", out2)
        code3, out3, _ = _run_cli(
            "--root", self.brain, "bootstrap", "--source", gemini_dir,
            "--days", "30", "--max-sessions", "10", "--force", "--deterministic",
        )
        self.assertEqual(code3, 0)
        self.assertIn("imported 1", out3)

        # Plain non-home bundle + Codex response_item envelope + fenced stdin apply.
        plain = os.path.join(self.tmp, "plain-bundle")
        os.makedirs(plain, exist_ok=True)
        Bundle(plain).generate_index()
        synth_pat = "github_pat_" + ("abcd1234" * 3)
        codex_file = os.path.join(self.tmp, "rollout-1.jsonl")
        with open(codex_file, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "type": "response_item",
                "timestamp": "2026-11-24T12:00:00Z",
                "payload": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": f"Rotate {synth_pat} every 90 days."}],
                },
            }) + "\n")

        conflict_code, _o, _e = _run_cli(
            "--root", plain, "bootstrap", "--request", "--deterministic"
        )
        self.assertNotEqual(conflict_code, 0)

        req_code, req_out, _ = _run_cli(
            "--root", plain, "bootstrap", "--source", codex_file,
            "--days", "365", "--max-sessions", "5", "--force", "--request",
        )
        self.assertEqual(req_code, 0)
        self.assertNotIn(synth_pat, req_out)
        digest = re.search(r'"digest":\s*"([0-9a-f]{64})"', req_out).group(1)

        fenced_reply = "```json\n" + json.dumps({
            "digest": digest,
            "notes": [{
                "slug": "token-rotation",
                "project": "ops",
                "title": "Token rotation",
                "summary": "Rotate credentials every 90 days.",
                "sources": ["S001"],
            }],
            "threads": [],
            "lessons": [],
        }) + "\n```\n"
        with mock.patch("sys.stdin", io.StringIO(fenced_reply)):
            app_code, app_out, app_err = _run_cli(
                "--root", plain, "bootstrap", "--source", codex_file, "--apply", "-"
            )
        self.assertEqual(app_code, 0, app_err)
        self.assertIn("1 note(s)", app_out)
        self.assertIn("notes/token-rotation.md", Bundle(plain).notes)

    def test_validation_edges_corrupt_watermarks_and_registered_room_routing(self) -> None:
        # Pre-register a real room so bootstrap writes directly into projects/registered-app/.
        proj_dir = os.path.join(self.tmp, "registered-app")
        os.makedirs(proj_dir, exist_ok=True)
        self.assertEqual(
            home.room_for(self.brain, proj_dir, create=True), "projects/registered-app"
        )
        # Corrupt bootstrap.json falls back cleanly to empty watermarks.
        wm_file = os.path.join(self.brain, ".muninn", "bootstrap.json")
        with open(wm_file, "w", encoding="utf-8") as fh:
            fh.write("{ invalid json")
        self.assertEqual(bootstrap.load_watermarks(self.brain), {})
        with open(wm_file, "w", encoding="utf-8") as fh:
            json.dump({"files": "not-a-dict"}, fh)
        self.assertEqual(bootstrap.load_watermarks(self.brain), {})

        # Non-existent and empty files are skipped cleanly during discovery and collection.
        empty_f = os.path.join(self.tmp, "empty.jsonl")
        open(empty_f, "w", encoding="utf-8").close()
        bad_json = os.path.join(self.tmp, "bad.json")
        with open(bad_json, "w", encoding="utf-8") as fh:
            fh.write("{ broken")
        non_list_json = os.path.join(self.tmp, "nonlist.json")
        with open(non_list_json, "w", encoding="utf-8") as fh:
            json.dump({"messages": "nope"}, fh)
        self.assertEqual(
            bootstrap.discover_archives(sources=[os.path.join(self.tmp, "missing-dir")]), []
        )

        # Malformed JSONL lines and oversized lines are skipped while valid lines survive.
        mixed_jsonl = os.path.join(self.tmp, "registered-app", "mixed.jsonl")
        with open(mixed_jsonl, "w", encoding="utf-8") as fh:
            fh.write("{not-json\n")
            fh.write("[]\n")
            fh.write("x" * (bootstrap.MAX_LINE_BYTES + 10) + "\n")
            fh.write(json.dumps({"role": "user", "content": "<SYSTEM>skip</SYSTEM>"}) + "\n")
            fh.write(json.dumps({
                "role": "user",
                "timestamp": "2026-11-24T09:00:00Z",
                "content": "Configure connection pool limits.",
            }) + "\n")

        cands, wms = bootstrap.collect_candidates(
            self.brain, [empty_f, bad_json, non_list_json, mixed_jsonl]
        )
        self.assertEqual(len(cands), 1)
        digest = bootstrap.candidate_digest(cands)
        b = Bundle(self.brain)
        d = Dynamics(self.brain)

        # Validation error branches in _parse_json_reply and apply_response.
        with self.assertRaises(ValueError):
            bootstrap._parse_json_reply("x" * (bootstrap.MAX_REPLY_BYTES + 5))
        with self.assertRaises(ValueError):
            bootstrap._parse_json_reply("{bad")
        with self.assertRaises(TypeError):
            bootstrap._parse_json_reply("[]")
        for err_type, bad in (
            (ValueError, {"digest": digest, "notes": [{"sources": ["S001"]}] * 25,
                          "threads": [], "lessons": []}),
            (TypeError, {"digest": digest, "notes": ["bad"], "threads": [], "lessons": []}),
            (ValueError, {"digest": digest, "notes": [{"sources": []}],
                          "threads": [], "lessons": []}),
            (ValueError, {"digest": digest, "notes": [{"sources": ["S001"], "title": "", "summary": "x"}],
                          "threads": [], "lessons": []}),
            (TypeError, {"digest": digest, "notes": [], "threads": ["bad"], "lessons": []}),
            (ValueError, {"digest": digest, "notes": [],
                          "threads": [{"sources": ["S001"], "title": "T"}], "lessons": []}),
            (TypeError, {"digest": digest, "notes": [], "threads": [], "lessons": ["bad"]}),
            (ValueError, {"digest": digest, "notes": [], "threads": [],
                          "lessons": [{"sources": ["S001"], "title": "L", "body": "", "guards": ["a"]}]}),
        ):
            with self.assertRaises(err_type):
                bootstrap.apply_response(b, d, cands, json.dumps(bad), wms)

        # Registered room note lands under projects/registered-app/!
        counts = bootstrap.apply_response(b, d, cands, json.dumps({
            "digest": digest,
            "notes": [{
                "slug": "pool-limits",
                "project": "registered-app",
                "title": "Pool limits",
                "summary": "Max 32 connections per worker.",
                "sources": ["S001"],
            }],
            "threads": [{
                "slug": "pool-work",
                "title": "Pool work",
                "state": "",
                "episode": "Checked pool bounds.",
                "sources": ["S001"],
            }],
            "lessons": [],
        }), wms)
        self.assertEqual(counts["notes"], 1)
        self.assertIn("projects/registered-app/pool-limits.md", Bundle(self.brain).notes)

        # Protected episode in deterministic mode is skipped rather than overwritten.
        ep_rel = f"threads/{cands[0]['project']}/bootstrap-{cands[0]['file_key'][:12]}.md"
        b2 = Bundle(self.brain)
        b2.write_note(ep_rel, {"type": "episode", "title": "Curated ep", "provenance": "curated"}, "Keep.")
        imp, skp = bootstrap.bootstrap_deterministic(b2, d, cands, {})
        self.assertEqual((imp, skp), (0, 1))


if __name__ == "__main__":
    unittest.main()
