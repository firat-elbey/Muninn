"""Convert ambient agent activity into bounded ledger events.

The intake maps a file path to a note, assigns a sliding-window session,
debounces repeated events, and writes through ``Dynamics``. Unmappable paths
are ignored, and hook execution always exits successfully.

The Claude Code adapter passes only ``session_id`` and
``tool_input.file_path`` beyond intake. It reads ``tool_name`` only to
classify the event and does not retain the value. Prompt text, transcript
paths, working directories, tool output, and credentials do not enter the
ledger.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import zipimport

from .dynamics import Dynamics, gap_key, repository_identity
from .store import Bundle

SESSION_WINDOW = 30 * 60  # seconds: observes this close share one session
DEBOUNCE = 60             # seconds: same session, note, and kind collapse
KINDS = ("touch", "encode", "outcome")
TOOL_KINDS = {
    "Read": "touch",
    "Edit": "encode",
    "Write": "encode",
    "read_file": "touch",
    "search_replace": "encode",
    "write": "encode",
}
INFLIGHT_SHOWN = 8        # bound on intents rendered into a primed pack


def map_note(bundle: Bundle, value, prefer: str | None = None) -> str | None:
    """Resolve an observed path to one bundle note.

Resolution checks an exact bundle-relative path, an absolute path inside the
bundle, and the ``resource:`` index. A home bundle can prefer one project
subtree before applying the global lookup. Suffix matching requires at least
two path components so that a common file name cannot match every project.
    """
    raw = str(value).strip() if value else ""
    if not raw:
        return None
    rel = raw.replace(os.sep, "/")
    rel = rel[2:] if rel.startswith("./") else rel
    if rel in bundle.notes:
        return rel
    ab = os.path.abspath(os.path.expanduser(raw))
    # resolve OUR root too (macOS /tmp -> /private/tmp), never the observed
    # path: realpath on a hostile path could stall on a dead mount
    for base in (bundle.root, os.path.realpath(bundle.root)):
        if ab.startswith(base + os.sep):
            r = os.path.relpath(ab, base).replace(os.sep, "/")
            if r in bundle.notes:
                return r
    if prefer:
        hit = _resource_hit(bundle, raw, rel, ab, prefix=prefer)
        if hit:
            return hit
    return _resource_hit(bundle, raw, rel, ab, prefix=None)


def _resource_hit(bundle: Bundle, raw: str, rel: str, ab: str,
                  prefix: str | None) -> str | None:
    res = bundle.by_resource(prefix)
    for key in (raw, rel, ab):
        if key in res:
            return res[key].path
    norm = ab.replace(os.sep, "/")
    # suffix match needs >=2 path components: a bare-filename resource
    # ("README.md") must not soak up touches from every repo's README
    hits = [k for k in res
            if "/" in str(k).lstrip("/")
            and norm.endswith("/" + str(k).lstrip("/"))]
    if hits:  # most-specific suffix wins; ties break by key (deterministic)
        hits.sort(key=lambda k: (-len(str(k)), str(k)))
        return res[hits[0]].path
    return None


def infer_session(dyn: Dynamics, now: float | None = None) -> str:
    """The sliding window: reuse the last session id while the last
    session-carrying event is under 30 minutes old, else mint a new id."""
    now = time.time() if now is None else now
    if dyn.last_session_id and 0 <= now - dyn.last_event_ts <= SESSION_WINDOW:
        return dyn.last_session_id
    return f"s{int(now)}"


def observe_event(bundle: Bundle, dyn: Dynamics, kind: str, note=None,
                  valence: float = 0.0, session: str | None = None,
                  now: float | None = None,
                  prefer: str | None = None) -> str | None:
    """The narrow-waist intake. Returns the mapped note path when an
    event landed, else None (unmappable → dropped; repeat → debounced;
    a note-less outcome lands but has no path to return). Appends via
    the existing Dynamics methods only. ``prefer`` scopes the mapping
    to one room of a home brain (see map_note)."""
    if kind not in KINDS:  # the waist holds for library callers too:
        return None  # no sense can invent new ledger shapes
    now = time.time() if now is None else now
    dyn.consolidate_if_due(now)  # decay is ambient: every intake checks
    path = map_note(bundle, note, prefer=prefer) if note else None
    if kind == "outcome":
        if note and path is None:
            return None  # a note was named but means nothing here: drop
        sess = session or (infer_session(dyn, now) if path else None)
        dyn.outcome(float(valence), note=path, session=sess)
        return path
    if path is None:
        _sense_gap(dyn, note, now)  # unmapped real use = a knowledge gap
        return None  # touch/encode without a mappable note: drop
    session = session or infer_session(dyn, now)
    if now - dyn.last_seen_in_session(session, kind, path) < DEBOUNCE:
        return None  # This session already recorded this use within the window.
    dyn.touch(path, valence=float(valence), kind=kind,
              session=session)
    return path


def _sense_gap(dyn: Dynamics, note, now: float) -> None:
    """An unmapped path the agent really touched is the gap signal the
    reflection loop (review.py) feeds on. PRIVACY RULE: only a path
    inside a git repository is recorded, and only REPO-RELATIVE: an
    absolute path outside any repo (a dotfile, a stray download) never
    reaches the ledger. Debounced like every other sense."""
    raw = str(note or "").strip()
    if not raw:
        return
    visible = os.path.abspath(os.path.expanduser(raw))
    if not os.path.isfile(visible):
        return
    top = git_out(os.path.dirname(visible), "rev-parse", "--show-toplevel")
    if not top:
        return  # outside any repo: never recorded
    # Resolve both paths after Git identifies the visible repository. This
    # normalizes filesystem aliases while rejecting a link that leaves it.
    ab = os.path.realpath(visible)
    top = os.path.realpath(top)
    try:
        if os.path.commonpath((top, ab)) != top:
            return
    except ValueError:
        return
    rel = os.path.relpath(ab, top).replace(os.sep, "/")
    repository = repository_identity(top)
    key = gap_key(rel, repository)
    if now - dyn.last_seen.get(f"gap|{key}", float("-inf")) < DEBOUNCE:
        return
    dyn.gap(rel, ts=now, repository=repository)


# -- in-flight awareness (intents rendered into primed packs) ----------------

def git_out(workdir: str, *args: str) -> str:
    """One git read in ``workdir``; '' on any failure (fails soft outside
    a repo, like the situation harvest in activate.py)."""
    try:
        r = subprocess.run(["git", "-C", workdir, *args],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def current_branch(workdir: str) -> str:
    """The checked-out branch name, '' when none. symbolic-ref first: it
    works on an UNBORN branch (a fresh clone/init with no commits: the
    normal state of a just-spawned agent container), where rev-parse
    fails. A detached HEAD is NOT a branch: rev-parse spells it 'HEAD',
    which must never key an intent or match one."""
    name = (git_out(workdir, "symbolic-ref", "--short", "-q", "HEAD")
            or git_out(workdir, "rev-parse", "--abbrev-ref", "HEAD"))
    return "" if name == "HEAD" else name


def _age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _paths_overlap(a: str, b: str) -> bool:
    """Two repo-ish paths mean the same file if equal, or if one is a
    component-boundary suffix of the other (repo-relative vs deeper-rooted
    spellings of one file). Suffix matching needs the shorter side to
    carry a directory: a bare filename must not soak up every same-named
    file in the tree (the map_note discipline; equality still covers
    root-level files)."""
    a = a.strip().strip("/").replace(os.sep, "/")
    b = b.strip().strip("/").replace(os.sep, "/")
    if not a or not b:
        return False
    if a == b:
        return True
    return (("/" in b and a.endswith("/" + b))
            or ("/" in a and b.endswith("/" + a)))


def inflight_section(bundle: Bundle, dyn: Dynamics, workdir: str | None = None,
                     now: float | None = None) -> str:
    """The awareness block appended to primed packs: active ``intent``
    events: other work happening on this knowledge right now: with an
    overlap flag when a claimed path collides with the situation (the
    current branch's uncommitted changes, compared raw and via the
    file→note mapping). Awareness only: it reorders nothing and blocks
    nothing. '' when no intent is active."""
    now = time.time() if now is None else now
    intents = dyn.active_intents(now)
    if not intents:
        return ""
    wd = os.path.abspath(workdir or os.getcwd())
    branch = current_branch(wd)
    changed = _changed_files(wd)
    my_notes = {n for n in (map_note(bundle, os.path.join(wd, p))
                            for p in changed) if n}
    lines = ["## In flight (announced by other agents)", ""]
    shown = sorted(intents.items(), key=lambda kv: (-kv[1].get("ts", 0), kv[0]))
    for b, i in shown[:INFLIGHT_SHOWN]:
        paths = i.get("paths", [])
        overlap = sorted(
            {p for p in paths for c in changed if _paths_overlap(p, c)}
            | {p for p in paths if map_note(bundle, p) in my_notes})
        entry = f"- `{b}`"
        if i.get("goal"):
            entry += f": {i['goal']}"
        entry += f" (started {_age(now - i.get('ts', now))} ago"
        if paths:
            entry += "; touches: " + ", ".join(paths[:6])
            if len(paths) > 6:
                entry += f" +{len(paths) - 6} more"
        entry += ")"
        if b == branch:
            entry += ": this branch (current branch announcement)"
        elif overlap:
            entry += (": **overlaps your working set: "
                      + ", ".join(overlap[:4]) + "**")
        lines.append(entry)
    if len(shown) > INFLIGHT_SHOWN:
        lines.append(f"- ... and {len(shown) - INFLIGHT_SHOWN} more")
    lines.append("")
    lines.append("*(awareness, not locks: coordinate if you must touch "
                 "overlapping files; `muninn intent --done` retires yours)*")
    return "\n".join(lines)


# -- lessons: corrective knowledge selected by path guards ------------------

LESSONS_SHOWN = 4  # bound on lessons rendered into a primed pack


def _changed_files(wd: str) -> list[str]:
    out: list[str] = []
    for line in git_out(wd, "status", "--porcelain",
                        "-uall").splitlines()[:200]:
        p = line[2:].strip().split(" -> ")[-1]
        if p:
            out.append(p)
    return out


def _guard_hits(guard: str, changed: str) -> bool:
    """A guard matches a changed file exactly/by boundary suffix (the
    _paths_overlap rule) or as a DIRECTORY PREFIX: 'ui/styles' guards
    everything under ui/styles/."""
    if _paths_overlap(guard, changed):
        return True
    g = guard.strip().strip("/").replace(os.sep, "/")
    c = changed.strip().strip("/").replace(os.sep, "/")
    return bool(g) and (c.startswith(g + "/") or f"/{g}/" in f"/{c}")


def lessons_section(bundle: Bundle, dyn: Dynamics, workdir: str | None = None,
                    session: str | None = None) -> str:
    """The guard block for primed packs: lesson notes (type: lesson)
    whose ``guards:`` paths overlap the session's uncommitted changes :
    the warning arrives exactly when the mistake is about to repeat.
    Guardless lessons (process rules: 'never silently skip tasks') ride
    along every session. Served lessons log recall events (with the
    session id), so the reflection loop measures whether they get USED.
    '' when nothing applies."""
    lessons = [(p, n) for p, n in sorted(bundle.notes.items())
               if str(n.meta.get("type", "")) == "lesson"]
    if not lessons:
        return ""
    wd = os.path.abspath(workdir or os.getcwd())
    changed = _changed_files(wd)
    picked: list[tuple[str, object, list[str]]] = []
    for path, note in lessons:
        gs = note.guards()
        hit = sorted({g for g in gs for c in changed
                      if _guard_hits(g, c)})
        if hit or not gs:  # guarded-and-overlapping, or guardless
            picked.append((path, note, hit))
    if not picked:
        return ""
    # overlapping guards first (most specific warning wins the slots)
    picked.sort(key=lambda t: (not t[2], t[0]))
    lines = ["## Lessons for this change", ""]
    for path, note, hit in picked[:LESSONS_SHOWN]:
        lines.append(f"### {note.title} ({path})")
        if hit:
            lines.append(f"*guards: {', '.join(hit)}: you are changing "
                         "these*")
        body = note.body.strip()
        if body:
            lines.append(body if len(body) <= 600 else body[:600] + " …")
        lines.append("")
        dyn.touch(path, kind="recall", session=session)
    if len(picked) > LESSONS_SHOWN:
        lines.append(f"*(... and {len(picked) - LESSONS_SHOWN} more: "
                     "`muninn lesson` lists them)*")
    return "\n".join(lines).rstrip()


# -- agent hook adapters (see also `muninn hook` in cli.py) ------------------

def hook_payload(
        text: str,
) -> tuple[str | None, str | None, str | None, str]:
    """Extract the three allowed fields and the transient adapter kind."""
    try:  # RecursionError: absurdly nested JSON must not break exit-0
        data = json.loads(text or "{}")
    except (ValueError, RecursionError):
        return None, None, None, "generic"
    if not isinstance(data, dict):
        return None, None, None, "generic"
    adapter = "grok" if any(
        key in data for key in ("sessionId", "toolName", "toolInput")
    ) else "generic"
    ti = data.get("tool_input")
    if not isinstance(ti, dict):
        ti = data.get("toolInput")
    fp = None
    if isinstance(ti, dict):
        for key in ("file_path", "target_file"):
            if isinstance(ti.get(key), str) and ti[key]:
                fp = ti[key]
                break

    def _s(v, cap=4096):
        return v[:cap] if isinstance(v, str) and v else None

    sid = data.get("session_id")
    if not isinstance(sid, str):
        sid = data.get("sessionId")
    tool = data.get("tool_name")
    if not isinstance(tool, str):
        tool = data.get("toolName")
    return _s(sid, 128), _s(tool), _s(fp), adapter


def hook_fields(text: str) -> tuple[str | None, str | None, str | None]:
    """Extract only the session identifier, tool name, and file path.

    This function is the privacy waist. Every other field is discarded before
    any value can reach the ledger or disk. The tool name selects a bounded
    event kind and is not stored.
    """
    sid, tool, path, _adapter = hook_payload(text)
    return sid, tool, path


def _hook_command() -> str:
    """Bind standalone hooks to the running archive, not a PATH namesake."""
    main_loader = getattr(sys.modules.get("__main__"), "__loader__", None)
    if (isinstance(__loader__, zipimport.zipimporter)
            and isinstance(main_loader, zipimport.zipimporter)
            and sys.argv and sys.executable):
        archive = os.path.abspath(__loader__.archive)
        if (os.path.realpath(archive) == os.path.realpath(main_loader.archive)
                == os.path.realpath(sys.argv[0])):
            return " ".join(shlex.quote(part) for part in
                            (os.path.abspath(sys.executable), "-I", archive))
    return shlex.quote(shutil.which("muninn") or "muninn")


def hook_config(root: str, adapter: str = "claude") -> str:
    """Return lifecycle hooks for Claude, Codex, or Grok."""
    if adapter not in ("claude", "codex", "grok"):
        raise ValueError(f"unsupported hook adapter: {adapter}")
    base = f"{_hook_command()} --root {shlex.quote(root)} hook"
    tail = " 2>/dev/null || true"
    start = f"{base} session-start"
    matcher = "Read|Edit|Write|read_file|search_replace|write"
    start_extra = {}
    if adapter == "codex":
        start_extra = {"statusMessage": "Loading Muninn context",
                       "additionalContextLimit": 2500}

    def handler(action: str) -> dict:
        return {"type": "command", "command": action + tail}

    return json.dumps({"hooks": {
        "SessionStart": [{"hooks": [{**handler(start), **start_extra}]}],
        "PostToolUse": [{"matcher": matcher, "hooks": [
            handler(f"{base} post-tool")]}],
        "SessionEnd": [{"hooks": [handler(f"{base} session-end")]}],
    }}, indent=2)


def stdin_text() -> str:
    """Hook stdin, tty-safe: a manual run without a pipe returns ''
    instead of hanging a session-critical command. Reads are capped (8 MB)
    so a giant tool_response is at worst a dropped event, never an OOM."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return ""
        return sys.stdin.read(8 << 20)
    except (OSError, ValueError):
        return ""
