"""Rank notes and render budgeted context packs.

Recall delegates scoring to :mod:`muninn.activate`, applies bounded usage
strength, and excludes an obsolete note when its superseding note exists. The
flat ablation follows the same lexical path without the graph or usage state.

A context pack contains a compact bundle index, selected focus notes, and a
reason for each selection. Greedy facet coverage limits near-duplicate notes,
and token allocation follows activation with a per-note floor. Output remains
deterministic for a fixed bundle and sidecar state.
"""

from __future__ import annotations

import re
from dataclasses import replace

from .activate import (
    FIELD_WEIGHTS,
    GOAL_SPAN,
    STOPWORDS,
    STRENGTH_LO,
    STRENGTH_SPAN,
    ActivationMap,
    Cue,
    Evidence,
    activate,
    cues_from_goals,
    cues_from_query,
    cues_from_situation,
    goal_alignment,
    relevance,
    superseded_paths,
    tokens,
)
from .dynamics import PIN_FLOOR, Dynamics
from .relationships import relationship_hits
from .store import Bundle, Note

__all__ = (
    "FIELD_WEIGHTS",
    "GOAL_SPAN",
    "MODES",
    "PACK_NOTE_FLOOR",
    "PIN_FLOOR",
    "STOPWORDS",
    "STRENGTH_LO",
    "STRENGTH_SPAN",
    "ActivationMap",
    "context_pack",
    "cues_from_goals",
    "estimate_tokens",
    "goal_alignment",
    "recall",
    "relevance",
    "tokens",
)

MODES = ("muninn", "muninn-walk", "flat", "dump")
PACK_NOTE_FLOOR = 80  # per-note token floor in a pack (plus title + why)


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


# -- ranking (shared by recall and context_pack) -----------------------------

def _why(bundle: Bundle, e: Evidence) -> str:
    """The one-line "why loaded", now with hop evidence."""
    bits = []
    if e.direct > 0:
        bits.append(f"cue-match {e.direct:.2f}")
    elif e.walk > 0:
        bits.append("linked from a match")
    if e.carrier and e.direct == 0:
        # hop evidence only for notes the cue did NOT hit directly: two
        # direct hits each citing the other ("A via B" while B says "via A")
        # reads as circular nonsense to the consuming agent
        src, kind = e.carrier
        if kind == "link":
            title = bundle.notes[src].title if src in bundle.notes else src
            bits.append(f"via [[{title}]]")
        else:
            base = src.rsplit("/", 1)[-1]
            bits.append(f"used-with {base[:-3] if base.endswith('.md') else base}")
    if e.goal_rel >= 0.3:
        g = e.goal
        if len(g) > 32:  # word-boundary cut: "…and tea" reads as a typo
            g = (g[:32].rsplit(" ", 1)[0] or g[:32]) + "…"
        bits.append(f"aligned with active goal: {g}")
    if e.strength > 0:
        # a never-touched note's strength is only the capped recall
        # nudge: printing 0.01 next to an earned 0.34 reads like a
        # penalty for having been useful, so say what it is instead
        bits.append("unused" if e.unused else f"strength {e.strength:.2f}")
    if e.captured:
        bits.append("used before a recorded outcome")
    if e.assoc:
        # transparency for the reflection loop: an adaptation must be
        # visible where it acts, so a bad learned lift is correctable
        bits.append("learned from a past miss")
    if e.relation:
        seed = bundle.notes.get(e.relation_seed)
        title = seed.title if seed is not None else e.relation_seed
        bits.append(f"{e.relation} one hop from [[{title}]]")
    return "; ".join(bits)


def _ranked_for(bundle: Bundle, dyn: Dynamics | None, cues: list[Cue],
                use_dynamics: bool,
                include_stale: bool) -> list[tuple[Note, float, str, Evidence]]:
    """Activate the cues and return admissible notes best first:
    supersession excluded (file-level and dynamics-level), zero scores
    dropped, ties broken by path (deterministic). use_dynamics=False is
    the flat ablation: same code path, no walk, no memory."""
    amap = activate(
        bundle,
        dyn if use_dynamics else None,
        cues,
        iters=3 if use_dynamics else 0,
        include_stale=include_stale,
    )
    excluded = (set() if include_stale
                else superseded_paths(bundle, dyn if use_dynamics else None))
    out: list[tuple[Note, float, str, Evidence]] = []
    for path, score in amap.scores.items():
        if score <= 0 or path in excluded:
            continue
        e = amap.evidence[path]
        note = bundle.notes[path]
        why = _why(bundle, e)
        if note.is_stale():  # OKF v0.2 §5.5: served, never hidden, but
            why += ("; STALE since "  # the reader sees the expiry date
                    + str(note.meta.get("stale_after", ""))[:10])
        out.append((note, score, why, e))
    out.sort(key=lambda t: (-t[1], t[0].path))
    return out


