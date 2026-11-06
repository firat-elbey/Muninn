"""Verify bounded, source-backed memory excerpts and delivered-note accounting."""

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from muninn import cli
from muninn.dynamics import Dynamics
from muninn.observe import hook_config
from muninn.recall import _body_blocks, context_pack, estimate_tokens
from muninn.store import Bundle


@pytest.mark.parametrize("budget", [1, 10, 40, 80, 140, 200])
@pytest.mark.parametrize("mode", ["muninn", "flat", "dump"])
def test_pack_never_exceeds_total_budget(tmp_path, budget, mode):
    bundle = Bundle(str(tmp_path))
    for i in range(8):
        bundle.write_note(f"note{i}.md", {"title": "Long title " * 20 + str(i)},
                          "cache configuration\n" * 200)
    text = context_pack(bundle, None, "cache " * 400, budget=budget, mode=mode)
    assert len(text) <= budget * 4


def test_only_rendered_notes_receive_recall_events(tmp_path):
    bundle = Bundle(str(tmp_path))
    for i in range(8):
        bundle.write_note(f"n{i}.md", {"title": f"Cache {i}"},
                          "cache settings " * 300)
    dynamics = Dynamics(str(tmp_path))
    text = context_pack(bundle, dynamics, "cache", budget=140, k=8, index=False)
    with open(dynamics.ledger_path, encoding="utf-8") as source:
        served = [json.loads(line)["note"] for line in source
                  if json.loads(line)["kind"] == "recall"]
    assert served
    assert all(f"({path})" in text for path in served)
    assert len(served) == text.count("### ")


def test_compact_finds_late_evidence_and_preserves_qualification(tmp_path):
    bundle = Bundle(str(tmp_path))
    body = ("General architecture background.\n\n" * 30
            + "The archive retention is 42 days.\n"
            + "This rule excludes legal holds.\n\n"
            + "Unrelated deployment history.\n\n" * 30)
    bundle.write_note("archive.md", {"title": "Archive policy"}, body)
    text = context_pack(bundle, None, "archive retention", budget=200, compact=True)
    assert "The archive retention is 42 days." in text
    assert "This rule excludes legal holds." in text
    source_lines = bundle.notes["archive.md"].body.splitlines()
    start = source_lines.index("The archive retention is 42 days.") + 1
    assert f"Body lines {start - 2}-{start + 3}:" in text
    assert "archive.md" in text
    assert "Compact index" not in text
    assert estimate_tokens(text) <= 200
    assert text.count("Unrelated deployment history.") <= 1


def test_compact_preserves_fences_as_a_complete_block(tmp_path):
    bundle = Bundle(str(tmp_path))
    bundle.write_note("command.md", {"title": "Backup command"},
                      "Background.\n\n```sh\nbackup --verify\n\n# Keep this option.\n```\n")
    text = context_pack(bundle, None, "backup verify", budget=200, compact=True)
    assert "```sh\nbackup --verify\n\n# Keep this option.\n```" in text


def test_fence_like_code_with_trailing_text_does_not_close_a_block():
    body = "```text\nbackup command\n```not-a-closing-fence\n\nKeep legal holds.\n```"
    assert _body_blocks(body) == [(1, 6, body)]


def test_compact_keeps_separate_adjacent_qualification(tmp_path):
    bundle = Bundle(str(tmp_path))
    bundle.write_note("archive.md", {"title": "Archive retention"},
                      "Background.\n\n" * 30 + "Archive retention is 42 days.\n\n"
                      "This rule excludes legal holds.\n\n" + "History.\n\n" * 30)
    text = context_pack(bundle, None, "archive retention", compact=True, budget=200)
    assert "42 days" in text
    assert "This rule excludes legal holds." in text


def test_compact_keeps_heading_scope_before_intervening_paragraphs(tmp_path):
    bundle = Bundle(str(tmp_path))
    body = ("## Proposed policy (not approved)\n\n"
            + "Historical background.\n\n" * 20
            + "Archive retention is 42 days.\n\n"
            + "Historical background.\n\n" * 20)
    bundle.write_note("proposal.md", {"title": "Archive retention"}, body)
    text = context_pack(bundle, None, "archive retention", compact=True, budget=400)
    assert "42 days" in text
    assert "Proposed policy (not approved)" in text


