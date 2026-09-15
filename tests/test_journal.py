"""Verify threads, journal entries, session continuity, and transcript import.

A thread maintains current state above dated supporting episodes. Priming
places the relevant thread before repository facts and records served thread
notes as recall events. Transcript import is deterministic, applies secret
scrubbing, and enforces fixed size limits.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from muninn import cli, journal, review  # noqa: E402
from muninn.dynamics import Dynamics  # noqa: E402
from muninn.store import Bundle  # noqa: E402


class _Root(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _run(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--root", self.root, *argv])
        return buf.getvalue()


class TestJournalVerb(_Root):
    def test_same_second_episodes_sort_by_numeric_sequence(self):
        b, d = Bundle(self.root), Dynamics(self.root)
        for sequence in range(1, 13):
            journal.add_episode(b, d, "Sequence", f"Episode {sequence}.",
                                when=1_753_500_000)
        episodes = journal.threads_of(b)["sequence"]["episodes"]
        self.assertEqual([n.body.strip() for _p, n in episodes],
                         [f"Episode {sequence}." for sequence in range(12, 0, -1)])

    def test_stale_bundle_cannot_replace_another_writers_episode_or_head(self):
        first, second = Bundle(self.root), Bundle(self.root)
        journal.add_episode(first, Dynamics(self.root), "Shared", "First episode.",
                            state="Current decision.", when=1_753_500_000)
        journal.add_episode(second, Dynamics(self.root), "Shared", "Second episode.",
                            when=1_753_500_000)
        thread = journal.threads_of(Bundle(self.root))["shared"]
        self.assertEqual(thread["head"].body.strip(), "Current decision.")
        self.assertEqual([n.body.strip() for _p, n in thread["episodes"]],
                         ["Second episode.", "First episode."])

    def test_concurrent_journal_writers_preserve_every_episode(self):
        bundles = [Bundle(self.root) for _ in range(8)]
        dynamics = [Dynamics(self.root) for _ in bundles]
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(journal.add_episode, bundle, dyn,
                                   "Parallel", f"Episode {index}.",
                                   when=1_753_500_000)
                       for index, (bundle, dyn) in enumerate(zip(bundles, dynamics))]
            for future in futures:
                future.result(timeout=5)
        episodes = journal.threads_of(Bundle(self.root))["parallel"]["episodes"]
        self.assertEqual({note.body.strip() for _path, note in episodes},
                         {f"Episode {index}." for index in range(8)})

    def test_same_second_collision_keeps_newest_first_order(self):
        # the collision suffix must extend an UNCHANGING base: eating the
        # HHMMSS stamp would make the renamed episode sort as the day's
        # newest forever ("...-2.md" > "...-183045.md" as strings)
        b, d = Bundle(self.root), Dynamics(self.root)
        t_morning = 1_753_500_000.0
        slug = journal.add_episode(b, d, "T", "first", when=t_morning)
        journal.add_episode(b, d, "T", "same-second twin", when=t_morning)
        journal.add_episode(b, d, "T", "evening episode",
                            when=t_morning + 8 * 3600)
        eps = journal.threads_of(b)[slug]["episodes"]
        self.assertEqual(len(eps), 3)
        self.assertIn("evening", eps[0][1].body)  # newest really is first
        twin = [p for p, _n in eps if p.endswith("-2.md")]
        self.assertEqual(len(twin), 1)  # the twin kept its full stamp
        self.assertRegex(twin[0], r"\d{8}-\d{6}-2\.md$")

    def test_journal_scrubs_secrets_at_the_write_boundary(self):
        secret = "sk-" + "a" * 40
        self._run("journal", "Deploy", "--body", f"used key {secret}",
                  "--state", f"CURRENT: rotated {secret}")
        b = Bundle(self.root)
        for path, note in b.notes.items():
            self.assertNotIn(secret, note.body, path)

    def test_journal_writes_episode_and_head(self):
        out = self._run("journal", "Pack Format Redesign",
                        "--body", "Decided against per-note budgets "
                                  "(too fiddly); next: coverage floor.",
                        "--state", "Redesigning packs. Decided: shares "
                                   "proportional to activation. Next: "
                                   "coverage floor.")
        self.assertIn("journaled: pack-format-redesign", out)
        b = Bundle(self.root)
        head = b.notes["threads/pack-format-redesign/thread.md"]
        self.assertEqual(head.meta.get("type"), "thread")
        self.assertIn("shares proportional", head.body)
        episodes = [n for p, n in b.notes.items()
                    if n.meta.get("type") == "episode"
                    and p.startswith("threads/pack-format-redesign/")]
        self.assertEqual(len(episodes), 1)
        self.assertIn("per-note budgets", episodes[0].body)

    def test_journal_appends_episodes_head_replaced_only_by_state(self):
        self._run("journal", "T", "--body", "ep one", "--state", "state one")
        self._run("journal", "T", "--body", "ep two")  # no --state
        b = Bundle(self.root)
        head = b.notes["threads/t/thread.md"]
        self.assertIn("state one", head.body)  # untouched without --state
        eps = [p for p, n in b.notes.items()
               if n.meta.get("type") == "episode"]
        self.assertEqual(len(eps), 2)

    def test_journal_list_and_show(self):
        self._run("journal", "Alpha", "--body", "a1", "--state", "A state")
        self._run("journal", "Beta", "--body", "b1")
        listing = self._run("journal")
        self.assertIn("alpha", listing)
        self.assertIn("beta", listing)
        show = self._run("journal", "Alpha", "--show")
        self.assertIn("A state", show)
        self.assertIn("a1", show)


class TestWhereWeLeftOff(_Root):
    def _seed(self):
        self._run("journal", "Framework Routing",
                  "--body", "Chose trie-based routing over regex table; "
                            "regex was 40ms/req. Next: nested mounts.",
                  "--state", "Building the framework's router. DECIDED: "
                             "trie over regex (40ms/req). IN PROGRESS: "
                             "nested mounts. OPEN: wildcard precedence.")
        self._run("journal", "Docs Overhaul", "--body", "moved to mkdocs",
                  "--state", "Docs migration to mkdocs, half done.")

    def test_matching_thread_leads_the_boot(self):
        self._seed()
        b, d = Bundle(self.root), Dynamics(self.root)
        out = journal.threads_section(
            b, d, cue="framework routing nested mounts", session="s1")
        self.assertIn("## Where we left off", out)
        self.assertIn("Framework Routing", out)
        self.assertIn("DECIDED: trie over regex", out)   # the head
        self.assertIn("Chose trie-based routing", out)   # latest episode
        self.assertNotIn("mkdocs", out)  # unrelated thread stays out
        # served episodically = recall events, so review measures them
        m = review.session_metrics(d.ledger_path, "s1")
        self.assertIn("threads/framework-routing/thread.md", m["served"])

    def test_no_match_falls_back_to_most_recent_thread(self):
        self._seed()
        out = journal.threads_section(Bundle(self.root),
                                      Dynamics(self.root),
                                      cue="zzz unrelated qqq")
        self.assertIn("## Where we left off", out)
        self.assertIn("Docs Overhaul", out)  # most recently journaled

    def test_empty_without_threads(self):
        self.assertEqual(
            journal.threads_section(Bundle(self.root),
                                    Dynamics(self.root), cue="x"), "")

    def test_prime_cli_opens_with_the_thread(self):
        self._seed()
        out = self._run("prime", "--cwd", self.root, "--budget", "300")
        self.assertLess(out.index("Where we left off"),
                        out.index("Context pack"))  # continuity FIRST


class TestTranscriptImport(_Root):
    def test_malformed_message_shapes_are_skipped_before_valid_message(self):
        path = os.path.join(self.root, "malformed.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.writelines(
                json.dumps({"type": "user", "message": message}) + "\n"
                for message in ("not an object", ["item"], 42, True, None,
                                {"content": "Review the release."}))
        self.assertIn("imported 1 session", self._run("import-transcripts", path))
        notes = [note for note in Bundle(self.root).notes.values()
                 if note.meta.get("type") == "episode"]
        self.assertEqual(len(notes), 1)
        self.assertIn("Review the release.", notes[0].body)

    def test_thread_slug_does_not_preserve_secret_shaped_text(self):
        secret = "sk-" + "SYNTHETIC" * 5
        self._run("journal", secret, "--body", "Safe decision.")
        for path in Bundle(self.root).notes:
            self.assertNotIn(secret.lower(), path)

    def test_import_scrubs_summary_before_truncating_title(self):
        secret = "sk-" + "a" * 40
        d, _ = self._write_transcript(
            "proj-summary", "s1.jsonl", ["Review the release."],
            summary="x" * 110 + secret)
        self._run("import-transcripts", d)
        ep = next(n for n in Bundle(self.root).notes.values()
                  if n.meta.get("type") == "episode")
        self.assertNotIn("sk-", ep.title)
        self.assertIn("[scrubbed]", ep.title)

    def _write_transcript(self, dirname, name, msgs, summary=None):
        d = os.path.join(self.root, "..", dirname)
        os.makedirs(d, exist_ok=True)
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        path = os.path.join(d, name)
        with open(path, "w", encoding="utf-8") as fh:
            if summary:
                fh.write(json.dumps({"type": "summary",
                                     "summary": summary}) + "\n")
            for i, m in enumerate(msgs):
                fh.write(json.dumps({
                    "type": "user",
                    "timestamp": f"2026-07-0{(i % 8) + 1}T10:00:00Z",
                    "message": {"role": "user", "content": m}}) + "\n")
            fh.write("not json\n")  # junk lines never break an import
        return d, path

    def test_import_encodes_sessions_as_episodes(self):
        d, _ = self._write_transcript(
            "proj-a", "s1.jsonl",
            ["let's build the auth flow with magic links",
             [{"type": "text", "text": "use redis for the token store"}]],
            summary="Auth flow design")
        out = self._run("import-transcripts", d)
        self.assertIn("imported 1 session(s)", out)
        b = Bundle(self.root)
        eps = [n for n in b.notes.values()
               if n.meta.get("type") == "episode"]
        self.assertEqual(len(eps), 1)
        self.assertIn("Auth flow design", eps[0].title)
        self.assertIn("magic links", eps[0].body)
        self.assertIn("redis for the token store", eps[0].body)  # blocks too

    def test_import_scrubs_secret_shaped_strings(self):
        openai_key = "sk-" + "abc123def456ghi789jkl012mno345pqr"
        github_token = "ghp_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcd1234"
        d, _ = self._write_transcript(
            "proj-b", "s1.jsonl",
            [f"set OPENAI key {openai_key} and {github_token} then "
             "password=SuperSecret99 for the db"])
        self._run("import-transcripts", d)
        b = Bundle(self.root)
        ep = next(n for n in b.notes.values()
                  if n.meta.get("type") == "episode")
        for secret in (openai_key[:9], github_token[:10], "SuperSecret99"):
            self.assertNotIn(secret, ep.body)
        self.assertIn("[scrubbed]", ep.body)

    def test_import_caps_messages_and_is_idempotent(self):
        d, _ = self._write_transcript(
            "proj-c", "s1.jsonl", [f"message number {i}" for i in range(80)])
        self._run("import-transcripts", d)
        b = Bundle(self.root)
        ep = next(n for n in b.notes.values()
                  if n.meta.get("type") == "episode")
        self.assertLessEqual(ep.body.count("\n- "), journal.IMPORT_MSG_CAP)
        out = self._run("import-transcripts", d)  # re-run: no duplicates
        self.assertIn("0 session(s)", out)


if __name__ == "__main__":
    unittest.main()
