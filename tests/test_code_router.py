from __future__ import annotations

import pytest

from muninn.code_router import (
    current_protected_hybrid_ranking,
    dependency_direction,
    query_anchor_paths,
    query_route,
    routed_dependency_ranking,
    routed_native_hybrid_ranking,
    routed_seeded_dependency_ranking,
    selected_base_hybrid_ranking,
    trace_hybrid_ranking,
)
from muninn.code_search import CodeSearchIndex, SourceRecord
from muninn.persistent_code_search import PersistentCodeSearchIndex


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ({"changed_file": "src/a.py"}, "code"),
        ({"review_comment": "check", "path": "src/a.py"}, "comment"),
        ({"command": "test", "failure_excerpt": "failed"}, "trace"),
        ({"anchor_file": "src/a.py", "anchor_diff": "@@"}, "ripple"),
        ({"question": "where"}, "general"),
    ],
)
def test_query_route_uses_observable_schema(query, expected) -> None:
    assert query_route(query) == expected


def test_query_anchor_paths_combines_fields_and_literal_mentions() -> None:
    query = {
        "given_file": "tests/test_service.py",
        "failure_excerpt": "src/service.py:17 failed",
    }
    assert query_anchor_paths(
        query,
        {"src/service.py", "src/store.py", "tests/test_service.py"},
    ) == ["tests/test_service.py", "src/service.py"]


def test_query_anchor_paths_reads_nested_sequences_and_normalizes_fields() -> None:
    available = {"src/a.py", "src/b.py", "tests/test_a.py"}
    assert query_anchor_paths(
        [{"message": "inspect src/a.py"}, ("then src/b.py", 7)],
        available,
    ) == ["src/a.py", "src/b.py"]
    assert query_anchor_paths(
        {
            "changed_files": ["src\\a.py", 7, "tests/test_a.py"],
            "metadata": {"note": "src/b.py"},
        },
        available,
    ) == ["src/a.py", "tests/test_a.py", "src/b.py"]


def test_routed_dependency_ranking_uses_route_direction() -> None:
    index = CodeSearchIndex([
        SourceRecord("src/service.py", "", imports=("store",)),
        SourceRecord("store.py", ""),
        SourceRecord("tests/test_service.py", "", imports=("src.service",)),
    ])
    route, anchors, ranking = routed_dependency_ranking(
        index,
        {"changed_file": "src/service.py"},
    )
    assert route == "code"
    assert anchors == ["src/service.py"]
    assert ranking == ["tests/test_service.py"]


def test_seeded_dependency_ranking_adds_retrieval_paths() -> None:
    index = CodeSearchIndex([
        SourceRecord("tests/test_a.py", "test a", imports=("src.a",)),
        SourceRecord("src/a.py", "def a(): pass"),
    ])

    route, anchors, ranking = routed_seeded_dependency_ranking(
        index,
        {"command": "pytest", "failure_excerpt": "failed"},
        ["tests/test_a.py"],
    )

    assert route == "trace"
    assert anchors == ["tests/test_a.py"]
    assert ranking == ["src/a.py"]


def test_dependency_direction_rejects_unknown_route() -> None:
    with pytest.raises(ValueError, match="unknown code-query route"):
        dependency_direction("hidden-label")


def test_trace_hybrid_combines_native_relations_with_external_rankings() -> None:
    index = CodeSearchIndex([
        SourceRecord(
            "tests/test_recovery.py",
            "def test_recovery(): recover()",
            symbols=("test_recovery",),
            symbol_relations=(("test_recovery", "recover", "calls"),),
        ),
        SourceRecord("src/recovery.py", "def recover(): pass", symbols=("recover",)),
        SourceRecord("src/other.py", "def other(): pass", symbols=("other",)),
    ])
    available = [
        "README.md",
        "src/other.py",
        "tests/test_recovery.py",
        "src/recovery.py",
    ]

    ranking, components = trace_hybrid_ranking(
        index,
        "recover failed",
        available,
        [
            "tests/test_recovery.py",
            "src/other.py",
            "src/recovery.py",
            "README.md",
        ],
        [
            "src/recovery.py",
            "README.md",
            "src/other.py",
            "tests/test_recovery.py",
        ],
        seed_files=1,
    )

    assert set(ranking) == set(available)
    assert ranking[0] == "src/recovery.py"
    assert components["directory"][:2] == [
        "tests/test_recovery.py",
        "src/recovery.py",
    ]
    assert components["symbol"][0] == "src/recovery.py"


def test_trace_hybrid_requires_complete_external_rankings() -> None:
    index = CodeSearchIndex([SourceRecord("src/a.py", "")])

    with pytest.raises(ValueError, match="semantic ranking differs"):
        trace_hybrid_ranking(
            index,
            "failed",
            ["src/a.py", "README.md"],
            ["src/a.py"],
            ["src/a.py", "README.md"],
        )


