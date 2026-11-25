"""Discover local agent transcripts and synthesize approved onboarding memory.

Discovery inspects regular-file metadata only and never reads or stores message
bodies. Extraction requires explicit ``--source`` paths, rejects symbolic links
and special files, bounds file and line reads, scrubs credentials, omits tool
outputs and assistant dumps, and tracks file watermarks by SHA-256 path digest
in ``.muninn/bootstrap.json``.

The offline ``--request`` and ``--apply`` exchange mirrors ``muninn enrich``:
the host coding agent receives bounded session handles and returns validated
JSON notes, threads, and path-guarded lessons without adding third-party
dependencies.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import tempfile
import time

from .dynamics import Dynamics, sidecar_lock
from .home import is_home, rooms
from .journal import HEAD_NAME, THREADS_DIR, _clip, _slug, scrub
from .store import MUNINN_ACTOR, Bundle, generated_stamp

WATERMARK_FILE = "bootstrap.json"
DEFAULT_MAX_SESSIONS = 50
MAX_SESSIONS = 100
MAX_EXCERPTS = 8
EXCERPT_CHARS = 240
MAX_NOTES = 24
MAX_THREADS = 12
MAX_LESSONS = 12
BODY_CAP = 1200
MAX_TRANSCRIPT_BYTES = 16 << 20
MAX_JSON_PARSE_BYTES = 4 << 20
MAX_LINE_BYTES = 256 << 10
MAX_REPLY_BYTES = 2 << 20
ALLOWED_RESPONSE_KEYS = frozenset({"digest", "notes", "threads", "lessons"})
RESERVED_SLUGS = frozenset({"index", "log"})

_TOKEN_SECRETS = re.compile(
    r"(-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
    r"|-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"
    r"|\bgithub_pat_[A-Za-z0-9_]{20,}\b"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}\b"
    r"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"
    r"|\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"
    r"|['\"]?(?:password|passwd|secret|token|api_key|apikey|access_token|auth_token|client_secret)['\"]?\s*[:=]\s*['\"]?[^\s'\",}\]]+['\"]?)",
    re.IGNORECASE,
)
_URL_CREDENTIALS = re.compile(r"([a-z][a-z0-9+.-]*://[^/\s:@]+:)[^@\s/]+(@)", re.IGNORECASE)
_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _scrub_all(text: str) -> str:
    """Scrub tokens, quoted key-value secrets, Bearer headers, and URL passwords."""
    step1 = _URL_CREDENTIALS.sub(r"\1[scrubbed]\2", str(text))
    step2 = _TOKEN_SECRETS.sub("[scrubbed]", step1)
    return scrub(step2)


def _safe_slug(value: object, fallback: str = "note") -> str:
    """Build a scrubbed, non-reserved OKF slug."""
    cleaned = _scrub_all(str(value or "")).replace("[scrubbed]", "")
    slug = _slug(cleaned) if cleaned.strip() else fallback
    if slug in {"thread", "scrubbed"} and fallback != "thread":
        slug = fallback
    if slug in RESERVED_SLUGS:
        slug = f"{slug}-note"
    return slug


def _is_protected_note(note) -> bool:
    """Protect curated, user-added, and journaled notes lacking extracted/inferred provenance."""
    return note is not None and note.meta.get("provenance") not in ("extracted", "inferred")


def _day_cutoff(days: int | None, now: float | None = None) -> float | None:
    """Compute a UTC day-floor cutoff so --request and --apply match within the same day."""
    if days is None:
        return None
    current = time.time() if now is None else now
    utc_midnight = (int(current) // 86400) * 86400
    return float(utc_midnight - int(days) * 86400)


def _project_slug_for_dir(directory: str) -> str:
    """Derive a stable project slug from an archive directory name."""
    norm = os.path.normpath(os.path.abspath(directory))
    base = os.path.basename(norm)
    if base.lower() in {"chats", "sessions", "logs"}:
        parent = os.path.basename(os.path.dirname(norm))
        if parent and not parent.startswith("."):
            base = parent
        elif base.lower() == "sessions":
            base = "codex-sessions"
    cleaned = base.lstrip("-")
    parts = [part for part in re.split(r"[-_]+", cleaned) if part]
    tail = "-".join(parts[-2:]) if len(parts) > 2 else cleaned
    return _safe_slug(tail or base, fallback="project")


def _is_regular_within(filepath: str, root_real: str) -> os.stat_result | None:
    """Return lstat metadata only when filepath is a non-symlink regular file inside root_real."""
    try:
        st = os.lstat(filepath)
    except OSError:
        return None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return None
    if st.st_size <= 0 or st.st_size > MAX_TRANSCRIPT_BYTES:
        return None
    real = os.path.realpath(filepath)
    try:
        if os.path.commonpath([root_real, real]) != root_real:
            return None
    except ValueError:
        return None
    return st


def _transcript_files_in(path: str) -> list[tuple[str, os.stat_result]]:
    """List regular ``.jsonl`` and ``.json`` transcript files without following symlinks."""
    expanded = os.path.abspath(os.path.expanduser(path))
    if os.path.islink(expanded):
        return []
    try:
        top_st = os.lstat(expanded)
    except OSError:
        return []
    root_real = os.path.realpath(expanded)
    if stat.S_ISREG(top_st.st_mode):
        if expanded.endswith((".jsonl", ".json")) and 0 < top_st.st_size <= MAX_TRANSCRIPT_BYTES:
            return [(expanded, top_st)]
        return []
    if not stat.S_ISDIR(top_st.st_mode):
        return []
    found: list[tuple[str, os.stat_result]] = []
    for dirpath, dirnames, filenames in os.walk(expanded, followlinks=False):
        dirnames[:] = sorted(
            d for d in dirnames
            if not d.startswith(".") and not os.path.islink(os.path.join(dirpath, d))
        )
        for name in sorted(filenames):
            if not name.endswith((".jsonl", ".json")) or name.startswith("."):
                continue
            candidate = os.path.join(dirpath, name)
            checked = _is_regular_within(candidate, root_real)
            if checked is not None:
                found.append((candidate, checked))
    return found


def _default_archive_groups() -> list[str]:
    """Return non-symlink local agent archive directories under the user home."""
    home_dir = os.path.expanduser("~")
    groups: list[str] = []
    claude_projects = os.path.join(home_dir, ".claude", "projects")
    if not os.path.islink(claude_projects) and os.path.isdir(claude_projects):
        for name in sorted(os.listdir(claude_projects)):
            full = os.path.join(claude_projects, name)
            if not name.startswith(".") and not os.path.islink(full) and os.path.isdir(full):
                groups.append(full)
    codex_sessions = os.path.join(home_dir, ".codex", "sessions")
    if not os.path.islink(codex_sessions) and os.path.isdir(codex_sessions):
        named_subdirs = []
        has_year_dirs = False
        for name in sorted(os.listdir(codex_sessions)):
            full = os.path.join(codex_sessions, name)
            if name.startswith(".") or os.path.islink(full) or not os.path.isdir(full):
                continue
            if re.fullmatch(r"\d{4}", name):
                has_year_dirs = True
            else:
                named_subdirs.append(full)
        if named_subdirs:
            groups.extend(named_subdirs)
        if has_year_dirs or not named_subdirs:
            groups.append(codex_sessions)
    gemini_tmp = os.path.join(home_dir, ".gemini", "tmp")
    if not os.path.islink(gemini_tmp) and os.path.isdir(gemini_tmp):
        for name in sorted(os.listdir(gemini_tmp)):
            parent = os.path.join(gemini_tmp, name)
            chats = os.path.join(parent, "chats")
            if (
                not name.startswith(".")
                and not os.path.islink(parent)
                and not os.path.islink(chats)
                and os.path.isdir(chats)
            ):
                groups.append(chats)
    return groups


def discover_archives(
    sources: list[str] | None = None,
    days: int | None = None,
    now: float | None = None,
) -> list[dict]:
    """Inspect regular-file transcript metadata without opening any transcript file."""
    cutoff = _day_cutoff(days, now=now)
    targets = (
        [os.path.abspath(os.path.expanduser(s)) for s in sources]
        if sources
        else _default_archive_groups()
    )
    results: list[dict] = []
    for target in targets:
        mtimes = [
            st.st_mtime
            for _fp, st in _transcript_files_in(target)
            if cutoff is None or st.st_mtime >= cutoff
        ]
        if not mtimes:
            continue
        group_dir = target if os.path.isdir(target) else os.path.dirname(target)
        results.append({
            "source": target,
            "project_slug": _project_slug_for_dir(group_dir),
            "sessions": len(mtimes),
            "earliest": time.strftime("%Y-%m-%d", time.gmtime(min(mtimes))),
            "latest": time.strftime("%Y-%m-%d", time.gmtime(max(mtimes))),
        })
    results.sort(key=lambda item: (item["project_slug"], item["source"]))
    return results


def _watermark_path(root: str) -> str:
    return os.path.join(os.path.abspath(root), ".muninn", WATERMARK_FILE)


def load_watermarks(root: str) -> dict[str, dict[str, int]]:
    """Read hashed file watermarks from ``.muninn/bootstrap.json``."""
    path = _watermark_path(root)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, RecursionError):
        return {}
    files = data.get("files", {}) if isinstance(data, dict) else {}
    if not isinstance(files, dict):
        return {}
    out: dict[str, dict[str, int]] = {}
    for key, val in files.items():
        if isinstance(key, str) and isinstance(val, dict):
            mtime_ns = val.get("mtime_ns")
            size = val.get("size")
            if type(mtime_ns) is int and type(size) is int:
                out[key] = {"mtime_ns": mtime_ns, "size": size}
    return out


def save_watermarks(root: str, watermarks: dict[str, dict[str, int]]) -> None:
    """Persist hashed file watermarks atomically with mode 0600 inside ``.muninn/``."""
    path = _watermark_path(root)
    sidecar = os.path.dirname(path)
    os.makedirs(sidecar, exist_ok=True)
    payload = json.dumps({"version": 1, "files": watermarks}, indent=2, sort_keys=True) + "\n"
    with sidecar_lock(root, "bootstrap"):
        fd, tmp = tempfile.mkstemp(prefix=".bootstrap-", suffix=".tmp", dir=sidecar)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, path)
        except BaseException:
            if os.path.lexists(tmp):
                os.unlink(tmp)
            raise


def _file_key(filepath: str) -> str:
    return hashlib.sha256(os.path.realpath(filepath).encode("utf-8", "replace")).hexdigest()


def _extract_content_text(content: object) -> str:
    """Extract plain user text blocks while ignoring tool results and non-text payloads."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in {"text", "input_text"} and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return " ".join(parts)
    return ""


