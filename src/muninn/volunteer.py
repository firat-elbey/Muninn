"""Precision-gated memory that an agent may volunteer without a search request.

Explicit search and context packs retain their broader retrieval contract.
This module implements the narrower passive contract confirmed on BrainBench:
one exact entity page, no approximate identity matching, and no repeat serve
within a session.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable

from .activate import superseded_paths
from .dynamics import Dynamics
from .recall import _truncate, recall_explain
from .relationships import parse_relationship_query, relationship_hits
from .store import Bundle, Note

ENTITY_TYPES = frozenset({"person", "company", "fund"})
GENERIC_IDENTITY_TOKENS = frozenset(
    {
        "capital",
        "company",
        "fund",
        "labs",
        "partners",
        "systems",
        "ventures",
        "works",
    }
)
WORD = re.compile(r"[^\W_]+")


def identity_tokens(value: str) -> tuple[str, ...]:
    """Normalize an identity without stemming or approximate matching."""

    return tuple(WORD.findall(unicodedata.normalize("NFC", value).lower()))


def _complete_identity(tokens: tuple[str, ...]) -> bool:
    """Accept full names, including unsegmented non-ASCII identities.

    Keep the existing four-character gate for ASCII components. A complete
    non-ASCII name can contain two characters without spaces; it still needs
    an exact, unique token match. This does not perform word segmentation.
    """
    return len(tokens) >= 2 or (len(tokens) == 1 and len(tokens[0]) >= 2
                                and not tokens[0].isascii())


def note_identities(note: Note) -> list[tuple[str, ...]]:
    """Return a note's title and aliases as distinct token sequences."""

    aliases = note.meta.get("aliases", [])
    aliases = aliases if isinstance(aliases, list) else [aliases]
    output = []
    for value in [note.title, *[str(alias) for alias in aliases]]:
        tokens = identity_tokens(value)
        if tokens and tokens not in output:
            output.append(tokens)
    return output


def contains_sequence(query: tuple[str, ...], identity: tuple[str, ...]) -> bool:
    """Return whether an identity occurs contiguously in a query."""

    width = len(identity)
    return any(
        query[index : index + width] == identity
        for index in range(len(query) - width + 1)
    )


def component_occurs_without_conflicting_neighbor(
    query: str, component: str, note_tokens: set[str]
) -> bool:
    """Reject a partial identity beside a different title-cased component."""

    words = WORD.findall(unicodedata.normalize("NFC", query))
    lowered = [word.lower() for word in words]
    for index, token in enumerate(lowered):
        if token != component:
            continue
        neighbors = words[max(0, index - 1) : index] + words[index + 1 : index + 2]
        conflict = any(
            len(value) >= 4
            and value.istitle()
            and value.lower() not in note_tokens
            for value in neighbors
        )
        if not conflict:
            return True
    return False


def eligible_entity_paths(
    bundle: Bundle,
    query: str,
    excluded_paths: Iterable[str] = (),
) -> set[str]:
    """Resolve exact entity identities within one active-source bundle."""

    excluded = set(excluded_paths)
    phrases: dict[tuple[str, ...], set[str]] = defaultdict(set)
    components: dict[str, set[str]] = defaultdict(set)
    identities: dict[str, list[tuple[str, ...]]] = {}
    for path, note in bundle.notes.items():
        if path in excluded:
            continue
        if str(note.meta.get("type", "")).lower() not in ENTITY_TYPES:
            continue
        values = note_identities(note)
        identities[path] = values
        for value in values:
            if _complete_identity(value):
                phrases[value].add(path)
            for token in value:
                if len(token) >= 4 and token not in GENERIC_IDENTITY_TOKENS:
                    components[token].add(path)

    query_tokens = identity_tokens(query)
    query_set = set(query_tokens)
    eligible = set()
    for path, values in identities.items():
        note_tokens = {token for value in values for token in value}
        phrase_match = any(
            _complete_identity(value)
            and len(phrases[value]) == 1
            and contains_sequence(query_tokens, value)
            for value in values
        )
        component_match = any(
            token in query_set
            and len(paths) == 1
            and path in paths
            and component_occurs_without_conflicting_neighbor(
                query, token, note_tokens
            )
            for token, paths in components.items()
        )
        if phrase_match or component_match:
            eligible.add(path)
    return eligible


def volunteer_explain(
    bundle: Bundle,
    dyn: Dynamics | None,
    cue: str,
    *,
    session: str | None = None,
    served_paths: Iterable[str] = (),
    k: int = 1,
    reactivate: bool = True,
):
    """Return exact entity pages under the confirmed passive-recall contract."""

    if k < 0 or k > 3:
        raise ValueError("passive page cap must be between zero and three")
    if k == 0:
        return []
    excluded = superseded_paths(bundle, dyn)
    relation_request = parse_relationship_query(bundle, cue)
    relational = relationship_hits(
        bundle,
        cue,
        excluded_paths=excluded,
    )
    if relation_request is not None:
        eligible = {hit.path for hit in relational} - set(served_paths)
    else:
        eligible = (
            eligible_entity_paths(bundle, cue, excluded)
            - set(served_paths)
        )
    ranked = recall_explain(
        bundle,
        None,
        cue,
        k=max(1, len(bundle.notes)),
        use_dynamics=False,
        reactivate=False,
        include_stale=False,
    )
    hits = [hit for hit in ranked if hit[0].path in eligible][:k]
    if reactivate and dyn is not None:
        for note, _score, _why, _evidence in hits:
            dyn.touch(note.path, kind="recall", session=session)
    return hits


def volunteer_pack(
    bundle: Bundle,
    dyn: Dynamics | None,
    cue: str,
    *,
    session: str | None = None,
    served_paths: Iterable[str] = (),
    k: int = 1,
    budget: int = 400,
    reactivate: bool = True,
) -> str:
    """Render passive context, or return an empty string when no entity qualifies."""

    hits = volunteer_explain(
        bundle,
        dyn,
        cue,
        session=session,
        served_paths=served_paths,
        k=k,
        reactivate=False,
    )
    if not hits:
        return ""
    limit = max(0, budget) * 4
    text = "# Volunteered memory\n\n"
    served = []
    for index, (note, _score, why, _evidence) in enumerate(hits):
        header = f"### {note.title} ({note.path})\n"
        if why:
            header += f"*why loaded: {why}*\n"
        remaining = limit - len(text) - len(header) - 2
        if remaining < 0:
            continue
        share = remaining // max(1, len(hits) - index) // 4
        body = _truncate(note.body.strip(), share)
        if note.body.strip() and not body:
            continue
        text += header + body + "\n\n"
        served.append(note.path)
    if not served:
        return ""
    if reactivate and dyn is not None:
        for path in served:
            dyn.touch(path, kind="recall", session=session)
    return text.rstrip("\n")
