"""Compute deterministic multi-cue activation over one merged graph.

Weighted query facets seed a bounded walk over authored links and learned
co-use relationships. An :class:`ActivationMap` records each final score and
the evidence that produced it.

Graph support can add at most 50 percent to a direct lexical score. A note
without a lexical match can inherit at most 0.4 times the strongest direct
score. Usage strength remains within a 0.8 to 1.2 multiplier, and an active
goal remains within a 1.0 to 1.3 multiplier. These limits allow reordering but
prevent an adaptive signal from excluding a relevant note or outranking the
strongest lexical result. Fixed iterations, sorted adjacency, and rounded
scores make the calculation deterministic.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field

from .dynamics import PIN_FLOOR, Dynamics
from .store import Bundle, Note

# -- lexical primitives (moved from recall.py, re-exported there) ----------

STOPWORDS = frozenset(
    "a an and are as at be but by for from has have how in is it of on or "
    "that the this to was we what when where which who will with you your "
    "does do not no can cannot could should would may might my me i our us "
    "if then than so just also".split())

FIELD_WEIGHTS = {"title": 3.0, "tags": 3.0, "description": 2.0,
                 "headings": 2.0, "body": 1.0}

_HEADING = re.compile(r"^#+\s+(.*)$", re.M)
_WORD = re.compile(r"[^\W_][\w.-]*")

# Tiny suffix stemmer: longest suffix first, replacement applied only when
# the stem stays >= 3 chars and is not itself a stopword; two passes so a
# plural can shed one more suffix ("decays" -> "decay" -> "decai"). Both
# cue and note fields pass through tokens(), so same-form matching is
# untouched: this only bridges cross-form pairs (dormant ~ dormancy,
# correction ~ corrections). Identifier-ish tokens (digits, dots, dashes)
# are never stemmed.
_SUFFIXES = (("ions", ""), ("ancy", "an"), ("ance", "an"), ("ies", "i"),
             ("ing", ""), ("ion", ""), ("ity", ""), ("ant", "an"),
             ("es", ""), ("ed", ""), ("s", ""), ("y", "i"))


def _stem(w: str) -> str:
    if len(w) <= 3 or not w.isascii() or not w.isalpha():
        return w
    for _ in range(2):
        for suf, rep in _SUFFIXES:
            if w.endswith(suf):
                cand = w[:-len(suf)] + rep
                if len(cand) >= 3 and cand not in STOPWORDS:
                    w = cand
                    break  # one rule per pass
        else:
            break  # no rule applied; a second pass cannot help
    return w


# Identifier subtokens (field-tested on Flask): code-repo knowledge speaks
# in `teardown_request` / `createUrlAdapter` / `ctx.py`, but agents ask in
# words: a whole-identifier index makes prose queries structurally unable
# to reach identifier-bearing notes. Split camelCase before lower() erases
# the boundary; split _.- identifiers into parts EMITTED ALONGSIDE the
# whole token, so exact-form matches keep their strength.
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_IDENT_SEP = re.compile(r"[_.\-]+")


def terms(text: str) -> list[str]:
    """Return normalized terms while preserving frequency.

    ``tokens`` remains the set-valued compatibility surface used by the
    existing overlap ranker.  Frequency-aware rankers, including BM25F, use
    this form so repeated evidence and document length remain measurable.
    Identifier subtokens are emitted beside the complete identifier.
    """
    out: list[str] = []
    normalized = unicodedata.normalize("NFC", text)
    for w in _WORD.findall(_CAMEL.sub(" ", normalized).lower()):
        if w in STOPWORDS:
            continue
        out.append(_stem(w))
        if "_" in w or "." in w or "-" in w:
            out.extend(_stem(p) for p in _IDENT_SEP.split(w)
                       if len(p) >= 2 and p not in STOPWORDS)
    return out


def tokens(text: str) -> set[str]:
    """Return the distinct normalized terms used by overlap scoring."""
    return set(terms(text))


def _fields(note: Note) -> dict[str, set[str]]:
    return {
        "title": tokens(note.title),
        "tags": tokens(" ".join(note.tags)),
        "description": tokens(note.description),
        "headings": tokens(" ".join(_HEADING.findall(note.body))),
        "body": tokens(note.body),
    }


def relevance(cue_toks: set[str], note: Note,
              fields: dict[str, set[str]] | None = None) -> float:
    """Field-weighted lexical match. ``fields`` lets a caller scoring many
    cues against the same note tokenize it once (pure cache, same result)."""
    if not cue_toks:
        return 0.0
    score = 0.0
    for name, toks in (fields or _fields(note)).items():
        if toks:
            score += FIELD_WEIGHTS[name] * len(cue_toks & toks)
    return score / len(cue_toks)


def _non_url_tokens(note: Note) -> set[str]:
    """Tokens of the note with URL-ish chunks removed (any whitespace run
    containing '/' or starting 'www.'). Where a single-token goal match
    must land to count as alignment."""
    text = " ".join((note.title, " ".join(note.tags), note.description,
                     note.body))
    return tokens(" ".join(w for w in text.split() if "/" not in w
                           and not w.lower().startswith("www.")))


def goal_alignment(goal_toks: set[str], note: Note,
                   fields: dict[str, set[str]] | None = None) -> float:
    """Goal-side relevance, stricter than the query's (goals tilt, they
    never seed): zero unless the goal matches >=2 distinct tokens, or
    its one matching token occurs outside URL-ish text. A lone 'docs'
    from 'github.com/x/docs/…' in a title is a path fragment: matching
    it on every pack was the round-2 goal slot tax (N1)."""
    if not goal_toks:
        return 0.0
    flds = fields or _fields(note)
    matched: set[str] = set()
    for toks in flds.values():
        matched |= goal_toks & toks
    if not matched:
        return 0.0
    if len(matched) == 1 and not (matched & _non_url_tokens(note)):
        return 0.0
    return relevance(goal_toks, note, flds)


# -- bounded blend constants ------------------------------------------------

WALK_GAIN_CAP = 0.5   # graph support boosts a direct hit by at most +50%
ORPHAN_CAP = 0.4      # a zero-lexical note caps at 0.4x the top direct hit
# The optional semantic channel uses cosine similarity above EMBED_COS_FLOOR
# as a second relevance source. EMBED_WEIGHT scales the value, and ORPHAN_CAP
# limits it against the strongest lexical result. A semantic-only result
# cannot outrank a strong lexical match. When the note also matches words,
# the semantic value enters the ordinary bounded gain calculation. Without an
# endpoint or cache, scoring remains byte-identical to lexical scoring.
EMBED_COS_FLOOR = 0.3  # cosine at/below this is noise: contributes nothing
EMBED_WEIGHT = 2.0     # scales the bounded cosine surplus into the lexical seed
BASE_PRIOR = 0.25     # untouched notes still receive flow (strength adds)
# Strength REORDERS, it never gates: bounded 0.8..1.2 (same as before).
TEST_DAMP = 0.5  # lexical seed multiplier for notes tagged `test`
ASSOC_WEIGHT = 0.5   # scale of the learned-association seed (review.py)
ASSOC_MIN_OVERLAP = 2  # cue tokens an association must share to fire
WASTE_N = 3          # consecutive wasted serves before the damp applies
WASTE_DAMP = 0.8     # One bounded reduction; independent use resets it.
STRENGTH_LO = 0.8
STRENGTH_SPAN = 0.4
# Active goals tilt the same bounded way: up to 1.3x, never a gate.
GOAL_SPAN = 0.3


@dataclass
class Cue:
    """One weighted retrieval facet: a query clause, a quoted span, a rare
    token group, an active goal, or a bit of the situation."""

    text: str
    weight: float = 1.0
    origin: str = "query"


@dataclass
class Evidence:
    """Why a note is activated: which facets hit it lexically, and which
    hop/edge carried walk activation to it."""

    facets: list[str] = field(default_factory=list)   # cue texts that hit
    carrier: tuple[str, str] | None = None            # (source path, 'link'|'co-use')
    hop: int = 0                                      # walk iteration of the carrier
    direct: float = 0.0                               # weighted lexical seed
    walk: float = 0.0                                 # walk-inherited activation
    gain: float = 0.0                                 # applied score gain over pure direct
    strength: float = 0.0                             # floored usage strength
    unused: bool = False                              # never touched (nor pinned)
    goal: str = ""                                    # best matching active goal
    goal_rel: float = 0.0
    captured: bool = False                            # preceded a strong outcome
    assoc: bool = False                               # carried by a learned assoc
    relation: str = ""                                # typed one-hop relation
    relation_seed: str = ""                           # exact seed note path
    relation_direction: str = ""                      # in | out from the seed
    relation_hop: int = 0                             # currently one when present


@dataclass
class ActivationMap:
    """The one object recall spends: final scores + per-note evidence."""

    scores: dict[str, float]
    evidence: dict[str, Evidence]


def superseded_paths(bundle: Bundle, dyn: Dynamics | None) -> set[str]:
    """Paths a default recall will not return: superseded at the file level
    (frontmatter ``supersedes``), at the dynamics level (supersede
    events), or carrying OKF v0.2 ``status: deprecated`` (§5.4: "kept
    for links and history; no longer current": exactly the superseded
    contract, declared by the producer instead of a correcting note).
    The single admissibility rule shared by scoring and ranking."""
    sup = set(bundle.superseded_by())
    sup |= {p for p, n in bundle.notes.items()
            if n.status() == "deprecated"}
    if dyn is not None:
        sup |= {p for p, e in dyn.entries.items() if e.get("superseded")}
    return sup


def _floored_strength(dyn: Dynamics | None, note: Note) -> float:
    """Usage strength with the frontmatter pin floor. Dynamics-level pins
    already keep ``dyn.strength`` at or above PIN_FLOOR by construction."""
    s = dyn.strength(note.path) if dyn is not None else 0.0
    if note.meta.get("pinned"):
        s = max(s, PIN_FLOOR)
    return s


# -- cue builders ------------------------------------------------------------

# single-quote spans need word boundaries or contractions ("I've") open them
_QUOTED = re.compile(r"\"([^\"]{3,})\"|(?:^|(?<=\s))'([^']{3,})'(?=\W|$)")
_SENTENCES = re.compile(r"[.?!;\n]+")
_CLAUSES = re.compile(r",|\band\b|\bor\b|\bbut\b|\bversus\b|\bvs\b", re.I)


def _rare(tok: str) -> bool:
    """Specific-looking token: long, or carrying a digit (ports, dates)."""
    return tok not in STOPWORDS and (len(tok) >= 5 or any(c.isdigit() for c in tok))


def cues_from_query(query: str, max_cues: int = 10) -> list[Cue]:
    """Decompose a query into weighted facets: the full query, quoted spans,
    sentence/clause splits, and rare-token groups. Deterministic; facets
    whose token set duplicates an earlier cue are dropped."""
    query = " ".join(query.split())
    cues: list[Cue] = []
    seen: set[frozenset[str]] = set()

    def add(text: str, weight: float, origin: str, min_toks: int = 2) -> None:
        text = " ".join(text.split())
        toks = frozenset(tokens(text))
        if len(toks) < min_toks or toks in seen or len(cues) >= max_cues:
            return
        seen.add(toks)
        cues.append(Cue(text, weight, origin))

    add(query, 1.0, "query", min_toks=1)
    for m in _QUOTED.finditer(query):
        add(m.group(1) or m.group(2) or "", 0.9, "quote")
    parts = [c for s in _SENTENCES.split(query) for c in _CLAUSES.split(s)]
    if len(parts) > 1:
        for clause in parts:
            add(clause, 0.7, "clause")
    run: list[str] = []
    groups: list[list[str]] = []
    for w in _WORD.findall(query.lower()):
        if _rare(w):
            run.append(w)
        elif run:
            groups.append(run)
            run = []
    if run:
        groups.append(run)
    groups.sort(key=len, reverse=True)  # multi-token groups first (stable)
    for g in groups[:4]:
        add(" ".join(g[:3]), 0.5, "rare", min_toks=1)
    return cues


def cues_from_goals(dyn: Dynamics | None) -> list[Cue]:
    """Active goals as explicit cues (for callers that want goals to seed
    the walk rather than only tilt scores)."""
    if dyn is None:
        return []
    return [Cue(text, min(1.0, w), "goal")
            for text, w in sorted(dyn.goals.items()) if w > 0]


def cues_from_situation(bundle: Bundle, dyn: Dynamics | None,
                        workdir: str | None = None) -> list[Cue]:
    """Harvest the SITUATION as cues: no query needed (absorbs the old
    prime_cue harvesting). The environment (where you are, what changed,
    what you were just doing) activates memory, the way walking into the
    kitchen reminds you why you came. Deterministic, no LLM, fails soft."""
    import subprocess
    wd = os.path.abspath(workdir or os.getcwd())

    def _git(*args: str) -> str:
        try:
            r = subprocess.run(["git", "-C", wd, *args], capture_output=True,
                               text=True, timeout=5)
            return r.stdout.strip() if r.returncode == 0 else ""
        except Exception:
            return ""

    cues: list[Cue] = []
    place = " ".join(b for b in (os.path.basename(wd),
                                 os.path.basename(os.path.dirname(wd))) if b)
    if place:
        cues.append(Cue(place, 0.8, "cwd"))
    # symbolic-ref first: it works on an UNBORN branch (a fresh clone/init
    # with no commits: the normal state of a just-spawned agent
    # container), where rev-parse fails and the branch cue silently
    # vanished; a detached HEAD's literal "HEAD" is not a useful cue
    branch = (_git("symbolic-ref", "--short", "-q", "HEAD")
              or _git("rev-parse", "--abbrev-ref", "HEAD"))
    branch = "" if branch == "HEAD" else branch
    if branch:
        cues.append(Cue(branch, 0.6, "branch"))
    names = []
    for line in _git("status", "--porcelain").splitlines()[:10]:
        # porcelain = 2 status chars + separator; slice at 2 and strip so a
        # one-space separator can't clip the filename's first letter
        name = line[2:].strip().split(" -> ")[-1].split("/")[-1]
        names.append(name.rsplit(".", 1)[0].replace("-", " ").replace("_", " "))
    changes = " ".join(n for n in names if n)
    if changes:
        cues.append(Cue(changes, 0.8, "changes"))
    subject = _git("log", "-1", "--format=%s")
    if subject:
        cues.append(Cue(subject, 0.6, "commit"))
    if dyn is not None:  # what you were just doing primes its associates
        for path in dyn.recent(5):
            note = bundle.notes.get(path)
            if note and note.title:
                cues.append(Cue(note.title, 0.5, "recent"))
    return cues


# -- the merged graph and the measured walk ----------------------------------

def merged_graph(
    bundle: Bundle,
    dyn: Dynamics | None,
    include_stale: bool = False,
) -> dict[str, list[tuple[str, float, str]]]:
    """Return a sorted undirected graph of authored and learned relationships.

