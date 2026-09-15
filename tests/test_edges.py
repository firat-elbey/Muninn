"""Verify error handling, state limits, and less common command paths.

The tests cover command wrappers, nonblocking hook behavior under malformed
input, session and intent limits, pack-budget overflow, and the permissive
frontmatter and link rules in Section 1 of the specification.
"""

import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from muninn import cli, enrich, extract  # noqa: E402
from muninn.dynamics import (INTENT_KEYS, SENSE_KEYS, Dynamics)  # noqa: E402
from muninn.observe import (_age, git_out, hook_config,  # noqa: E402
                            hook_fields, inflight_section, observe_event,
                            stdin_text)
from muninn.recall import _truncate, context_pack  # noqa: E402
from muninn.store import Bundle, Note, parse_frontmatter  # noqa: E402


def _run_cli(*argv):
    """Run the CLI capturing stdout+stderr; returns (out, err)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        cli.main(list(argv))
    return out.getvalue(), err.getvalue()


class _TmpRoot(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)


# -- CLI: the memory verbs end-to-end ----------------------------------------

class TestCliMemoryVerbs(_TmpRoot):
    def test_add_with_tags_description_and_supersedes(self):
        _run_cli("--root", self.root, "add", "a/old.md", "--title", "Old",
                 "--body", "port 5432")
        out, _ = _run_cli("--root", self.root, "add", "a/new.md",
                          "--title", "New", "--description", "the fix",
                          "--tags", "db, infra", "--supersedes", "a/old.md",
                          "--body", "port 7433")
        self.assertIn("added a/new.md", out)
        b = Bundle(self.root)
        self.assertEqual(b.notes["a/new.md"].meta["tags"], ["db", "infra"])
        self.assertEqual(b.notes["a/new.md"].meta["supersedes"], ["a/old.md"])
        self.assertEqual(b.superseded_by(), {"a/old.md": "a/new.md"})
        # the ledger-level supersede landed too
        self.assertTrue(Dynamics(self.root).entries["a/old.md"]["superseded"])

    def test_add_reads_body_from_stdin(self):
        with mock.patch("sys.stdin", io.StringIO("piped body text")):
            _run_cli("--root", self.root, "add", "s.md", "--title", "S")
        self.assertIn("piped body text", Bundle(self.root).notes["s.md"].body)

    def test_touch_outcome_pin_supersede_index(self):
        _run_cli("--root", self.root, "add", "n.md", "--title", "N",
                 "--body", "x")
        out, _ = _run_cli("--root", self.root, "touch", "n.md")
        self.assertIn("touched n.md", out)
        out, _ = _run_cli("--root", self.root, "outcome", "0.9",
                          "--why", "it worked")
        self.assertIn("captured window of 1 notes", out)
        out, _ = _run_cli("--root", self.root, "pin", "n.md")
        self.assertIn("pinned n.md", out)
        out, _ = _run_cli("--root", self.root, "pin", "n.md", "--unpin")
        self.assertIn("unpinned n.md", out)
        d = Dynamics(self.root)
        self.assertFalse(d.entries["n.md"]["pinned"])
        out, _ = _run_cli("--root", self.root, "supersede", "n.md", "m.md")
        self.assertIn("n.md superseded by m.md", out)
        out, _ = _run_cli("--root", self.root, "index")
        self.assertIn("index.md regenerated", out)

    def test_goal_declare_then_retire_by_text(self):
        out, _ = _run_cli("--root", self.root, "goal", "ship the fix")
        self.assertIn("active goal: ship the fix", out)
        out, _ = _run_cli("--root", self.root, "goal", "ship the fix",
                          "--off")
        self.assertIn("retired goal: ship the fix", out)
        self.assertEqual(Dynamics(self.root).goals, {})

    def test_recall_explain_prints_facets_and_carrier(self):
        b = Bundle(self.root)
        b.write_note("port.md", {"title": "Postgres port"},
                     "postgres listens on 7433. see [[Wal shipping]]")
        b.write_note("wal.md", {"title": "Wal shipping"},
                     "barman archives segments hourly")
        out, _ = _run_cli("--root", self.root, "recall", "postgres port",
                          "--explain", "--no-reactivate")
        self.assertIn("facet hit:", out)
        self.assertIn("port.md", out)
        # wal.md shares no words with the cue: it can only arrive over the
        # authored link, and --explain must show that hop
        self.assertIn("carried by: port.md (link", out)

    def test_hoist_root_equals_form(self):
        out, _ = _run_cli("skill", f"--root={self.root}")
        self.assertIn(self.root, out)

    def test_demo_command_runs_and_cleans_up(self):
        out, _ = _run_cli("demo")
        self.assertIn("Retrieve again with the same files and query", out)
        m = re.search(r"temporary demonstration bundle remains at (\S+)\.",
                      out, re.IGNORECASE)
        self.assertTrue(m)
        shutil.rmtree(m.group(1), ignore_errors=True)


class TestCliNeverCrashSurfaces(_TmpRoot):
    def test_observe_unknown_kind_is_a_stderr_line_not_a_crash(self):
        out, err = _run_cli("--root", self.root, "observe",
                            "--kind", "explode", "--note", "x.md")
        self.assertIn("unknown kind", err)
        self.assertEqual(out, "")

    def test_observe_json_non_dict_is_silently_dropped(self):
        with mock.patch("sys.stdin", io.StringIO("[1, 2, 3]")):
            out, err = _run_cli("--root", self.root, "observe", "--json")
        self.assertEqual((out, err), ("", ""))

    def test_observe_junk_valence_is_caught(self):
        payload = json.dumps({"kind": "outcome", "valence": "NaNo"})
        with mock.patch("sys.stdin", io.StringIO(payload)):
            _out, err = _run_cli("--root", self.root, "observe", "--json")
        self.assertIn("muninn observe:", err)

    def test_hook_swallows_internal_errors(self):
        # the contract: a broken bundle/pack must NEVER error a hook :
        # cmd_hook catches everything and stays exit-0
        with mock.patch("sys.stdin", io.StringIO("{}")), \
                mock.patch("muninn.cli.context_pack",
                           side_effect=RuntimeError("boom")):
            _out, err = _run_cli("--root", self.root, "hook",
                                 "session-start")
        self.assertIn("muninn hook: boom", err)

    def test_hook_print_config_is_valid_json_for_the_root(self):
        out, _ = _run_cli("--root", self.root, "hook", "print-config")
        cfg = json.loads(out)
        self.assertEqual(set(cfg["hooks"]),
                         {"SessionStart", "PostToolUse", "SessionEnd"})
        self.assertIn(os.path.abspath(self.root), out)

    def test_sync_error_exits_one_via_cli(self):
        with self.assertRaises(SystemExit) as cm:
            _run_cli("--root", self.root, "sync")  # tmpdir: not a repo
        self.assertEqual(cm.exception.code, 1)

    def test_intent_without_branch_anywhere_fails_loud(self):
        # a tmpdir has no git branch to key on: announce and --done must
        # both refuse rather than key an intent on ""
        with self.assertRaises(SystemExit):
            _run_cli("--root", self.root, "intent", "work",
                     "--cwd", self.root)
        with self.assertRaises(SystemExit):
            _run_cli("--root", self.root, "intent", "--done",
                     "--cwd", self.root)

    def test_intent_done_unknown_branch_lists_active_ones(self):
        Dynamics(self.root).intent("busy/branch", goal="in progress")
        buf = io.StringIO()
        with redirect_stdout(buf), self.assertRaises(SystemExit):
            cli.main(["--root", self.root, "intent", "--done",
                      "--branch", "never/announced"])
        self.assertIn("busy/branch: in progress", buf.getvalue())


@unittest.skipUnless(extract.available(), "tree-sitter not installed")
class TestCliExtractionBranches(_TmpRoot):
    def setUp(self):
        super().setUp()
        self.src = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.src, ignore_errors=True)
        with open(os.path.join(self.src, "app.py"), "w") as fh:
            fh.write("import json\n\ndef main():\n    return json.dumps({})\n")

    def test_extract_json_prints_relation_and_secret_notice(self):
        out_json = os.path.join(self.root, "g.json")
        with mock.patch("muninn.extract.config_value_count", return_value=2):
            out, err = _run_cli("--root", self.root, "extract", self.src,
                                "--json", out_json)
        self.assertIn("edges by relation:", out)
        self.assertIn("captured 2 config value(s)", err)
        self.assertTrue(os.path.exists(out_json))

    def test_enrich_unavailable_tree_sitter_soft_fails(self):
        with mock.patch("muninn.extract.available", return_value=False):
            out, _ = _run_cli("--root", self.root, "enrich", self.src)
        self.assertIn("tree-sitter not installed", out)

    def test_enrich_privacy_refusal_keeps_the_base(self):
        with mock.patch("muninn.extract.enrich",
                        side_effect=enrich.PrivacyError("remote refused")):
            out, err = _run_cli("--root", self.root, "enrich", self.src)
        self.assertIn("enrichment endpoint refused (base kept)", err)
        self.assertIn("tree-sitter base:", out)

    def test_build_enrich_privacy_refusal_still_imports_base(self):
        with mock.patch("muninn.extract.enrich",
                        side_effect=enrich.PrivacyError("remote refused")):
            out, err = _run_cli("--root", self.root, "build", self.src,
                                "--enrich")
        self.assertIn("enrichment endpoint refused", err)
        self.assertIn("imported", out)

    def test_build_graphify_oserror_soft_fails(self):
        with mock.patch("muninn.cli.shutil.which",
                        return_value="/nonexistent/graphify"), \
                mock.patch("muninn.cli.subprocess.run",
                           side_effect=OSError("exec format error")):
            out, _ = _run_cli("--root", self.root, "build", self.src)
        self.assertIn("could not run graphify", out)

    def test_build_graphify_without_output_soft_fails(self):
        ok = mock.Mock(returncode=0)
        with mock.patch("muninn.cli.shutil.which",
                        return_value="/nonexistent/graphify"), \
                mock.patch("muninn.cli.subprocess.run", return_value=ok):
            out, _ = _run_cli("--root", self.root, "build", self.src)
        self.assertIn("no graph.json was found", out)


# -- observe: sense helpers --------------------------------------------------

class TestObserveHelpers(_TmpRoot):
    def test_age_buckets(self):
        self.assertEqual(_age(30), "1m")
        self.assertEqual(_age(45 * 60), "45m")
        self.assertEqual(_age(3 * 3600), "3h")
        self.assertEqual(_age(2 * 86400 + 5), "2d")

    def test_git_out_swallows_subprocess_failure(self):
        with mock.patch("muninn.observe.subprocess.run",
                        side_effect=OSError("no git")):
            self.assertEqual(git_out("/anywhere", "status"), "")

    def test_outcome_with_note_maps_or_drops(self):
        b = Bundle(self.root)
        b.write_note("n.md", {"title": "N"}, "x")
        d = Dynamics(self.root)
        # mappable note: outcome lands AND touches the note (encode)
        self.assertEqual(observe_event(b, d, "outcome", "n.md",
                                       valence=0.9), "n.md")
        self.assertEqual(d.entries["n.md"]["recurrence"], 1)
        # unmappable note named: the whole event drops, no phantom outcome
        before = os.path.getsize(d.ledger_path)
        self.assertIsNone(observe_event(b, d, "outcome", "ghost.md",
                                        valence=0.9))
        self.assertEqual(os.path.getsize(d.ledger_path), before)

    def test_inflight_path_list_is_capped_with_a_count(self):
        b = Bundle(self.root)
        d = Dynamics(self.root)
        d.intent("busy/branch", paths=[f"src/f{i}.py" for i in range(9)],
                 goal="wide refactor")
        out = inflight_section(b, d, workdir=self.root)
        self.assertIn("+3 more", out)  # 9 claimed, 6 shown

    def test_hook_fields_rejects_junk_shapes(self):
        self.assertEqual(hook_fields("not json"), (None, None, None))
        self.assertEqual(hook_fields("[1,2]"), (None, None, None))
        deep = "[" * 100000 + "]" * 100000  # RecursionError must not escape
        self.assertEqual(hook_fields(deep), (None, None, None))
        sid, tool, fp = hook_fields(json.dumps(
            {"session_id": "s" * 500, "tool_name": "Read",
             "tool_input": {"file_path": "/a/b.md"}}))
        self.assertEqual(len(sid), 128)  # capped
        self.assertEqual((tool, fp), ("Read", "/a/b.md"))

    def test_hook_config_quotes_the_root(self):
        cfg = json.loads(hook_config("/tmp/kb with spaces"))
        cmd = cfg["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertIn("'/tmp/kb with spaces'", cmd)

    def test_hook_config_uses_the_resolved_executable(self):
        with mock.patch("shutil.which",
                        return_value="/tmp/venv with spaces/muninn"):
            cfg = json.loads(hook_config("/tmp/kb"))
        cmd = cfg["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertTrue(cmd.startswith("'/tmp/venv with spaces/muninn' "))

    def test_stdin_text_is_tty_and_error_safe(self):
        with mock.patch("sys.stdin", None):
            self.assertEqual(stdin_text(), "")
        tty = mock.Mock()
        tty.isatty.return_value = True
        with mock.patch("sys.stdin", tty):
            self.assertEqual(stdin_text(), "")
        broken = mock.Mock()
        broken.isatty.side_effect = OSError("gone")
        with mock.patch("sys.stdin", broken):
            self.assertEqual(stdin_text(), "")


# -- dynamics: bounds and junk tolerance --------------------------------------

class TestDynamicsBounds(_TmpRoot):
    def test_replay_without_a_ledger_resets_clean(self):
        d = Dynamics(self.root)
        d.replay()
        self.assertEqual((d.entries, d.order, d._applied_bytes), ({}, [], 0))

    def test_junk_ledger_lines_are_skipped_on_replay(self):
        d = Dynamics(self.root)
        d.touch("good.md")
        with open(d.ledger_path, "a", encoding="utf-8") as fh:
            fh.write("\n")                 # blank line
            fh.write("{truncated json\n")  # crash mid-append
        os.remove(d.state_path)
        fresh = Dynamics(self.root)
        self.assertEqual(fresh.entries["good.md"]["recurrence"], 1)

    def test_session_map_is_bounded_to_50(self):
        d = Dynamics(self.root)
        for i in range(55):
            d.touch(f"n{i}.md", session=f"s{i}")
        self.assertLessEqual(len(d._session_notes), 51)
        self.assertNotIn("s0", d._session_notes)  # oldest fell off

    def test_debounce_map_is_bounded_to_sense_keys(self):
        d = Dynamics(self.root)
        for i in range(SENSE_KEYS + 10):
            d.touch(f"n{i}.md")
        self.assertEqual(len(d.last_seen), SENSE_KEYS)
        self.assertNotIn("touch|n0.md", d.last_seen)

    def test_unpin_clamps_strength_back_to_priority(self):
        d = Dynamics(self.root)
        d.pin("rule.md")  # pinned while never touched: floor holds it up
        self.assertGreaterEqual(d.entries["rule.md"]["strength"], 0.5)
        d.pin("rule.md", value=False)
        self.assertLess(d.entries["rule.md"]["strength"], 0.5)

    def test_intent_store_is_bounded_oldest_expiring_falls_off(self):
        d = Dynamics(self.root)
        d.intent("drop/me", ttl=61)  # expires soonest -> the eviction pick
        for i in range(INTENT_KEYS):
            d.intent(f"b{i}", ttl=3600 + i)
        self.assertEqual(len(d.intents), INTENT_KEYS)
        self.assertNotIn("drop/me", d.intents)

    def test_outcome_with_note_encodes_it_first(self):
        d = Dynamics(self.root)
        d.outcome(0.9, note="win.md", why="shipped")
        self.assertEqual(d.entries["win.md"]["recurrence"], 1)
        self.assertGreater(d.entries["win.md"]["captured"], 0)


# -- recall: pack budgets, modes, truncation ----------------------------------

class TestPackBudgets(_TmpRoot):
    def _bundle(self, n_notes=3, title=None, body="postgres backup steps"):
        b = Bundle(self.root)
        for i in range(n_notes):
            b.write_note(f"n{i}.md",
                         {"title": title or f"Note {i}",
                          "description": "d"}, f"{body} {i} " + "x " * 120)
        return b

    def test_truncate_marks_the_cut(self):
        out = _truncate("line\n" * 500, budget_tokens=20)
        self.assertTrue(out.endswith("[... truncated]"))
        self.assertLess(len(out), 200)

    def test_unknown_mode_fails_loud(self):
        b = self._bundle()
        with self.assertRaisesRegex(ValueError, "unknown mode"):
            context_pack(b, Dynamics(self.root), "x", mode="typo")

    def test_dump_mode_concatenates_until_budget(self):
        b = self._bundle()
        out = context_pack(b, Dynamics(self.root), "ignored", mode="dump",
                           budget=60)
        self.assertIn("full bundle", out)
        self.assertTrue(out.endswith("[... truncated]"))

    def test_index_overflows_to_an_and_n_more_line(self):
        b = self._bundle(n_notes=40)
        out = context_pack(b, Dynamics(self.root), "postgres backup",
                           budget=300, k=1)
        self.assertRegex(out, r"\.\.\. and \d+ more")

    def test_focus_stops_at_the_budget_never_empty(self):
        b = self._bundle(n_notes=6)
        out = context_pack(b, Dynamics(self.root), "postgres backup",
                           budget=140, k=6, index=False)
        focus = out.split("## Focus")[1]
        self.assertGreaterEqual(focus.count("###"), 1)  # floor: one note
        self.assertLess(focus.count("###"), 6)          # budget: not all

    def test_duplicate_titles_do_not_stack_the_focus(self):
        b = self._bundle(n_notes=4, title="Same Title")
        out = context_pack(b, Dynamics(self.root), "postgres backup",
                           budget=2000, k=4, index=False)
        self.assertEqual(out.count("### Same Title"), 1)

    def test_goal_in_why_loaded_cuts_at_a_word_boundary(self):
        # field-tested on Flask: "aligned with active goal: harden request lifecycle
        # and tea": a mid-word cut reads as a typo, not a truncation
        b = Bundle(self.root)
        b.write_note("n.md", {"title": "Teardown paths"},
                     "harden the teardown lifecycle paths")
        d = Dynamics(self.root)
        d.goal("harden request lifecycle and teardown paths", weight=0.8)
        from muninn.recall import recall
        hits = recall(b, d, "teardown lifecycle", reactivate=False)
        why = hits[0][2]
        self.assertIn("aligned with active goal: harden request lifecycle", why)
        self.assertIn("…", why)
        self.assertNotRegex(
            why,
            r"aligned with active goal: .*\btea\b",
        )  # no cut word

    def test_index_tail_orders_by_connectivity_not_alphabet(self):
        # field-tested on Flask: after the strength-ranked notes the index
        # fell to ALPHABETICAL, so the model's map led with css stubs
        # ("a {") while README-grade hubs sat under "... and N more"
        b = Bundle(self.root)
        b.write_note("zz-hub.md", {"title": "Architecture overview"},
                     "the hub: see [[Alpha Part]] and [[Beta Part]]")
        b.write_note("alpha.md", {"title": "Alpha Part"},
                     "detail, links back to [[Architecture overview]]")
        b.write_note("beta.md", {"title": "Beta Part"},
                     "detail, links back to [[Architecture overview]]")
        for i in range(4):  # alphabetically-early junk stubs, zero links
            b.write_note(f"aa-stub-{i}.md", {"title": f"a{i} {{"}, "x")
        out = context_pack(Bundle(self.root), Dynamics(self.root),
                           "unrelated cue zzz", budget=900, k=0)
        index = out.split("## Focus")[0]
        hub = index.index("Architecture overview")
        stub = index.index("a0 {")
        self.assertLess(hub, stub)  # the connected hub outranks junk stubs

    def test_test_tagged_notes_rank_below_production(self):
        # field-tested on gin/gson/sinatra: test code mirrors question
        # vocabulary and swamps production notes: demoted, never hidden
        b = Bundle(self.root)
        b.write_note("src.md", {"title": "Bind JSON body",
                                "tags": ["tree-sitter", "go"]},
                     "bind the json request body to a struct")
        b.write_note("tst.md", {"title": "Test bind JSON body",
                                "tags": ["tree-sitter", "go", "test"]},
                     "bind the json request body to a struct in a test")
        from muninn.recall import recall
        hits = recall(Bundle(self.root), Dynamics(self.root),
                      "bind json body to a struct", reactivate=False)
        paths = [n.path for n, _s, _w in hits]
        self.assertEqual(paths[0], "src.md")
        self.assertIn("tst.md", paths)  # demoted, still reachable

    def test_index_excludes_import_plumbing(self):
        # field-tested on express/ripgrep: import nodes are the best-
        # connected notes in a code bundle, so connectivity ordering made
        # them the map's landmarks: they are edges, not knowledge
        b = Bundle(self.root)
        b.write_note("mod.md", {"title": "Response module"},
                     "- imports [[node assert]] (extracted)")
        b.write_note("imp.md", {"title": "node assert", "type": "import"},
                     "")
        out = context_pack(Bundle(self.root), Dynamics(self.root),
                           "zzz nothing", budget=900, k=0)
        index = out.split("## Focus")[0]
        self.assertIn("Response module", index)
        self.assertNotIn("- node assert", index)

    def test_fallback_focus_bounds_k_titles_and_budget(self):
        b = self._bundle(n_notes=4, body="alpha beta gamma")
        no_match = context_pack(b, Dynamics(self.root), "zzz qqq", k=2,
                                budget=900, index=False)
        self.assertIn("no match for zzz qqq", no_match)
        self.assertEqual(no_match.count("###"), 2)  # k bound
        dup = Bundle(self.root)
        dup.write_note("dup.md", {"title": "Note 0"}, "alpha")
        no_match = context_pack(Bundle(self.root), Dynamics(self.root),
                                "zzz qqq", k=9, budget=900, index=False)
        self.assertEqual(no_match.count("### Note 0"), 1)  # dup title once
        tight = context_pack(Bundle(self.root), Dynamics(self.root),
                             "zzz qqq", k=9, budget=120, index=False)
        self.assertLess(tight.count("###"), 5)  # budget break


# -- store: permissive parsing (SPEC §1) --------------------------------------

class TestStoreParsing(_TmpRoot):
    def test_frontmatter_scalars_and_dash_lists(self):
        meta, body = parse_frontmatter(
            "---\n"
            'title: "Quoted Title"\n'
            "pinned: true\n"
            "draft: false\n"
            "weight: 3\n"
            "ratio: 0.5\n"
            "tags:\n- db\n- infra\n"
            "---\n"
            "the body\n")
        self.assertEqual(meta["title"], "Quoted Title")
        self.assertIs(meta["pinned"], True)
        self.assertIs(meta["draft"], False)
        self.assertEqual(meta["weight"], 3)
        self.assertEqual(meta["ratio"], 0.5)
        self.assertEqual(meta["tags"], ["db", "infra"])
        self.assertEqual(body.strip(), "the body")

    def test_unterminated_frontmatter_is_all_body(self):
        meta, body = parse_frontmatter("---\ntitle: X\nno closing fence")
        self.assertEqual(meta, {})
        self.assertIn("no closing fence", body)

    def test_title_falls_back_to_the_filename(self):
        n = Note(path="ops/db-port_map.md", meta={}, body="")
        self.assertEqual(n.title, "db port map")

    def test_markdown_links_resolve_relative_absolute_and_skip_urls(self):
        b = Bundle(self.root)
        b.write_note("docs/a.md", {"title": "A"},
                     "see [b](b), [c](/docs/c.md), [me](a.md), "
                     "[web](https://x.io/d.md), [b again](b.md)")
        b.write_note("docs/b.md", {"title": "B"}, "b")
        b.write_note("docs/c.md", {"title": "C"}, "c")
        b2 = Bundle(self.root)  # reload with all notes present
        self.assertEqual(b2.notes["docs/a.md"].links,
                         ["docs/b.md", "docs/c.md"])  # deduped, no self/url

    def test_wikilink_resolves_by_alias(self):
        b = Bundle(self.root)
        b.write_note("pg.md", {"title": "Postgres",
                               "aliases": ["the database", "pgsql"]}, "x")
        b.write_note("uses.md", {"title": "Uses"}, "runs on [[pgsql]]")
        self.assertEqual(Bundle(self.root).notes["uses.md"].links, ["pg.md"])

    def test_typed_links_dedupe_target_relation_pairs(self):
        b = Bundle(self.root)
        b.write_note("m.md", {"title": "M"},
                     "- imports [[Target]] (extracted)\n"
                     "- imports [[Target]] (inferred)\n")
        b.write_note("target.md", {"title": "Target"}, "t")
        typed = Bundle(self.root).notes["m.md"].typed_links
        self.assertEqual(len(typed), 1)  # duplicate (target, relation) skipped

    def test_supersedes_without_md_extension_normalizes(self):
        b = Bundle(self.root)
        b.write_note("old.md", {"title": "Old"}, "x")
        b.write_note("new.md", {"title": "New",
                                "supersedes": ["/old"]}, "y")
        self.assertEqual(Bundle(self.root).superseded_by(),
                         {"old.md": "new.md"})


# -- extract: the generic guarantees (not per-grammar shapes) -----------------

class TestExtractGenericGuards(unittest.TestCase):
    def test_available_fails_soft_when_import_breaks(self):
        with mock.patch.dict(sys.modules, {"tree_sitter": None}):
            self.assertFalse(extract.available())

    def test_languages_lists_the_resolvable_grammars(self):
        langs = extract.languages()
        self.assertIn("python", langs)
        self.assertIn("markdown", langs)
        self.assertEqual(langs, sorted(langs))

    def test_body_cap_clips_at_a_clean_boundary(self):
        long = "word " * 2000
        clipped = extract._body(long)
        self.assertTrue(clipped.endswith(" …"))
        self.assertLessEqual(len(clipped), extract.BODY_CAP + 4)
        self.assertEqual(extract._body(""), "")
        self.assertEqual(extract._body("short"), "short")

    @unittest.skipUnless(extract.available(), "tree-sitter not installed")
    def test_pathological_json_depth_is_refused_before_parsing(self):
        src = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, src, ignore_errors=True)
        deep = '{"a":\n' * 300 + "1" + "\n}" * 300  # depth 300 >> MAX_DEPTH
        with open(os.path.join(src, "deep.json"), "w") as fh:
            fh.write(deep)
        with open(os.path.join(src, "sane.json"), "w") as fh:
            fh.write('{"port": 5432,\n "nested": {"x": 1}}\n')
        # the deep file is skipped BEFORE the native parser could stack-
        # overflow; the sane neighbor still parses: no crash, no loss
        graph, stats = extract.extract_path(src)
        self.assertEqual(stats["files"], 1)
        self.assertEqual(stats["by_lang"], {"json": 1})

    @unittest.skipUnless(extract.available(), "tree-sitter not installed")
    def test_nested_markdown_sections_nest_their_nodes(self):
        src = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, src, ignore_errors=True)
        with open(os.path.join(src, "doc.md"), "w") as fh:
            fh.write("# Top\n\nintro\n\n## Middle\n\nbody\n\n### Deep\n\n"
                     "leaf text\n")
        graph, _ = extract.extract_path(src)
        labels = {n.get("label") for n in graph["nodes"]}
        self.assertLessEqual({"Top", "Middle", "Deep"}, labels)

    @unittest.skipUnless(extract.available(), "tree-sitter not installed")
    def test_js_require_becomes_an_import_edge(self):
        src = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, src, ignore_errors=True)
        with open(os.path.join(src, "app.js"), "w") as fh:
            fh.write('const fs = require("fs");\nmodule.exports = {};\n')
        graph, _ = extract.extract_path(src)
        rels = {e.get("relation") for e in graph["links"]}
        self.assertIn("imports", rels)

    def test_import_graph_skips_malformed_nodes(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        b = Bundle(root)
        graph = {"nodes": [
            {"no_id": True},                       # malformed: skipped
            {"id": "m1", "label": "Real Concept", "kind": "concept",
             "file_type": "concept", "body": "kept"},
        ], "links": []}
        written, _skipped = extract.import_graph(b, graph)
        self.assertEqual(written, 1)


# -- enrich: privacy guards and untrusted-reply parsing -----------------------

class TestEnrichGuards(unittest.TestCase):
    def setUp(self):
        enrich._WARNED_REMOTE.clear()
        self.addCleanup(enrich._WARNED_REMOTE.clear)

    def test_non_http_scheme_always_raises(self):
        with mock.patch.dict(os.environ,
                             {"MUNINN_ENRICH_URL": "ftp://127.0.0.1/x"}):
            with self.assertRaises(enrich.PrivacyError):
                enrich._endpoint()

    def test_remote_host_refused_without_opt_in(self):
        env = {"MUNINN_ENRICH_URL": "https://api.example.com/v1/x"}
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("MUNINN_ENRICH_ALLOW_REMOTE", None)
            with self.assertRaisesRegex(enrich.PrivacyError, "not local"):
                enrich._endpoint()

    def test_remote_opt_in_warns_once_per_host(self):
        env = {"MUNINN_ENRICH_URL": "https://api.example.com/v1/x",
               "MUNINN_ENRICH_ALLOW_REMOTE": "1"}
        with mock.patch.dict(os.environ, env):
            err = io.StringIO()
            with redirect_stderr(err):
                _url, _model, remote = enrich._endpoint()
                enrich._endpoint()  # second config read: silent
            self.assertTrue(remote)
        self.assertEqual(err.getvalue().count("remote enrichment host"), 1)

    def test_bad_env_numbers_fall_back(self):
        with mock.patch.dict(os.environ,
                             {"MUNINN_ENRICH_TEMPERATURE": "warm",
                              "MUNINN_ENRICH_TIMEOUT": "soon"}):
            self.assertEqual(enrich._temperature(), 0.0)
            self.assertEqual(enrich._cmd_timeout(),
                             float(enrich._CMD_TIMEOUT))

    def test_chat_endpoint_without_url_raises(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MUNINN_ENRICH_URL", None)
            with self.assertRaisesRegex(ValueError, "no MUNINN_ENRICH_URL"):
                enrich._chat_endpoint("sys", "user")

    def test_extract_json_skips_broken_candidates_and_honors_escapes(self):
        reply = ('the model says {oops not json} but then emits '
                 '{"concepts": [], "note": "brace \\" } inside a string"} ok')
        obj = enrich._extract_json(reply)
        self.assertIsNotNone(obj)
        self.assertIn("} inside a string", obj["note"])
        self.assertIsNone(enrich._extract_json("no objects here"))

    def test_apply_dedupes_colliding_labels_and_ids(self):
        graph = {"nodes": [{"id": "n1", "label": "Cache"}], "links": []}
        proposal = {"concepts": [
            {"label": "Cache", "summary": "s1"},   # taken by a base node
            {"label": "cache", "summary": "s2"},   # case-insensitive clash
            {"label": "A-B", "summary": "s3"},
            {"label": "A B", "summary": "s4"},     # same slug as A-B
        ], "edges": []}
        added = enrich._apply(graph, proposal, handle_of={})
        self.assertEqual(added, 4)
        labels = [n["label"] for n in graph["nodes"] if n.get("_origin")]
        self.assertIn("Cache (concept)", labels)
        self.assertIn("cache (concept 2)", labels)
        ids = [n["id"] for n in graph["nodes"] if n.get("_origin")]
        self.assertIn("enrich:concept:a-b", ids)
        self.assertIn("enrich:concept:a-b-2", ids)  # slug collision suffixed


# -- embed: transport format, cache lifecycle, cosine guards ------------------

class _FakeResp:
    def __init__(self, doc):
        self._data = json.dumps(doc).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestEmbedPaths(_TmpRoot):
    def test_cosine_guards_and_identity(self):
        from muninn.embed import cosine
        self.assertEqual(cosine([], [1.0]), 0.0)
        self.assertEqual(cosine([1.0], [1.0, 2.0]), 0.0)  # length mismatch
        self.assertEqual(cosine([0.0, 0.0], [1.0, 1.0]), 0.0)  # zero norm
        self.assertAlmostEqual(cosine([1.0, 2.0], [1.0, 2.0]), 1.0)

    def test_post_sends_model_and_rejects_count_mismatch(self):
        from muninn import embed
        opener = mock.Mock()
        opener.open.return_value = _FakeResp(
            {"data": [{"embedding": [0.1, 0.2]}]})
        vecs = embed._post("http://127.0.0.1:1/v1/e", "m1", ["one"],
                           opener, "")
        self.assertEqual(vecs, [[0.1, 0.2]])
        sent = json.loads(opener.open.call_args[0][0].data)
        self.assertEqual(sent["model"], "m1")
        opener.open.return_value = _FakeResp(
            {"data": [{"embedding": [0.1]}]})  # 1 vector for 2 inputs
        with self.assertRaisesRegex(ValueError, "1 vectors for 2 inputs"):
            embed._post("http://127.0.0.1:1/v1/e", None, ["a", "b"],
                        opener, "")

    def test_embed_notes_offline_serves_cache_hits_only(self):
        from muninn import embed
        b = Bundle(self.root)
        b.write_note("hit.md", {"title": "Hit"}, "cached body")
        b.write_note("miss.md", {"title": "Miss"}, "never embedded")
        b = Bundle(self.root)
        sha = embed._sha(embed._note_text(b.notes["hit.md"]))
        embed._save_cache(b, {"model": None, "notes": {
            "hit.md": {"sha": sha, "vec": [1.0, 0.0]},
            "miss.md": {"sha": "stale", "vec": [9.9]}}})
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MUNINN_EMBED_URL", None)
            vecs = embed.embed_notes(b)
        self.assertEqual(vecs, {"hit.md": [1.0, 0.0]})  # stale sha not served

    def test_embed_notes_model_change_reembeds_and_prunes(self):
        from muninn import embed
        b = Bundle(self.root)
        b.write_note("n.md", {"title": "N"}, "body")
        b = Bundle(self.root)
        sha = embed._sha(embed._note_text(b.notes["n.md"]))
        embed._save_cache(b, {"model": "old-model", "notes": {
            "n.md": {"sha": sha, "vec": [5.0]},        # valid: but old model
            "gone.md": {"sha": "x", "vec": [1.0]}}})   # note no longer exists
        with mock.patch.dict(os.environ,
                             {"MUNINN_EMBED_URL": "http://127.0.0.1:1/v1/e"}), \
                mock.patch("muninn.embed._embed_batch",
                           side_effect=lambda u, m, t, o, k:
                           [[0.5, 0.5]] * len(t)):
            vecs = embed.embed_notes(b)
        self.assertEqual(vecs, {"n.md": [0.5, 0.5]})  # re-embedded, not [5.0]
        cache = embed._load_cache(b)
        self.assertEqual(set(cache["notes"]), {"n.md"})  # gone.md pruned


# -- ingest: source excerpts (what an imported note may quote) ----------------

class TestIngestSnippets(unittest.TestCase):
    def test_file_level_doc_snippet_skips_nonprose(self):
        from muninn.ingest import _doc_snippet
        lines = ("---\ntitle: G\n---\n# Guide\n\n| a | b |\n|---|---|\n\n"
                 "Real first paragraph of prose.\n\n```\ncode inside\n```\n"
                 "After the fence.\n").split("\n")
        out = _doc_snippet(lines, "guide.md", None, "guide.md", limit=400)
        self.assertIn("Real first paragraph", out)
        for noise in ("title: G", "| a | b |", "code inside", "# Guide"):
            self.assertNotIn(noise, out)

    def test_empty_section_yields_stub_never_the_next_section(self):
        from muninn.ingest import _doc_snippet
        lines = "# Doc\n\n## Empty\n\n## Next\n\nnext content\n".split("\n")
        self.assertEqual(
            _doc_snippet(lines, "Empty", None, "doc.md", limit=400), "")

    def test_code_snippet_identifier_rules(self):
        from muninn.ingest import _code_snippet
        lines = ('def restore(db):\n    """Restore from the basebackup.\n\n'
                 '    Long tail."""\n    return db\n').split("\n")
        out = _code_snippet(lines, "restore()", "x.py", limit=300)
        self.assertIn("def restore(db):", out)
        self.assertIn("Restore from the basebackup.", out)
        # a non-identifier label and a missing def both fail soft to ''
        self.assertEqual(_code_snippet(lines, "weird name!", "x.py", 300), "")
        self.assertEqual(_code_snippet(lines, "vanished", "x.py", 300), "")


