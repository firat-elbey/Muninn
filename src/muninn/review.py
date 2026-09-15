"""Compare served context with independently used context.

A completed session classifies notes as hits, misses, or unused served items.
It also records used paths that have no mapped note. A miss can create a
bounded, decaying cue association. Three consecutive unused serves can apply
one bounded score reduction until the next independent use. Repeated unmapped
paths can invoke deterministic extraction for a limited number of files.

Serving is never evidence of use. Each session is reviewed once, every change
is an ordinary ledger event, and all adjustments remain bounded and
rebuildable. ``MUNINN_NO_REVIEW=1`` disables the ambient review.
"""

from __future__ import annotations

import json
import os

from .activate import STOPWORDS, tokens
from .dynamics import Dynamics, repository_identity, sidecar_lock
from .store import Bundle

MISS_CAP = 5        # associations learned per session, most-used first
ASSOC_TOKENS = 16   # cue tokens kept per learned association
ASSOC_W = 0.5       # weight of one freshly learned association
GAP_MIN = 2         # sightings before an unmapped path is worth mapping
GAP_BUILDS = 8      # files mapped per review run
REVIEW_LOCK_TIMEOUT = 2.0


def session_metrics(ledger_path: str, session: str) -> dict:
    """One pass over the ledger for one session id: what was served,
    what was used, and the derived hit/miss/waste sets. Misses are
    ordered by use count (most-relied-on first); everything else is
    path-sorted (deterministic)."""
    served: set[str] = set()
    used: dict[str, int] = {}
    cue = ""
    reviewed = False
    try:
        fh = open(ledger_path, encoding="utf-8")
    except OSError:
        return {"served": [], "used": [], "hits": [], "misses": [],
                "waste": [], "cue": "", "reviewed": False}
    with fh:
        for line in fh:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict) or str(ev.get("session")) != session:
                continue
            kind = ev.get("kind")
            if kind == "session-begin" and ev.get("cue"):
                cue = str(ev["cue"])
            elif kind == "review":
                reviewed = True
            elif kind == "recall" and ev.get("note"):
                served.add(str(ev["note"]))
            elif kind in ("touch", "encode") and ev.get("note"):
                n = str(ev["note"])
                used[n] = used.get(n, 0) + 1
    hits = sorted(served & set(used))
    misses = sorted((n for n in used if n not in served),
                    key=lambda n: (-used[n], n))
    waste = sorted(served - set(used))
    return {"served": sorted(served), "used": sorted(used), "hits": hits,
            "misses": misses, "waste": waste, "cue": cue,
            "reviewed": reviewed}


def _assoc_tokens(cue: str) -> list[str]:
    """The situation tokens worth learning from: stopwords are already
    gone (tokens()); keep a bounded, deterministic prefix, longest (most
    specific) first."""
    toks = sorted(tokens(cue), key=lambda t: (-len(t), t))
    return [t for t in toks if t not in STOPWORDS][:ASSOC_TOKENS]


def _build_gaps(bundle: Bundle, dyn: Dynamics, workdir: str) -> list[str]:
    """The gap actuator: map repeatedly-used unmapped files with the
    tree-sitter extractor: deterministic, no LLM, bounded. Paths are
    relative to the repository identified by a canonical-root digest.
    Historical gaps without that identity cannot trigger extraction.
    Files that vanished, leave the root, or now map are skipped."""
    from . import extract
    if not extract.available():
        return []
    from .observe import git_out, map_note
    top = git_out(workdir, "rev-parse", "--show-toplevel")
    if not top:
        return []
    top = os.path.realpath(top)
    prefix = repository_identity(top) + ":"
    candidates = sorted((p for p, c in dyn.gap_counts.items()
                         if c >= GAP_MIN and p.startswith(prefix)),
                        key=lambda p: (-dyn.gap_counts[p], p))[:GAP_BUILDS]
    built: list[str] = []
    for key in candidates:
        rel = key[len(prefix):]
        full = os.path.join(top, rel)
        if (not os.path.isfile(full) or os.path.islink(full)
                or os.path.commonpath((top, os.path.realpath(full))) != top
                or map_note(bundle, full)):
            continue
        graph = extract.extract_file(full, src=rel)
        if graph.get("nodes"):
            extract.import_graph(bundle, graph)
            built.append(key)
    if built:
        bundle.notes, bundle.backlinks = {}, {}
        bundle._load()  # the new notes are part of the bundle NOW
    return built