def _reactivate(dyn: Dynamics,
                hits: list[tuple[Note, float, str, Evidence]],
                session: str | None = None) -> None:
    for note, _s, _w, _e in hits:
        try:
            dyn.touch(note.path, kind="recall", session=session)
        except OSError:
            # Retrieval must remain available when the derived sidecar is
            # read-only. Stop after the first failed metadata write.
            break


def _relational_reorder(
    bundle: Bundle,
    cue: str,
    ranked: list[tuple[Note, float, str, Evidence]],
    dyn: Dynamics | None,
    include_stale: bool,
) -> list[tuple[Note, float, str, Evidence]]:
    """Place exact typed answers first and retain the lexical remainder."""

    excluded = set() if include_stale else superseded_paths(bundle, dyn)
    relation_hits = relationship_hits(
        bundle,
        cue,
        include_stale=include_stale,
        excluded_paths=excluded,
    )
    if not relation_hits:
        return ranked
    lexical_rank = {item[0].path: index for index, item in enumerate(ranked)}
    ordered = sorted(
        (hit for hit in relation_hits if hit.path not in excluded),
        key=lambda hit: (lexical_rank.get(hit.path, len(ranked)), hit.path),
    )
    if not ordered:
        return ranked
    by_path = {item[0].path: item for item in ranked}
    peak = ranked[0][1] if ranked else 1.0
    step = max(abs(peak), 1.0) / 1_000_000
    graph_ranked = []
    for index, hit in enumerate(ordered):
        existing = by_path.get(hit.path)
        if existing is None:
            evidence = Evidence(
                carrier=(hit.seed, "link"),
                hop=1,
                walk=peak,
                relation=hit.relation,
                relation_seed=hit.seed,
                relation_direction=hit.direction,
                relation_hop=hit.hop_count,
            )
            note = bundle.notes[hit.path]
        else:
            note = existing[0]
            evidence = replace(
                existing[3],
                relation=hit.relation,
                relation_seed=hit.seed,
                relation_direction=hit.direction,
                relation_hop=hit.hop_count,
            )
        score = peak + step * (len(ordered) - index)
        graph_ranked.append((note, score, _why(bundle, evidence), evidence))
    graph_paths = {item[0].path for item in graph_ranked}
    return graph_ranked + [
        item for item in ranked if item[0].path not in graph_paths
    ]


def recall_explain(bundle: Bundle, dyn: Dynamics | None, cue: str, k: int = 5,
                   use_dynamics: bool = True, reactivate: bool = True,
                   include_stale: bool = False,
                   ) -> list[tuple[Note, float, str, Evidence]]:
    """recall() plus the per-hit Evidence (facets, carrier hop, components)."""
    ranked = _ranked_for(bundle, dyn, [Cue(cue, 1.0, "query")],
                         use_dynamics, include_stale)
    ranked = _relational_reorder(
        bundle,
        cue,
        ranked,
        dyn if use_dynamics else None,
        include_stale,
    )
    top = ranked[:k]
    if reactivate and use_dynamics and dyn is not None:
        _reactivate(dyn, top)
    return top


def recall(bundle: Bundle, dyn: Dynamics | None, cue: str, k: int = 5,
           use_dynamics: bool = True, reactivate: bool = True,
           include_stale: bool = False) -> list[tuple[Note, float, str]]:
    """Top-k notes for the cue. Returns (note, score, why). Recall logs a
    recall event per returned note: a small bounded nudge, never a full
    touch (junk cannot amplify itself by being retrieved)."""
    return [(n, s, w) for n, s, w, _e in
            recall_explain(bundle, dyn, cue, k, use_dynamics, reactivate,
                           include_stale)]