Typed links retain their stated weight, plain links use weight 1.0, and co-use
relationships use at most ``min(1, weight / 3)``. When several relationships
connect the same pair, the highest weight remains; an authored relationship
wins a tie. Superseded notes are excluded unless ``include_stale`` is true.
    """
    pair: dict[tuple[str, str], tuple[float, str]] = {}
    noncurrent = set() if include_stale else superseded_paths(bundle, dyn)
    for path, note in bundle.notes.items():
        if path in noncurrent:
            continue
        typed: dict[str, float] = {}
        for e in note.typed_links:
            typed[e["target"]] = max(typed.get(e["target"], 0.0), e["weight"])
        plain = set(note.plain_links)
        for tgt in note.links:
            if tgt != path and tgt in bundle.notes and tgt not in noncurrent:
                key = (path, tgt) if path < tgt else (tgt, path)
                w = 1.0 if tgt in plain else typed.get(tgt, 1.0)
                if key not in pair or w > pair[key][0]:
                    pair[key] = (w, "link")
    if dyn is not None:
        for a, nbrs in dyn.coactivation().items():
            if a not in bundle.notes or a in noncurrent:
                continue
            for b, w in nbrs.items():
                if a >= b or b not in bundle.notes or b in noncurrent:
                    continue  # visit each pair once, smaller path first
                cw = min(1.0, w / 3.0)
                if (a, b) not in pair or cw > pair[(a, b)][0]:
                    pair[(a, b)] = (cw, "co-use")
    adj: dict[str, list[tuple[str, float, str]]] = {}
    for (a, b), (w, kind) in sorted(pair.items()):
        adj.setdefault(a, []).append((b, w, kind))
        adj.setdefault(b, []).append((a, w, kind))
    for nbrs in adj.values():
        nbrs.sort()
    return adj


def activate(
    bundle: Bundle,
    dyn: Dynamics | None,
    cues: list[Cue],
    iters: int = 3,
    damping: float = 0.5,
    include_stale: bool = False,
) -> ActivationMap:
    """Combine cue relevance, graph support, and bounded adaptive signals.