def test_compact_can_expand_for_a_large_complete_primary_block(tmp_path):
    bundle = Bundle(str(tmp_path))
    body = "Archive retention policy " + "detail " * 100 + "retains legal holds."
    bundle.write_note("archive.md", {"title": "Archive retention"}, body)
    text = context_pack(bundle, None, "archive retention", compact=True, budget=900)
    assert body in text


def test_compact_discloses_evidence_that_cannot_fit(tmp_path):
    bundle = Bundle(str(tmp_path))
    bundle.write_note("archive.md", {"title": "Archive retention"},
                      "Archive retention " + "detail " * 2000)
    text = context_pack(bundle, None, "archive retention", compact=True, budget=200)
    assert "archive.md" in text
    assert "No complete excerpt fits" in text


def test_compact_no_match_does_not_spend_tokens_on_unrelated_notes(tmp_path):
    bundle = Bundle(str(tmp_path))
    bundle.write_note("zebra.md", {"title": "Zebra"}, "Unrelated private body.")
    text = context_pack(bundle, None, "postgres retention", compact=True)
    assert "No matching notes" in text
    assert "Unrelated private body" not in text


def test_compact_retains_staleness_and_excludes_superseded_notes(tmp_path):
    bundle = Bundle(str(tmp_path))
    bundle.write_note("old.md", {"title": "Archive"}, "Archive retention is 7 days.")
    bundle.write_note("new.md", {"title": "Archive update", "supersedes": ["old.md"],
                                 "stale_after": "2000-01-01"},
                      "Archive retention is 42 days.")
    text = context_pack(bundle, None, "archive retention", compact=True)
    assert "42 days" in text
    assert "7 days" not in text
    assert "STALE" in text


def test_compact_cli_is_available(tmp_path):
    bundle = Bundle(str(tmp_path))
    bundle.write_note("cache.md", {"title": "Cache"}, "Cache entries expire after 5 minutes.")
    stream = io.StringIO()
    with redirect_stdout(stream):
        cli.main(["--root", str(tmp_path), "pack", "cache", "--compact", "--no-reactivate"])
    assert "5 minutes" in stream.getvalue()
    assert "Body lines" in stream.getvalue()


@pytest.mark.parametrize("compact", [False, True])
def test_primary_short_note_is_complete_before_secondary_context(tmp_path, compact):
    bundle = Bundle(str(tmp_path))
    body = "Cache recovery steps " + "detail " * 25 + "take exactly 37 minutes."
    bundle.write_note("primary.md", {"title": "Cache recovery steps"}, body)
    for i in range(5):
        bundle.write_note(f"secondary{i}.md", {"title": f"Recovery topic {i}"},
                          "Recovery background " * 20)
    text = context_pack(bundle, None, "cache recovery steps", budget=200,
                        compact=compact, index=False)
    assert body in text
    assert len(text) <= 800


def test_session_end_reviews_the_reported_session(tmp_path):
    bundle = Bundle(str(tmp_path))
    bundle.write_note("cache.md", {"title": "Cache"}, "Cache configuration.")
    dyn = Dynamics(str(tmp_path))
    dyn.session_begin("first", cue="cache")
    dyn.touch("cache.md", session="first")
    dyn.session_begin("second", cue="cache")
    dyn.touch("cache.md", session="second")
    with mock.patch("muninn.cli.stdin_text", return_value='{"session_id":"first"}'):
        cli.main(["--root", str(tmp_path), "hook", "session-end"])
    reviewed = Dynamics(str(tmp_path)).reviewed
    assert "first" in reviewed
    assert "second" not in reviewed


def test_session_end_respects_review_opt_out(tmp_path, monkeypatch):
    monkeypatch.setenv("MUNINN_NO_REVIEW", "1")
    with mock.patch("muninn.cli.stdin_text", return_value='{"session_id":"first"}'), \
            mock.patch("muninn.cli.review_session") as review:
        cli.main(["--root", str(tmp_path), "hook", "session-end"])
    review.assert_not_called()


def test_codex_hook_context_settings_belong_to_command_handler(tmp_path):
    configuration = json.loads(hook_config(str(tmp_path), adapter="codex"))
    group = configuration["hooks"]["SessionStart"][0]
    assert "additionalContextLimit" not in group
    assert "statusMessage" not in group
    handler = group["hooks"][0]
    assert handler["additionalContextLimit"] == 2500
    assert handler["statusMessage"] == "Loading Muninn context"
