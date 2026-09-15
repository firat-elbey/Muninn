"""Persist session continuity as thread heads and dated episodes.

Each ``threads/<slug>/thread.md`` file contains the current compiled state.
A supplied ``--state`` value replaces that head deliberately. Dated episode
notes append the supporting history.

The agent distills a conversation because Muninn does not read transcripts
automatically. The explicit ``import-transcripts`` command can ingest an
existing archive after scrubbing secret-shaped strings. Session initialization
serves the relevant head and latest episode before ordinary recalled notes and
records those items as recall events.
"""

from __future__ import annotations

import json
import os
import re
import time

from .activate import tokens
from .dynamics import Dynamics, sidecar_lock
from .store import MUNINN_ACTOR, Bundle, generated_stamp

THREADS_DIR = "threads"
HEAD_NAME = "thread.md"
SHOWN_HEAD_CHARS = 500     # head chars served in the boot section
SHOWN_EPISODE_CHARS = 350  # latest-episode chars served
FALLBACK_DAYS = 30         # a recent thread rides along even unmatched
IMPORT_MSG_CAP = 30        # user messages kept per imported session
IMPORT_MSG_CHARS = 240     # chars kept per message
_TS_FMT = "%Y-%m-%dT%H:%M:%S"

# secret-shaped strings never survive an import (conservative, additive)
_SECRETS = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}"
    r"|AKIA[A-Z0-9]{12,}|xox[bap]-[A-Za-z0-9-]{10,}"
    r"|(?:password|passwd|secret|token|api_key|apikey)\s*[=:]\s*\S+)",
    re.IGNORECASE)


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", scrub(str(text)).lower()).strip("-")[:60]
    return s or "thread"


def scrub(text: str) -> str:
    """Replace secret-shaped substrings with '[scrubbed]'."""
    return _SECRETS.sub("[scrubbed]", text)


# -- writing ------------------------------------------------------------------

def add_episode(bundle: Bundle, dyn: Dynamics, thread: str, body: str,
                state: str | None = None, title: str | None = None,
                when: float | None = None) -> str:
    """Append one episode to a thread (creating it if new); ``state``
    deliberately replaces the compiled head. Returns the thread slug."""
    slug = _slug(thread)
    when = time.time() if when is None else when
    stamp = time.strftime(_TS_FMT, time.localtime(when))
    head_rel = f"{THREADS_DIR}/{slug}/{HEAD_NAME}"
    with sidecar_lock(bundle.root, "journal"):
        if state or not os.path.lexists(os.path.join(bundle.root, head_rel)):
            bundle.write_note(head_rel,
                              {"type": "thread", "title": scrub(str(thread)),
                               "generated": generated_stamp(when)},
                              scrub(state) if state
                              else "(no compiled state yet: see episodes)")
        base = (f"{THREADS_DIR}/{slug}/"
                f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(when))}")
        ep_rel, n = base + ".md", 2
        while os.path.lexists(os.path.join(bundle.root, ep_rel)):
            ep_rel = f"{base}-{n}.md"
            n += 1
        bundle.write_note(ep_rel,
                          {"type": "episode",
                           "title": scrub(title or f"{thread}: {stamp[:10]}"),
                           "generated": generated_stamp(when)},
                          scrub(body))
    dyn.touch(ep_rel, kind="encode")
    return slug


# -- reading ------------------------------------------------------------------

def threads_of(bundle: Bundle) -> dict[str, dict]:
    """slug -> {head: Note|None, episodes: [(path, Note)] newest-first}."""
    out: dict[str, dict] = {}
    for path, note in sorted(bundle.notes.items()):
        if not path.startswith(THREADS_DIR + "/"):
            continue
        parts = path.split("/")
        if len(parts) != 3:
            continue
        t = out.setdefault(parts[1], {"head": None, "episodes": []})
        if parts[2] == HEAD_NAME:
            t["head"] = note
        elif note.meta.get("type") == "episode":
            t["episodes"].append((path, note))
    for t in out.values():
        t["episodes"].sort(key=_episode_order, reverse=True)
    return out


def _episode_order(item) -> tuple[str, int, str]:
    """Order episodes by their recorded date and numeric collision suffix."""
    path, note = item
    match = re.fullmatch(r"(\d{8}-\d{6})(?:-(\d+))?\.md",
                         os.path.basename(path))
    return (note.generated_at(), int(match.group(2) or 1) if match else 0,
            path)


def _thread_ts(t: dict) -> float:
    # generated.at (OKF v0.2), legacy v0.1 timestamp as fallback: old
    # bundles keep working (Note.generated_at holds the fallback rule)
    stamp = ""
    if t["episodes"]:
        stamp = t["episodes"][0][1].generated_at()
    elif t["head"] is not None:
        stamp = t["head"].generated_at()
    try:
        return time.mktime(time.strptime(stamp[:19], _TS_FMT))
    except ValueError:
        return 0.0


