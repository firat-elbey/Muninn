"""Maintain consumer-specific retrieval state in an append-only sidecar.

Independent use strengthens a note, consolidation reduces unused strength,
recorded outcomes raise the salience of recent precursors, supersession lowers
obsolete notes, and pinned notes retain a floor. Recall alone contributes only
a small bounded value and is not treated as independent use.

``ledger.jsonl`` is authoritative. ``state.json`` is a rebuildable cache.
Both files reside under ``.muninn/``, which allows consumers to share one
knowledge bundle without sharing their retrieval history.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import posixpath
import stat
import tempfile
import time
from contextlib import contextmanager

DECAY = 0.98            # per-consolidation multiplicative decay of un-touched strength
CAPTURE_WINDOW = 12     # how many recently-touched notes a strong outcome captures
CAPTURE_DELTA = 0.5     # |valence| above which capture fires
CAPTURE_BOOST = 0.5     # capture adds min(1,|v|)*CAPTURE_BOOST, capped at 2.0
FLOOR = 0.15            # below this a note is dormant (not recalled proactively)
PIN_FLOOR = 0.5         # pinned notes never drop below this
LTD_FACTOR = 0.25       # superseded notes keep only this fraction of strength
W_RECURRENCE = 0.4
W_AFFECT = 0.3
W_RECALL = 0.1          # max priority a note can earn from recalls alone
BASE_IMPORTANCE = 0.3
SENSE_KEYS = 512        # bound on the sense-intake debounce map
INTENT_TTL = 24 * 3600  # default seconds before an unrefreshed intent expires
INTENT_KEYS = 64        # bound on stored intents (oldest-expiring falls off)
ASSOC_FLOOR = 0.1       # learned associations below this dissolve entirely
ASSOC_DECAY = DECAY ** 8  # ~0.85/tick: an unreinforced assoc dissolves in
#                           about two weeks of daily ticks (1.0 -> <0.1 in
#                           ~14): adaptation that stops paying off leaves
ASSOC_KEYS = 128        # bound on stored associations (weakest falls off)
GAP_KEYS = 256          # bound on the unmapped-path counter map
REVIEWED_KEYS = 100     # bound on remembered reviewed-session ids
RULE_KEYS = 128         # bound on the learned-rule registry
STATE_RULES = 10        # bump when event semantics change: stale caches
#                         derived under old rules are rebuilt from the
#                         ledger (v2: recalls no longer count as use;
#                         v3: sense state: session window, debounce map,
#                         consolidate anchor: is derived from the ledger;
#                         v4: intents: in-flight work announcements;
#                         v5: the reflection loop: assoc/gap/review;
#                         v6: the evolution loop: feedback/rule;
#                         v7: rules carry ts/superseded_ts: the evolve
#                         pass gates on them;
#                         v8: supersession note paths are canonicalized;
#                         v9: gap observations retain repository scope;
#                         v10: debounce retains session scope and replay
#                         normalizes scrubbed goal identities)


def repository_identity(root: str) -> str:
    """Hash a canonical source root without retaining its filesystem path."""
    return hashlib.sha256(os.fsencode(os.path.realpath(root))).hexdigest()


def gap_key(path: str, repository: str | None = None) -> str:
    """Keep historical gap keys distinct from validated repository scopes."""
    if repository is None:
        return path
    if (not isinstance(repository, str) or len(repository) != 64
            or any(character not in "0123456789abcdef" for character in repository)):
        return ""
    return f"{repository}:{path}"


def _session_sense_key(session: str, kind: str, path: str) -> str:
    """Encode an unambiguous session, event kind, and note cache key."""
    return json.dumps([str(session), kind, path], separators=(",", ":"))


@contextmanager
def sidecar_lock(root: str, name: str = "ledger", timeout: float = 2.0):
    """Serialize a local write without holding a lock during network work."""
    directory = os.path.join(os.path.abspath(root), ".muninn")
    os.makedirs(directory, exist_ok=True)
    descriptor = os.open(os.path.join(directory, name + ".lock"),
                         os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o600)
    with os.fdopen(descriptor, "a") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError(f"Muninn {name} write lock is not a regular file")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Muninn {name} write lock is busy")
                time.sleep(0.01)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _canonical_supersession_path(value: object) -> str:
    """Return one stable note key for a supersession ledger event."""

    raw = str(value).strip().replace("\\", "/").lstrip("/")
    if not raw:
        return ""
    if not raw.endswith(".md"):
        raw += ".md"
    if (
        "\x00" in raw
        or ".." in raw.split("/")
    ):
        return raw
    return posixpath.normpath(raw)


class Dynamics:
    """Usage-memory state for one bundle, persisted in <root>/.muninn/."""

    def __init__(self, root: str):
        self.dir = os.path.join(os.path.abspath(root), ".muninn")
        self.ledger_path = os.path.join(self.dir, "ledger.jsonl")
        self.state_path = os.path.join(self.dir, "state.json")
        self.entries: dict[str, dict] = {}
        self.order: list[str] = []  # touch order, most recent last
        self.coact: dict[str, int] = {}  # "a|b" (sorted) -> co-use count
        self._session_notes: dict[str, list[str]] = {}
        self.goals: dict[str, float] = {}  # active concerns -> weight
        self.intents: dict[str, dict] = {}  # branch -> in-flight claim
        # the reflection loop (review.py): all bounded, all replayable
        self.assocs: dict[str, dict] = {}     # note -> {toks, w, ts}
        self.serve_miss: dict[str, int] = {}  # note -> consecutive wasted serves
        self.gap_counts: dict[str, int] = {}  # repo-relative path -> sightings
        self.reviewed: list[str] = []         # session ids already reviewed
        self.rules: dict[str, dict] = {}      # learned-rule registry (evolve)
        self.session_cues: dict[str, str] = {}  # session -> prime cue text
        # sense-intake state (observe.py): all derived, rebuilt on replay
        self.last_session_id: str | None = None  # sliding session window …
        self.last_event_ts = 0.0     # … ts of its last session-carrying event
        self.first_event_ts = 0.0    # ts of the first ledger event ever
        self.last_consolidate_ts = 0.0  # ambient-decay anchor
        self.last_seen: dict[str, float] = {}  # "kind|note" -> ts (debounce)
        self.session_last_seen: dict[str, float] = {}  # encoded session/kind/note -> ts
        self._applied_bytes = 0  # ledger bytes this state has applied
        self._ledger_identity: list[int] | None = None
        self._load()

    # -- persistence ------------------------------------------------------

    def _load(self) -> None:
        try:
            stat = os.stat(self.ledger_path)
            ledger_size = stat.st_size
            self._ledger_identity = [stat.st_dev, stat.st_ino]
        except FileNotFoundError:
            ledger_size = 0
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, encoding="utf-8") as fh:
                    d = json.load(fh)
                if not (isinstance(d, dict) and isinstance(d.get("entries"), dict)
                        and isinstance(d.get("order"), list)
                        and isinstance(d.get("coact", {}), dict)
                        and isinstance(d.get("session_notes", {}), dict)):
                    raise ValueError("state.json has the wrong shape")
                # state.json is only a cache: it records how many ledger
                # bytes it has APPLIED. If the file holds more (another
                # consumer wrote, or a crash landed between log and save),
                # the ledger wins: rebuild from it.
                if d.get("applied_bytes", -1) != ledger_size:
                    raise ValueError("state.json is stale vs the ledger")
                if d.get("ledger_identity") != self._ledger_identity:
                    raise ValueError("state.json predates ledger replacement")
                # a cache derived under OLD event rules must not carry its
                # strengths forward (a bundle corrupted by the recalls-
                # count-as-use era heals here, on first load)
                if d.get("rules", 1) != STATE_RULES:
                    raise ValueError("state.json predates current rules")
                self.entries = d["entries"]
                self.order = d["order"]
                self.coact = d.get("coact", {})
                self._session_notes = d.get("session_notes", {})
                self.goals = d.get("goals", {}) if isinstance(
                    d.get("goals", {}), dict) else {}
                self.intents = d.get("intents", {}) if isinstance(
                    d.get("intents", {}), dict) else {}
                for key, typ in (("assocs", dict), ("serve_miss", dict),
                                 ("gap_counts", dict), ("reviewed", list),
                                 ("session_cues", dict)):
                    v = d.get(key, typ())
                    setattr(self, key, v if isinstance(v, typ) else typ())
                rr = d.get("rule_registry", {})  # `rules` names the rule count.
                self.rules = rr if isinstance(rr, dict) else {}  # is the
                #                                 state-format version
                self.last_session_id = (str(d["last_session_id"])
                                        if d.get("last_session_id") else None)
                self.last_event_ts = float(d.get("last_event_ts", 0) or 0)
                self.first_event_ts = float(d.get("first_event_ts", 0) or 0)
                self.last_consolidate_ts = float(
                    d.get("last_consolidate_ts", 0) or 0)
                seen = d.get("last_seen", {})
                self.last_seen = seen if isinstance(seen, dict) else {}
                session_seen = d.get("session_last_seen", {})
                self.session_last_seen = (session_seen
                                          if isinstance(session_seen, dict) else {})
                self._applied_bytes = ledger_size
                return
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        if os.path.exists(self.ledger_path):
            self.replay()

    def _save(self) -> None:
        os.makedirs(self.dir, exist_ok=True)
        descriptor, tmp = tempfile.mkstemp(prefix="state-", suffix=".tmp",
                                          dir=self.dir)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as fh:
                json.dump({"entries": self.entries, "order": self.order,
                           "coact": self.coact,
                           "session_notes": self._session_notes,
                           "goals": self.goals,
                           "intents": self.intents,
                           "assocs": self.assocs,
                           "serve_miss": self.serve_miss,
                           "gap_counts": self.gap_counts,
                           "reviewed": self.reviewed,
                           "session_cues": self.session_cues,
                           "rule_registry": self.rules,
                           "last_session_id": self.last_session_id,
                           "last_event_ts": self.last_event_ts,
                           "first_event_ts": self.first_event_ts,
                           "last_consolidate_ts": self.last_consolidate_ts,
                           "last_seen": self.last_seen,
                           "session_last_seen": self.session_last_seen,
                           "applied_bytes": self._applied_bytes,
                           "ledger_identity": self._ledger_identity,
                           "rules": STATE_RULES}, fh, indent=1)
            os.replace(tmp, self.state_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _log(self, event: dict) -> None:
        os.makedirs(self.dir, exist_ok=True)
        event.setdefault("ts", round(time.time(), 3))
        line = json.dumps(event, separators=(",", ":")) + "\n"
        with (sidecar_lock(os.path.dirname(self.dir)),
              open(self.ledger_path, "a", encoding="utf-8") as fh):
            stat = os.fstat(fh.fileno())
            identity = [stat.st_dev, stat.st_ino]
            if self._applied_bytes and self._ledger_identity != identity:
                self._applied_bytes = -1
            self._ledger_identity = identity
            fh.write(line)
        if self._applied_bytes >= 0:
            self._applied_bytes += len(line.encode("utf-8"))

    def replay(self) -> None:
        """Rebuild every derived field from the authoritative ledger."""
        self.entries, self.order = {}, []
        self.coact, self._session_notes = {}, {}
        self.goals = {}
        self.intents = {}
        self.assocs, self.serve_miss = {}, {}
        self.gap_counts, self.reviewed, self.session_cues = {}, [], {}
        self.rules = {}
        self.last_session_id, self.last_event_ts = None, 0.0
        self.first_event_ts, self.last_consolidate_ts = 0.0, 0.0
        self.last_seen = {}
        self.session_last_seen = {}
        if not os.path.exists(self.ledger_path):
            self._applied_bytes = 0
            self._ledger_identity = None
            return
        with open(self.ledger_path, encoding="utf-8") as fh:
            stat = os.fstat(fh.fileno())
            self._applied_bytes = stat.st_size
            self._ledger_identity = [stat.st_dev, stat.st_ino]
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                self._apply(ev, log=False)
        self._save()

    # -- events -----------------------------------------------------------

    def _entry(self, path: str) -> dict:
        e = self.entries.get(path)
        if e is None:
            e = {"recurrence": 0, "recalls": 0, "affect_mag": 0.0,
                 "captured": 0.0, "pinned": False, "superseded": False,
                 "last_ts": 0.0, "strength": 0.0}
            self.entries[path] = e
        return e

    def _wire(self, path: str, session: str) -> None:
        """Used together, surfaced together: notes touched in the same
        session get linked. Pair weights feed associative spreading at recall."""
        notes = self._session_notes.setdefault(session, [])
        for other in notes:
            if other == path:
                continue
            key = "|".join(sorted((path, other)))
            self.coact[key] = self.coact.get(key, 0) + 1
        if path not in notes:
            notes.append(path)
        if len(self._session_notes) > 50:  # keep only recent sessions
            oldest = next(iter(self._session_notes))
            del self._session_notes[oldest]

    def last_seen_in_session(self, session: str, kind: str, path: str) -> float:
        """Return the last independent use for this session, kind, and note."""
        return self.session_last_seen.get(_session_sense_key(session, kind, path),
                                          float("-inf"))

    def _mark_seen(self, kind: str, path: str, ts: float,
                   session: str | None = None) -> None:
        """Retain global and session observations in separate bounded maps.

        Each map keeps its last SENSE_KEYS updated keys in ledger order.
        Historical events without session identities update only the global map.
        """
        key = kind + "|" + path
        self.last_seen.pop(key, None)
        self.last_seen[key] = ts
        if len(self.last_seen) > SENSE_KEYS:
            del self.last_seen[next(iter(self.last_seen))]
        if session:
            key = _session_sense_key(session, kind, path)
            self.session_last_seen.pop(key, None)
            self.session_last_seen[key] = ts
            if len(self.session_last_seen) > SENSE_KEYS:
                del self.session_last_seen[next(iter(self.session_last_seen))]

    def _claim_window(self, session, ts: float) -> None:
        """A session-carrying event refreshes the sliding session window
        (observe.py infers ids for session-less ambient events from it)."""
        self.last_session_id = str(session)
        self.last_event_ts = ts

    def _apply(self, ev: dict, log: bool = True) -> None:
        kind = ev.get("kind")
        if kind == "supersede":
            ev["note"] = _canonical_supersession_path(ev.get("note", ""))
            ev["by"] = _canonical_supersession_path(ev.get("by", ""))
        # stamp the timestamp BEFORE logging so live state and a ledger
        # replay see the identical event (replay must rebuild exactly)
        ev.setdefault("ts", round(time.time(), 3))
        ts = ev["ts"] if isinstance(ev.get("ts"), (int, float)) else 0.0
        if not self.first_event_ts:  # the ambient-decay anchor for bundles
            self.first_event_ts = ts  # that never ran a consolidate
        if log:
            self._log(dict(ev))
        if kind in ("touch", "encode"):
            path = ev["note"]
            e = self._entry(path)
            e["recurrence"] += 1
            e["affect_mag"] = max(e["affect_mag"], abs(float(ev.get("valence", 0.0))))
            e["last_ts"] = ev.get("ts", time.time())
            if path in self.order:
                self.order.remove(path)
            self.order.append(path)
            self.serve_miss.pop(path, None)  # real use forgives waste
            self._mark_seen(kind, path, ts, session=ev.get("session"))
            if ev.get("session"):
                self._wire(path, str(ev["session"]))
                self._claim_window(ev["session"], ts)
            e["strength"] = self._priority(e)
        elif kind == "recall":
            # Recall is not independent use and does not enter the
            # capture window (self.order): only a small bounded counter.
            # Strength moves one recall-step at a time toward (never past)
            # the note's earned priority: a read-only query can neither
            # amplify junk nor undo accumulated decay the way a touch's
            # full snap-back does.
            e = self._entry(ev["note"])
            e["recalls"] = e.get("recalls", 0) + 1
            e["last_ts"] = ev.get("ts", time.time())
            e["strength"] = round(
                min(e["strength"] + W_RECALL / 10.0, self._priority(e)), 4)
            if ev.get("session"):
                # an EXPLICIT session ties this recall to ambient use
                # (prime --session, the agent hook): co-use learns what
                # was primed together: bounded edges only, still no
                # recurrence, no capture window, no strength beyond the
                # capped nudge. Source retrieval sets wire=false because
                # its served-vs-used review requires a session id but serving
                # alone must not create a personal source relation.
                if ev.get("wire", True):
                    self._wire(ev["note"], str(ev["session"]))
                self._claim_window(ev["session"], ts)
        elif kind == "outcome":
            v = float(ev.get("valence", 0.0))
            if abs(v) >= CAPTURE_DELTA:
                boost = min(1.0, abs(v)) * CAPTURE_BOOST
                for path in self.order[-CAPTURE_WINDOW:]:
                    e = self._entry(path)
                    e["captured"] = min(2.0, e["captured"] + boost)
                    # boost ADDS to the (possibly decayed) current strength,
                    # ceilinged at full priority: capture must not erase
                    # accumulated forgetting the way a full reset would
                    e["strength"] = round(
                        min(e["strength"] + boost, self._priority(e)), 4)
        elif kind == "pin":
            e = self._entry(ev["note"])
            e["pinned"] = bool(ev.get("value", True))
            if e["pinned"]:
                e["strength"] = max(e["strength"], self._priority(e))
            else:
                e["strength"] = min(e["strength"], self._priority(e))
        elif kind == "supersede":
            note = str(ev["note"])
            e = self._entry(note)  # the OLD note
            e["superseded"] = True
            # a correction only ever weakens: never resurrect decayed strength
            e["strength"] = min(e["strength"], self._priority(e))
        elif kind == "goal":
            from .journal import scrub
            # Legacy goals must retire through the same identity as new writes.
            text = scrub(str(ev.get("text", ""))).strip()
            w = float(ev.get("weight", 0.0))
            if text:
                if w > 0:
                    self.goals[text] = min(1.0, w)
                else:
                    self.goals.pop(text, None)
        elif kind == "intent":
            # an in-flight work announcement, keyed by branch: awareness
            # for OTHER consumers of a shared/synced ledger: never a lock.
            # Re-declaring refreshes (a heartbeat); `done` retires; an
            # unrefreshed intent expires after its ttl (filtered at read
            # time, so replay stays deterministic).
            branch = str(ev.get("branch", "")).strip()
            if branch:
                if ev.get("done"):
                    self.intents.pop(branch, None)
                else:
                    ttl = ev.get("ttl", INTENT_TTL)
                    try:
                        ttl = max(60.0, float(ttl))
                    except (TypeError, ValueError):
                        ttl = float(INTENT_TTL)
                    raw = ev.get("paths", [])
                    paths = [str(p).strip().replace(os.sep, "/")
                             for p in (raw if isinstance(raw, list) else [])
                             if str(p).strip()][:32]
                    self.intents[branch] = {
                        "goal": str(ev.get("goal", ""))[:200],
                        "paths": paths, "ts": ts, "expires": ts + ttl}
                    while len(self.intents) > INTENT_KEYS:
                        oldest = min(self.intents,
                                     key=lambda b: (self.intents[b].get(
                                         "expires", 0), b))
                        del self.intents[oldest]
        elif kind == "feedback":
            pass  # feedback is scanned from the ledger by evolve.py :
            #       raw preference signal never becomes derived state
        elif kind == "code-relation":
            pass  # code_relations.py replays these bounded events directly
        elif kind == "rule":
            # the evolution loop's registry: promote adds/replaces a
            # learned rule; demote marks it superseded (kept, dated,
            # struck through in the learned file: never deleted)
            rid = str(ev.get("id", "")).strip()
            if rid:
                if ev.get("action") == "demote":
                    if rid in self.rules:
                        self.rules[rid]["status"] = "superseded"
                        self.rules[rid]["superseded_ts"] = ts
                        self.rules[rid]["superseded_date"] = time.strftime(
                            "%Y-%m-%d", time.localtime(ts or None))
                else:
                    raw = ev.get("toks", [])
                    self.rules[rid] = {
                        "domain": str(ev.get("domain", "general"))[:24],
                        "text": str(ev.get("text", ""))[:240],
                        "toks": [str(t)[:40] for t in
                                 (raw if isinstance(raw, list) else [])][:12],
                        "polarity": 1 if ev.get("polarity", -1) > 0 else -1,
                        "count": int(ev.get("count", 0) or 0),
                        "sessions": int(ev.get("sessions", 0) or 0),
                        "status": "active",
                        # the ts is the registry's LAST WORD on this
                        # ground: evolve only acts on evidence newer than
                        # it (no flip-flop, no resurrection from stale
                        # feedback)
                        "ts": ts,
                        "date": time.strftime("%Y-%m-%d",
                                              time.localtime(ts or None))}
                    while len(self.rules) > RULE_KEYS:
                        victim = min(  # superseded first, then oldest,
                            self.rules.items(),  # then rid: deterministic
                            key=lambda kv: (
                                kv[1].get("status") != "superseded",
                                float(kv[1].get("ts", 0) or 0), kv[0]))[0]
                        del self.rules[victim]
        elif kind == "session-begin":
            # a Sense marked an agent session opening: claim the sliding
            # window so session-less observes that follow join it
            if ev.get("session"):
                self._claim_window(ev["session"], ts)
                if ev.get("cue"):
                    sid = str(ev["session"])
                    self.session_cues[sid] = str(ev["cue"])[:300]
                    while len(self.session_cues) > 50:
                        del self.session_cues[next(iter(self.session_cues))]
        elif kind == "assoc":
            # the reflection loop learned a cue->note association from a
            # MISS (real use the pack failed to serve). Bounded: token
            # list capped, weight capped at 1.0, weakest entry falls off
            # past ASSOC_KEYS, and consolidate decays every weight.
            note = str(ev.get("note", "")).strip()
            raw = ev.get("toks", [])
            toks = [str(t)[:40] for t in (raw if isinstance(raw, list) else [])
                    if str(t).strip()]
            if note and toks:
                try:
                    w = max(0.0, min(1.0, float(ev.get("w", 0.5))))
                except (TypeError, ValueError):
                    w = 0.5
                cur = self.assocs.get(note)
                if cur:
                    merged = list(dict.fromkeys(list(cur["toks"]) + toks))
                    self.assocs[note] = {
                        "toks": merged[:24],
                        "w": round(min(1.0, float(cur.get("w", 0)) + w), 4),
                        "ts": ts}
                else:
                    self.assocs[note] = {"toks": toks[:24],
                                         "w": round(w, 4), "ts": ts}
                while len(self.assocs) > ASSOC_KEYS:
                    weakest = min(self.assocs,
                                  key=lambda n: (self.assocs[n]["w"], n))
                    del self.assocs[weakest]
        elif kind == "gap":
            # an unmapped repo-relative path the agent really used :
            # knowledge that does not exist yet. Counter only; the review
            # actuator decides when it is worth mapping.
            p = str(ev.get("path", "")).strip()
            key = gap_key(p, ev.get("repository"))
            if p and key and not os.path.isabs(p) and ".." not in p.split("/"):
                self.gap_counts[key] = self.gap_counts.get(key, 0) + 1
                self._mark_seen("gap", key, ts)
                while len(self.gap_counts) > GAP_KEYS:
                    rare = min(self.gap_counts,
                               key=lambda k: (self.gap_counts[k], k))
                    del self.gap_counts[rare]
        elif kind == "review":
            # one session's reflection: mark it reviewed (idempotence) and
            # count its wasted serves (served, never touched). Touches
            # reset the counter, so only CONSECUTIVE waste accumulates.
            sid = str(ev.get("session", "")).strip()
            if sid and sid not in self.reviewed:
                self.reviewed.append(sid)
                del self.reviewed[:-REVIEWED_KEYS]
                raw = ev.get("waste", [])
                for n in (raw if isinstance(raw, list) else []):
                    n = str(n)
                    self.serve_miss[n] = self.serve_miss.get(n, 0) + 1
                for p in (ev.get("built", [])
                          if isinstance(ev.get("built", []), list) else []):
                    self.gap_counts.pop(str(p), None)
        elif kind == "consolidate":
            self.last_consolidate_ts = ts
            self._consolidate_apply()

    def touch(self, path: str, valence: float = 0.0, kind: str = "touch",
              session: str | None = None, wire: bool = True) -> dict:
        # the env fallback never applies to recalls: only an EXPLICIT
        # session (prime --session, the agent hook) may tie a recall to
        # co-use: a plain query under `export MUNINN_SESSION` stays
        # read-only (SPEC §3: plain queries never wire)
        if session is None and kind != "recall":
            session = os.environ.get("MUNINN_SESSION")
        ev = {"kind": kind, "note": path, "valence": valence}
        if session:
            ev["session"] = session
        if kind == "recall" and not wire:
            ev["wire"] = False
        self._apply(ev)
        self._save()
        return self.entries[path]

    def outcome(self, valence: float, note: str | None = None, why: str = "",
                session: str | None = None) -> int:
        """A signed outcome. Strong |valence| captures the recent window."""
        from .journal import scrub
        ev = {"kind": "outcome", "valence": valence, "why": scrub(str(why))[:200]}
        if note:
            self.touch(note, valence=valence, kind="encode", session=session)
        self._apply(ev)
        self._save()
        return min(len(self.order), CAPTURE_WINDOW)

    def session_begin(self, session: str, cue: str | None = None) -> None:
        """A Sense marked an agent session opening (e.g. the Claude Code
        hook). Logged, so replay rebuilds the same window state. ``cue``
        (the prime cue) is kept so the reflection loop can learn
        situation->note associations from this session's misses."""
        from .journal import scrub
        ev: dict = {"kind": "session-begin", "session": str(session)}
        if cue:
            ev["cue"] = scrub(str(cue))[:300]
        self._apply(ev)
        self._save()

    def assoc(self, note: str, toks, w: float = 0.5) -> None:
        """Record a learned cue->note association (the miss actuator)."""
        self._apply({"kind": "assoc", "note": note, "toks": list(toks),
                     "w": w})
        self._save()

    def gap(self, path: str, ts: float | None = None,
            repository: str | None = None) -> None:
        """Count a sighting of an unmapped repo-relative path. ``ts`` lets
        the sense pass its own clock so debounce and event time agree."""
        ev: dict = {"kind": "gap", "path": path}
        if repository is not None:
            if not gap_key(path, repository):
                raise ValueError("gap repository identity must be a SHA-256 digest")
            ev["repository"] = repository
        if ts is not None:
            ev["ts"] = round(float(ts), 3)
        self._apply(ev)
        self._save()

    def feedback(self, text: str, domain: str = "general",
                 polarity: int = -1, session: str | None = None) -> None:
        """One preference observation (the agent logs it the moment the
        user corrects/rewrites/reacts). Raw signal, ledger-only."""
        if session is None:
            session = os.environ.get("MUNINN_SESSION")
        from .journal import scrub  # lazy: journal imports dynamics
        ev = {"kind": "feedback", "text": scrub(str(text))[:240],
              "domain": str(domain)[:24],
              "polarity": 1 if polarity > 0 else -1}
        # Millisecond timestamps can tie in a fast session. Evidence logged
        # after a rule must still be newer than that rule, or a valid reversal
        # can be ignored by the evolution pass.
        rule_floor = max(
            (max(float(rule.get("ts", 0) or 0),
                 float(rule.get("superseded_ts", 0) or 0))
             for rule in self.rules.values()), default=0.0)
        ev["ts"] = max(round(time.time(), 3), rule_floor + 0.001)
        if session:
            ev["session"] = session
        self._apply(ev)
        self._save()

    def code_relation_mark(self, event) -> None:
        """Append one validated repository relation without query text."""
        from .code_relations import CodeRelationEvent, LEDGER_KIND
        if not isinstance(event, CodeRelationEvent):
            raise TypeError("code relation event has the wrong type")
        self._apply({
            "kind": LEDGER_KIND,
            "repository": event.repository,
            "route": event.route,
            "session": event.session,
            "anchors": list(event.anchors),
            "targets": list(event.targets),
        })
        self._save()

    def rule_mark(self, rid: str, domain: str, text: str, toks, polarity: int,
                  count: int, sessions: int) -> None:
        """Seal a promoted rule into the ledger (replay rebuilds it)."""
        self._apply({"kind": "rule", "action": "promote", "id": rid,
                     "domain": domain, "text": text, "toks": list(toks),
                     "polarity": polarity, "count": count,
                     "sessions": sessions})
        self._save()

    def rule_demote(self, rid: str) -> None:
        """Repeated opposite evidence superseded this rule."""
        self._apply({"kind": "rule", "action": "demote", "id": rid})
        self._save()

    def review_mark(self, session: str, hits, misses, waste,
                    built=()) -> None:
        """Seal one session's reflection into the ledger (idempotence
        marker + waste counting + observability)."""
        self._apply({"kind": "review", "session": str(session),
                     "hits": list(hits), "misses": list(misses),
                     "waste": list(waste), "built": list(built)})
        self._save()

    def goal(self, text: str, weight: float = 0.7) -> None:
        """Declare (weight>0) or retire (weight=0) a current concern. Active
        goals tilt retrieval toward what matters right now."""
        from .journal import scrub
        self._apply({"kind": "goal", "text": scrub(str(text)), "weight": weight})
        self._save()

    def intent(self, branch: str, paths=(), goal: str = "",
               ttl: float | None = None) -> dict | None:
        """Announce in-flight work on a branch: awareness, never a lock.
        Other consumers of a shared (or synced) ledger see it in their
        primed packs. Re-declaring the same branch refreshes it."""
        from .journal import scrub
        ev = {"kind": "intent", "branch": str(branch), "paths": list(paths),
              "goal": scrub(str(goal)),
              "ttl": float(ttl if ttl is not None else INTENT_TTL)}
        self._apply(ev)
        self._save()
        return self.intents.get(str(branch).strip())

    def intent_done(self, branch: str) -> None:
        """Retire an announced intent (the work landed or was abandoned)."""
        self._apply({"kind": "intent", "branch": str(branch), "done": True})
        self._save()

    def active_intents(self, now: float | None = None) -> dict[str, dict]:
        """The intents that have not expired, branch-keyed. Expiry is
        filtered here at read time: stored state never mutates with the
        clock, so ledger replay stays deterministic."""
        now = time.time() if now is None else now
        return {b: i for b, i in self.intents.items()
                if float(i.get("expires", 0) or 0) > now}

    def pin(self, path: str, value: bool = True) -> None:
        self._apply({"kind": "pin", "note": path, "value": value})
        self._save()

    def supersede(self, old_path: str, new_path: str) -> None:
        """Record a correction: old_path is superseded by new_path."""
        self._apply({"kind": "supersede", "note": old_path, "by": new_path})
        self._save()

    # -- consolidation (the nightly 'seal') --------------------------------

    def _priority(self, e: dict) -> float:
        # recalls contribute a SMALL bounded term; everything else needs a
        # real touch: a never-touched note caps at W_RECALL (0.1), below
        # the dormancy floor and below any once-touched note (0.34)
        base = W_RECALL * min(10, e.get("recalls", 0)) / 10.0
        if e["recurrence"] > 0:
            base += (BASE_IMPORTANCE
                     + W_RECURRENCE * min(10, e["recurrence"]) / 10.0
                     + W_AFFECT * min(1.0, e["affect_mag"])
                     + e["captured"])
        if e["superseded"]:
            base *= LTD_FACTOR
        if e["pinned"]:
            base = max(base, PIN_FLOOR)
        return round(base, 4)

    def _consolidate_apply(self) -> None:
        """Decay COMPOUNDS across consolidations; reinforcement events reset
        strength to full priority. That is the forgetting curve: touched
        notes snap back, untouched ones fade multiplicatively."""
        for e in self.entries.values():
            e["captured"] = round(e["captured"] * DECAY, 4)
            decayed = e["strength"] * DECAY
            floor = PIN_FLOOR if e["pinned"] else 0.0
            e["strength"] = round(max(decayed, floor), 4)
        for note in list(self.assocs):  # unreinforced learning dissolves
            w = round(self.assocs[note].get("w", 0) * ASSOC_DECAY, 4)
            if w < ASSOC_FLOOR:
                del self.assocs[note]
            else:
                self.assocs[note]["w"] = w

    def consolidate(self) -> tuple[int, int]:
        """Decay un-reinforced strength. Returns (active, dormant) counts.
        Dormant notes stay on disk and stay queryable: they just stop
        being *proactively* recalled until something touches them again."""
        self._apply({"kind": "consolidate"})
        self._save()
        active = sum(1 for e in self.entries.values() if e["strength"] >= FLOOR)
        return active, len(self.entries) - active

    def consolidate_if_due(self, now: float | None = None) -> int:
        """Ambient decay: every Sense intake (observe/prime/hooks) runs
        this check, so the nightly cron becomes optional. Appends one
        consolidate tick per full day since the last one, CAPPED at 3 :
        returning from a vacation never triggers a decay avalanche (the
        remainder is forgiven: the anchor jumps to now). A bundle that
        never consolidated anchors on its first ledger event; an empty
        ledger never ticks. Racing intakes may each tick before seeing
        the other's append: tolerated like every sidecar race, bounded
        (<= 0.98^3 per racer) rather than locked. Returns ticks applied."""
        now = time.time() if now is None else now
        anchor = self.last_consolidate_ts or self.first_event_ts
        if anchor <= 0:
            return 0
        ticks = max(0, min(3, int((now - anchor) // 86400)))
        for _ in range(ticks):
            self._apply({"kind": "consolidate"})
        if ticks:
            self._save()
        return ticks

    # -- reads --------------------------------------------------------------

    def strength(self, path: str) -> float:
        e = self.entries.get(path)
        return e["strength"] if e else 0.0

    def is_dormant(self, path: str) -> bool:
        e = self.entries.get(path)
        return bool(e) and e["strength"] < FLOOR and not e["pinned"]

    def dormant_split(self) -> tuple[int, int]:
        """Split the dormant entries into ``(faded, recall_only)``:
        *faded* notes were really used and decayed below the floor;
        *recall-only* notes were never touched at all: tracked only by
        capped recall events (query-bait, not faded knowledge). Display
        split only: ``is_dormant`` semantics for recall are unchanged."""
        faded = recall_only = 0
        for e in self.entries.values():
            if e["strength"] >= FLOOR or e["pinned"]:
                continue
            if e["recurrence"] > 0:
                faded += 1
            else:
                recall_only += 1
        return faded, recall_only

    def strongest(self, k: int = 10) -> list[tuple[str, float]]:
        pairs = [(p, e["strength"]) for p, e in self.entries.items()]
        pairs.sort(key=lambda x: x[1], reverse=True)
        return pairs[:k]

    def coactivation(self) -> dict[str, dict[str, int]]:
        """Adjacency view of the used-together graph: path -> {other: w}."""
        adj: dict[str, dict[str, int]] = {}
        for key, w in self.coact.items():
            a, b = key.split("|", 1)
            adj.setdefault(a, {})[b] = w
            adj.setdefault(b, {})[a] = w
        return adj

    def recent(self, k: int = 5) -> list[str]:
        return self.order[-k:]
