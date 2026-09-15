"""Promote repeated feedback into local style rules.

The agent records explicit feedback because Muninn does not read conversations.
Feedback is clustered by domain, polarity, and token overlap. Two related
events can appear as an unconfirmed preference in a context pack. Three events
across at least two sessions promote a dated rule under
``style-learned/<domain>.md``.

A repeated reversal supersedes the earlier rule without deleting it. The
adopted style repository remains unchanged, and raw feedback remains in the
private ledger. Deterministic clustering and ledger events make the rule
registry rebuildable. ``MUNINN_NO_EVOLVE=1`` disables ambient promotion.
"""

from __future__ import annotations

import hashlib
import json
import os
import time

from .activate import tokens
from .dynamics import Dynamics
from .journal import scrub
from .store import Bundle

PROMOTE_N = 3       # feedback events a cluster needs to become a rule
MIN_SESSIONS = 2    # ... across at least this many distinct sessions
OVERLAP = 2         # shared content tokens that cluster two feedbacks
WHISPER_MIN = 2     # sightings before a candidate whispers into packs
WHISPER_SHOWN = 3   # bound on whispered candidates
LEARNED_DIR = "style-learned"


def feedback_events(ledger_path: str) -> list[dict]:
    """All feedback events, oldest first."""
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
            if isinstance(ev, dict) and ev.get("kind") == "feedback":
                out.append({
                    "text": str(ev.get("text", "")),
                    "domain": str(ev.get("domain", "general")),
                    "polarity": 1 if ev.get("polarity", -1) > 0 else -1,
                    "session": (ev["session"].strip()
                                if isinstance(ev.get("session"), str) else ""),
                    "ts": float(ev.get("ts", 0) or 0)})
    return out


def _toks(text: str) -> set[str]:
    return {t for t in tokens(text) if len(t) > 2}


def _clusters(fbs: list[dict]) -> list[dict]:
    """Greedy token-overlap clustering within a domain. Deterministic:
    events oldest-first, first matching cluster wins."""
    clusters: list[dict] = []
    for fb in sorted(fbs, key=lambda f: (f["ts"], f["text"])):
        ft = _toks(fb["text"])
        home = None
        for c in clusters:
            # OPPOSITE-polarity feedback on the same ground is its OWN
            # candidate: merged, a reversal would cancel itself out and
            # the guide could never change its mind
            if (c["domain"] == fb["domain"]
                    and c["polarity"] == fb["polarity"]
                    and len(c["toks"] & ft) >= OVERLAP):
                home = c
                break
        if home is None:
            home = {"domain": fb["domain"], "toks": set(), "count": 0,
                    "sessions": set(), "polarity": fb["polarity"],
                    "text": fb["text"], "ts": fb["ts"]}
            clusters.append(home)
        home["toks"] |= ft
        home["count"] += 1
        if fb["session"]:
            home["sessions"].add(fb["session"])
        if fb["ts"] >= home["ts"]:  # the rule speaks in the freshest words
            home["text"], home["ts"] = fb["text"], fb["ts"]
    return clusters


def _rule_id(domain: str, toks, polarity: int) -> str:
    # polarity is part of the identity: a reversal phrased in the SAME
    # words must mint a new rule, never overwrite the superseded one :
    # "marked, dated, never deleted" depends on distinct ids
    core = ",".join(sorted(toks)[:6])
    return "r-" + hashlib.sha1(
        f"{domain}|{polarity}|{core}".encode()).hexdigest()[:10]


def _write_learned(bundle: Bundle, dyn: Dynamics) -> list[str]:
    """Render every domain's learned file FROM THE REGISTRY (replay-safe,
    idempotent): active rules with evidence counts, superseded rules
    struck through and dated: the compiled law over an auditable past."""
    by_domain: dict[str, list[tuple[str, dict]]] = {}
    for rid, r in sorted(dyn.rules.items()):
        by_domain.setdefault(r.get("domain", "general"), []).append((rid, r))
    written = []
    for domain, rules in sorted(by_domain.items()):
        lines = ["Learned from repeated feedback: promoted only after "
                 f"{PROMOTE_N}+ events across {MIN_SESSIONS}+ sessions; "
                 "raw evidence lives in the private ledger.", ""]
        for _rid, r in rules:
            if r.get("status") == "superseded":
                continue
            lines.append(f"- {r['text']}  *(evidence: {r.get('count', 0)} "
                         f"event(s), {r.get('sessions', 0)} session(s), "
                         f"promoted {r.get('date', '')})*")
        gone = [(rid, r) for rid, r in rules
                if r.get("status") == "superseded"]
        if gone:
            lines.append("")
            for _rid, r in gone:
                lines.append(f"- ~~{r['text']}~~  *(superseded "
                             f"{r.get('superseded_date', '')})*")
        rel = f"{LEARNED_DIR}/{domain}.md"
        bundle.write_note(rel, {"type": "learned-style",
                                "title": f"Learned style: {domain}",
                                "pinned": True, "provenance": "curated"},
                          "\n".join(lines))
        written.append(rel)
    return written