def prime_cue(bundle: Bundle, dyn: Dynamics | None,
              workdir: str | None = None) -> str:
    """Harvest the SITUATION as a cue: no query needed. This is the
    involuntary-recall path (harvesting lives in
    activate.cues_from_situation; this keeps the one-string contract)."""
    return " ".join(c.text for c in cues_from_situation(bundle, dyn, workdir))


# -- context packs ------------------------------------------------------------

def _truncate(text: str, budget_tokens: int) -> str:
    limit = max(0, budget_tokens * 4)
    if len(text) <= limit:
        return text
    marker = "\n[... truncated]"
    if limit < len(marker):
        return ""
    prefix = text[:limit - len(marker)]
    if "\n" in prefix:
        prefix = prefix.rsplit("\n", 1)[0]
    return prefix + marker


def _body_blocks(body: str) -> list[tuple[int, int, str]]:
    """Keep paragraphs, lists, and fenced examples intact, with body lines."""
    lines = body.splitlines()
    blocks = []
    start = None
    fence = ""
    for i, line in enumerate(lines):
        match = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if match:
            run = match.group(1)
            if not fence:
                fence = run
            elif (run[0] == fence[0] and len(run) >= len(fence)
                  and not line[match.end():].strip()):
                fence = ""
        if start is None and line.strip():
            start = i
        if start is not None and not line.strip() and not fence:
            blocks.append((start + 1, i, "\n".join(lines[start:i])))
            start = None
    if start is not None:
        blocks.append((start + 1, len(lines), "\n".join(lines[start:])))
    return blocks


def _compact_body(body: str, cue: str, budget: int) -> str:
    """Select exact complete blocks using only the present retrieval cue."""
    blocks = _body_blocks(body)
    query = tokens(cue)
    matched = [query & tokens(text) for _start, _end, text in blocks]
    headings: dict[int, int] = {}
    ancestors = []
    for i, (_start, _end, text) in enumerate(blocks):
        heading = re.match(r"^(#{1,6})\s", text)
        if heading:
            level = len(heading.group(1))
            headings = {depth: index for depth, index in headings.items()
                        if depth < level}
            headings[level] = i
        ancestors.append(set(headings.values()))
    candidates = sorted(range(len(blocks)),
                        key=lambda i: (-len(matched[i]), i))
    selected: set[int] = set()
    covered: set[str] = set()
    body_lines = body.splitlines()

    def render(indices: set[int]) -> str:
        ranges: list[list[int]] = []
        for i in sorted(indices):
            if ranges and i == ranges[-1][-1] + 1:
                ranges[-1].append(i)
            else:
                ranges.append([i])
        output = []
        for group in ranges:
            start, end = blocks[group[0]][0], blocks[group[-1]][1]
            text = "\n".join(body_lines[start - 1:end])
            output.append(f"Body lines {start}-{end}:\n{text}")
        return "\n\n".join(output)

    whole = render(set(range(len(blocks))))
    if len(whole) <= budget * 4:
        return whole
    for i in candidates:
        # Retain the first relevant block, then only additional query facets.
        if selected and not matched[i] - covered:
            continue
        # Adjacent blocks can qualify an assertion or establish its subject.
        group = set(range(max(0, i - 1), min(len(blocks), i + 2)))
        group |= ancestors[i]
        candidate = selected | group
        if len(render(candidate)) > max(0, budget * 4):
            continue
        selected = candidate
        for j in group:
            covered |= matched[j]
    return render(selected)