# -- activate/viz: scoring guards and orphan edges -----------------------------

class TestActivateGuards(_TmpRoot):
    def test_empty_token_sets_score_zero(self):
        from muninn.activate import goal_alignment, relevance
        note = Note(path="n.md", meta={"title": "N"}, body="anything")
        self.assertEqual(relevance(set(), note), 0.0)
        self.assertEqual(goal_alignment(set(), note), 0.0)

    def test_situation_harvest_survives_a_broken_git(self):
        from muninn.activate import cues_from_situation
        b = Bundle(self.root)
        with mock.patch("subprocess.run", side_effect=OSError("no git")):
            cues = cues_from_situation(b, Dynamics(self.root),
                                       workdir=self.root)
        self.assertTrue(any(c.origin == "cwd" for c in cues))  # fails soft

    def test_merged_graph_drops_couse_pairs_outside_the_bundle(self):
        from muninn.activate import merged_graph
        b = Bundle(self.root)
        b.write_note("in.md", {"title": "In"}, "x")
        d = Dynamics(self.root)
        d.touch("in.md", session="s1")
        d.touch("ghost.md", session="s1")  # wired, but not a bundle note
        adj = merged_graph(Bundle(self.root), d)
        self.assertNotIn("ghost.md", adj)
        self.assertEqual(adj.get("in.md", []), [])

    def test_mermaid_skips_edges_to_dropped_nodes(self):
        from muninn.export import export_graph
        from muninn.viz import render_mermaid
        b = Bundle(self.root)
        b.write_note("a.md", {"title": "A"}, "x")
        g = export_graph(Bundle(self.root), Dynamics(self.root))
        g["links"].append({"source": "a.md", "target": "nope.md"})
        out = render_mermaid(g, top=5)
        self.assertIn("graph", out)
        self.assertNotIn("nope", out)


