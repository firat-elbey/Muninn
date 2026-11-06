"""Bounded per-consumer reordering for shared source-file candidates."""

from __future__ import annotations

import hashlib
import posixpath
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Sequence
from urllib.parse import urlsplit

from .activate import tokens

if TYPE_CHECKING:
    from .dynamics import Dynamics


STRENGTH_WEIGHT = 0.2
ASSOCIATION_WEIGHT = 0.2
CO_USE_WEIGHT = 0.1
PERSONAL_FACTOR_CAP = 1.5
WASTE_COUNT = 3
WASTE_FACTOR = 0.8
ASSOCIATION_OVERLAP = 2
CO_USE_ANCHORS = 3
CO_USE_SATURATION = 3
DIRECTORY_STRENGTH_FLOOR = 0.15
DIRECTORY_ROUTES = 2
DIRECTORY_FILES_PER_ROUTE = 5
DIRECTORY_RANK_CONSTANT = 60


@dataclass(frozen=True)
class CodeMemoryEvidence:
    """The bounded personal signals applied to one shared candidate."""

    path: str
    key: str
    base_rank: int
    base_score: float
    personal_score: float
    factor: float
    strength: float
    association: float
    co_use: float
    wasted: bool
    protected: bool


@dataclass(frozen=True)
class PersonalizedCodeRanking:
    """Reordered hits and the evidence needed to explain every position."""

    hits: tuple[Any, ...]
    evidence: tuple[CodeMemoryEvidence, ...]


@dataclass(frozen=True)
class DirectoryCodeMemoryEvidence:
    """The shared and learned-directory evidence for one returned file."""

    path: str
    shared_rank: int
    fused_score: float
    directory: str
    directory_strength: float
    directory_route_rank: int | None
    admitted: bool
    protected: bool


@dataclass(frozen=True)
class DirectoryPersonalizedCodeRanking:
    """A fixed-budget ranking expanded through used source directories."""

    hits: tuple[Any, ...]
    evidence: tuple[DirectoryCodeMemoryEvidence, ...]
    directories: tuple[str, ...]


@dataclass(frozen=True)
class CodeMemoryConfig:
    """Ablation switches for the preregistered learning evaluation."""

    strength: bool = True
    association: bool = True
    co_use: bool = True
    waste: bool = True
    directory_route_weight: float = 0.0


def normalize_repository(repository: str) -> str:
    """Return one credential-free repository identity before hashing."""
    value = str(repository).strip().replace("\\", "/")
    if "://" in value:
        parsed = urlsplit(value)
        value = f"{parsed.hostname or ''}/{parsed.path.lstrip('/')}"
    elif value.startswith("git@") and ":" in value:
        host, path = value.split(":", 1)
        value = f"{host.split('@', 1)[1]}/{path}"
    value = value.strip("/")
    if value.endswith(".git"):
        value = value[:-4]
    if not value:
        raise ValueError("repository identity is empty")
    return value.casefold()


def normalize_code_path(path: str) -> str:
    """Return a repository-relative path or reject an unsafe identity."""
    value = str(path).strip().replace("\\", "/")
    value = value[2:] if value.startswith("./") else value
    value = posixpath.normpath(value)
    if not value or value == "." or value.startswith("/"):
        raise ValueError("code path must be repository-relative")
    if value == ".." or value.startswith("../"):
        raise ValueError("code path escapes the repository")
    return value


def _repository_hash(repository: str) -> str:
    identity = normalize_repository(repository)
    return hashlib.sha256(identity.encode()).hexdigest()[:20]


def code_key(repository: str, path: str) -> str:
    """Return a stable ledger key without storing a remote or local root."""
    return f"code:{_repository_hash(repository)}:{normalize_code_path(path)}"


def code_directory(path: str) -> str | None:
    """Return the immediate repository-relative parent of a source path."""
    parent = posixpath.dirname(normalize_code_path(path))
    return None if not parent or parent == "." else parent


def code_directory_key(repository: str, path: str) -> str | None:
    """Return the isolated ledger key for a source file's parent directory."""
    directory = code_directory(path)
    if directory is None:
        return None
    return f"code-directory:{_repository_hash(repository)}:{directory}"


def used_strength(dynamics: Dynamics, key: str) -> float:
    """Ignore recall-only strength because serving is not evidence of use."""
    entry = dynamics.entries.get(key)
    if not entry or int(entry.get("recurrence", 0)) == 0:
        return 0.0
    return min(1.0, max(0.0, float(entry.get("strength", 0.0))))