def evolve_once(bundle: Bundle, dyn: Dynamics) -> dict:
    """One idempotent pass: cluster all feedback, promote what qualifies,
    demote what repeated opposite evidence contradicts, rewrite the
    learned overlay. Returns {promoted, demoted, candidates}."""
    fbs = feedback_events(dyn.ledger_path)
    result = {"promoted": [], "demoted": [], "candidates": []}
    if not fbs:
        return result
    changed = False
    for c in _clusters(fbs):
        qualifies = (c["count"] >= PROMOTE_N
                     and len(c["sessions"]) >= MIN_SESSIONS)
        pol = c["polarity"]
        # every rule on the same ground: active OR superseded: the
        # registry's history on this territory, not just its present
        ground = [(rid, r) for rid, r in sorted(dyn.rules.items())
                  if r.get("domain") == c["domain"]
                  and len(set(r.get("toks", [])) & c["toks"]) >= OVERLAP]
        active = next(((rid, r) for rid, r in ground
                       if r.get("status") != "superseded"), None)
        if not qualifies:
            if c["count"] >= WHISPER_MIN and active is None:
                result["candidates"].append(
                    {"text": c["text"], "count": c["count"],
                     "domain": c["domain"]})
            continue
        # the registry's last word on this ground: evidence that is not
        # NEWER than it can neither resurrect a superseded rule nor
        # re-fight a settled reversal: without this, the ledger being
        # append-only would make every pass after a reversal demote and
        # re-promote both sides forever
        latest = max((max(float(r.get("ts", 0) or 0),
                          float(r.get("superseded_ts", 0) or 0))
                      for _rid, r in ground), default=0.0)
        if ground and c["ts"] <= latest:
            continue
        if active is not None:
            if active[1].get("polarity") == pol:
                continue  # already law: new evidence just agrees
            dyn.rule_demote(active[0])  # repeated opposite evidence wins
            result["demoted"].append(active[1]["text"])
            changed = True
        dyn.rule_mark(_rule_id(c["domain"], c["toks"], pol), c["domain"],
                      c["text"], sorted(c["toks"])[:12], pol, c["count"],
                      len(c["sessions"]))
        result["promoted"].append(c["text"])
        changed = True
    if changed:
        _write_learned(bundle, dyn)
    return result


def evolve_if_due(bundle: Bundle, dyn: Dynamics) -> dict | None:
    """The ambient entry point (rides session-end, next to review).
    MUNINN_NO_EVOLVE=1 is the kill switch."""
    if os.environ.get("MUNINN_NO_EVOLVE"):
        return None
    if not feedback_events(dyn.ledger_path):
        return None
    return evolve_once(bundle, dyn)


def whisper_section(dyn: Dynamics) -> str:
    """Unconfirmed candidates ride primed packs immediately: behavior
    adapts today, identity changes only on promotion. Bounded, labeled
    as unconfirmed, gone when promoted or when the feedback stops."""
    fbs = feedback_events(dyn.ledger_path)
    if not fbs:
        return ""
    cands = []
    for c in _clusters(fbs):
        promoted = any(r.get("status") != "superseded"
                       and r.get("domain") == c["domain"]
                       and len(set(r.get("toks", [])) & c["toks"]) >= OVERLAP
                       for r in dyn.rules.values())
        if not promoted and WHISPER_MIN <= c["count"]:
            cands.append(c)
    if not cands:
        return ""
    cands.sort(key=lambda c: (-c["count"], -c["ts"]))
    lines = ["## Recently observed (unconfirmed)",
             "*(repeated feedback, not yet promoted to the style guide: "
             "lean this way)*", ""]
    for c in cands[:WHISPER_SHOWN]:
        lines.append(f"- {c['text']}  *(seen {c['count']}×, "
                     f"{c['domain']})*")
    return "\n".join(lines)


def note_feedback(dyn: Dynamics, text: str, domain: str = "general",
                  polarity: int = -1, session: str | None = None) -> None:
    """Log one preference observation (scrubbed, bounded)."""
    dyn.feedback(scrub(str(text)), domain=domain, polarity=polarity,
                 session=session)


_ = time  # (kept for symmetry with sibling modules' date stamping)
