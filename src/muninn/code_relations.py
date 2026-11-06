"""Bounded repository relations learned only from observed file use."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .code_memory import normalize_code_path, normalize_repository
from .lexical import protected_rank_fusion

FEATURE_SETS = (
    ("exact",),
    ("exact", "directory"),
    ("exact", "directory", "stem"),
)
RELATION_SATURATION = 3
RANK_CONSTANT = 60
LEDGER_KIND = "code-relation"
REPOSITORY_HASH = re.compile(r"[0-9a-f]{20}")
RELATION_ROUTES = frozenset({"code", "comment", "trace", "ripple", "general"})


@dataclass(frozen=True)
class CodeRelationConfig:
    """One bounded relational reordering configuration."""

    features: tuple[str, ...] = ("exact",)
    weight: float = 0.25
    candidate_pool: int = 50

    def __post_init__(self) -> None:
        if self.features not in FEATURE_SETS:
            raise ValueError("unknown code relation feature set")
        if not 0.0 < self.weight <= 0.5:
            raise ValueError("code relation weight must be in (0, 0.5]")
        if self.candidate_pool < 1:
            raise ValueError("code relation candidate pool must be positive")


@dataclass(frozen=True)
class CodeRelationEvent:
    """One append-only relation update derived from independent file use."""

    sequence: int
    repository: str
    route: str
    session: str
    anchors: tuple[str, ...]
    targets: tuple[str, ...]


@dataclass(frozen=True)
class RelationalCodeRanking:
    """One reordered file ranking with bounded relation evidence."""

    paths: tuple[str, ...]
    relation_paths: tuple[str, ...]
    relation_scores: tuple[tuple[str, float], ...]
    changed: bool
    admitted_at_20: tuple[str, ...]
    failure: str | None = None


def _repository_hash(repository: str) -> str:
    return hashlib.sha256(normalize_repository(repository).encode()).hexdigest()[:20]


def _normalized_stem(path: str) -> str:
    stem = posixpath.splitext(posixpath.basename(path))[0].casefold()
    stem = re.sub(r"^(?:test|tests)[_.-]+", "", stem)
    return re.sub(r"[_.-]+(?:test|tests|spec)$", "", stem)


def path_features(path: str, features: Sequence[str]) -> tuple[str, ...]:
    """Return namespaced, repository-relative hierarchy features."""
    normalized = normalize_code_path(path)
    result = []
    if "exact" in features:
        result.append(f"exact:{normalized}")
    if "directory" in features:
        directory = posixpath.dirname(normalized)
        if directory and directory != ".":
            result.append(f"directory:{directory}")
    if "stem" in features:
        stem = _normalized_stem(normalized)
        if stem:
            result.append(f"stem:{stem}")
    return tuple(result)


def _validate_stored_feature(value: object) -> str:
    if not isinstance(value, str) or len(value) > 500:
        raise ValueError("code relation feature is invalid")
    kind, separator, payload = value.partition(":")
    if not separator or kind not in FEATURE_SETS[-1] or not payload:
        raise ValueError("code relation feature is invalid")
    if kind in {"exact", "directory"}:
        if normalize_code_path(payload) != payload:
            raise ValueError("code relation path is not normalized")
    elif "/" in payload or "\\" in payload:
        raise ValueError("code relation stem is invalid")
    return value


def _stored_event(value: object, sequence: int) -> CodeRelationEvent:
    if not isinstance(value, dict):
        raise ValueError("code relation ledger event is not an object")
    repository = value.get("repository")
    route = value.get("route")
    session = value.get("session")
    anchors = value.get("anchors")
    targets = value.get("targets")
    if not isinstance(repository, str) or not REPOSITORY_HASH.fullmatch(
        repository
    ):
        raise ValueError("code relation repository identity is invalid")
    if route not in RELATION_ROUTES:
        raise ValueError("code relation route is invalid")
    if not isinstance(session, str) or not session or len(session) > 200:
        raise ValueError("code relation session is invalid")
    if not isinstance(anchors, list) or not isinstance(targets, list):
        raise ValueError("code relation features are invalid")
    if len(anchors) != len(set(anchors)) or len(targets) != len(set(targets)):
        raise ValueError("code relation features contain duplicates")
    return CodeRelationEvent(
        sequence=sequence,
        repository=repository,
        route=route,
        session=session,
        anchors=tuple(_validate_stored_feature(item) for item in anchors),
        targets=tuple(_validate_stored_feature(item) for item in targets),
    )


class CodeRelationMemory:
    """Replayable directional relations from query anchors to used files."""

    def __init__(self, events: Iterable[CodeRelationEvent] = ()):
        self.events: list[CodeRelationEvent] = []
        self._counts: dict[tuple[str, str, str, str], int] = defaultdict(int)
        self.load_failures = 0
        for event in events:
            self._append(event)

    @classmethod
    def from_ledger(cls, ledger_path: str | Path) -> "CodeRelationMemory":
        """Rebuild relation state from safe events in ledger order."""
        memory = cls()
        try:
            handle = Path(ledger_path).open(encoding="utf-8")
        except OSError:
            return memory
        with handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if (
                    not isinstance(value, dict)
                    or value.get("kind") != LEDGER_KIND
                ):
                    continue
                try:
                    memory.replay(_stored_event(value, len(memory.events) + 1))
                except (TypeError, ValueError):
                    memory.load_failures += 1
        return memory

    def _append(self, event: CodeRelationEvent) -> None:
        if event.sequence != len(self.events) + 1:
            raise ValueError("code relation event sequence is not contiguous")
        if not REPOSITORY_HASH.fullmatch(event.repository):
            raise ValueError("code relation event identity is incomplete")
        if event.route not in RELATION_ROUTES:
            raise ValueError("code relation event route is invalid")
        if not event.session or len(event.session) > 200:
            raise ValueError("code relation event session is invalid")
        if not event.anchors or not event.targets:
            raise ValueError("code relation event has no relation")
        if (
            len(event.anchors) != len(set(event.anchors))
            or len(event.targets) != len(set(event.targets))
        ):
            raise ValueError("code relation event has duplicate features")
        for feature in (*event.anchors, *event.targets):
            _validate_stored_feature(feature)
        self.events.append(event)
        for anchor in event.anchors:
            anchor_kind = anchor.partition(":")[0]
            for target in event.targets:
                if target.partition(":")[0] != anchor_kind:
                    continue
                key = (event.repository, event.route, anchor, target)
                self._counts[key] += 1

    def observe(
        self,
        repository: str,
        route: str,
        session: str,
        anchor_paths: Iterable[str],
        used_paths: Iterable[str],
        *,
        features: Sequence[str],
    ) -> CodeRelationEvent | None:
        """Record relations only after independently observed file use."""
        anchors = tuple(dict.fromkeys(
            feature
            for path in anchor_paths
            for feature in path_features(path, features)
        ))
        targets = tuple(dict.fromkeys(
            feature
            for path in used_paths
            for feature in path_features(path, features)
        ))
        if not anchors or not targets:
            return None
        event = CodeRelationEvent(
            sequence=len(self.events) + 1,
            repository=_repository_hash(repository),
            route=route,
            session=session,
            anchors=anchors,
            targets=targets,
        )
        self._append(event)
        return event

    def replay(self, event: CodeRelationEvent) -> None:
        """Apply one previously recorded event in contiguous sequence."""
        self._append(event)

    def rank(
        self,
        repository: str,
        route: str,
        anchor_paths: Iterable[str],
        static_ranking: Sequence[str],
        *,
        config: CodeRelationConfig,
    ) -> RelationalCodeRanking:
        """Fuse one learned relation arm into a complete static ranking."""
        static = list(static_ranking)
        if len(static) != len(set(static)):
            raise ValueError("static code ranking contains duplicate paths")
        if not static:
            return RelationalCodeRanking((), (), (), False, ())
        anchors = tuple(dict.fromkeys(
            feature
            for path in anchor_paths
            for feature in path_features(path, config.features)
        ))
        repository_key = _repository_hash(repository)
        scores = {}
        for path in static[:config.candidate_pool]:
            target_features = path_features(path, config.features)
            score = sum(
                min(
                    RELATION_SATURATION,
                    self._counts.get(
                        (repository_key, route, anchor, target),
                        0,
                    ),
                ) / RELATION_SATURATION
                for anchor in anchors
                for target in target_features
                if anchor.partition(":")[0] == target.partition(":")[0]
            )
            if score > 0:
                scores[path] = score
        static_ranks = {path: rank for rank, path in enumerate(static, 1)}
        relation = sorted(
            scores,
            key=lambda path: (-scores[path], static_ranks[path], path),
        )
        if not relation:
            return RelationalCodeRanking(tuple(static), (), (), False, ())
        fused = protected_rank_fusion(
            static,
            [static, relation],
            protected=1,
            rank_constant=RANK_CONSTANT,
            weights=[1.0, config.weight],
        )
        paths = tuple(path for path, _score in fused)
        admitted = tuple(
            path for path in paths[:20] if static_ranks[path] > 20
        )
        return RelationalCodeRanking(
            paths=paths,
            relation_paths=tuple(relation),
            relation_scores=tuple(
                (path, round(scores[path], 6)) for path in relation
            ),
            changed=paths != tuple(static),
            admitted_at_20=admitted,
        )

    def digest(self) -> str:
        """Return a stable digest of the append-only event history."""
        payload = json.dumps(
            [asdict(event) for event in self.events],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(payload).hexdigest()


def observe_code_use(
    dynamics,
    repository: str,
    route: str,
    session: str,
    anchor_paths: Iterable[str],
    used_paths: Iterable[str],
    *,
    config: CodeRelationConfig,
) -> CodeRelationEvent | None:
    """Append one relation event after independent file use."""
    memory = CodeRelationMemory.from_ledger(dynamics.ledger_path)
    if memory.load_failures:
        return None
    event = memory.observe(
        repository,
        route,
        session,
        anchor_paths,
        used_paths,
        features=config.features,
    )
    if event is not None:
        dynamics.code_relation_mark(event)
    return event


def apply_code_relations(
    dynamics,
    repository: str,
    route: str,
    anchor_paths: Iterable[str],
    static_ranking: Sequence[str],
    *,
    config: CodeRelationConfig,
) -> RelationalCodeRanking:
    """Apply replayed relations or return the unchanged static ranking."""
    static = tuple(static_ranking)
    try:
        memory = CodeRelationMemory.from_ledger(dynamics.ledger_path)
        if memory.load_failures:
            raise ValueError(
                f"{memory.load_failures} malformed code relation events"
            )
        return memory.rank(
            repository,
            route,
            anchor_paths,
            static,
            config=config,
        )
    except (OSError, TypeError, ValueError) as error:
        return RelationalCodeRanking(
            paths=static,
            relation_paths=(),
            relation_scores=(),
            changed=False,
            admitted_at_20=(),
            failure=f"{type(error).__name__}: {error}",
        )
