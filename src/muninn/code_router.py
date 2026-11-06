"""Observable-query routing for bounded code retrieval."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .lexical import (
    diverse_protected_rank_fusion,
    protected_rank_fusion,
    reciprocal_rank_fusion,
)

ROUTES = ("code", "comment", "trace", "ripple", "general")
SINGLE_PATH_FIELDS = (
    "anchor_file",
    "changed_file",
    "given_file",
    "path",
)
MULTI_PATH_FIELDS = ("changed_files", "implementation_files")
DEPENDENCY_DIRECTIONS = {
    "code": "dependents",
    "comment": "both",
    "trace": "dependencies",
    "ripple": "dependents",
    "general": "both",
}


def query_route(query: object) -> str:
    """Classify a query from its released fields, without task labels."""
    keys = set(query) if isinstance(query, Mapping) else set()
    if {"command", "failure_excerpt"} <= keys:
        return "trace"
    if {"anchor_file", "anchor_diff"} <= keys:
        return "ripple"
    if "review_comment" in keys and ({"given_file", "path"} & keys):
        return "comment"
    if "changed_file" in keys or "implementation_files" in keys:
        return "code"
    return "general"


def _query_strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _query_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _query_strings(item)


def query_anchor_paths(
    query: object,
    available_paths: Iterable[str],
) -> list[str]:
    """Return explicit or literally mentioned corpus paths in stable order."""
    available = set(available_paths)
    explicit: list[str] = []
    if isinstance(query, Mapping):
        for field in SINGLE_PATH_FIELDS:
            value = query.get(field)
            if isinstance(value, str):
                explicit.append(value.replace("\\", "/"))
        for field in MULTI_PATH_FIELDS:
            values = query.get(field)
            if isinstance(values, list):
                explicit.extend(
                    value.replace("\\", "/")
                    for value in values
                    if isinstance(value, str)
                )
    result = list(dict.fromkeys(
        path for path in explicit if path in available))
    seen = set(result)
    text = "\n".join(_query_strings(query)).replace("\\", "/").casefold()
    for path in sorted(available):
        if path not in seen and path.casefold() in text:
            result.append(path)
            seen.add(path)
    return result


def dependency_direction(route: str) -> str:
    """Return the fixed graph direction for one observable query route."""
    if route not in ROUTES:
        raise ValueError(f"unknown code-query route: {route}")
    return DEPENDENCY_DIRECTIONS[route]


def routed_dependency_ranking(
    index: Any,
    query: object,
    *,
    depth: int = 2,
    decay: float = 0.6,
) -> tuple[str, list[str], list[str]]:
    """Rank dependencies from anchors selected only from the query."""
    route = query_route(query)
    anchors = query_anchor_paths(query, index.records)
    ranking = index.dependency_ranking(
        anchors,
        direction=dependency_direction(route),
        depth=depth,
        decay=decay,
    )
    return route, anchors, ranking


def routed_seeded_dependency_ranking(
    index: Any,
    query: object,
    seed_paths: Iterable[str],
    *,
    depth: int = 2,
    decay: float = 0.6,
) -> tuple[str, list[str], list[str]]:
    """Walk from explicit query paths and a bounded retrieval seed list."""
    route = query_route(query)
    explicit = query_anchor_paths(query, index.records)
    anchors = list(dict.fromkeys([
        *explicit,
        *(path for path in seed_paths if path in index.records),
    ]))
    ranking = index.dependency_ranking(
        anchors,
        direction=dependency_direction(route),
        depth=depth,
        decay=decay,
    )
    return route, anchors, ranking


def _complete_ranking(
    ranking: Iterable[str],
    available_paths: Iterable[str],
) -> list[str]:
    available = set(available_paths)
    result = list(dict.fromkeys(path for path in ranking if path in available))
    result.extend(sorted(available - set(result)))
    return result


def _require_complete_ranking(
    ranking: Iterable[str],
    available_paths: Iterable[str],
    *,
    label: str,
) -> list[str]:
    result = list(ranking)
    available = set(available_paths)
    if len(result) != len(set(result)) or set(result) != available:
        raise ValueError(f"{label} ranking differs from the corpus")
    return result


def trace_hybrid_ranking(
    index: Any,
    query_text: str,
    available_paths: Iterable[str],
    semantic_ranking: Iterable[str],
    repo_map_ranking: Iterable[str],
    *,
    seed_files: int = 10,
    symbol_depth: int = 2,
    symbol_neighbor_cap: int = 50,
    symbols_per_anchor: int = 3,
    rank_constant: int = 60,
    semantic_weight: float = 1.0,
    directory_weight: float = 0.75,
    repo_map_weight: float = 0.5,
    symbol_weight: float = 1.5,
) -> tuple[list[str], dict[str, list[str]]]:
    """Fuse semantic, directory, repository-map, and symbol rankings."""
    available = tuple(available_paths)
    if len(available) != len(set(available)):
        raise ValueError("the corpus contains duplicate paths")
    if seed_files < 1:
        raise ValueError("trace seed files must be positive")
    semantic = _require_complete_ranking(
        semantic_ranking, available, label="semantic"
    )
    repo_map = _require_complete_ranking(
        repo_map_ranking, available, label="repository-map"
    )
    current = _complete_ranking(
        (
            hit.path
            for hit in index.search(
                query_text,
                mode="structural-fusion",
                limit=len(available),
            )
        ),
        available,
    )
    semantic_seeds = semantic[:seed_files]
    directory = _complete_ranking(
        index.directory_neighborhood_ranking(
            semantic_seeds,
            seed_files=seed_files,
        ),
        available,
    )
    structural_seeds = current[:seed_files]
    symbol = _complete_ranking(
        [
            *index.symbol_dependency_ranking(
                structural_seeds,
                depth=symbol_depth,
                neighbor_cap=symbol_neighbor_cap,
                symbols_per_anchor=symbols_per_anchor,
            ),
            *current,
        ],
        available,
    )
    fused = reciprocal_rank_fusion(
        [semantic, directory, repo_map, symbol],
        rank_constant=rank_constant,
        weights=[
            semantic_weight,
            directory_weight,
            repo_map_weight,
            symbol_weight,
        ],
    )
    ranking = _complete_ranking((path for path, _score in fused), available)
    return ranking, {
        "semantic": semantic,
        "directory": directory,
        "repo_map": repo_map,
        "symbol": symbol,
    }


def selected_base_hybrid_ranking(
    route: str,
    available_paths: Iterable[str],
    current_ranking: Iterable[str],
    structural_ranking: Iterable[str],
    repo_map_ranking: Iterable[str],
    reranker_ranking: Iterable[str],
    semantic_ranking: Iterable[str],
    *,
    rank_constant: int = 60,
) -> tuple[list[str], dict[str, list[str]]]:
    """Fuse the fixed components selected before the native trace route."""
    if route not in ROUTES:
        raise ValueError(f"unknown code-query route: {route}")
    available = tuple(available_paths)
    if len(available) != len(set(available)):
        raise ValueError("the corpus contains duplicate paths")
    components = {
        "current": _require_complete_ranking(
            current_ranking, available, label="current"
        ),
        "repo_map": _require_complete_ranking(
            repo_map_ranking, available, label="repository-map"
        ),
        "reranker": _require_complete_ranking(
            reranker_ranking, available, label="reranker"
        ),
        "semantic": _require_complete_ranking(
            semantic_ranking, available, label="semantic"
        ),
        "structural": _require_complete_ranking(
            structural_ranking, available, label="structural"
        ),
    }
    labels = tuple(sorted(components))
    rankings = [components[label] for label in labels]
    if route == "code":
        weights = {
            "current": 0.25,
            "repo_map": 1.5,
            "reranker": 1.5,
            "semantic": 0.0,
            "structural": 1.0,
        }
        fused = diverse_protected_rank_fusion(
            [
                (components["reranker"], 2),
                (components["repo_map"], 1),
            ],
            rankings,
            rank_constant=rank_constant,
            weights=[weights[label] for label in labels],
        )
    elif route == "comment":
        weights = {
            "current": 1.0,
            "repo_map": 1.0,
            "reranker": 0.0,
            "semantic": 1.5,
            "structural": 0.25,
        }
        fused = protected_rank_fusion(
            components["semantic"],
            rankings,
            protected=2,
            rank_constant=rank_constant,
            weights=[weights[label] for label in labels],
        )
    elif route == "ripple":
        weights = {
            "current": 1.12,
            "repo_map": 1.0,
            "reranker": 0.0,
            "semantic": 0.0,
            "structural": 0.0,
        }
        fused = diverse_protected_rank_fusion(
            [
                (components["current"], 1),
                (components["repo_map"], 2),
            ],
            rankings,
            rank_constant=rank_constant,
            weights=[weights[label] for label in labels],
        )
    else:
        return components["current"], components
    return _complete_ranking(
        (path for path, _score in fused), available
    ), components


def routed_native_hybrid_ranking(
    index: Any,
    query: object,
    query_text: str,
    available_paths: Iterable[str],
    base_ranking: Iterable[str],
    current_ranking: Iterable[str],
    semantic_ranking: Iterable[str] | None = None,
    repo_map_ranking: Iterable[str] | None = None,
) -> tuple[str, list[str], dict[str, list[str]], str | None]:
    """Apply the frozen native route and record any trace fallback.

    Code, comment, and ripple queries retain the selected base ranking.
    Trace queries use the native structural fusion. General queries and any
    failed trace component return the current Muninn ranking.
    """
    available = tuple(available_paths)
    if len(available) != len(set(available)):
        raise ValueError("the corpus contains duplicate paths")
    base = _require_complete_ranking(
        base_ranking, available, label="base"
    )
    current = _require_complete_ranking(
        current_ranking, available, label="current"
    )
    route = query_route(query)
    if route in {"code", "comment", "ripple"}:
        return route, base, {"base": base, "current": current}, None
    if route != "trace":
        return route, current, {"current": current}, None
    try:
        if semantic_ranking is None:
            raise ValueError("semantic ranking is absent")
        if repo_map_ranking is None:
            raise ValueError("repository-map ranking is absent")
        ranking, trace_components = trace_hybrid_ranking(
            index,
            query_text,
            available,
            semantic_ranking,
            repo_map_ranking,
        )
    except Exception as error:  # noqa: BLE001 - this boundary must fail soft.
        reason = f"{type(error).__name__}: {error}"
        return route, current, {"current": current}, reason
    return route, ranking, {
        "base": base,
        "current": current,
        **trace_components,
    }, None


def current_protected_hybrid_ranking(
    query: object,
    available_paths: Iterable[str],
    native_ranking: Iterable[str] | None,
    current_ranking: Iterable[str],
) -> tuple[str, list[str], dict[str, list[str]], str | None]:
    """Use the native candidate only for code and trace queries.

    Comment, ripple, and general queries retain current Muninn. A missing or
    invalid native ranking also returns current Muninn and records the error.
    """
    available = tuple(available_paths)
    if len(available) != len(set(available)):
        raise ValueError("the corpus contains duplicate paths")
    current = _require_complete_ranking(
        current_ranking, available, label="current"
    )
    route = query_route(query)
    if route not in {"code", "trace"}:
        return route, current, {"current": current}, None
    try:
        if native_ranking is None:
            raise ValueError("native ranking is absent")
        native = _require_complete_ranking(
            native_ranking, available, label="native"
        )
    except Exception as error:  # noqa: BLE001 - this boundary must fail soft.
        reason = f"{type(error).__name__}: {error}"
        return route, current, {"current": current}, reason
    return route, native, {
        "current": current,
        "native": native,
    }, None
