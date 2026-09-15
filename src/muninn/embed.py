"""Provide an optional semantic seed for lexical recall.

When configured, an OpenAI-compatible embedding endpoint converts cues and
notes into vectors. Cosine similarity can admit a note without matching words,
but the activation limits prevent it from outranking a strong lexical result.

``MUNINN_EMBED_URL`` accepts loopback hosts by default. A nonlocal endpoint
requires ``MUNINN_EMBED_ALLOW_REMOTE=1`` and produces a diagnostic naming the
host. Local requests ignore proxies and reject redirects. Transport failure or
malformed output returns control to lexical recall without raising. A
``PrivacyError`` remains explicit. Rebuildable vectors are stored under
``.muninn/embeddings.json`` by note path and content digest.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse

# The egress policy: PrivacyError, loopback pinning, the hardened
# no-proxy/no-redirect opener vs the consented normal one, the fail-soft
# transport tuple: is shared machinery (netcfg.py, one home for every
# optional network channel). The names below are this module's stable
# surface; tests and callers keep using them unchanged.
from .netcfg import (  # noqa: E402
    HARDENED_OPENER as _OPENER,
    LOCAL_HOSTS as _LOCAL_HOSTS,
    REMOTE_OPENER as _REMOTE_OPENER,
    PrivacyError,
    allow_remote as _nc_allow_remote,
)

_CACHE_NAME = "embeddings.json"
_BATCH = 64        # texts per endpoint call (SPEC: batch <= 64)
_TIMEOUT = 10      # seconds per call
_MAX_CHARS = 8000  # cap per note text: the gist is enough, keeps payloads sane

_WARNED_REMOTE: set[str] = set()  # hosts already warned about (warn once each)


def _allow_remote() -> bool:
    """True when the user has explicitly opted in to a non-local endpoint via
    ``MUNINN_EMBED_ALLOW_REMOTE`` (``1``/``true``/``yes``, case-insensitive)."""
    return _nc_allow_remote("MUNINN_EMBED_ALLOW_REMOTE")


def _warn_remote(host: str) -> None:
    """One stderr line the first time note content is sent to a consented
    remote ``host``: a visible reminder that content is leaving the machine."""
    if host not in _WARNED_REMOTE:
        _WARNED_REMOTE.add(host)
        print(f"muninn: sending note content to remote embedding host {host} "
              "(MUNINN_EMBED_ALLOW_REMOTE set)", file=sys.stderr)


def _endpoint() -> tuple[str | None, str | None, bool]:
    """``(url, model, remote)`` from the environment, or ``(None, None,
    False)`` when no endpoint is configured. A non-local host RAISES
    ``PrivacyError`` at config time (its own type, deliberately NOT fail-soft)
    UNLESS ``MUNINN_EMBED_ALLOW_REMOTE`` opts in: then ``remote`` is True (a
    normal proxy/redirect-following opener is used and a one-line stderr
    warning names the host). The http(s) scheme guard always applies."""
    url = os.environ.get("MUNINN_EMBED_URL")
    if not url:
        return None, None, False
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    remote = host not in _LOCAL_HOSTS
    if remote and not _allow_remote():
        raise PrivacyError(
            f"MUNINN_EMBED_URL host {host!r} is not local: refusing to send "
            "note content off-machine by default. Use a loopback endpoint "
            "(127.0.0.1, localhost, or ::1): a local OpenAI-compatible "
            "embeddings server (e.g. llama.cpp `llama-server --embeddings`) "
            "at http://127.0.0.1:8181/v1/embeddings: or set "
            "MUNINN_EMBED_ALLOW_REMOTE=1 to send to this remote host on "
            "purpose.")
    if parts.scheme not in ("http", "https"):
        raise PrivacyError(
            f"MUNINN_EMBED_URL scheme {parts.scheme!r} is not http(s): "
            "expected an OpenAI-compatible /v1/embeddings endpoint (e.g. "
            "llama.cpp `llama-server --embeddings` at "
            "http://127.0.0.1:8181/v1/embeddings).")
    if remote:
        _warn_remote(host)
    return url, os.environ.get("MUNINN_EMBED_MODEL") or None, remote


def _cache_path(bundle) -> str:
    return os.path.join(bundle.root, ".muninn", _CACHE_NAME)


def available(bundle) -> bool:
    """Whether the semantic channel can be attempted for this bundle: an
    endpoint is configured (a loopback host, or a consented remote via
    ``MUNINN_EMBED_ALLOW_REMOTE``) OR a cached vector set exists. The URL is
    validated FIRST, so a non-local/non-http URL raises here: the earliest
    explicit privacy check, even when a cache is present, unless the user opted
    in (privacy)."""
    if _endpoint()[0] is not None:  # configured: validates, may raise
        return True
    return os.path.exists(_cache_path(bundle))


def _warn(msg: str) -> None:
    print(f"muninn embed: {msg}", file=sys.stderr)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _note_text(note) -> str:
    """The text embedded for a note: title + description + body, capped."""
    parts = [note.title, note.description, note.body]
    return "\n".join(p for p in parts if p).strip()[:_MAX_CHARS]


def _load_cache(bundle) -> dict:
    try:
        with open(_cache_path(bundle), encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict) and isinstance(d.get("notes"), dict):
            return d
    except (OSError, ValueError):
        pass
    return {"model": None, "notes": {}}


def _save_cache(bundle, cache: dict) -> None:
    path = _cache_path(bundle)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
        os.replace(tmp, path)
    except OSError as e:  # a read-only bundle must not break recall
        _warn(f"could not write embeddings cache: {e}")


from .netcfg import TRANSPORT as _TRANSPORT  # noqa: E402  (stable name)


def _prefix(kind: str) -> str:
    """Task prefix for a ``"query"`` (a cue) or ``"doc"`` (a note), for
    retrieval models that expect one (e.g. nomic's ``"search_query: "`` /
    ``"search_document: "``). Read from ``MUNINN_EMBED_QUERY_PREFIX`` /
    ``MUNINN_EMBED_DOC_PREFIX``; both default empty, so the payload is
    byte-identical to no prefix unless set."""
    env = ("MUNINN_EMBED_QUERY_PREFIX" if kind == "query"
           else "MUNINN_EMBED_DOC_PREFIX")
    return os.environ.get(env, "")


def _post(url: str, model: str | None, texts: list[str], opener,
          prefix: str) -> list[list[float]]:
    """One POST to the OpenAI-compatible endpoint via ``opener`` (the hardened
    no-proxy/no-redirect opener for a local host, a normal one for a consented
    remote). ``prefix`` is prepended to each input (empty by default → payload
    byte-identical to no prefix). Raises on any transport or format error
    (callers fail soft around this)."""
    payload: dict = {"input": [prefix + t for t in texts]}
    if model:
        payload["model"] = model
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        resp = opener.open(req, timeout=_TIMEOUT)
    except urllib.error.HTTPError as e:
        e.close()  # a 4xx/5xx IS a file-like response: release its socket, then
        raise      # re-raise so the caller still fails soft (caught by _TRANSPORT)
    with resp:
        doc = json.loads(resp.read().decode("utf-8"))
    rows = doc["data"]
    if len(rows) != len(texts):
        raise ValueError(f"endpoint returned {len(rows)} vectors for "
                         f"{len(texts)} inputs")
    return [_vector(row["embedding"]) for row in rows]


def _vector(value) -> list[float]:
    """Validate provider and cached vectors before using their arithmetic."""
    if (not isinstance(value, list) or not value
            or any(isinstance(item, bool) or not isinstance(item, (int, float))
                   for item in value)):
        raise ValueError("embedding must be a nonempty numeric list")
    try:
        vector = [float(item) for item in value]
    except OverflowError:
        raise ValueError("embedding component exceeds the numeric range") from None
    if not all(math.isfinite(item) for item in vector):
        raise ValueError("embedding components must be finite")
    return vector


def _embed_batch(url: str, model: str | None, texts: list[str], opener,
                 kind: str) -> list[list[float]]:
    prefix = _prefix(kind)
    out: list[list[float]] = []
    for i in range(0, len(texts), _BATCH):
        out.extend(_post(url, model, texts[i:i + _BATCH], opener, prefix))
    return out


def embed_text(text: str) -> list[float] | None:
    """Embed one text (a CUE) via the endpoint. Returns the vector, or
    ``None`` (fail-soft) when no endpoint is configured or the call fails. A
    non-local URL raises unless opted in (privacy). The query prefix, if set,
    is prepended."""
    url, model, remote = _endpoint()
    if not url or not text.strip():
        return None
    opener = _REMOTE_OPENER if remote else _OPENER
    try:
        return _embed_batch(url, model, [text[:_MAX_CHARS]], opener, "query")[0]
    except _TRANSPORT as e:
        _warn(f"embed_text failed ({e}); recall stays lexical")
        return None


def embed_notes(bundle, dyn=None) -> dict[str, list[float]]:
    """Return cached or newly computed vectors by note path.