Each cue produces field-weighted lexical seeds. A degree-normalized walk then
uses authored and co-use edges. Optional semantic and learned-association
channels can admit a note within the orphan limit. Usage strength and active
goals apply their documented multipliers after the walk. Setting ``iters=0``
produces lexical scoring on the same code path. Rounded scores and sorted
relationships make the result deterministic.
    """
    cue_toks = [(c, toks) for c in cues if (toks := tokens(c.text))]
    evidence = {path: Evidence() for path in bundle.notes}
    fields_by_path = {path: _fields(note) for path, note in bundle.notes.items()}

    lex: dict[str, float] = {}
    for path, note in bundle.notes.items():
        flds = fields_by_path[path]
        total = 0.0
        for c, toks in cue_toks:
            r = relevance(toks, note, flds)
            if r > 0:
                total += c.weight * r
                evidence[path].facets.append(c.text)
        if total > 0:
            # Test-origin notes use TEST_DAMP because test names often contain
            # the query vocabulary and can rank above production notes. An
            # explicit `test` cue restores relevance through the tag field.
            if "test" in note.tags:
                total *= TEST_DAMP
            lex[path] = total

    # -- optional semantic seed channel (embed.py, lazily imported) --------
    # A second relevance source: cosine(cue, note) above EMBED_COS_FLOOR,
    # summed across cues (weighted like the lexical seed). Available only
    # when a local endpoint is configured or a cache exists; a semantic
    # CONTRIBUTION additionally needs a live endpoint to embed the cues, so
    # a dead/absent endpoint (even with a note-vector cache) yields no
    # semantic seed and recall stays byte-identically lexical.
    sem: dict[str, float] = {}
    from . import embed as _embed
    if _embed.available(bundle):  # raises on a non-local MUNINN_EMBED_URL
        note_vecs = _embed.embed_notes(bundle, dyn)  # {} = fail-soft
        cue_vecs = ([(c, v) for c, _t in cue_toks
                     if (v := _embed.embed_text(c.text)) is not None]
                    if note_vecs else [])
        for path, nvec in note_vecs.items():
            s = 0.0
            for c, cvec in cue_vecs:
                cs = _embed.cosine(cvec, nvec)
                if cs > EMBED_COS_FLOOR:
                    s += c.weight * (cs - EMBED_COS_FLOOR)
            if s > 0:
                sem[path] = EMBED_WEIGHT * s
                evidence[path].facets.append("semantic")

    # -- learned-association channel (the reflection loop, review.py) ----
    # A missed note can acquire a bounded cue-to-note association after
    # independent use. The semantic orphan cap allows candidacy but prevents
    # the association from creating the strongest result.
    asc: dict[str, float] = {}
    if dyn is not None and dyn.assocs:
        all_toks: set[str] = set()
        for _c, t in cue_toks:
            all_toks |= t
        for path, a in dyn.assocs.items():
            if path not in bundle.notes:
                continue
            overlap = all_toks & set(a.get("toks", []))
            if len(overlap) >= ASSOC_MIN_OVERLAP:
                asc[path] = (ASSOC_WEIGHT * float(a.get("w", 0.0))
                             * min(1.0, len(overlap) / 4.0))
                evidence[path].facets.append("learned")
                evidence[path].assoc = True

    # The direct seed = lexical + bounded semantic. The orphan cap ceiling
    # is the strongest NON-superseded LEXICAL hit, so a semantic-only note
    # can enter candidacy but never top a strong lexical match (§ constants).
    sup = set() if include_stale else superseded_paths(bundle, dyn)
    top_lexical = max((v for p, v in lex.items() if p not in sup), default=0.0)
    seed: dict[str, float] = {}
    for path in bundle.notes:
        d = lex.get(path, 0.0)
        s = sem.get(path, 0.0)
        if s > 0 and top_lexical > 0:
            s = min(s, ORPHAN_CAP * top_lexical)  # bounded like the walk orphan
        a = asc.get(path, 0.0)
        if a > 0 and top_lexical > 0:
            a = min(a, ORPHAN_CAP * top_lexical)  # learned lifts share the cap
        total = d + s + a
        if total > 0:
            seed[path] = total
            evidence[path].direct = round(total, 4)

    prior = {path: BASE_PRIOR + _floored_strength(dyn, note)
             for path, note in bundle.notes.items()}

    adj = merged_graph(bundle, dyn, include_stale=include_stale)
    walk: dict[str, float] = {}
    best_carry: dict[str, float] = {}
    frontier = dict(seed)
    for it in range(1, max(0, iters) + 1):
        if not frontier:
            break
        nxt: dict[str, float] = {}
        for m in sorted(frontier):
            amt = frontier[m]
            nbrs = adj.get(m)
            if amt <= 1e-9 or not nbrs:
                continue
            z = sum(w * prior[n] for n, w, _k in nbrs)
            if z <= 0:
                continue
            out = amt * damping
            for n, w, kind in nbrs:
                share = out * (w * prior[n]) / z
                if share <= 1e-9:
                    continue
                nxt[n] = nxt.get(n, 0.0) + share
                if share > best_carry.get(n, 0.0):
                    best_carry[n] = share
                    evidence[n].carrier = (m, kind)
                    evidence[n].hop = it
        for n, v in nxt.items():
            walk[n] = walk.get(n, 0.0) + v
        frontier = nxt

    # top_lexical was computed over the notes admissible for this recall.
    goal_toks: list[tuple[str, set[str], float]] = []
    if dyn is not None:
        goal_toks = [(g, tokens(g), w) for g, w in sorted(dyn.goals.items())]

    scores: dict[str, float] = {}
    for path, note in bundle.notes.items():
        d = seed.get(path, 0.0)       # lexical + bounded semantic (feeds gain)
        d_lex = lex.get(path, 0.0)    # lexical only: selects the branch
        wk = walk.get(path, 0.0)
        ev = evidence[path]
        ev.walk = round(wk, 4)
        if d_lex > 0:
            # a real lexical hit: saturating gain, strictly below the cap,
            # so near-peer hits with mutual graph support stay compressed
            # enough for the bounded strength band to reorder them (never a
            # hard cliff). Any semantic seed rides inside d as a second
            # relevance source; it never changes which branch runs.
            gain = WALK_GAIN_CAP * wk / (wk + d) if wk > 0 else 0.0
            rel = d * (1.0 + gain)
            ev.gain = round(gain, 4)
        elif top_lexical > 0 and (d > 0 or wk > 0):
            # NO lexical hit of its own: scored only by semantic seed and/or
            # walk inflow, bounded to reorder: capped at ORPHAN_CAP x the
            # strongest lexical hit and given NO gain, so the graph and the
            # embeddings lift a note into candidacy but never past (or below)
            # a note that actually matched the words (reorder, never bury).
            rel = min(d + wk, ORPHAN_CAP * top_lexical)
        elif d > 0:
            # a semantic seed with NO lexical anchor anywhere in the bundle
            # (top_lexical == 0: nothing to bound against): score it directly.
            # A pure-walk note with no anchor (d == 0) still drops here, so
            # pure-lexical recall stays byte-identical to pre-embed.
            rel = d + wk
        else:
            continue
        score = rel
        if dyn is not None:
            s = _floored_strength(dyn, note)
            ev.strength = round(s, 4)
            entry = dyn.entries.get(path)
            # never touched and not pinned: any strength it shows was
            # only the capped recall nudge: display it as unused, not
            # as a number that reads like a penalty
            ev.unused = ((entry is None or entry.get("recurrence", 0) == 0)
                         and not (entry or {}).get("pinned")
                         and not note.meta.get("pinned"))
            score = rel * (STRENGTH_LO + STRENGTH_SPAN * min(1.0, s))
            best_goal, goal_rel = "", 0.0
            for gtext, gtoks, gw in goal_toks:
                r = goal_alignment(gtoks, note, fields_by_path[path]) * gw
                if r > goal_rel:
                    best_goal, goal_rel = gtext, r
            if goal_rel > 0:
                score *= 1.0 + GOAL_SPAN * min(1.0, goal_rel)
                ev.goal, ev.goal_rel = best_goal, round(goal_rel, 4)
            ev.captured = dyn.entries.get(path, {}).get("captured", 0.0) > 0.2
            if dyn.serve_miss.get(path, 0) >= WASTE_N:
                # A repeatedly unused served note receives one bounded step.
                # never compounding, forgiven by the first real touch
                score *= WASTE_DAMP
        scores[path] = round(score, 6)
    return ActivationMap(scores=scores, evidence=evidence)