def personalize_code_hits(hits: Sequence[Any], query: str,
                          dynamics: Dynamics, repository: str, *,
                          limit: int | None = None,
                          config: CodeMemoryConfig = CodeMemoryConfig()
                          ) -> PersonalizedCodeRanking:
    """Reorder a fixed shared candidate set through bounded ledger signals."""
    selected = list(hits if limit is None else hits[:max(0, limit)])
    if not selected:
        return PersonalizedCodeRanking((), ())
    query_tokens = tokens(query)
    keys = {hit.path: code_key(repository, hit.path) for hit in selected}
    anchors = [keys[hit.path] for hit in selected[:CO_USE_ANCHORS]]
    adjacency = dynamics.coactivation() if config.co_use else {}
    evidence_by_path = {}
    for rank, hit in enumerate(selected, 1):
        key = keys[hit.path]
        strength = used_strength(dynamics, key) if config.strength else 0.0
        association = 0.0
        learned = dynamics.assocs.get(key) if config.association else None
        if learned:
            overlap = len(query_tokens & set(learned.get("toks", ())))
            if overlap >= ASSOCIATION_OVERLAP:
                association = min(1.0, max(0.0, float(
                    learned.get("w", 0.0)))) * min(1.0, overlap / 4.0)
        co_use_count = max(
            (adjacency.get(key, {}).get(anchor, 0)
             for anchor in anchors if anchor != key), default=0)
        co_use = min(1.0, co_use_count / CO_USE_SATURATION)
        factor = min(
            PERSONAL_FACTOR_CAP,
            1.0 + STRENGTH_WEIGHT * strength
            + ASSOCIATION_WEIGHT * association
            + CO_USE_WEIGHT * co_use,
        )
        wasted = config.waste and dynamics.serve_miss.get(key, 0) >= WASTE_COUNT
        if wasted:
            factor *= WASTE_FACTOR
        base_score = 1.0 / rank
        evidence_by_path[hit.path] = CodeMemoryEvidence(
            path=hit.path,
            key=key,
            base_rank=rank,
            base_score=round(base_score, 6),
            personal_score=round(base_score * factor, 6),
            factor=round(factor, 6),
            strength=round(strength, 6),
            association=round(association, 6),
            co_use=round(co_use, 6),
            wasted=wasted,
            protected=rank == 1,
        )
    tail = sorted(
        selected[1:],
        key=lambda hit: (
            -evidence_by_path[hit.path].personal_score,
            evidence_by_path[hit.path].base_rank,
            hit.path,
        ),
    )
    ordered = [selected[0], *tail]
    return PersonalizedCodeRanking(
        hits=tuple(ordered),
        evidence=tuple(evidence_by_path[hit.path] for hit in ordered),
    )


def personalize_code_directory_hits(
        hits: Sequence[Any], dynamics: Dynamics, repository: str, *,
        route_weight: float, limit: int = 10
        ) -> DirectoryPersonalizedCodeRanking:
    """Fuse complete shared retrieval with bounded familiar-directory arms."""
    if not 0.0 < route_weight <= 1.0:
        raise ValueError("directory route weight must be in (0, 1]")
    shared = list(hits)
    if not shared or limit <= 0:
        return DirectoryPersonalizedCodeRanking((), (), ())
    shared_paths = [hit.path for hit in shared]
    shared_ranks = {path: rank for rank, path in enumerate(shared_paths, 1)}
    by_path = {hit.path: hit for hit in shared}
    prefix = f"code-directory:{_repository_hash(repository)}:"
    strengths = {
        key[len(prefix):]: used_strength(dynamics, key)
        for key in dynamics.entries
        if key.startswith(prefix) and
        used_strength(dynamics, key) >= DIRECTORY_STRENGTH_FLOOR
    }
    files_by_directory: dict[str, list[str]] = {}
    for path in shared_paths:
        directory = code_directory(path)
        if directory in strengths:
            files_by_directory.setdefault(directory, []).append(path)
    eligible = sorted(
        (shared_ranks[paths[0]], -strengths[directory], directory)
        for directory, paths in files_by_directory.items() if paths
    )[:DIRECTORY_ROUTES]
    directories = tuple(item[2] for item in eligible)
    routes = [
        files_by_directory[directory][:DIRECTORY_FILES_PER_ROUTE]
        for directory in directories
    ]
    from .lexical import reciprocal_rank_fusion
    fused = reciprocal_rank_fusion(
        [shared_paths, *routes], rank_constant=DIRECTORY_RANK_CONSTANT,
        weights=[1.0, *([route_weight] * len(routes))])
    fused_scores = dict(fused)
    protected = shared_paths[0]
    ordered_paths = [protected, *(
        path for path, _score in fused if path != protected
    )][:limit]
    route_ranks = {
        (directory, path): rank
        for directory, route in zip(directories, routes)
        for rank, path in enumerate(route, 1)
    }
    evidence = []
    for path in ordered_paths:
        directory = code_directory(path) or ""
        route_rank = route_ranks.get((directory, path))
        evidence.append(DirectoryCodeMemoryEvidence(
            path=path,
            shared_rank=shared_ranks[path],
            fused_score=round(fused_scores[path], 12),
            directory=directory,
            directory_strength=round(
                strengths.get(directory, 0.0), 6),
            directory_route_rank=route_rank,
            admitted=shared_ranks[path] > 20,
            protected=path == protected,
        ))
    return DirectoryPersonalizedCodeRanking(
        hits=tuple(by_path[path] for path in ordered_paths),
        evidence=tuple(evidence), directories=directories)


def learn_code_session(dynamics: Dynamics, repository: str, session: str,
                       query: str, served: Sequence[str],
                       used: Sequence[str], *,
                       learn_directories: bool = False,
                       relation_anchor_paths: Sequence[str] = (),
                       relation_route: str | None = None,
                       relation_config=None) -> dict | None:
    """Record one scored session, then learn only from independent use."""
    dynamics.session_begin(session, cue=query)
    for path in dict.fromkeys(served):
        dynamics.touch(
            code_key(repository, path), kind="recall", session=session,
            wire=False)
    for path in dict.fromkeys(used):
        dynamics.touch(code_key(repository, path), session=session)
    from .review import review_usage_session
    metrics = review_usage_session(dynamics, session)
    if learn_directories:
        for path in dict.fromkeys(used):
            key = code_directory_key(repository, path)
            if key is not None:
                dynamics.touch(key, session="")
    if relation_config is not None:
        if not relation_route:
            raise ValueError("relation route is required with relation learning")
        from .code_relations import observe_code_use
        observe_code_use(
            dynamics,
            repository,
            relation_route,
            session,
            relation_anchor_paths,
            used,
            config=relation_config,
        )
    return metrics