The cache key combines the note path and content digest. Changed or missing
entries are embedded in batches and persisted after each successful batch.
A transport failure returns the vectors already available, while an unapproved
nonlocal URL raises ``PrivacyError``. The optional ``dyn`` argument is
accepted for call-site symmetry and does not affect vectors.
    """
    url, model, remote = _endpoint()  # raises on non-local URL unless opted in
    cache = _load_cache(bundle)
    cached: dict = cache.get("notes", {})
    if url and cache.get("model") != model:
        cached = {}  # a model change invalidates vectors we can re-embed

    vecs: dict[str, list[float]] = {}
    todo: list[tuple[str, str, str]] = []  # (path, text, sha)
    for path, note in bundle.notes.items():
        text = _note_text(note)
        if not text:
            continue
        sha = _sha(text)
        hit = cached.get(path)
        if isinstance(hit, dict) and hit.get("sha") == sha:
            try:
                vecs[path] = _vector(hit.get("vec"))
                continue
            except ValueError:
                pass  # Invalid derived vectors must not obstruct lexical recall.
        todo.append((path, text, sha))

    if todo:
        if not url:
            return vecs  # offline: serve whatever the cache matched by sha
        opener = _REMOTE_OPENER if remote else _OPENER
        # Embed batch by batch and PERSIST the batches that landed even when a
        # later one fails. A transient endpoint error (e.g. a flaky server's
        # HTTP 500) then costs only the un-embedded tail, not the whole run.
        # (Before: one 500 in a multi-batch bundle discarded every vector
        # already computed and wrote no cache, so each retry re-embedded from
        # scratch and re-failed: the eval's ~3.8k-requests, zero-cache bug.)
        new = 0
        failed = False
        for i in range(0, len(todo), _BATCH):
            chunk = todo[i:i + _BATCH]
            try:
                fresh = _embed_batch(url, model, [t for _p, t, _s in chunk],
                                     opener, "doc")
            except _TRANSPORT as e:
                _warn(f"embed_notes failed ({e}); recall stays lexical")
                failed = True
                break
            for (path, _t, sha), vec in zip(chunk, fresh):
                vecs[path] = vec
                cached[path] = {"sha": sha, "vec": vec}
                new += 1
        if new:
            # only a real embed writes: a total failure (no batch landed)
            # never CREATES or truncates a cache: a dead endpoint stays a no-op
            for gone in set(cached) - set(bundle.notes):  # rebuildable: prune
                del cached[gone]
            _save_cache(bundle, {"model": model, "notes": cached})
        if failed:
            return {}  # fail-soft: this call's recall stays lexical
    return vecs


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity in [-1, 1]; 0.0 for empty/mismatched/zero vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    scale_a, scale_b = max(map(abs, a)), max(map(abs, b))
    if not scale_a or not scale_b:
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        x, y = x / scale_a, y / scale_b
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / ((na ** 0.5) * (nb ** 0.5))))
