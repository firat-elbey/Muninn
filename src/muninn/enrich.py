"""Add optional model-derived concepts to a deterministic graph.

Enrichment appends ``provenance: inferred`` concepts and cross-file edges at
weight 0.5. It never rewrites or removes extracted nodes, edges, frontmatter,
or prose. Labels and relationships are treated as untrusted input, validated,
and bounded.

Provider resolution uses an injected callable, ``MUNINN_ENRICH_CMD``,
``MUNINN_ENRICH_URL``, or an offline request and response exchange, in that
order. The exchange includes a digest so that a response cannot apply to a
changed base graph.

The URL must use a loopback host unless
``MUNINN_ENRICH_ALLOW_REMOTE=1`` is set. Local requests ignore proxies and
reject redirects. A configured command is an explicit decision to pass the
prompt to that command; Muninn cannot constrain the command's network
behavior. Provider failure or malformed output returns the unchanged
deterministic graph.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse

# The egress policy is shared machinery (netcfg.py): one home for every
# optional network channel, so embed and enrich can never drift apart.
from .ingest import _clean, _linksafe
from .netcfg import (
    HARDENED_OPENER as _OPENER,
    LOCAL_HOSTS as _LOCAL_HOSTS,
    REMOTE_OPENER as _REMOTE_OPENER,
    TRANSPORT as _TRANSPORT,
    PrivacyError,
    allow_remote as _nc_allow_remote,
)

_TIMEOUT = 60          # seconds per call: an LLM is slower than an embedder
_MAX_NODES_PROMPT = 200  # base nodes described to the model (deterministic head)
MAX_CONCEPTS = 24      # inferred concept nodes accepted per enrichment
MAX_EDGES = 60         # inferred direct edges accepted per enrichment
MAX_MEMBERS = 12       # member edges accepted per concept (a theme grouping
#                        Larger groups reduce precision.)
_SUMMARY_CAP = 400     # chars of a concept summary kept (becomes the note body)


_WARNED_REMOTE: set[str] = set()


def _warn(msg: str) -> None:
    print(f"muninn enrich: {msg}", file=sys.stderr)


def _allow_remote() -> bool:
    return _nc_allow_remote("MUNINN_ENRICH_ALLOW_REMOTE")


def _warn_remote(host: str) -> None:
    if host not in _WARNED_REMOTE:
        _WARNED_REMOTE.add(host)
        print(f"muninn: sending graph summary to remote enrichment host {host} "
              "(MUNINN_ENRICH_ALLOW_REMOTE set)", file=sys.stderr)


def _endpoint() -> tuple[str | None, str | None, bool]:
    """``(url, model, remote)`` from the environment, or ``(None, None, False)``
    when no endpoint is configured. A non-local host RAISES ``PrivacyError`` at
    config time (its own type, NOT fail-soft) unless ``MUNINN_ENRICH_ALLOW_REMOTE``
    opts in; a non-http(s) scheme always raises. Mirrors ``embed._endpoint``."""
    url = os.environ.get("MUNINN_ENRICH_URL")
    if not url:
        return None, None, False
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    remote = host not in _LOCAL_HOSTS
    if remote and not _allow_remote():
        raise PrivacyError(
            f"MUNINN_ENRICH_URL host {host!r} is not local: refusing to send "
            "the graph summary off-machine by default. Use a loopback endpoint "
            "(127.0.0.1, localhost, or ::1): a local OpenAI-compatible "
            "/v1/chat/completions server (e.g. llama.cpp `llama-server`) at "
            "http://127.0.0.1:8080/v1/chat/completions: or set "
            "MUNINN_ENRICH_ALLOW_REMOTE=1 to send to this remote host on "
            "purpose.")
    if parts.scheme not in ("http", "https"):
        raise PrivacyError(
            f"MUNINN_ENRICH_URL scheme {parts.scheme!r} is not http(s): "
            "expected an OpenAI-compatible /v1/chat/completions endpoint.")
    if remote:
        _warn_remote(host)
    return url, os.environ.get("MUNINN_ENRICH_MODEL") or None, remote


def available() -> bool:
    """True when an enrichment endpoint is configured (validates the URL, so a
    nonlocal or non-HTTP URL raises at this explicit privacy check)."""
    return _endpoint()[0] is not None


def _temperature() -> float:
    try:
        value = float(os.environ.get("MUNINN_ENRICH_TEMPERATURE", "0") or 0)
        return value if math.isfinite(value) else 0.0
    except ValueError:
        return 0.0


def _chat_endpoint(system: str, user: str) -> str:
    """One POST to the configured OpenAI-compatible chat endpoint; returns the
    assistant message content. Raises on any transport/format error (the caller
    fails soft around this). Used when ``enrich`` is not given an explicit
    ``chat`` callable."""
    url, model, remote = _endpoint()
    if not url:
        raise ValueError("no MUNINN_ENRICH_URL configured")
    payload: dict = {
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": _temperature(),
        "stream": False,
    }
    if model:
        payload["model"] = model
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    opener = _REMOTE_OPENER if remote else _OPENER
    try:
        resp = opener.open(req, timeout=_TIMEOUT)
    except urllib.error.HTTPError as e:
        e.close()  # a 4xx/5xx is a file-like response: release the socket, then
        raise      # re-raise so the caller still fails soft (caught by _TRANSPORT)
    with resp:
        doc = json.loads(resp.read().decode("utf-8"))
    return doc["choices"][0]["message"]["content"]


# -- provider selection ------------------------------------------------------

_CMD_TIMEOUT = 300  # seconds a coding-agent CLI gets per call (they think)


def _cmd_timeout() -> float:
    try:
        return float(os.environ.get("MUNINN_ENRICH_TIMEOUT", "") or _CMD_TIMEOUT)
    except ValueError:
        return float(_CMD_TIMEOUT)


def _chat_cmd(cmd: str):
    """Return a callable that exchanges a prompt with the configured command.

    The command is split without a shell, and the prompt travels through
    standard input. Spawn failure, nonzero exit, and timeout raise an error
    that the enrichment boundary handles.
    """
    argv = shlex.split(cmd)

    def chat(system: str, user: str) -> str:
        proc = subprocess.run(
            argv, input=(system + "\n\n" + user).encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=_cmd_timeout())
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()[:200]
            raise ValueError(f"agent command exited {proc.returncode}"
                             + (f": {err}" if err else ""))
        return proc.stdout.decode("utf-8", "replace")

    return chat


def resolve_chat():
    """Resolve a command provider, URL provider, or offline exchange.

    Command configuration takes precedence over URL validation. Invalid
    command syntax returns no provider after one diagnostic. A configured
    nonlocal URL without consent raises ``PrivacyError``.
    """
    cmd = os.environ.get("MUNINN_ENRICH_CMD", "").strip()
    if cmd:
        try:
            return _chat_cmd(cmd)  # shlex-splits eagerly: may ValueError
        except ValueError as e:
            _warn(f"MUNINN_ENRICH_CMD is not parseable ({e}); enrichment "
                  "skipped (fix the quoting, or unset it)")
            return None
    if available():  # validates MUNINN_ENRICH_URL; may raise PrivacyError
        return _chat_endpoint
    return None


# -- prompt / protocol ------------------------------------------------------

_SYSTEM = (
    "Add a small inferred layer to a deterministic knowledge graph. "
    "Do not restate information already represented by the extracted nodes. "
    "Use complete, formal, concise English for every label and summary.\n"
    "Return valid JSON only, without prose or a code fence, in this form:\n"
    '{"concepts": [{"label": "<short theme name>", '
    '"summary": "<one or two sentences>", "members": ["n3","n7"]}], '
    '"edges": [{"source": "n3", "target": "n7", "relation": "depends_on"}]}\n'
    "Rules: (1) A concept groups related existing nodes and should span more "
    "than one source file when the evidence supports that scope. Its members "
    "are node handles. (2) An edge records an inferred cross-file relationship. "
    "The relation must be one lowercase token such as relates_to, depends_on, "
    "part_of, uses, or contrasts_with. (3) Use only the provided handles. "
    "(4) Include only additions supported by the supplied nodes. (5) Return "
    "empty lists when no addition is supported.")


def _node_label(node: dict) -> str:
    return _clean(node.get("label")) or _clean(node.get("id")) or "?"


def _prompt_nodes(nodes: list[dict]) -> tuple[str, dict[str, str]]:
    """A compact handle table for the model and the handle->id map back.
    Deterministic head of at most ``_MAX_NODES_PROMPT`` id-bearing nodes in
    graph order. Nodes without a usable id are skipped (a malformed base graph
    must never crash the pass)."""
    handle_of: dict[str, str] = {}
    lines: list[str] = []
    for node in nodes:
        if len(handle_of) >= _MAX_NODES_PROMPT:
            break
        if not isinstance(node, dict) or not isinstance(
                node.get("id"), (str, int)):
            continue
        h = f"n{len(handle_of)}"
        handle_of[h] = node["id"]
        src = _clean(node.get("source_file")) or "?"
        kind = _clean(node.get("kind")) or _clean(node.get("file_type")) or "node"
        # one line per node: handle | kind | source | label: no bodies, so the
        # payload stays small and the model reasons over structure, not content
        lines.append(f"{h}\t{kind}\t{src}\t{_node_label(node)[:80]}")
    return "\n".join(lines), handle_of


def prompt_for(nodes: list[dict]) -> tuple[str, str, dict[str, str], str]:
    """Return the common prompt, handle map, and handle-table digest.

    Every provider receives the same prompt. The digest prevents an offline
    response from applying after its base graph changes.
    """
    table, handle_of = _prompt_nodes(nodes)
    user = ("Here are the extracted graph nodes as `handle<TAB>kind<TAB>"
            "source_file<TAB>label` lines:\n\n" + table +
            "\n\nAdd a small inferred layer as valid JSON under the schema.")
    digest = hashlib.sha256(table.encode("utf-8")).hexdigest()[:16]
    return _SYSTEM, user, handle_of, digest


def _balanced_object(t: str) -> dict | None:
    """The first parseable brace-balanced JSON object in ``t``. The scan is
    STRING-AWARE: a ``}`` inside a string literal (a label like
    ``"closes} early"``) never closes the object: and a failed candidate
    moves on to the next ``{`` instead of giving up."""
    start = t.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(t)):
            c = t[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(t[start:i + 1])
                        return obj if isinstance(obj, dict) else None
                    except ValueError:
                        break  # not JSON after all: try the next '{'
        start = t.find("{", start + 1)
    return None


def _extract_json(text: str) -> dict | None:
    """Best-effort parse of the model's reply: the whole text, then each
    ```-fenced block, then the first balanced ``{...}`` object anywhere :
    a fence that turns out not to hold the JSON never blocks the fallback."""
    if not text:
        return None
    t = text.strip()
    candidates = [t]
    candidates += [m.strip() for m in
                   re.findall(r"```(?:json)?\s*(.+?)```", t, re.DOTALL)]
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
    return _balanced_object(t)


def _rel_token(text) -> str:
    """An edge relation must be a single parseable token for the store grammar:
    strip link metacharacters, collapse whitespace to '_', lowercase."""
    s = re.sub(r"\s+", "_", _linksafe(text)).lower().strip("_")
    return s or "relates_to"


# -- the enrichment pass ----------------------------------------------------

def _validate_response_structure(proposal: dict) -> None:
    """Validate strict response types while preserving optional defaults."""
    for collection, required, optional in (
            ("concepts", ("label",), ("summary",)),
            ("edges", ("source", "target"), ("relation",))):
        items = proposal.get(collection, [])
        if not isinstance(items, list):
            raise TypeError(f"The response field {collection} must be a list.")
        for index, item in enumerate(items):
            field = f"{collection}[{index}]"
            if not isinstance(item, dict):
                raise TypeError(f"The response field {field} must be an object.")
            for name in required + tuple(name for name in optional if name in item):
                if not isinstance(item.get(name), str):
                    raise TypeError(f"The response field {field}.{name} must be a string.")
            if collection == "concepts":
                members = item.get("members", [])
                if (not isinstance(members, list)
                        or any(not isinstance(member, str) for member in members)):
                    raise TypeError(
                        f"The response field {field}.members must be a list of strings.")


def _apply(graph: dict, proposal: dict, handle_of: dict[str, str]) -> int:
    """Merge a validated proposal into ``graph`` IN PLACE, appending only
    ``inferred`` nodes/edges and never touching existing ones. Returns the
    number of graph elements added. Every value from the model is treated as
    untrusted (sanitised, bounded, id-checked)."""
    existing_ids = {n["id"] for n in graph["nodes"] if isinstance(n, dict)
                    and "id" in n}
    existing_edges = {(e.get("source"), e.get("target"), e.get("relation"))
                      for e in graph["links"]}
    # labels already in the graph: a concept named EXACTLY like a base node
    # would win the first-loaded wikilink index and its member links would
    # resolve to ITSELF (self-links drop): the edge silently vanishes
    taken_labels = {str(n.get("label", "")).strip().lower()
                    for n in graph["nodes"] if isinstance(n, dict)}
    added = 0
    concept_slugs: set[str] = set()

    concepts = proposal.get("concepts")
    concepts = concepts if isinstance(concepts, list) else []
    for c in concepts[:MAX_CONCEPTS]:
        if not isinstance(c, dict):
            continue
        label = _linksafe(c.get("label"))
        if not label:
            continue
        if label.lower() in taken_labels:
            label += " (concept)"
            k = 1
            while label.lower() in taken_labels:
                k += 1
                label = f"{label.rsplit(' (', 1)[0]} (concept {k})"
        taken_labels.add(label.lower())
        slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "concept"
        cid = f"enrich:concept:{slug}"
        base_cid, k = cid, 1
        while cid in existing_ids or cid in concept_slugs:
            k += 1
            cid = f"{base_cid}-{k}"
        concept_slugs.add(cid)
        summary = _clean(c.get("summary"))[:_SUMMARY_CAP]
        graph["nodes"].append({
            "id": cid, "label": label, "kind": "concept", "lang": "concept",
            "file_type": "concept", "body": summary,
            "provenance": "inferred", "confidence": "inferred",
            "_origin": "llm",
        })
        existing_ids.add(cid)
        added += 1
        # concept -> member edges (the cross-file connectivity, via the hub);
        # capped so a verbose/hostile reply can't flood edges through members
        members = c.get("members")
        members = members if isinstance(members, list) else []
        for h in members[:MAX_MEMBERS]:
            tgt = handle_of.get(str(h))
            if not tgt or tgt == cid:
                continue
            key = (cid, tgt, "relates_to")
            if key in existing_edges:
                continue
            existing_edges.add(key)
            graph["links"].append({"source": cid, "target": tgt,
                                   "relation": "relates_to",
                                   "confidence": "inferred"})
            added += 1

    edges = proposal.get("edges")
    edges = edges if isinstance(edges, list) else []
    for e in edges[:MAX_EDGES]:
        if not isinstance(e, dict):
            continue
        src = handle_of.get(str(e.get("source")))
        tgt = handle_of.get(str(e.get("target")))
        if not src or not tgt or src == tgt:
            continue  # inferred direct edges connect two REAL base nodes only
        relation = _rel_token(e.get("relation"))
        key = (src, tgt, relation)
        if key in existing_edges:
            continue
        existing_edges.add(key)
        graph["links"].append({"source": src, "target": tgt,
                               "relation": relation, "confidence": "inferred"})
        added += 1
    return added


def _copy_graph(graph: dict) -> dict:
    """A defensive copy every pass mutates instead of the caller's graph."""
    return {**graph,
            "nodes": [dict(n) for n in graph.get("nodes", []) if isinstance(n, dict)],
            "links": [dict(e) for e in graph.get("links", []) if isinstance(e, dict)]}


def _ingest_reply(out: dict, reply, handle_of: dict[str, str],
                  base_nodes: int) -> dict:
    """The shared tail every provider's reply funnels through: parse, validate,
    merge: or leave ``out`` unchanged. One waist, so the HTTP model, the agent
    CLI, and the offline handshake are hardened identically."""
    proposal = _extract_json(reply if isinstance(reply, str) else "")
    if not proposal:
        _warn("model returned no parseable JSON; base graph unchanged")
        return out
    n = _apply(out, proposal, handle_of)
    if n:
        print(f"muninn enrich: added {n} inferred element(s) on top of "
              f"{base_nodes} extracted node(s)", file=sys.stderr)
    return out


def enrich(graph: dict, chat=None) -> dict:
    """Append validated inferred material to a copy of ``graph``.

The optional ``chat`` callable receives the common system and user prompts.
Without an injected callable, provider resolution uses the configured command
or URL. Missing providers, transport errors, and invalid responses return the
unchanged copied graph. A nonlocal URL without consent raises
``PrivacyError``.
    """
    out = _copy_graph(graph)
    if not out["nodes"]:
        return out  # nothing to enrich

    if chat is None:
        chat = resolve_chat()  # a non-local URL may raise PrivacyError here
        if chat is None:
            _warn("no provider configured; leaving the deterministic base "
                  "unchanged. Plug a model in three ways: (1) the agent "
                  "handshake: `muninn enrich <path> --request`, fulfil it, "
                  "then `--apply <response.json>`; (2) MUNINN_ENRICH_CMD="
                  "'claude -p' (any prompt-on-stdin CLI); (3) MUNINN_ENRICH_URL "
                  "at a local /v1/chat/completions server")
            return out

    system, user, handle_of, _digest = prompt_for(out["nodes"])
    try:
        reply = chat(system, user)
    except _TRANSPORT as e:
        _warn(f"enrichment call failed ({e}); base graph unchanged")
        return out
    except Exception as e:  # a custom chat callable may raise anything: stay soft
        _warn(f"enrichment call errored ({e}); base graph unchanged")
        return out
    return _ingest_reply(out, reply, handle_of, len(graph.get("nodes", [])))


# -- the offline agent handshake ---------------------------------------------
#
# The zero-config default when a coding agent is ALREADY sitting in the repo:
# no server, no subprocess, nothing egresses. `render_request` emits the task
# (the same contract + handle table every provider sees, plus a digest of the
# table); the agent writes the JSON; `apply_response` ingests it through the
# same hardened waist. Mirrors the blessed emit/ingest pattern the memory side
# already uses (prime/pack emit TO the agent, observe ingests back).

RESPONSE_BASENAME = "response.json"  # the handshake's own artifact, by contract


def prune_handshake_files(graph: dict, extra_paths=()) -> dict:
    """Drop nodes (and their edges) extracted FROM the handshake's own
    artifacts: any file named ``response.json``, plus explicit
    ``extra_paths`` (source_file-relative: the caller passes the --apply
    file when it lives inside the scanned tree). Without this the flagship
    flow SELF-DEFEATS: the agent writes response.json into the scanned tree,
    --apply re-extracts it as new json nodes, the handle table shifts, and
    the digest refuses forever."""
    extras = {str(p).replace(os.sep, "/") for p in extra_paths}

    def doomed(n) -> bool:
        src = str(n.get("source_file", "")).replace(os.sep, "/")
        return bool(src) and (os.path.basename(src) == RESPONSE_BASENAME
                              or src in extras)

    dropped = {n.get("id") for n in graph.get("nodes", [])
               if isinstance(n, dict) and doomed(n)}
    if not dropped:
        return graph
    return {**graph,
            "nodes": [n for n in graph.get("nodes", [])
                      if not (isinstance(n, dict) and n.get("id") in dropped)],
            "links": [e for e in graph.get("links", [])
                      if isinstance(e, dict)
                      and e.get("source") not in dropped
                      and e.get("target") not in dropped]}

def render_request(graph: dict, path: str | None = None,
                   into: str | None = None, root: str | None = None) -> str:
    """The enrichment request as a self-contained markdown document for the
    agent reading it: how to respond, the contract, the handle table, and the
    digest to echo so ``apply_response`` can verify the base is unchanged.
    When the caller knows them, ``path``/``into``/``root`` make the printed
    apply command PASTE-READY: the fulfilling agent should never have to
    reconstruct flags (and MUST reuse the same path/root, or the re-extracted
    handles won't match the digest)."""
    nodes = [n for n in graph.get("nodes", []) if isinstance(n, dict)]
    system, user, _handle_of, digest = prompt_for(nodes)
    q = shlex.quote  # paths may carry spaces: the command must paste whole
    rootflag = f"--root {q(root)} " if root and root != "." else ""
    cmd = (f"    muninn {rootflag}enrich "
           f"{q(path) if path else '<same-path>'} "
           f"--apply response.json --into "
           f"{q(into or root) if (into or root) else '<bundle>'}")
    lines = [
        "# muninn enrichment request",
        "",
        "You (the agent reading this) are the model for this pass. Produce the",
        "STRICT JSON object described by the contract below, ADD a top-level",
        f'`"digest": "{digest}"` field (echoed verbatim; apply REFUSES a',
        "response without it or with a stale one), write it to `response.json`,",
        "and hand it back with exactly:",
        "",
        cmd,
        "",
        "(`--apply -` reads the JSON from stdin, so a pipe works too. Use the",
        "same path and --root as this request, or the handles won't match.",
        "A file named response.json is ignored by enrich's extraction, so",
        "writing it inside the scanned tree is safe.)",
    ]
    if len(nodes) > _MAX_NODES_PROMPT:
        lines += [
            "",
            f"NOTE: showing the first {_MAX_NODES_PROMPT} of {len(nodes)} "
            "nodes (deterministic head). Concepts/edges may only reference "
            "the handles listed below.",
        ]
    lines += [
        "",
        "## Contract",
        "",
        system,
        "",
        "Response shape: note the digest field, which the contract's schema",
        "line does not show but this handshake REQUIRES:",
        "",
        f'    {{"digest": "{digest}", "concepts": [...], "edges": [...]}}',
        "",
        "Each node row names its source_file: read those files for evidence",
        "before writing summaries. Do not infer claims from labels alone.",
        "Don't reuse an existing node's label verbatim as a concept label.",
        "",
        "## Nodes",
        "",
        user,
        "",
    ]
    return "\n".join(lines)


def apply_response(graph: dict, reply: str, *, strict: bool = False) -> dict:
    """Ingest an agent's handshake reply over the freshly re-extracted base
    ``graph``. Handles are re-derived deterministically (same tree, same
    handles). The digest is required because a reply without it did not follow the
    contract, and a mismatched one means the source changed since
    ``--request``, where stale handles could bind to the wrong nodes. Both
    are refused. Fail-soft throughout: refusal or unparseable JSON returns
    the base unchanged. Explicit CLI application uses strict mode to refuse
    output writes when response validation fails."""
    def refuse(message: str) -> None:
        if strict:
            raise ValueError(message)
        _warn(message)

    out = _copy_graph(graph)
    if not out["nodes"] and not strict:
        return out
    _system, _user, handle_of, digest = prompt_for(out["nodes"])
    proposal = _extract_json(reply if isinstance(reply, str) else "")
    if not proposal:
        refuse("The response contains no parseable JSON. No output was written.")
        return out
    got = proposal.get("digest")
    if not (isinstance(got, str) and got.strip()):
        refuse('response carries no "digest" field: the request mandates '
              "echoing it, so this reply did not follow the contract; refusing "
              "to apply. Re-read the request (--request) and include the "
              "digest in the JSON.")
        return out
    if got.strip() != digest:
        refuse("digest mismatch: the source changed since --request was "
              "issued (or --apply ran with a different <path>/--root than "
              "the request), so handles may point at different nodes; "
              "refusing to apply. Re-run --request with the same flags and "
              "redo the response.")
        return out
    if strict:
        try:
            _validate_response_structure(proposal)
        except TypeError as error:
            raise ValueError(str(error)) from error
    n = _apply(out, proposal, handle_of)
    if n:
        print(f"muninn enrich: added {n} inferred element(s) on top of "
              f"{len(graph.get('nodes', []))} extracted node(s)", file=sys.stderr)
    return out


def count_inferred(graph: dict) -> tuple[int, int]:
    """``(inferred_nodes, inferred_edges)`` in a graph: what enrichment added,
    for reporting. Nodes counted by ``provenance == 'inferred'``, edges by
    ``confidence == 'inferred'``."""
    nodes = sum(1 for n in graph.get("nodes", [])
                if isinstance(n, dict) and n.get("provenance") == "inferred")
    edges = sum(1 for e in graph.get("links", [])
                if isinstance(e, dict) and e.get("confidence") == "inferred")
    return nodes, edges