if __name__ == "__main__":
    unittest.main()


class TestInstallEntryPoint(_TmpRoot):
    """`muninn install`: AGENTS.md becomes the canonical protocol file;
    CLAUDE.md / GEMINI.md symlink to it so every harness's organically-
    read context file resolves to the same muninn protocol."""

    def test_install_writes_agents_md_and_links(self):
        proj = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, proj, ignore_errors=True)
        out, _ = _run_cli("--root", self.root, "install", proj)
        agents = os.path.join(proj, "AGENTS.md")
        with open(agents) as fh:
            text = fh.read()
        self.assertIn("# Muninn agent protocol", text)
        self.assertIn(os.path.abspath(self.root), text)  # paste-ready root
        for name in ("CLAUDE.md", "GEMINI.md"):
            p = os.path.join(proj, name)
            self.assertTrue(os.path.islink(p), name)
            self.assertEqual(os.readlink(p), "AGENTS.md")
        self.assertIn("canonical entry point", out)

    def test_install_appends_once_and_never_clobbers(self):
        proj = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, proj, ignore_errors=True)
        with open(os.path.join(proj, "AGENTS.md"), "w") as fh:
            fh.write("# My project\nexisting instructions\n")
        with open(os.path.join(proj, "CLAUDE.md"), "w") as fh:
            fh.write("hand-written claude notes\n")  # a REAL file: sacred
        _run_cli("--root", self.root, "install", proj)
        out2, _ = _run_cli("--root", self.root, "install", proj)  # again
        with open(os.path.join(proj, "AGENTS.md")) as fh:
            text = fh.read()
        self.assertIn("existing instructions", text)      # appended, kept
        self.assertEqual(text.count("# Muninn agent protocol"), 1)
        with open(os.path.join(proj, "CLAUDE.md")) as fh:
            self.assertEqual(fh.read(), "hand-written claude notes\n")
        self.assertFalse(os.path.islink(os.path.join(proj, "CLAUDE.md")))
        self.assertIn("left as-is", out2)

    def test_install_copy_mode_writes_managed_blocks(self):
        proj = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, proj, ignore_errors=True)
        _run_cli("--root", self.root, "install", proj, "--copy")
        for name in ("CLAUDE.md", "GEMINI.md"):
            p = os.path.join(proj, name)
            self.assertFalse(os.path.islink(p))  # real files, no symlinks
            with open(p) as fh:
                text = fh.read()
            self.assertIn("muninn:protocol:begin", text)
            self.assertIn("# Muninn agent protocol", text)
        # refresh, never duplicate: re-run replaces the managed block
        _run_cli("--root", self.root, "install", proj, "--copy")
        with open(os.path.join(proj, "CLAUDE.md")) as fh:
            self.assertEqual(fh.read().count("muninn:protocol:begin"), 1)

    def test_install_copy_preserves_user_prose_outside_markers(self):
        proj = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, proj, ignore_errors=True)
        with open(os.path.join(proj, "CLAUDE.md"), "w") as fh:
            fh.write("my own claude notes\n")
        _run_cli("--root", self.root, "install", proj, "--copy")
        with open(os.path.join(proj, "CLAUDE.md")) as fh:
            text = fh.read()
        self.assertIn("my own claude notes", text)      # prose kept
        self.assertIn("muninn:protocol:begin", text)    # block appended

    def test_doctor_reports_healthy_and_drifted_wiring(self):
        proj = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, proj, ignore_errors=True)
        _run_cli("--root", self.root, "install", proj)
        out, _ = _run_cli("--root", self.root, "doctor", proj)
        self.assertIn("AGENTS.md: protocol present", out)
        self.assertIn("CLAUDE.md: symlink -> AGENTS.md", out)
        self.assertIn("entry-point wiring: OK", out)
        # drift: someone replaces the symlink with an unwired real file
        os.remove(os.path.join(proj, "GEMINI.md"))
        with open(os.path.join(proj, "GEMINI.md"), "w") as fh:
            fh.write("stale hand-rolled gemini file\n")
        with self.assertRaises(SystemExit) as cm:
            _run_cli("--root", self.root, "doctor", proj)
        self.assertEqual(cm.exception.code, 1)

    def _home(self):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        canonical = os.path.join(home, "agents", "AGENTS.md")
        os.makedirs(os.path.dirname(canonical))
        with open(canonical, "w") as fh:
            fh.write("# House rules: terse replies, never skip tasks.\n")
        return home, canonical

    def test_install_global_wires_every_harness_slot(self):
        home, canonical = self._home()
        with mock.patch.dict(os.environ, {"HOME": home}):
            out, _ = _run_cli("--root", self.root, "install", "--global",
                              "--from", canonical)
        for slot in (".claude/CLAUDE.md", ".codex/AGENTS.md",
                     ".gemini/GEMINI.md", ".grok/AGENTS.md"):
            p = os.path.join(home, slot)
            self.assertTrue(os.path.islink(p), slot)
            self.assertEqual(os.readlink(p), canonical)
        self.assertIn("global slots wired", out)

    def test_install_global_never_clobbers_a_real_global_file(self):
        home, canonical = self._home()
        real = os.path.join(home, ".claude")
        os.makedirs(real)
        with open(os.path.join(real, "CLAUDE.md"), "w") as fh:
            fh.write("my precious hand-written global config\n")
        output = io.StringIO()
        with (mock.patch.dict(os.environ, {"HOME": home}),
              redirect_stdout(output), self.assertRaises(SystemExit) as cm):
            cli.main(["--root", self.root, "install", "--global",
                      "--from", canonical])
        self.assertEqual(cm.exception.code, 1)
        with open(os.path.join(real, "CLAUDE.md")) as fh:
            self.assertEqual(fh.read(),
                             "my precious hand-written global config\n")
        self.assertIn("left as-is", output.getvalue())

    def test_doctor_global_flags_a_broken_slot(self):
        home, canonical = self._home()
        with mock.patch.dict(os.environ, {"HOME": home}):
            _run_cli("--root", self.root, "install", "--global",
                     "--from", canonical)
            out, _ = _run_cli("--root", self.root, "doctor", "--global",
                              "--from", canonical)
            self.assertIn("global wiring: OK", out)
            gm = os.path.join(home, ".gemini", "GEMINI.md")
            os.remove(gm)
            with open(gm, "w") as fh:
                fh.write("stale unrelated file\n")
            with self.assertRaises(SystemExit) as cm:
                _run_cli("--root", self.root, "doctor", "--global",
                         "--from", canonical)
            self.assertEqual(cm.exception.code, 1)

    def test_install_global_missing_canonical_fails_with_hint(self):
        home, _ = self._home()
        with mock.patch.dict(os.environ, {"HOME": home}):
            with self.assertRaises(SystemExit):
                _run_cli("--root", self.root, "install", "--global",
                         "--from", os.path.join(home, "nope.md"))