def _short(text: str, limit: int = 120) -> str:
    """One-line, at most ``limit`` chars: for headers that quote a cue."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _cover_select(ranked: list[tuple[Note, float, str, Evidence]],
                  k: int) -> list[tuple[Note, float, str, Evidence]]:
    """Greedy facet-coverage selection: walking candidates best-first, a
    pick must cover a not-yet-covered facet before near-duplicates are
    admitted; leftover slots then fill by score. A candidate whose TITLE
    matches an already-admitted note is skipped outright unless it covers
    a new facet (duplicate-title stubs must not stack the focus).
    Returned best-first. For single-cue maps without duplicate titles
    this degenerates to plain top-k."""
    selected: list[tuple[Note, float, str, Evidence]] = []
    covered: set[str] = set()
    titles: set[str] = set()
    rest: list[tuple[Note, float, str, Evidence]] = []
    for item in ranked:
        if len(selected) >= k:
            break
        facets = set(item[3].facets)
        title = item[0].title.strip().lower()
        if title in titles and not facets - covered:
            continue  # a same-titled note that adds nothing new
        if not selected or facets - covered:
            selected.append(item)
            covered |= facets
            titles.add(title)
        else:
            rest.append(item)
    for item in rest:
        if len(selected) >= k:
            break
        title = item[0].title.strip().lower()
        if title in titles:
            continue
        selected.append(item)
        titles.add(title)
    selected.sort(key=lambda t: (-t[1], t[0].path))
    return selected


def _fallback_focus(bundle: Bundle, dyn: Dynamics | None, cue: str,
                    lines: list[str], budget: int, k: int) -> str:
    """The Focus body when nothing matched lexically or via the walk: say
    so explicitly, then show the strongest non-superseded notes within
    budget. These notes were NOT recalled: they are never reactivated :
    so a hook degrades visibly instead of silently."""
    lines.append(f"*(no match for {_short(cue)}; showing the strongest notes "
                 "instead)*")
    lines.append("")
    sup = superseded_paths(bundle, dyn)
    ranked: list[str] = []
    if dyn is not None:
        ranked = [p for p, _s in dyn.strongest(len(bundle.notes))
                  if p in bundle.notes and p not in sup]
    ranked += [p for p in sorted(bundle.notes)
               if p not in set(ranked) and p not in sup]
    shown = 0
    titles: set[str] = set()
    for path in ranked:
        if shown >= k:
            break
        note = bundle.notes[path]
        title = note.title.strip().lower()
        if title in titles:  # Duplicate-title stubs do not accumulate here.
            continue
        titles.add(title)
        remaining = max(0, (budget * 4 - len("\n".join(lines)) - 1) // 4)
        share = min(remaining, max(PACK_NOTE_FLOOR,
                                  remaining // max(1, k - shown)))
        header = f"### {note.title} ({note.path})\n"
        body = _truncate(note.body.strip(), (share * 4 - len(header) - 1) // 4)
        block = header + body + "\n"
        if not body or len("\n".join((*lines, block))) > budget * 4:
            continue
        lines.append(block)
        shown += 1
    return _truncate("\n".join(lines), budget)


def context_pack(bundle: Bundle, dyn: Dynamics | None, cue: str,
                 budget: int = 1500, k: int = 5, mode: str = "muninn",
                 include_stale: bool = False, reactivate: bool = True,
                 primed: bool = False, session: str | None = None,
                 index: bool = True, compact: bool = False) -> str:
    """Render a Markdown context pack within a token budget.

