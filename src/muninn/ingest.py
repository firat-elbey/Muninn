"""Import a derived node-link graph as OKF notes.

The Graphify adapter imports concept, document, rationale, and optional bounded
code nodes. A caller can provide the source root to add a short document or
code excerpt. Missing or unsafe source content leaves the ordinary stub body.

Relationships become typed links under ``# Connections``. Extracted,
inferred, and ambiguous confidence values retain explicit provenance.
Untrusted labels are sanitized before they enter paths or links. An import
skips a note when its rendered bytes already match the file on disk.
"""

from __future__ import annotations

import hashlib
import json
import os
import re

from . import store
from .store import Bundle, render_note

IMPORT_TYPES = {"concept", "document", "paper", "rationale"}
CODE_CAP = 500  # max code/file-level stub notes per import
SLUG_MAX = 120  # filename slugs from external ids are clamped to this
SNIPPET_DOC = 400  # ~chars of section text pulled into a document note body
SNIPPET_CAP = 600  # hard ceiling on pulled source content per note body


def _slug(text: str) -> str:
    """Filename slug for an external (untrusted, unbounded) label. Slugs
    longer than SLUG_MAX are clamped and given an 8-char sha suffix, so a
    385-char heading (a URL embedded in a doc title) can never exceed the
    filesystem's 255-byte name limit: and two long labels sharing a
    prefix still get distinct paths."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(s) > SLUG_MAX:
        digest = hashlib.sha256(s.encode("utf-8")).hexdigest()[:8]
        s = s[:SLUG_MAX].rstrip("-") + "-" + digest
    return s or "unnamed"


def _clean(text) -> str:
    """External graph strings are untrusted: strip control characters so
    they cannot inject frontmatter keys or mangle note bodies."""
    if text is None:
        return ""
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(text)).strip()


def _linksafe(text) -> str:
    """Identifiers written into wikilink/typed-line syntax must not carry
    link metacharacters: an untrusted label like ``Secret note|mask`` or
    ``X]] [[Y`` could otherwise forge edges to arbitrary bundle notes."""
    return re.sub(r"\s+", " ", re.sub(r"[\[\]|#()]", " ", _clean(text))).strip()


# -- source snippets: real content for imported bodies (fail-soft) ----------
#
# graphify knows each node's source file (``source_file``) and heading/
# symbol (``label``); when the caller passes the folder the graph was
# built from, we pull a short excerpt into the note body so packs can
# ANSWER instead of only point. Plain text heuristics only: no
# tree-sitter, no markdown parser: and every miss falls back to the
# stub body the importer always wrote.

_HEADING_LINE = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")
# Only plausibly-prose files feed the document path: dotfiles (.env,
# .npmrc, …) and config-ish types must never be excerpted into notes
# that later feed LLM context packs (secret-exposure guard).
DOC_EXTS = (".md", ".markdown", ".rst", ".txt")


def _norm_heading(text) -> str:
    return " ".join(str(text).split()).casefold()


def _clip(text: str, limit: int) -> str:
    """Clean truncation: cut at a word boundary under ``limit`` and mark
    the cut: never a mid-word chop (unless the window holds no boundary
    at all, e.g. one long URL)."""
    text = text.strip()
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(None, 1)[0].rstrip(" ,;:.-")
    return (cut or text[:limit]) + " …"


def _heading_start(lines: list[str], label: str, location) -> int | None:
    """Index of the first line under the node's heading. graphify's
    ``source_location`` ("L<n>") is trusted when line n really is a
    heading WITH the node's text (it disambiguates repeated heading
    texts; a stale location pointing at some other heading is ignored);
    otherwise the first heading whose text matches the label wins."""
    want = _norm_heading(label)
    loc = str(location or "")
    if loc.startswith("L") and loc[1:].isdigit():
        i = int(loc[1:]) - 1
        if 0 <= i < len(lines):
            m = _HEADING_LINE.match(lines[i])
            if m and _norm_heading(m.group(1)) == want:
                return i + 1
    for i, ln in enumerate(lines):
        m = _HEADING_LINE.match(ln)
        if m and _norm_heading(m.group(1)) == want:
            return i + 1
    return None


def _doc_snippet(lines: list[str], label: str, location, source_file: str,
                 limit: int) -> str:
    """First paragraph(s) under the node's heading (~``limit`` chars).
    A file-level node (label = the file's own name) takes the top of the
    file instead, skipping frontmatter and title lines. Tables and
    fenced code are not prose and are left out. An EMPTY section yields
    '' (the stub), never the next section's content: a wrong answer
    under a confident title is worse than a pointer."""
    file_level = False
    start = _heading_start(lines, label, location)
    if start is None:
        if _norm_heading(label) != _norm_heading(
                os.path.basename(source_file)):
            return ""
        file_level = True
        start = 0
        if lines and lines[0].strip() == "---":  # skip YAML frontmatter
            for j in range(1, len(lines)):
                if lines[j].strip() == "---":
                    start = j + 1
                    break
    paras: list[str] = []
    cur: list[str] = []
    total = 0
    for ln in lines[start:]:
        s = ln.strip()
        if s.startswith("```"):
            break  # fenced code: stop, the prose above is the snippet
        if _HEADING_LINE.match(ln) or s == "---":
            if file_level and not (cur or paras):
                continue  # the file's own title / rule before content
            break  # any heading ends a section snippet: even before
            #        content: an empty section must stay a stub
        if not s:
            if cur:
                paras.append(" ".join(cur))
                cur = []
            if total >= limit:
                break
            continue
        if s.startswith("|"):
            continue  # markdown tables read as noise in a note body
        cur.append(_clean(s))  # excerpts are external text: same
        total += len(s) + 1    # control-char discipline as rationale
        if total >= limit * 2:
            break
    if cur:
        paras.append(" ".join(cur))
    return _clip("\n\n".join(paras), limit)


def _docstring_head(lines: list[str], i: int, limit: int) -> str:
    """First lines of the docstring starting at/after line ``i`` (blank
    and comment lines skipped), quotes stripped. '' when none opens."""
    while i < len(lines) and (not lines[i].strip()
                              or lines[i].lstrip().startswith("#")):
        i += 1
    if i >= len(lines):
        return ""
    s = lines[i].strip()
    q = next((m for m in ('"""', "'''") if m in s[:4]), None)
    if q is None:
        return ""
    out: list[str] = []
    for ln in lines[i:i + 5]:  # the first lines are the summary
        t = ln.strip()
        if not out:
            t = t[t.index(q):]  # drop any r/b/f prefix before the quote
        closed = q in t if out else t.count(q) == 2
        out.append(_clean(t.replace(q, " ")))
        if closed:
            break
    return _clip(" ".join(x for x in out if x), limit)


def _code_snippet(lines: list[str], label: str, source_file: str,
                  limit: int) -> str:
    """The def/class line + the docstring's first lines, by plain line
    scan. A file-level node (label = the file's own name) takes the
    module docstring instead. '' when nothing matches (fail soft)."""
    name = str(label).strip()
    if _norm_heading(name) == _norm_heading(os.path.basename(source_file)):
        return _docstring_head(lines, 0, limit)
    # 'Store.write_note()' / '.__init__()' -> the last dotted segment
    name = name.lstrip(".").split("(")[0].split(".")[-1].strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or ""):
        return ""
    pat = re.compile(r"^\s*(?:async\s+def|def|class)\s+"
                     + re.escape(name) + r"\b")
    for i, ln in enumerate(lines):
        if pat.match(ln):
            j = i  # a wrapped signature: the docstring sits after the
            while (j < len(lines) and j - i < 8  # line that closes it
                   and not lines[j].split("#", 1)[0].rstrip().endswith(":")):
                j += 1
            doc = _docstring_head(lines, j + 1, limit)
            return _clip(_clean(ln) + ("\n" + doc if doc else ""), limit)
    return ""


def _source_snippet(source_root, node: dict, is_code: bool) -> str:
    """Excerpt from the node's source file, or ''. Paths in the graph
    are untrusted: only files really inside ``source_root`` are read :
    a graph.json must not be able to pull arbitrary files into notes."""
    src = node.get("source_file")
    if not source_root or not src:
        return ""
    base = os.path.basename(str(src))
    if base.startswith("."):
        return ""  # dotfiles (.env, …) are config/secrets, never content
    if not is_code and not base.lower().endswith(DOC_EXTS):
        return ""  # document excerpts come from prose file types only
    root = os.path.realpath(source_root)
    full = os.path.realpath(os.path.join(root, str(src)))
    if full != root and not full.startswith(root + os.sep):
        return ""
    try:
        with open(full, encoding="utf-8", errors="replace") as fh:
            text = fh.read(1 << 20)  # 1 MiB is plenty for a snippet
    except OSError:
        return ""
    lines = text.split("\n")
    label = _clean(node.get("label")) or _clean(node.get("id"))
    if is_code:
        return _code_snippet(lines, label, str(src), SNIPPET_CAP)
    return _doc_snippet(lines, label, node.get("source_location"),
                        str(src), SNIPPET_DOC)


def _connections(nid: str, nodes: dict, edges_by_src: dict,
                 targets: dict | None = None) -> list[str]:
    """Render a node's outgoing edges as typed-link lines (store grammar)."""
    rels = edges_by_src.get(nid, [])
    if not rels:
        return []
    # H2, not H1: these bodies get embedded under a pack's own H3 note
    # headings: an H1 inside them broke the pack's heading hierarchy
    lines = ["## Connections", ""]
    for e in rels[:30]:
        tgt = nodes.get(e.get("target"), {})
        tgt_label = _linksafe(tgt.get("label") or e.get("target")) or "?"
        target = (targets or {}).get(e.get("target"))
        reference = f"{target}|{tgt_label}" if target else tgt_label
        relation = _linksafe(e.get("relation")) or "related_to"
        # confidence must stay a single parseable word (store grammar)
        conf = re.sub(r"\s+", "-", _linksafe(e.get("confidence"))) or "inferred"
        lines.append(f"- {relation} [[{reference}]] ({conf.lower()})")
    return lines


def _carry_inferred(old_body: str, new_body: str,
                    refused: set[tuple[str, str]] | None = None) -> str:
    """Inferred typed-link lines that an earlier ENRICHMENT appended to this
    note survive a re-import. The deterministic importers know nothing about
    the model layer's additions, so re-rendering from a plain graph would
    silently destroy them: the opposite of the additive-never-rewriting
    promise. Carried lines keep their original order, appended under the new
    body's ``## Connections`` (created if the new body has none). Refused
    relation-target pairs do not survive an import ownership conflict."""
    new_lines = set(new_body.split("\n"))
    carried: list[str] = []
    for ln in old_body.split("\n"):
        m = store._TYPED_LINE.match(ln)
        if not m:
            continue
        if (m.group("conf") or "").strip().lower() != "inferred":
            continue
        identity = ((m.group("rel") or "").strip() or store.DEFAULT_RELATION,
                    Bundle._normalize_name(m.group("target")))
        if refused and identity in refused:
            continue
        if ln in new_lines or ln in carried:
            continue
        carried.append(ln)
    if not carried:
        return new_body
    if "# Connections" not in new_body:  # matches '## Connections' too
        return new_body.rstrip() + "\n\n## Connections\n\n" + "\n".join(carried)
    return new_body.rstrip() + "\n" + "\n".join(carried)


def write_notes(bundle: Bundle, nodes: dict, edges_by_src: dict,
                plan, *, qualify_links: bool = False) -> tuple[int, int]:
    """The ONE graph→notes writer both importers drive. ``plan(nid, node,
    label)`` returns ``(dir_prefix, meta, body_lines)`` for a node: or
    ``None`` to skip it entirely. Everything else is common and identical by
    construction: the slug + reserved-basename guard, the collision-free path
    claim, the curated-note ownership rule, the ``## Connections`` rendering,
    the inferred-line carry-over, the byte-identical hash-skip, and the
    counted-never-silent OSError soft-fail. Returns ``(written, skipped)``."""
    written = skipped = failed = 0
    failed_example = ""
    claimed: dict[str, str] = {}
    plans = {}
    for nid, node in nodes.items():
        label = _linksafe(node.get("label")) or _linksafe(str(nid)) or "unnamed"
        planned = plan(nid, node, label)
        if planned is None:
            continue
        dir_prefix, meta, body_lines = planned
        slug = _slug(label)
        if slug in ("index", "log"):  # OKF-reserved basenames never load back
            slug += "-node"
        base = f"{dir_prefix}/{slug}"
        rel, k = f"{base}.md", 1
        while claimed.get(rel, nid) != nid:  # distinct path per node on a clash
            k += 1
            rel = f"{base}-{k}.md"
        claimed[rel] = nid
        plans[nid] = (label, rel, meta, list(body_lines))

    # Allocate every path before deciding whether a label identifies one target.
    projected = dict(bundle.notes)
    protected = {}
    for nid, (_label, rel, meta, _body_lines) in plans.items():
        existing = projected.get(rel)
        if existing is None or existing.meta.get("provenance") in (
                "extracted", "inferred"):
            projected[rel] = store.Note(rel, meta, "")
        else:
            protected[nid] = rel
    accepted_edges: dict[str, list[dict]] = {}
    refused_edges: dict[str, list[dict]] = {}
    for source, edges in edges_by_src.items():
        for edge in edges:
            destination = (refused_edges if source in protected
                           or edge.get("target") in protected else accepted_edges)
            destination.setdefault(source, []).append(edge)
    refused_count = sum(len(edges) for edges in refused_edges.values())
    if refused_count:
        noun = "edge" if refused_count == 1 else "edges"
        print(f"The import skipped {refused_count} generated {noun} involving "
              "protected note paths.")
    identities: dict[str, set[str]] = {}
    for note in projected.values():
        for name in bundle._identity_names(note):
            identities.setdefault(name, set()).add(note.path)
    # Scoped maps must retain their targets when later imports reuse labels.
    targets = {
        nid: rel
        for nid, (label, rel, _meta, _body_lines) in plans.items()
        if nid not in protected and (
            qualify_links
            or identities.get(bundle._normalize_name(label)) != {rel}
            or (label in projected and label != rel)
            or ("/" in label and label + ".md" in projected
                and label + ".md" != rel))
    }

    for nid, (label, rel, meta, body_lines) in plans.items():
        existing = bundle.notes.get(rel)
        # ownership rule: only refresh notes a previous IMPORT wrote; any
        # human-authored note (curated, or no provenance at all) is sacred
        if existing and existing.meta.get("provenance") not in (
                "extracted", "inferred"):
            continue
        connections = _connections(nid, nodes, accepted_edges, targets)
        body_lines = body_lines + connections
        body = "\n".join(body_lines).strip() or label
        if existing is not None:  # enrichment's additions survive re-imports
            # Rewritten graph edges replace their legacy ambiguous label forms.
            replaced = set(_connections(nid, nodes, edges_by_src)) - set(connections)
            old_body = "\n".join(line for line in existing.body.split("\n")
                                 if line not in replaced)
            refused = {
                (_linksafe(edge.get("relation")) or store.DEFAULT_RELATION,
                 bundle._normalize_name(reference))
                for edge in refused_edges.get(nid, [])
                for reference in (plans[edge["target"]][0],
                                  protected[edge["target"]],
                                  protected[edge["target"]][:-3])
            }
            body = _carry_inferred(old_body, body, refused)
        # hash-skip: byte-identical re-imports never touch the file
        full = os.path.join(bundle.root, rel)
        if os.path.exists(full):
            try:
                with open(full, encoding="utf-8") as fh:
                    if fh.read() == render_note(dict(meta), body):
                        skipped += 1
                        continue
            except OSError:
                pass
        # one unwritable note (name too long, perms, disk full) must never
        # abort the import: skip it, keep going, report the damage at the end
        try:
            bundle.write_note(rel, meta, body)
        except OSError as e:
            failed += 1
            if not failed_example:
                failed_example = f"{rel}: {e}"
            continue
        written += 1
    if failed:
        print(f"warning: {failed} note(s) could not be written and were "
              f"skipped (e.g. {failed_example})")
    return written, skipped


def import_graphify(bundle: Bundle, graph_json_path: str,
                    subdir: str = "imported",
                    include_code: bool = False,
                    source_root: str | None = None,
                    code_cap: int | None = None) -> tuple[int, int]:
    """Import a Graphify JSON graph and report written and skipped notes.

Concept-level nodes are included by default. ``include_code`` adds at most
``code_cap`` code or file nodes. ``source_root`` can add bounded source
excerpts. Existing curated notes are never replaced, malformed nodes are
skipped with a diagnostic, and byte-identical generated notes count as
skipped. Explicit extracted or inferred provenance overrides legacy origin
metadata. Other explicit provenance values become inferred.
    """
    with open(graph_json_path, encoding="utf-8") as fh:
        g = json.load(fh)
    if not isinstance(g, dict):
        raise TypeError("graph JSON must be an object")
    for name in ("nodes", "links", "edges"):
        if name in g and not isinstance(g[name], list):
            raise TypeError(f"graph {name} must be a list")
    all_nodes = g.get("nodes", [])
    graph_edges = g.get("links") or g.get("edges") or []
    no_id = sum(1 for n in all_nodes if not isinstance(n, dict)
                or not isinstance(n.get("id"), (str, int)))
    if no_id:  # a malformed node must not abort the import (soft-fail)
        print(f"warning: skipped {no_id} node(s) with no usable id")
    nodes = {n["id"]: n for n in all_nodes if isinstance(n, dict)
             and isinstance(n.get("id"), (str, int))}
    edges_by_src: dict[str, list[dict]] = {}
    # graphify has TWO writers: the clustered path stores edges under
    # "links" (networkx node-link default); the raw --no-cluster path :
    # the exact mode `muninn build` invokes: stores them under "edges"
    # (graphify normalizes the same way, its #2212). And a graph written
    # from undirected storage can persist FLIPPED endpoints with the true
    # direction stashed in _src/_tgt (graphify #563/#2309): the markers
    # outrank the stored order.
    for e in graph_edges:
        if not isinstance(e, dict):
            continue
        src, tgt = e.get("_src", e.get("source", "")), e.get("_tgt")
        if src != e.get("source") or (tgt is not None
                                      and tgt != e.get("target")):
            e = dict(e, source=src,
                     target=tgt if tgt is not None else e.get("target"))
        edges_by_src.setdefault(src, []).append(e)

    cap = code_cap if code_cap is not None else CODE_CAP
    code_seen = cap_skipped = 0

    def plan(nid, node, label):
        """The graphify-specific decisions; everything common lives in
        write_notes. Counts code nodes in graph order BEFORE any common
        skip, so the capped set stays stable across re-imports."""
        nonlocal code_seen, cap_skipped
        ftype = node.get("file_type") or "concept"
        is_code = ftype not in IMPORT_TYPES
        if is_code:
            if not include_code:
                return None
            code_seen += 1
            if code_seen > cap:
                cap_skipped += 1
                return None
            # code structure comes from deterministic parsing -> extracted
            meta = {"type": _clean(ftype), "title": label,
                    "provenance": "extracted",
                    "tags": ["graphify-import", "code"]}
            body_lines = [_clean(node.get("rationale"))
                          or f"Code-level node `{label}` from the source graph.",
                          ""]
        else:
            # Legacy origin applies only when explicit provenance is absent.
            provenance = node.get(
                "provenance", "extracted" if node.get("_origin") == "ast"
                else "inferred")
            if provenance not in ("extracted", "inferred"):
                provenance = "inferred"
            meta = {"type": ftype, "title": label,
                    "provenance": provenance,
                    "tags": ["graphify-import"]}
            body_lines = []
            if node.get("rationale"):
                body_lines += [_clean(node["rationale"]), ""]
        snippet = _source_snippet(source_root, node, is_code)
        if snippet:  # real answer text, within the per-note content cap
            room = SNIPPET_CAP - sum(len(x) for x in body_lines)
            if room >= 80:
                body_lines += [_clip(snippet, room), ""]
        if node.get("source_file"):
            meta["resource"] = _clean(node["source_file"])
        return (f"{subdir}/code" if is_code else subdir), meta, body_lines

    written, skipped = write_notes(bundle, nodes, edges_by_src, plan)
    if cap_skipped:
        print(f"include-code: cap of {cap} stub notes reached; skipped "
              f"{cap_skipped} further code nodes (raise it with --max-code-notes)")
    return written, skipped