def _clip(text: str, cap: int) -> str:
    text = text.strip()
    return text if len(text) <= cap else text[:cap].rsplit(" ", 1)[0] + " …"


def threads_section(bundle: Bundle, dyn: Dynamics, cue: str = "",
                    session: str | None = None,
                    now: float | None = None) -> str:
    """The continuity block, printed BEFORE the context pack: the
    situation-matched thread's compiled head + its latest episode. With
    no lexical match, the most recently journaled thread rides along if
    it is fresh (< FALLBACK_DAYS) because recent work may remain unfinished.
    Served notes log recall events; '' when no threads exist."""
    now = time.time() if now is None else now
    threads = threads_of(bundle)
    if not threads:
        return ""
    cue_toks = tokens(cue)

    def _match(t: dict) -> int:
        text = " ".join(filter(None, [
            t["head"].title if t["head"] else "",
            t["head"].body if t["head"] else "",
            t["episodes"][0][1].body if t["episodes"] else ""]))
        return len(cue_toks & tokens(text))

    scored = sorted(((slug, t, _match(t)) for slug, t in threads.items()),
                    key=lambda x: (-x[2], -_thread_ts(x[1]), x[0]))
    slug, t, hits = scored[0]
    if hits < 2:  # no real match: fall back to the freshest thread only
        slug, t = max(threads.items(), key=lambda kv: _thread_ts(kv[1]))
        if now - _thread_ts(t) > FALLBACK_DAYS * 86400:
            return ""
    head, eps = t["head"], t["episodes"]
    title = head.title if head else slug
    age = max(0, int((now - _thread_ts(t)) // 86400))
    lines = [f"## Where we left off: {title}",
             f"*(thread `{slug}`, last touched "
             + (f"{age}d ago)*" if age else "today)*"), ""]
    if head and head.body.strip():
        lines += [_clip(head.body, SHOWN_HEAD_CHARS), ""]
        dyn.touch(head.path, kind="recall", session=session)
    if eps:
        path, ep = eps[0]
        lines += [f"**Latest episode** ({ep.title}):",
                  _clip(ep.body, SHOWN_EPISODE_CHARS), ""]
        dyn.touch(path, kind="recall", session=session)
    lines.append(f"*(full history: `muninn journal \"{title}\" --show`: "
                 f"{len(eps)} episode(s))*")
    return "\n".join(lines)


# -- transcript import (explicit, user-invoked; never ambient) ---------------

def _msg_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(b.get("text", "")) for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
    return ""


def import_transcripts(bundle: Bundle, dyn: Dynamics, path: str,
                       thread: str | None = None,
                       cap: int = 50) -> tuple[int, int]:
    """Encode Claude Code JSONL transcripts (a file, or a directory of
    them) into episode notes: deterministic, secrets scrubbed, bounded.
    Idempotent: a session already imported (by file stem) is skipped.
    Returns (imported, skipped)."""
    files = ([path] if os.path.isfile(path) else
             sorted(os.path.join(path, f) for f in os.listdir(path)
                    if f.endswith(".jsonl")))[:cap]
    default_thread = thread or os.path.basename(
        os.path.abspath(path if os.path.isdir(path)
                        else os.path.dirname(path)))
    imported = skipped = 0
    for fp in files:
        stem = os.path.basename(fp)[:-6][:40] or "session"
        slug = _slug(default_thread)
        ep_rel = f"{THREADS_DIR}/{slug}/imported-{_slug(stem)}.md"
        if ep_rel in bundle.notes:
            skipped += 1
            continue
        summary, msgs, first_ts = "", [], ""
        try:
            with open(fp, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(ev, dict):
                        continue
                    if ev.get("type") == "summary" and ev.get("summary"):
                        summary = scrub(str(ev["summary"]))[:120]
                    elif ev.get("type") == "user":
                        message = ev.get("message")
                        if not isinstance(message, dict):
                            continue
                        txt = _msg_text(message.get("content"))
                        txt = " ".join(txt.split())
                        if txt and not txt.startswith(("<", "[Request")):
                            if not first_ts:
                                first_ts = str(ev.get("timestamp", ""))[:10]
                            msgs.append(_clip(scrub(txt), IMPORT_MSG_CHARS))
                    if len(msgs) >= IMPORT_MSG_CAP:
                        break
        except OSError:
            continue
        if not msgs:
            skipped += 1
            continue
        title = scrub(summary or _clip(msgs[0], 60))
        body = (f"Imported transcript `{stem}`"
                + (f" ({first_ts})" if first_ts else "") + ".\n\n"
                "What the user asked for, in their words:\n"
                + "\n".join(f"- {m}" for m in msgs))
        bundle.write_note(ep_rel,
                          {"type": "episode", "title": title,
                           "provenance": "extracted",
                           "generated": ({"by": MUNINN_ACTOR,
                                          "at": first_ts + "T00:00:00"}
                                         if first_ts
                                         else generated_stamp())},
                          scrub(body))
        dyn.touch(ep_rel, kind="encode")
        imported += 1
    return imported, skipped