def _normalize_user_text(raw_content: object) -> str:
    collapsed = " ".join(_extract_content_text(raw_content).split())
    if not collapsed or collapsed.startswith(("<", "[Request")):
        return ""
    return _clip(_scrub_all(collapsed), EXCERPT_CHARS)


def _open_regular_stream(filepath: str):
    """Open a regular file with O_NOFOLLOW and O_NONBLOCK so symlinks and FIFOs fail closed."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(filepath, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_TRANSCRIPT_BYTES:
            os.close(fd)
            return None
        return os.fdopen(fd, "r", encoding="utf-8", errors="replace")
    except BaseException:
        os.close(fd)
        raise


def _user_content_from_event(ev: dict) -> object:
    """Extract user prompt content across Claude, Codex rollout, and generic envelopes."""
    if ev.get("type") == "user" and isinstance(ev.get("message"), dict):
        return ev["message"].get("content")
    if ev.get("role") == "user":
        return ev.get("content")
    if ev.get("type") == "response_item" and isinstance(ev.get("payload"), dict):
        payload = ev["payload"]
        if payload.get("role") == "user":
            return payload.get("content")
    return None


def _extract_session_excerpts(filepath: str, fallback_date: str) -> tuple[str, str, list[str]]:
    """Extract bounded, scrubbed user prompts and summary titles from one session file."""
    summary = ""
    first_date = ""
    excerpts: list[str] = []
    try:
        stream = _open_regular_stream(filepath)
    except OSError:
        return "", fallback_date, []
    if stream is None:
        return "", fallback_date, []

    with stream:
        if filepath.endswith(".jsonl"):
            scanned = 0
            for raw_line in stream:
                scanned += len(raw_line)
                if scanned > MAX_JSON_PARSE_BYTES:
                    break
                if len(raw_line) > MAX_LINE_BYTES:
                    continue
                try:
                    ev = json.loads(raw_line)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(ev, dict):
                    continue
                if ev.get("type") == "summary" and ev.get("summary"):
                    summary = _clip(_scrub_all(str(ev["summary"])), 120)
                    continue
                content = _user_content_from_event(ev)
                if content is None:
                    continue
                cleaned = _normalize_user_text(content)
                if cleaned:
                    ts = str(ev.get("timestamp", ""))[:10]
                    if not first_date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", ts):
                        first_date = ts
                    excerpts.append(cleaned)
                if len(excerpts) >= MAX_EXCERPTS:
                    break
            return summary, first_date or fallback_date, excerpts

        raw_json = stream.read(MAX_JSON_PARSE_BYTES + 1)
        if len(raw_json) > MAX_JSON_PARSE_BYTES:
            return "", fallback_date, []
        try:
            doc = json.loads(raw_json)
        except (ValueError, RecursionError):
            return "", fallback_date, []
        messages = doc.get("messages", []) if isinstance(doc, dict) else []
        if not isinstance(messages, list):
            return "", fallback_date, []
        for item in messages:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "user" or item.get("role") == "user":
                cleaned = _normalize_user_text(item.get("content"))
                if cleaned:
                    ts = str(item.get("timestamp", ""))[:10]
                    if not first_date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", ts):
                        first_date = ts
                    excerpts.append(cleaned)
                if len(excerpts) >= MAX_EXCERPTS:
                    break
        return summary, first_date or fallback_date, excerpts


def collect_candidates(
    root: str,
    sources: list[str],
    days: int | None = None,
    max_sessions: int = DEFAULT_MAX_SESSIONS,
    force: bool = False,
    now: float | None = None,
) -> tuple[list[dict], dict[str, dict[str, int]]]:
    """Collect scrubbed candidate session records from explicitly approved paths."""
    if not sources:
        raise ValueError(
            "Pass at least one approved --source directory or file after inspecting `muninn bootstrap`."
        )
    cutoff = _day_cutoff(days, now=now)
    cap = min(max(1, int(max_sessions)), MAX_SESSIONS)
    watermarks = load_watermarks(root)
    eligible: list[tuple[float, str, str, str, dict[str, int]]] = []
    seen_keys: set[str] = set()

    for src in sources:
        expanded = os.path.abspath(os.path.expanduser(src))
        group_dir = expanded if os.path.isdir(expanded) else os.path.dirname(expanded)
        project_slug = _project_slug_for_dir(group_dir)
        for filepath, st in _transcript_files_in(expanded):
            if cutoff is not None and st.st_mtime < cutoff:
                continue
            key = _file_key(filepath)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            wm = {"mtime_ns": int(st.st_mtime_ns), "size": int(st.st_size)}
            if not force and watermarks.get(key) == wm:
                continue
            eligible.append((st.st_mtime, key, project_slug, filepath, wm))

    # Bound file reads to the newest candidate sessions first, then order deterministically.
    eligible.sort(key=lambda entry: (-entry[0], entry[1]))
    raw_items: list[dict] = []
    for mtime, key, project_slug, filepath, wm in eligible:
        if len(raw_items) >= cap:
            break
        fallback_date = time.strftime("%Y-%m-%d", time.gmtime(mtime))
        summary, date_str, excerpts = _extract_session_excerpts(filepath, fallback_date)
        if not summary and not excerpts:
            continue
        raw_items.append({
            "project": project_slug,
            "date": date_str,
            "summary": summary or _clip(excerpts[0], 80),
            "excerpts": excerpts,
            "file_key": key,
            "watermark": wm,
        })

    raw_items.sort(key=lambda item: (item["project"], item["date"], item["file_key"]))
    for index, item in enumerate(raw_items, start=1):
        item["handle"] = f"S{index:03d}"
    return raw_items, watermarks


def candidate_digest(candidates: list[dict]) -> str:
    """Compute a SHA-256 digest over the exact model-visible candidate fields."""
    canonical = [
        {
            "handle": c["handle"],
            "project": c["project"],
            "date": c["date"],
            "summary": c["summary"],
            "excerpts": c["excerpts"],
        }
        for c in candidates
    ]
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def render_request(
    candidates: list[dict],
    root: str | None = None,
    sources: list[str] | tuple[str, ...] = (),
    days: int | None = None,
    max_sessions: int = DEFAULT_MAX_SESSIONS,
    force: bool = False,
) -> str:
    """Render the complete model perception and action contract for bootstrap."""
    digest = candidate_digest(candidates)
    q = shlex.quote
    parts = ["muninn"]
    if root and root != ".":
        parts.extend(["--root", q(root)])
    parts.append("bootstrap")
    for src in sources:
        parts.extend(["--source", q(src)])
    if days is not None:
        parts.extend(["--days", str(days)])
    if max_sessions != DEFAULT_MAX_SESSIONS:
        parts.extend(["--max-sessions", str(max_sessions)])
    if force:
        parts.append("--force")
    parts.extend(["--apply", "response.json"])
    apply_cmd = "    " + " ".join(parts)

    schema_example = json.dumps(
        {
            "digest": digest,
            "notes": [
                {
                    "slug": "topic-slug",
                    "project": "project-slug",
                    "title": "Short factual title",
                    "summary": "Durable architectural fact or decision.",
                    "sources": ["S001"],
                }
            ],
            "threads": [
                {
                    "slug": "workstream-slug",
                    "title": "Workstream title",
                    "state": "CURRENT: state. DECIDED: choice. NEXT: next action.",
                    "episode": "Summary of recent progress across cited sessions.",
                    "sources": ["S001"],
                }
            ],
            "lessons": [
                {
                    "title": "Preventable mistake or correction",
                    "body": "Concrete rule that prevents repeating the mistake.",
                    "guards": ["required/path/prefix"],
                    "sources": ["S001"],
                }
            ],
        },
        indent=2,
    )

    last_handle = candidates[-1]["handle"] if candidates else "S001"
    lines = [
        "# Muninn onboarding bootstrap request",
        "",
        "Synthesize durable onboarding knowledge from the scrubbed session excerpts below.",
        "Write the strict JSON response to `response.json` and apply it with:",
        "",
        apply_cmd,
        "",
        "## Perception and action contract",
        "",
        f"- Candidate sessions supplied: {len(candidates)} (`S001` through `{last_handle}`, max {MAX_SESSIONS}; one summary line plus up to {MAX_EXCERPTS} scrubbed user excerpts of {EXCERPT_CHARS} chars each).",
        "- Omitted inputs: unapproved directories, symbolic links, tool inputs and outputs, shell logs, system prompts, assistant code dumps, and unchanged watermarked files.",
        f"- Allowed outputs: up to {MAX_NOTES} `notes`, {MAX_THREADS} `threads`, and {MAX_LESSONS} path-guarded unpinned `lessons` (each body capped at {BODY_CAP} chars; `provenance: inferred`).",
        "- Safety bounds: every item must cite valid `sources` handles, lessons must declare at least one `guards` path prefix, the exact `digest` must be echoed, and user or `curated` notes are never overwritten.",
        "",
        "## Required JSON schema",
        "",
        "```json",
        schema_example,
        "```",
        "",
        "## Candidate sessions",
        "",
    ]
    for c in candidates:
        lines.append(f"### {c['handle']} (project: `{c['project']}`, date: {c['date']})")
        lines.append(f"Summary: {c['summary']}")
        for ex in c["excerpts"]:
            lines.append(f"- {ex}")
        lines.append("")
    return "\n".join(lines)


def _parse_json_reply(reply: str) -> dict:
    if len(reply or "") > MAX_REPLY_BYTES:
        raise ValueError("The bootstrap response exceeds the 2 MiB size limit.")
    text = (reply or "").strip()
    fenced = _JSON_FENCE.search(text)
    if fenced:
        text = fenced.group(1)
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError) as error:
        raise ValueError("The bootstrap response is not valid JSON.") from error
    if not isinstance(parsed, dict):
        raise TypeError("The bootstrap response must be a JSON object.")
    return parsed


def _validate_sources(field_sources: object, valid_handles: set[str], label: str) -> list[str]:
    if not isinstance(field_sources, list) or not field_sources:
        raise ValueError(f"{label} must include a non-empty `sources` list.")
    cleaned: list[str] = []
    for handle in field_sources:
        if not isinstance(handle, str) or handle not in valid_handles:
            raise ValueError(f"{label} references an unknown session handle: {handle!r}.")
        cleaned.append(handle)
    return cleaned


def _resolve_existing(bundle: Bundle, by_lower: dict[str, object], rel: str) -> tuple[str, object | None]:
    """Resolve case-insensitive collisions to existing canonical bundle paths."""
    canonical = bundle._canonical_note_path(rel)
    hit = by_lower.get(canonical.casefold())
    if hit is not None:
        existing_path, existing_note = hit
        return existing_path, existing_note
    return canonical, None


def apply_response(
    bundle: Bundle,
    dyn: Dynamics,
    candidates: list[dict],
    reply: str,
    watermarks: dict[str, dict[str, int]],
) -> dict[str, int]:
    """Validate an agent synthesis response completely before writing bounded notes."""
    proposal = _parse_json_reply(reply)
    unknown_keys = set(proposal) - ALLOWED_RESPONSE_KEYS
    if unknown_keys:
        raise ValueError(f"Unexpected top-level field(s): {', '.join(sorted(unknown_keys))}.")
    expected = candidate_digest(candidates)
    if proposal.get("digest") != expected:
        raise ValueError(
            "Digest mismatch: the candidate transcript set changed since --request. "
            "Run --request again with the same flags."
        )
    notes_in = proposal.get("notes", [])
    threads_in = proposal.get("threads", [])
    lessons_in = proposal.get("lessons", [])
    if (
        not isinstance(notes_in, list)
        or not isinstance(threads_in, list)
        or not isinstance(lessons_in, list)
    ):
        raise TypeError("`notes`, `threads`, and `lessons` must be JSON lists.")
    if (
        len(notes_in) > MAX_NOTES
        or len(threads_in) > MAX_THREADS
        or len(lessons_in) > MAX_LESSONS
    ):
        raise ValueError("Response exceeds maximum notes, threads, or lessons bounds.")

    valid_handles = {c["handle"] for c in candidates}
    registered_rooms = set(rooms(bundle.root).values()) if is_home(bundle.root) else set()
    by_lower: dict[str, tuple[str, object]] = {
        path.casefold(): (path, note) for path, note in bundle.notes.items()
    }

    validated_notes: list[tuple[str, object | None, str, str]] = []
    for idx, item in enumerate(notes_in):
        if not isinstance(item, dict):
            raise TypeError(f"notes[{idx}] must be an object.")
        _validate_sources(item.get("sources"), valid_handles, f"notes[{idx}]")
        title = _scrub_all(str(item.get("title", "")).strip())[:120]
        summary = _scrub_all(str(item.get("summary", "")).strip())[:BODY_CAP]
        if not title or not summary:
            raise ValueError(f"notes[{idx}] requires non-empty `title` and `summary`.")
        proj_slug = _safe_slug(item.get("project") or "general", fallback="general")
        note_slug = _safe_slug(item.get("slug") or title, fallback="note")
        if is_home(bundle.root) and proj_slug in registered_rooms:
            raw_rel = f"projects/{proj_slug}/{note_slug}.md"
        elif is_home(bundle.root):
            raw_rel = f"notes/{proj_slug}/{note_slug}.md"
        else:
            raw_rel = f"notes/{note_slug}.md"
        target_rel, existing_note = _resolve_existing(bundle, by_lower, raw_rel)
        validated_notes.append((target_rel, existing_note, title, summary))

    validated_threads: list[tuple[str, str, object | None, str, str, str]] = []
    for idx, item in enumerate(threads_in):
        if not isinstance(item, dict):
            raise TypeError(f"threads[{idx}] must be an object.")
        _validate_sources(item.get("sources"), valid_handles, f"threads[{idx}]")
        title = _scrub_all(str(item.get("title", "")).strip())[:120]
        state = _scrub_all(str(item.get("state", "")).strip())[:BODY_CAP]
        episode = _scrub_all(str(item.get("episode", "")).strip())[:BODY_CAP]
        if not title or (not state and not episode):
            raise ValueError(f"threads[{idx}] requires `title` and `state` or `episode`.")
        t_slug = _safe_slug(item.get("slug") or title, fallback="thread")
        head_rel, existing_head = _resolve_existing(
            bundle, by_lower, f"{THREADS_DIR}/{t_slug}/{HEAD_NAME}"
        )
        validated_threads.append((t_slug, head_rel, existing_head, title, state, episode))

    validated_lessons: list[tuple[str, object | None, str, str, list[str]]] = []
    for idx, item in enumerate(lessons_in):
        if not isinstance(item, dict):
            raise TypeError(f"lessons[{idx}] must be an object.")
        _validate_sources(item.get("sources"), valid_handles, f"lessons[{idx}]")
        title = _scrub_all(str(item.get("title", "")).strip())[:120]
        body = _scrub_all(str(item.get("body", "")).strip())[:BODY_CAP]
        raw_guards = item.get("guards", [])
        if not title or not body or not isinstance(raw_guards, list):
            raise ValueError(f"lessons[{idx}] requires `title`, `body`, and list `guards`.")
        guards = [
            _scrub_all(str(g).strip())[:120]
            for g in raw_guards
            if isinstance(g, str) and g.strip()
        ][:8]
        if not guards:
            raise ValueError(f"lessons[{idx}] requires at least one path prefix in `guards`.")
        l_slug = _safe_slug(title, fallback="lesson")
        target_rel, existing_lesson = _resolve_existing(bundle, by_lower, f"lessons/{l_slug}.md")
        validated_lessons.append((target_rel, existing_lesson, title, body, guards))

    counts = {"notes": 0, "threads": 0, "lessons": 0, "skipped_curated": 0}
    stamp = generated_stamp()

    for rel, existing, title, summary in validated_notes:
        if _is_protected_note(existing):
            counts["skipped_curated"] += 1
            continue
        written_rel = bundle.write_note(
            rel,
            {"type": "note", "title": title, "provenance": "inferred", "generated": stamp},
            summary,
        )
        dyn.touch(written_rel, kind="encode")
        counts["notes"] += 1

    now_ts = time.time()
    for t_slug, head_rel, existing_head, title, state, episode in validated_threads:
        if _is_protected_note(existing_head):
            counts["skipped_curated"] += 1
        elif state or existing_head is None:
            bundle.write_note(
                head_rel,
                {"type": "thread", "title": title, "provenance": "inferred", "generated": stamp},
                state or "(no compiled state yet: see episodes)",
            )
        base = f"{THREADS_DIR}/{t_slug}/{time.strftime('%Y%m%d-%H%M%S', time.localtime(now_ts))}"
        ep_rel, suffix = base + ".md", 2
        while os.path.lexists(os.path.join(bundle.root, ep_rel)):
            ep_rel = f"{base}-{suffix}.md"
            suffix += 1
        written_ep = bundle.write_note(
            ep_rel,
            {
                "type": "episode",
                "title": f"{title}: bootstrap",
                "provenance": "inferred",
                "generated": stamp,
            },
            episode or state,
        )
        dyn.touch(written_ep, kind="encode")
        counts["threads"] += 1

    for rel, existing, title, body, guards in validated_lessons:
        if _is_protected_note(existing):
            counts["skipped_curated"] += 1
            continue
        written_rel = bundle.write_note(
            rel,
            {
                "type": "lesson",
                "title": title,
                "pinned": False,
                "provenance": "inferred",
                "guards": guards,
                "generated": stamp,
            },
            body,
        )
        dyn.touch(written_rel, kind="encode")
        counts["lessons"] += 1

    updated = dict(watermarks)
    for c in candidates:
        updated[c["file_key"]] = c["watermark"]
    save_watermarks(bundle.root, updated)
    bundle.generate_index()
    return counts


def bootstrap_deterministic(
    bundle: Bundle,
    dyn: Dynamics,
    candidates: list[dict],
    watermarks: dict[str, dict[str, int]],
) -> tuple[int, int]:
    """Import scrubbed episode notes without a model synthesis pass."""
    imported = skipped = 0
    by_lower: dict[str, tuple[str, object]] = {
        path.casefold(): (path, note) for path, note in bundle.notes.items()
    }
    for c in candidates:
        raw_rel = f"{THREADS_DIR}/{c['project']}/bootstrap-{c['file_key'][:12]}.md"
        ep_rel, existing = _resolve_existing(bundle, by_lower, raw_rel)
        if _is_protected_note(existing):
            skipped += 1
            continue
        body_lines = [
            f"Imported session ({c['date']}): {c['summary']}",
            "",
            "Recorded user requests:",
            *(f"- {ex}" for ex in c["excerpts"]),
        ]
        written_rel = bundle.write_note(
            ep_rel,
            {
                "type": "episode",
                "title": c["summary"][:80],
                "provenance": "extracted",
                "generated": {"by": MUNINN_ACTOR, "at": f"{c['date']}T00:00:00"},
            },
            "\n".join(body_lines),
        )
        dyn.touch(written_rel, kind="encode")
        imported += 1
    updated = dict(watermarks)
    for c in candidates:
        updated[c["file_key"]] = c["watermark"]
    save_watermarks(bundle.root, updated)
    bundle.generate_index()
    return imported, skipped