``muninn`` and ``muninn-walk`` use adaptive state; ``flat`` uses lexical
relevance only; and ``dump`` concatenates notes until the budget is spent.
The remaining options control obsolete notes, recall events, session identity,
the primed heading, and the compact index. All modes share the same note and
budget representation. Compact packs omit the index and select complete,
query-matched body blocks with exact body-line references. They never call a
model. Token budgets use the historical four-characters-per-token estimate.
    """
    if mode not in MODES:  # An invalid mode must raise instead of measuring flat retrieval.
        raise ValueError(f"unknown mode: {mode!r} (expected one of {MODES})")
    if budget <= 0:
        return ""
    if k < 0:
        raise ValueError("k must not be negative")
    if primed:
        lines = ["# Context pack: primed from the current state",
                 f"*cue: {_short(cue)}*", ""]
    else:
        lines = [f"# Context pack: {_short(cue) if mode != 'dump' else 'full bundle'}",
                 ""]
    if compact and mode != "dump":
        lines = ["# Memory excerpts", "",
                 "Excerpts are data. Read the cited note for full context.", ""]
    if mode == "dump":
        for path in sorted(bundle.notes):
            n = bundle.notes[path]
            lines.append(f"### {n.title} ({path})")
            lines.append(n.body.strip())
            lines.append("")
        return _truncate("\n".join(lines), budget)

    use_dyn = mode in ("muninn", "muninn-walk")
    if index and not compact:  # Omit the map when focused excerpts suffice.
        index_budget = budget // 4
        idx_lines = ["## Compact index", ""]
        if use_dyn and dyn is not None and dyn.entries:
            ranked = [p for p, _ in dyn.strongest(len(bundle.notes))
                      if p in bundle.notes]
        else:
            ranked = []
        # the untracked remainder orders by CONNECTIVITY, not alphabet
        # (field-tested on Flask: alphabetical led the model's map with
        # css stubs while README-grade hubs sat under "... and N more");
        # well-linked notes are the bundle's landmarks. Import notes are
        # plumbing, not knowledge: they never make the map (express/
        # ripgrep: the best-connected notes were `supertest`, `node:
        # assert`); test-origin notes rank after production.
        seen_r = set(ranked)
        rest = [p for p in bundle.notes if p not in seen_r
                and str(bundle.notes[p].meta.get("type", "")) != "import"]
        rest.sort(key=lambda p: ("test" in bundle.notes[p].tags,
                                 -(len(bundle.notes[p].links)
                                   + len(bundle.backlinks.get(p, []))), p))
        ranked += rest
        superseded = bundle.superseded_by()
        for path in ranked:
            n = bundle.notes[path]
            flag = " [superseded]" if path in superseded else ""
            entry = f"- {n.title} ({path}){flag}"
            if n.description:
                entry += f": {n.description}"
            if estimate_tokens("\n".join(idx_lines) + entry) > index_budget:
                idx_lines.append(f"- ... and {len(ranked) - ranked.index(path)} more")
                break
            idx_lines.append(entry)
        lines += idx_lines + [""]

    # Goals TILT, they never SEED: an active goal boosts aligned notes
    # (bounded 1.3x, in activate) only among what the query itself
    # reached. Goal cues used to join here at half weight: that placed
    # one goal-matching note into Focus on every query, displacing the
    # query's own 5th candidate (the round-2 goal slot tax, N1).
    cues = (cues_from_query(cue) if mode == "muninn-walk"
            else [Cue(cue, 1.0, "query")])
    ranked = _ranked_for(bundle, dyn, cues, use_dyn, include_stale)
    ranked = _relational_reorder(
        bundle,
        cue,
        ranked,
        dyn if use_dyn else None,
        include_stale,
    )
    if any(item[3].relation for item in ranked):
        hits = ranked[:k]
    else:
        hits = _cover_select(ranked, k)
    if not compact:
        lines.append("## Focus (recalled for this cue)")
        lines.append("")
    if not hits and k > 0:  # never emit an empty Focus silently (a hook
        if compact:
            return _truncate("\n".join((*lines, "No matching notes.")), budget)
        return _fallback_focus(bundle, dyn, cue, lines,  # degrades visibly)
                               budget, k)
    total_act = sum(s for _n, s, _w, _e in hits)
    admitted = []
    for note, score, why, evidence in hits:
        # The share is proportional to activation over the remaining notes.
        # every admitted note keeps title + why + ~80 tokens of body
        remaining = max(0, (budget * 4 - len("\n".join(lines)) - 1) // 4)
        block = [f"### {note.title} ({note.path})"]
        if why:
            block.append(f"*why loaded: {why}*")
        overhead = (len("\n".join(block)) + 5) // 4
        share = min(remaining, max(PACK_NOTE_FLOOR + overhead,
                    int(remaining * score / max(total_act, 1e-9))))
        total_act -= score
        body_budget = max(0, share - overhead)
        minimum_body = min(PACK_NOTE_FLOOR, (len(note.body.strip()) + 3) // 4)
        if admitted and body_budget < minimum_body:
            continue
        if compact:
            body = _compact_body(note.body, cue, min(160, body_budget))
            if not body and not admitted:
                body = _compact_body(note.body, cue,
                                     max(0, remaining - overhead))
            if not body:
                body = "No complete excerpt fits. Read the note."
        else:
            body = _truncate(note.body.strip(), body_budget)
        if not body:
            continue
        block.append(body)
        block.append("")
        text = "\n".join(block)
        if len("\n".join((*lines, text))) > budget * 4:
            continue
        lines.append(text)
        admitted.append((note, score, why, evidence))
    if use_dyn and reactivate and dyn is not None:
        _reactivate(dyn, admitted, session)
    return _truncate("\n".join(lines), budget)
