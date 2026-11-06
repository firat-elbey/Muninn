from __future__ import annotations

import json
from pathlib import Path

import pytest

from muninn import code_relations
from muninn.code_memory import learn_code_session
from muninn.code_relations import (
    CodeRelationConfig,
    CodeRelationEvent,
    CodeRelationMemory,
    apply_code_relations,
    observe_code_use,
    path_features,
)
from muninn.dynamics import Dynamics


def test_path_features_normalize_test_counterparts() -> None:
    assert path_features(
        "tests/unit/test_recovery.py",
        ("exact", "directory", "stem"),
    ) == (
        "exact:tests/unit/test_recovery.py",
        "directory:tests/unit",
        "stem:recovery",
    )


def test_relation_memory_changes_only_after_observed_use() -> None:
    memory = CodeRelationMemory()
    config = CodeRelationConfig(
        features=("exact", "directory", "stem"),
        weight=0.5,
        candidate_pool=50,
    )
    static = [
        "src/other.py",
        "tests/test_other.py",
        "src/recovery.py",
        "tests/test_recovery.py",
    ]

    before = memory.rank(
        "example/repo", "trace", ["tests/test_recovery.py"], static,
        config=config,
    )
    assert before.paths == tuple(static)

    event = memory.observe(
        "example/repo",
        "trace",
        "session-1",
        ["tests/test_recovery.py"],
        ["src/recovery.py"],
        features=config.features,
    )
    after = memory.rank(
        "example/repo", "trace", ["tests/test_recovery.py"], static,
        config=config,
    )

    assert event is not None
    assert after.changed is True
    assert after.paths.index("src/recovery.py") < static.index("src/recovery.py")


def test_relation_memory_isolates_repositories() -> None:
    memory = CodeRelationMemory()
    config = CodeRelationConfig()
    memory.observe(
        "one/repo", "code", "session-1", ["src/a.py"], ["tests/test_a.py"],
        features=config.features,
    )

    result = memory.rank(
        "two/repo",
        "code",
        ["src/a.py"],
        ["src/a.py", "other.py", "tests/test_a.py"],
        config=config,
    )

    assert result.changed is False


def test_relation_memory_replays_exactly() -> None:
    memory = CodeRelationMemory()
    config = CodeRelationConfig(features=("exact", "directory"))
    memory.observe(
        "one/repo", "code", "session-1", ["src/a.py"], ["tests/test_a.py"],
        features=config.features,
    )
    replay = CodeRelationMemory(memory.events)
    incremental = CodeRelationMemory()
    for event in memory.events:
        incremental.replay(event)

    assert replay.digest() == memory.digest()
    assert incremental.digest() == memory.digest()
    assert replay.rank(
        "one/repo",
        "code",
        ["src/a.py"],
        ["src/a.py", "other.py", "tests/test_a.py"],
        config=config,
    ) == memory.rank(
        "one/repo",
        "code",
        ["src/a.py"],
        ["src/a.py", "other.py", "tests/test_a.py"],
        config=config,
    )


def test_relation_config_rejects_unregistered_values() -> None:
    with pytest.raises(ValueError, match="feature set"):
        CodeRelationConfig(features=("directory",))
    with pytest.raises(ValueError, match="weight"):
        CodeRelationConfig(weight=0.75)
    with pytest.raises(ValueError, match="candidate pool"):
        CodeRelationConfig(candidate_pool=0)


def test_stored_relation_validation_rejects_each_malformed_field() -> None:
    valid = {
        "repository": "a" * 20,
        "route": "code",
        "session": "session-1",
        "anchors": ["exact:src/a.py"],
        "targets": ["exact:tests/test_a.py"],
    }
    invalid = [
        None,
        {**valid, "repository": "owner/repo"},
        {**valid, "route": "unknown"},
        {**valid, "session": ""},
        {**valid, "anchors": "exact:src/a.py"},
        {**valid, "anchors": ["exact:src/a.py", "exact:src/a.py"]},
        {**valid, "anchors": [None]},
        {**valid, "anchors": ["unknown:value"]},
        {**valid, "anchors": ["exact:../a.py"]},
        {**valid, "anchors": ["stem:a/b"]},
        {**valid, "anchors": ["stem:" + "x" * 501]},
    ]

    for value in invalid:
        with pytest.raises(ValueError):
            code_relations._stored_event(value, 1)


def test_append_rejects_invalid_in_memory_events() -> None:
    valid = CodeRelationEvent(
        sequence=1,
        repository="a" * 20,
        route="code",
        session="session-1",
        anchors=("exact:src/a.py",),
        targets=("exact:tests/test_a.py",),
    )
    invalid = [
        CodeRelationEvent(**{**valid.__dict__, "sequence": 2}),
        CodeRelationEvent(**{**valid.__dict__, "repository": "bad"}),
        CodeRelationEvent(**{**valid.__dict__, "route": "bad"}),
        CodeRelationEvent(**{**valid.__dict__, "session": ""}),
        CodeRelationEvent(**{**valid.__dict__, "anchors": ()}),
        CodeRelationEvent(
            **{
                **valid.__dict__,
                "anchors": ("exact:src/a.py", "exact:src/a.py"),
            }
        ),
    ]

    for event in invalid:
        with pytest.raises(ValueError):
            CodeRelationMemory().replay(event)