@pytest.mark.parametrize(
    ("available", "seed_files", "message"),
    [
        (["src/a.py", "src/a.py"], 1, "duplicate paths"),
        (["src/a.py"], 0, "seed files must be positive"),
    ],
)
def test_trace_hybrid_rejects_invalid_corpus_controls(
    available: list[str], seed_files: int, message: str
) -> None:
    index = CodeSearchIndex([SourceRecord("src/a.py", "")])
    with pytest.raises(ValueError, match=message):
        trace_hybrid_ranking(
            index,
            "a failed",
            available,
            available,
            available,
            seed_files=seed_files,
        )


def test_trace_hybrid_matches_persistent_native_relations(tmp_path) -> None:
    records = [
        SourceRecord(
            "tests/test_recovery.py",
            "def test_recovery(): recover()",
            symbols=("test_recovery",),
            symbol_relations=(("test_recovery", "recover", "calls"),),
        ),
        SourceRecord(
            "src/recovery.py", "def recover(): pass", symbols=("recover",)
        ),
        SourceRecord("src/other.py", "def other(): pass", symbols=("other",)),
    ]
    memory = CodeSearchIndex(records)
    persistent = PersistentCodeSearchIndex.build_records(
        records, tmp_path / "code.sqlite"
    )
    available = [
        "README.md",
        "src/other.py",
        "tests/test_recovery.py",
        "src/recovery.py",
    ]
    semantic = [
        "tests/test_recovery.py",
        "src/other.py",
        "src/recovery.py",
        "README.md",
    ]
    repo_map = [
        "src/recovery.py",
        "README.md",
        "src/other.py",
        "tests/test_recovery.py",
    ]
    try:
        expected = trace_hybrid_ranking(
            memory,
            "recover failed",
            available,
            semantic,
            repo_map,
            seed_files=1,
        )
        actual = trace_hybrid_ranking(
            persistent,
            "recover failed",
            available,
            semantic,
            repo_map,
            seed_files=1,
        )
        assert actual == expected
    finally:
        persistent.close()


def test_routed_native_hybrid_retains_base_for_non_trace_query() -> None:
    available = ["src/a.py", "tests/test_a.py"]
    route, ranking, components, error = routed_native_hybrid_ranking(
        object(),
        {"changed_file": "src/a.py"},
        "verify a",
        available,
        ["tests/test_a.py", "src/a.py"],
        available,
    )

    assert route == "code"
    assert ranking == ["tests/test_a.py", "src/a.py"]
    assert components["current"] == available
    assert error is None


def test_routed_native_hybrid_fails_soft_on_missing_trace_component() -> None:
    records = [
        SourceRecord("src/a.py", "def a(): pass", symbols=("a",)),
        SourceRecord("src/b.py", "def b(): pass", symbols=("b",)),
    ]
    index = CodeSearchIndex(records)
    current = ["src/b.py", "src/a.py"]
    route, ranking, components, error = routed_native_hybrid_ranking(
        index,
        {"command": "pytest", "failure_excerpt": "a failed"},
        "pytest a failed",
        [record.path for record in records],
        ["src/a.py", "src/b.py"],
        current,
    )

    assert route == "trace"
    assert ranking == current
    assert components == {"current": current}
    assert error == "ValueError: semantic ranking is absent"


def test_routed_native_hybrid_covers_general_and_trace_boundaries() -> None:
    records = [
        SourceRecord("src/a.py", "def a(): pass", symbols=("a",)),
        SourceRecord("src/b.py", "def b(): pass", symbols=("b",)),
    ]
    index = CodeSearchIndex(records)
    available = [record.path for record in records]
    current = ["src/b.py", "src/a.py"]

    general = routed_native_hybrid_ranking(
        index,
        {"question": "where is a"},
        "where is a",
        available,
        available,
        current,
    )
    assert general == (
        "general",
        current,
        {"current": current},
        None,
    )

    missing_map = routed_native_hybrid_ranking(
        index,
        {"command": "pytest", "failure_excerpt": "a failed"},
        "pytest a failed",
        available,
        available,
        current,
        semantic_ranking=available,
    )
    assert missing_map[1] == current
    assert missing_map[3] == "ValueError: repository-map ranking is absent"

    successful = routed_native_hybrid_ranking(
        index,
        {"command": "pytest", "failure_excerpt": "a failed"},
        "pytest a failed",
        available,
        available,
        current,
        semantic_ranking=available,
        repo_map_ranking=current,
    )
    assert successful[0] == "trace"
    assert successful[1] == available
    assert set(successful[2]) == {
        "base",
        "current",
        "semantic",
        "directory",
        "repo_map",
        "symbol",
    }
    assert successful[3] is None