def _apply_session_review(dyn: Dynamics, session: str, metrics: dict,
                          built=()) -> dict:
    """Apply bounded learning to one already measured usage session."""
    cue = metrics["cue"] or dyn.session_cues.get(session, "")
    toks = _assoc_tokens(cue) if cue else []
    if len(toks) >= 2:
        for note in metrics["misses"][:MISS_CAP]:
            dyn.assoc(note, toks, w=ASSOC_W)
        for note in metrics["hits"]:
            association = dyn.assocs.get(note)
            if association and len(
                    set(association.get("toks", [])) & set(toks)) >= 2:
                dyn.assoc(note, toks, w=0.2)
    dyn.review_mark(
        session, metrics["hits"], metrics["misses"], metrics["waste"], built)
    metrics["built"] = list(built)
    metrics["session"] = session
    return metrics


def _pending_metrics(dyn: Dynamics, session: str) -> dict | None:
    """Reload authoritative state while holding the separate review lock."""
    dyn.replay()
    if session in dyn.reviewed:
        return None
    metrics = session_metrics(dyn.ledger_path, session)
    # Old review markers remain authoritative after the bounded cache evicts them.
    if (metrics.pop("reviewed")
            or not metrics["served"] and not metrics["used"]):
        return None
    return metrics


def review_usage_session(dyn: Dynamics, session: str) -> dict | None:
    """Review notes or source keys when no knowledge-gap build is needed."""
    session = str(session)
    with sidecar_lock(os.path.dirname(dyn.dir), "review",
                      timeout=REVIEW_LOCK_TIMEOUT):
        metrics = _pending_metrics(dyn, session)
        if metrics is None:
            return None
        return _apply_session_review(dyn, session, metrics)


def review_session(bundle: Bundle, dyn: Dynamics, session: str,
                   build_gaps: bool = True,
                   workdir: str | None = None) -> dict | None:
    """Reflect on one completed session and repair bounded retrieval gaps."""
    session = str(session)
    # Acquire the review lock before individual ledger locks, never the reverse.
    with sidecar_lock(os.path.dirname(dyn.dir), "review",
                      timeout=REVIEW_LOCK_TIMEOUT):
        metrics = _pending_metrics(dyn, session)
        if metrics is None:
            return None
        built = (_build_gaps(bundle, dyn, os.path.abspath(workdir or os.getcwd()))
                 if build_gaps else [])
        return _apply_session_review(dyn, session, metrics, built)


def review_if_due(bundle: Bundle, dyn: Dynamics,
                  workdir: str | None = None) -> dict | None:
    """The ambient entry point (session-end hook): reflect on the last
    session exactly once. MUNINN_NO_REVIEW=1 is the kill switch: the
    loop is opt-out-able at any moment, and nothing else changes."""
    if os.environ.get("MUNINN_NO_REVIEW"):
        return None
    session = dyn.last_session_id
    if not session or session in dyn.reviewed:
        return None
    return review_session(bundle, dyn, session, workdir=workdir)


def recent_reviews(ledger_path: str, k: int = 10) -> list[dict]:
    """The last ``k`` sealed reviews, oldest first: the observability
    read for `muninn review`."""
    out: list[dict] = []
    try:
        fh = open(ledger_path, encoding="utf-8")
    except OSError:
        return out
    with fh:
        for line in fh:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict) and ev.get("kind") == "review":
                out.append(ev)
    return out[-k:]