def test_relation_memory_handles_absent_irrelevant_and_invalid_ledger(tmp_path) -> None:
    absent = CodeRelationMemory.from_ledger(tmp_path / "absent.jsonl")
    assert absent.events == []

    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        "not json\n"
        + json.dumps({"kind": "other"})
        + "\n"
        + json.dumps(
            {
                "kind": "code-relation",
                "repository": "bad",
                "route": "code",
                "session": "session-1",
                "anchors": ["exact:src/a.py"],
                "targets": ["exact:tests/test_a.py"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    memory = CodeRelationMemory.from_ledger(ledger)
    assert memory.events == []
    assert memory.load_failures == 1


def test_relation_empty_inputs_and_duplicate_rankings_are_bounded(tmp_path) -> None:
    memory = CodeRelationMemory()
    config = CodeRelationConfig()
    assert memory.observe(
        "owner/repo", "code", "session-1", [], ["src/a.py"],
        features=config.features,
    ) is None
    assert memory.rank("owner/repo", "code", [], [], config=config).paths == ()
    with pytest.raises(ValueError, match="duplicate"):
        memory.rank(
            "owner/repo",
            "code",
            [],
            ["src/a.py", "src/a.py"],
            config=config,
        )

    dynamics = Dynamics(str(tmp_path))
    (tmp_path / ".muninn").mkdir(exist_ok=True)
    Path(dynamics.ledger_path).write_text(
        json.dumps({"kind": "code-relation", "repository": "bad"}) + "\n",
        encoding="utf-8",
    )
    assert observe_code_use(
        dynamics,
        "owner/repo",
        "code",
        "session-1",
        ["src/a.py"],
        ["tests/test_a.py"],
        config=config,
    ) is None


def test_relations_persist_without_repository_or_query_text(tmp_path) -> None:
    dynamics = Dynamics(str(tmp_path))
    config = CodeRelationConfig(features=("exact", "directory"), weight=0.5)
    static = ["src/a.py", "src/other.py", "tests/test_a.py"]

    learn_code_session(
        dynamics,
        "https://user:secret@example.com/Owner/Repo.git",
        "session-1",
        "private query text",
        static[:1],
        ["tests/test_a.py"],
        relation_anchor_paths=["src/a.py"],
        relation_route="code",
        relation_config=config,
    )
    result = apply_code_relations(
        dynamics,
        "example.com/owner/repo",
        "code",
        ["src/a.py"],
        static,
        config=config,
    )

    ledger = tmp_path / ".muninn" / "ledger.jsonl"
    text = ledger.read_text(encoding="utf-8")
    events = [json.loads(line) for line in text.splitlines()]
    relation = next(row for row in events if row["kind"] == "code-relation")
    assert result.failure is None
    assert result.paths.index("tests/test_a.py") < static.index(
        "tests/test_a.py"
    )
    assert relation["repository"] == "438c78e3534a0aede20a"
    assert relation["anchors"] == ["exact:src/a.py", "directory:src"]
    relation_text = json.dumps(relation, sort_keys=True)
    assert "private query text" not in relation_text
    assert "user:secret" not in relation_text


def test_relations_replay_after_ledger_reordering(tmp_path) -> None:
    dynamics = Dynamics(str(tmp_path))
    config = CodeRelationConfig(weight=0.5)
    for number, target in enumerate(("tests/test_a.py", "tests/test_b.py"), 1):
        learn_code_session(
            dynamics,
            "owner/repo",
            f"session-{number}",
            "query",
            [],
            [target],
            relation_anchor_paths=["src/a.py"],
            relation_route="code",
            relation_config=config,
        )
    before = CodeRelationMemory.from_ledger(dynamics.ledger_path)
    ledger = tmp_path / ".muninn" / "ledger.jsonl"
    rows = ledger.read_text(encoding="utf-8").splitlines()
    ledger.write_text("\n".join(reversed(rows)) + "\n", encoding="utf-8")
    after = CodeRelationMemory.from_ledger(ledger)

    assert len(before.events) == 2
    assert len(after.events) == 2
    assert {event.targets for event in before.events} == {
        event.targets for event in after.events
    }


def test_malformed_relation_state_returns_static_ranking(tmp_path) -> None:
    dynamics = Dynamics(str(tmp_path))
    ledger = tmp_path / ".muninn" / "ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        json.dumps({
            "kind": "code-relation",
            "repository": "not-a-hash",
            "route": "code",
            "session": "session-1",
            "anchors": ["exact:src/a.py"],
            "targets": ["exact:tests/test_a.py"],
            "ts": 1,
        }) + "\n",
        encoding="utf-8",
    )
    static = ["src/a.py", "src/other.py", "tests/test_a.py"]

    result = apply_code_relations(
        dynamics,
        "owner/repo",
        "code",
        ["src/a.py"],
        static,
        config=CodeRelationConfig(),
    )

    assert result.paths == tuple(static)
    assert result.changed is False
    assert result.failure == "ValueError: 1 malformed code relation events"