def test_routed_native_hybrid_rejects_duplicate_corpus_paths() -> None:
    with pytest.raises(ValueError, match="duplicate paths"):
        routed_native_hybrid_ranking(
            object(),
            {"question": "where"},
            "where",
            ["src/a.py", "src/a.py"],
            ["src/a.py", "src/a.py"],
            ["src/a.py", "src/a.py"],
        )


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ({"changed_file": "src/a.py"}, ["tests/test_a.py", "src/a.py"]),
        (
            {"command": "pytest", "failure_excerpt": "a failed"},
            ["tests/test_a.py", "src/a.py"],
        ),
        (
            {"review_comment": "check a", "path": "src/a.py"},
            ["src/a.py", "tests/test_a.py"],
        ),
        (
            {"anchor_file": "src/a.py", "anchor_diff": "@@"},
            ["src/a.py", "tests/test_a.py"],
        ),
        ({"question": "where is a"}, ["src/a.py", "tests/test_a.py"]),
    ],
)
def test_current_protected_hybrid_uses_only_code_and_trace(
    query: dict[str, str], expected: list[str]
) -> None:
    available = ["src/a.py", "tests/test_a.py"]
    route, ranking, components, error = current_protected_hybrid_ranking(
        query,
        available,
        ["tests/test_a.py", "src/a.py"],
        ["src/a.py", "tests/test_a.py"],
    )

    assert route == query_route(query)
    assert ranking == expected
    assert set(components) == (
        {"current", "native"} if route in {"code", "trace"} else {"current"}
    )
    assert error is None


def test_current_protected_hybrid_fails_soft_on_invalid_native() -> None:
    current = ["src/a.py", "tests/test_a.py"]
    route, ranking, components, error = current_protected_hybrid_ranking(
        {"changed_file": "src/a.py"},
        current,
        ["src/a.py"],
        current,
    )

    assert route == "code"
    assert ranking == current
    assert components == {"current": current}
    assert error == "ValueError: native ranking differs from the corpus"


def test_current_protected_hybrid_handles_missing_and_duplicate_inputs() -> None:
    current = ["src/a.py", "tests/test_a.py"]
    route, ranking, components, error = current_protected_hybrid_ranking(
        {"changed_file": "src/a.py"},
        current,
        None,
        current,
    )
    assert route == "code"
    assert ranking == current
    assert components == {"current": current}
    assert error == "ValueError: native ranking is absent"

    with pytest.raises(ValueError, match="duplicate paths"):
        current_protected_hybrid_ranking(
            {"question": "where"},
            ["src/a.py", "src/a.py"],
            None,
            ["src/a.py", "src/a.py"],
        )


def test_current_protected_hybrid_fails_soft_on_native_runtime_error() -> None:
    def broken_ranking():
        raise RuntimeError("index unavailable")
        yield "src/a.py"

    current = ["src/a.py", "tests/test_a.py"]
    route, ranking, components, error = current_protected_hybrid_ranking(
        {"changed_file": "src/a.py"},
        current,
        broken_ranking(),
        current,
    )

    assert route == "code"
    assert ranking == current
    assert components == {"current": current}
    assert error == "RuntimeError: index unavailable"


@pytest.mark.parametrize(
    ("route", "expected_prefix"),
    [
        ("code", ["tests/test_a.py", "src/a.py", "README.md"]),
        ("comment", ["src/a.py", "README.md"]),
        ("ripple", ["src/b.py", "src/a.py", "README.md"]),
        ("general", ["src/b.py", "README.md", "src/a.py"]),
    ],
)
def test_selected_base_hybrid_preserves_route_prefixes(
    route: str, expected_prefix: list[str]
) -> None:
    available = [
        "README.md",
        "src/a.py",
        "src/b.py",
        "tests/test_a.py",
    ]
    ranking, components = selected_base_hybrid_ranking(
        route,
        available,
        ["src/b.py", "README.md", "src/a.py", "tests/test_a.py"],
        ["src/a.py", "src/b.py", "README.md", "tests/test_a.py"],
        ["src/a.py", "README.md", "src/b.py", "tests/test_a.py"],
        ["tests/test_a.py", "src/a.py", "src/b.py", "README.md"],
        ["src/a.py", "README.md", "tests/test_a.py", "src/b.py"],
    )

    assert ranking[: len(expected_prefix)] == expected_prefix
    assert set(ranking) == set(available)
    assert set(components) == {
        "current",
        "repo_map",
        "reranker",
        "semantic",
        "structural",
    }


def test_selected_base_hybrid_rejects_invalid_route_and_duplicate_corpus() -> None:
    ranking = ["src/a.py"]
    with pytest.raises(ValueError, match="unknown code-query route"):
        selected_base_hybrid_ranking(
            "hidden",
            ranking,
            ranking,
            ranking,
            ranking,
            ranking,
            ranking,
        )

    duplicate = ["src/a.py", "src/a.py"]
    with pytest.raises(ValueError, match="duplicate paths"):
        selected_base_hybrid_ranking(
            "code",
            duplicate,
            duplicate,
            duplicate,
            duplicate,
            duplicate,
            duplicate,
        )
