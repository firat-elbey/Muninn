"""Keep explicit parser-backed source modes separate from lexical retrieval."""

import sys
from importlib import metadata
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from muninn import code_search, extract
from muninn import source_retrieval as retrieval

PARSER_PACKAGES = (
    "tree-sitter",
    "tree-sitter-language-pack",
    "tree-sitter-c-sharp",
    "tree-sitter-embedded-template",
    "tree-sitter-yaml",
)


@pytest.mark.parametrize("mode", ["overlap", "bm25f", "lexical-hybrid", "hybrid", "graph-fusion"])
def test_lexical_modes_keep_default_profile_without_probing_parsers(tmp_path, monkeypatch, mode):
    source = tmp_path / "source"
    source.mkdir()
    (source / "service.py").write_text("def core_definition(): pass\n", encoding="utf-8")

    def unexpected_probe(*args):
        raise AssertionError("Lexical retrieval must not inspect parser capability or versions.")

    monkeypatch.setattr(extract, "parser_issues", unexpected_probe)
    monkeypatch.setattr(metadata, "version", unexpected_probe)
    first = retrieval.search_source(tmp_path / "brain", source, "core_definition", mode=mode)
    assert first.database == retrieval.source_database(tmp_path / "brain", source)
    assert first.rebuilt
    assert [hit.path for hit in first.hits] == ["service.py"]
    second = retrieval.search_source(tmp_path / "brain", source, "core_definition", mode=mode)
    assert not second.rebuilt
    assert second.hits == first.hits


@pytest.mark.skipif(not extract.available(), reason="Syntax parsers are unavailable")
@pytest.mark.parametrize("mode", ["symbol", "structural-fusion"])
def test_explicit_modes_use_real_parsers_without_changing_default(tmp_path, monkeypatch, mode):
    source = tmp_path / "source"
    source.mkdir()
    fixtures = {
        "service.py": "def restore_snapshot(value):\n    return value\n",
        "service.ts": "export function restoreSnapshot(value: number): number { return value; }\n",
        "service.js": "export function restoreSnapshot(value) { return value; }\n",
        "service.cs": "public class Service { public int RestoreSnapshot(int value) { return value; } }\n",
        "service.go": "package service\nfunc RestoreSnapshot(value int) int { return value }\n",
    }
    for name, text in fixtures.items():
        (source / name).write_text(text, encoding="utf-8")
    brain = tmp_path / "brain"
    baseline = retrieval.search_source(brain, source, "restoreSnapshot")
    default_database = Path(baseline.database)
    before = (default_database.read_bytes(), default_database.stat().st_mtime_ns)

    result = retrieval.search_source(brain, source, "restoreSnapshot", mode=mode)
    parser_hits = {hit.path for hit in result.hits if "symbol" in dict(hit.arms)}
    assert {"service.ts", "service.js", "service.cs", "service.go"} <= parser_hits
    assert result.database != baseline.database
    assert result.rebuilt
    for excerpt in result.pack.excerpts:
        lines = fixtures[excerpt.path].splitlines()
        assert excerpt.text == "\n".join(lines[excerpt.start_line - 1:excerpt.end_line])

    repeated = retrieval.search_source(brain, source, "restoreSnapshot", mode=mode, refresh=False)
    assert repeated.database == result.database
    assert not repeated.rebuilt
    assert repeated.hits == result.hits

    def unexpected_probe():
        raise AssertionError("Default retrieval must not inspect parser capability.")

    monkeypatch.setattr(extract, "parser_issues", unexpected_probe)
    after = retrieval.search_source(brain, source, "restoreSnapshot")
    assert after.database == retrieval.source_database(brain, source)
    assert after.hits == baseline.hits
    assert not after.rebuilt
    assert (default_database.read_bytes(), default_database.stat().st_mtime_ns) == before


def test_capability_changes_rebuild_explicit_cache_even_without_refresh(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "service.ts").write_text("function ParsedDefinition() {}\n", encoding="utf-8")
    (source / "service.py").write_text("def core_definition(): pass\n", encoding="utf-8")
    brain = tmp_path / "brain"
    state = {"ready": False, "versions": {name: "1.0" for name in PARSER_PACKAGES}}
    monkeypatch.setattr(extract, "parser_issues", lambda: [] if state["ready"] else ["Unavailable."])
    monkeypatch.setattr(metadata, "version", lambda name: state["versions"][name])

    def structural_record(path, text, filesystem_path):
        assert state["ready"], "An unavailable parser must use the standard-library fallback."
        if path.endswith(".ts"):
            return code_search.SourceRecord(path, text, symbols=("ParsedDefinition",))
        return code_search.source_record(path, text)

    monkeypatch.setattr(code_search, "structural_source_record", structural_record)
    core = retrieval.search_source(brain, source, "ParsedDefinition", mode="symbol", refresh=False)
    assert not core.hits
    fallback = retrieval.search_source(brain, source, "core_definition", mode="symbol", refresh=False)
    assert [hit.path for hit in fallback.hits] == ["service.py"]

    state["ready"] = True
    parsed = retrieval.search_source(brain, source, "ParsedDefinition", mode="symbol", refresh=False)
    assert [hit.path for hit in parsed.hits] == ["service.ts"]
    assert parsed.database != core.database
    assert parsed.rebuilt
    reused = retrieval.search_source(brain, source, "ParsedDefinition", mode="structural-fusion", refresh=False)
    assert reused.database == parsed.database
    assert not reused.rebuilt

    for name in PARSER_PACKAGES:
        previous = parsed
        state["versions"][name] = "1.1"
        parsed = retrieval.search_source(brain, source, "ParsedDefinition", mode="symbol", refresh=False)
        assert parsed.database != previous.database
        assert parsed.rebuilt
        assert [hit.path for hit in parsed.hits] == ["service.ts"]

    state["ready"] = False
    unavailable = retrieval.search_source(brain, source, "ParsedDefinition", mode="symbol", refresh=False)
    assert unavailable.database != parsed.database
    assert not unavailable.hits
    assert unavailable.rebuilt


def test_missing_parser_distribution_metadata_has_a_stable_profile(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "service.py").write_text("def core_definition(): pass\n", encoding="utf-8")
    monkeypatch.setattr(extract, "parser_issues", lambda: ["Unavailable."])

    def missing_version(name):
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, "version", missing_version)
    first = retrieval.search_source(tmp_path / "brain", source, "core_definition", mode="symbol")
    second = retrieval.search_source(tmp_path / "brain", source, "core_definition", mode="symbol")
    assert first.database == second.database
    assert first.hits == second.hits
    assert first.rebuilt
    assert not second.rebuilt


@pytest.mark.skipif(not extract.available(), reason="Syntax parsers are unavailable")
def test_structural_profile_still_refreshes_changed_source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    fixture = source / "service.ts"
    fixture.write_text("function originalDefinition() {}\n", encoding="utf-8")
    first = retrieval.search_source(tmp_path / "brain", source, "originalDefinition", mode="symbol")
    assert [hit.path for hit in first.hits] == ["service.ts"]

    fixture.write_text("function replacementDefinition() {}\n", encoding="utf-8")
    stale = retrieval.search_source(
        tmp_path / "brain", source, "replacementDefinition", mode="symbol", refresh=False
    )
    assert not stale.rebuilt
    assert not stale.hits
    fresh = retrieval.search_source(tmp_path / "brain", source, "replacementDefinition", mode="symbol")
    assert fresh.rebuilt
    assert fresh.database == first.database
    assert [hit.path for hit in fresh.hits] == ["service.ts"]
    removed = retrieval.search_source(tmp_path / "brain", source, "originalDefinition", mode="symbol")
    assert not removed.rebuilt
    assert not removed.hits
