"""Extract a deterministic structural graph from source and document files.

Specialized parsers cover Markdown, configuration formats, SQL, Python, and
JavaScript or TypeScript. A bounded tree-sitter walk covers other available
grammars. Every extracted node and edge receives explicit provenance and
weight 1.0. Optional enrichment can append inferred material but cannot modify
this base.

Tree-sitter dependencies are loaded only when extraction runs. Their absence
does not affect the standard-library core. Extraction sorts paths, assigns
stable identifiers, and uses no model, network request, clock, or random
source.

The scanner excludes hidden, generated, dependency, cache, and known
secret-bearing paths. It limits file size, nesting depth, and nodes per file.
Configuration values may still enter note bodies, so a bundle requires review
before publication.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re

from . import store
from .ingest import _clean

# The module applies two provenance markers. `extracted` maps to edge
# weight 1.0 (store.CONF_WEIGHT): a deterministic parse is as certain as it
# gets. The optional model layer can add `inferred` provenance at weight 0.5.
PROVENANCE = "extracted"
CONFIDENCE = "extracted"

BODY_CAP = 4000  # chars of content kept per node body (section prose can be big)
# Nesting cap, used two ways: (1) a PRE-PARSE guard: a raw-byte depth estimate
# over this is skipped before parsing, because the native tree-sitter parsers
# STACK-OVERFLOW (segfault, uncatchable by Python) on pathologically deep input
# (YAML indentation crashes ~300 levels deep); (2) a traversal recursion cap.
# 200 is far above any real config/code (which nest <30 deep) yet safely below
# the native crash threshold.
MAX_DEPTH = 200
# Per-file node cap (field-tested on redis: 447 JSON test fixtures became
# 260k+ config-key notes, 94% stubs). Real code/docs rarely define >200
# things per file; a data fixture easily does: and a fixture's keys are
# data, not knowledge. The cap truncates that FILE's contribution
# (module node + first N, edges pruned consistently); other files are
# untouched.
MAX_FILE_NODES = 200

# File extension -> tree-sitter grammar name (get_language). Lower-cased ext.
# COMPREHENSIVE: a broad ext -> grammar map over the language pack's 165+
# grammars, so pointing the extractor at any project produces a graph. A
# grammar the pack lacks is skipped gracefully (fail-soft at _parser).
# Grammar NAMES are the pack's own (e.g. `csharp`, not `c_sharp`; `.jsx`/`.js`
# use the `javascript` grammar, which parses JSX; `.tsx` uses `tsx`).
EXT_LANG = {
    # -- hand-written rich extractors (markdown/config/sql/python) ----------
    ".md": "markdown", ".markdown": "markdown", ".mdown": "markdown",
    ".mkd": "markdown", ".mdx": "markdown",
    ".json": "json", ".json5": "json", ".jsonc": "json",
    ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml",
    ".sql": "sql",
    ".py": "python", ".pyi": "python",
    # -- javascript / typescript family (rich explicit extractor) ----------
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "javascript",              # the JS grammar parses JSX
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".tsx": "tsx",
    # -- everything below uses the GENERIC fallback extractor --------------
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin", ".kts": "kotlin",
    ".scala": "scala", ".sc": "scala",
    ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp",
    ".hh": "cpp", ".hxx": "cpp", ".c++": "cpp", ".h++": "cpp",
    ".cs": "csharp",
    ".rb": "ruby", ".rake": "ruby", ".gemspec": "ruby",
    ".php": "php", ".php5": "php", ".phtml": "php",
    ".swift": "swift",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".ksh": "bash",
    ".lua": "lua",
    ".r": "r", ".rmd": "r",
    ".html": "html", ".htm": "html", ".xhtml": "html",
    ".css": "css",
    ".scss": "scss",
    ".vue": "vue",
    ".svelte": "svelte",
    ".xml": "xml", ".xsd": "xml", ".xsl": "xml", ".svg": "xml",
    ".rst": "rst",
    ".tex": "latex", ".latex": "latex", ".sty": "latex",
    ".hcl": "hcl", ".tf": "terraform", ".tfvars": "terraform",
    ".proto": "proto",
    ".graphql": "graphql", ".gql": "graphql",
    ".ex": "elixir", ".exs": "elixir",
    ".erl": "erlang", ".hrl": "erlang",
    ".hs": "haskell",
    ".ml": "ocaml", ".mli": "ocaml",
    ".clj": "clojure", ".cljs": "clojure", ".cljc": "clojure", ".edn": "clojure",
    ".jl": "julia",
    ".dart": "dart",
    ".zig": "zig",
    ".elm": "elm",
    ".ada": "ada", ".adb": "ada", ".ads": "ada",
    ".pl": "perl", ".pm": "perl",
    ".ps1": "powershell", ".psm1": "powershell",
    ".groovy": "groovy", ".gradle": "groovy",
    ".sol": "solidity",
    ".nix": "nix",
    ".cmake": "cmake",
    ".m": "objc",
    ".mm": "objc",
    ".vim": "vim",
    ".dockerfile": "dockerfile",
    ".ini": "ini", ".cfg": "ini",
    ".prisma": "prisma",
    ".vy": "python",                    # vyper reads closely as python-ish
    ".f": "fortran", ".f90": "fortran", ".f95": "fortran",
    ".cu": "cuda", ".cuh": "cuda",
    ".rkt": "racket",
    ".scm": "scheme", ".ss": "scheme",
    ".nim": "nim",
    ".v": "verilog", ".sv": "systemverilog",
    ".vhd": "vhdl", ".vhdl": "vhdl",
    ".pas": "pascal",
    ".d": "d",
    ".cr": "crystal",
    ".fs": "fsharp", ".fsx": "fsharp",
    ".hx": "haxe",
    ".gd": "gdscript",
    ".tcl": "tcl",
}

# Bare filenames (no useful extension) -> grammar. Matched case-insensitively
# on the whole basename, tried before the extension map.
NAME_LANG = {
    "dockerfile": "dockerfile", "containerfile": "dockerfile",
    "makefile": "make", "gnumakefile": "make",
    "cmakelists.txt": "cmake",
    "gemfile": "ruby", "rakefile": "ruby", "podfile": "ruby",
    "go.mod": "gomod", "go.sum": "gosum",
}

# node kind -> graphify file_type, so a `--json` graph round-trips through
# ingest.import_graphify sanely (concept/document import by default; the code
# kinds import under `--include-code`, as graphify's own code nodes do).
_FILE_TYPE = {
    "heading": "document", "key": "concept", "table": "concept",
    "column": "concept", "module": "module", "class": "class",
    "function": "function", "method": "function", "import": "import",
    # explicit JS/TS + generic-fallback kinds
    "component": "function", "constructor": "function", "export": "concept",
    "struct": "class", "interface": "class", "enum": "class",
    "trait": "class", "impl": "class", "type": "class", "namespace": "module",
}

# directories never worth walking (mirrors store's walk + build scratch, plus
# the JS/build/vendor scratch a broad multi-language sweep must skip)
_SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__",
              store.SIDECAR_DIR, "graphify-out", ".venv", "venv",
              "dist", "build", ".mypy_cache", ".pytest_cache",
              ".next", "out", "vendor", "coverage", "target",
              ".gradle", ".idea", ".tox", "bower_components"}

# whole filenames never worth reading: dependency lockfiles (huge, machine-
# generated, near-zero structural value): they carry a real extension
# (.json/.yaml) so they would otherwise be swept.
_SKIP_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml",
               "npm-shrinkwrap.json", "composer.lock", "poetry.lock",
               "cargo.lock", "gemfile.lock", "pipfile.lock"}

MAX_FILE_BYTES = 400 * 1024  # skip files larger than this (minified bundles,
#                              generated blobs): bounds parse cost + node count


# -- optional dependency (lazy, fail-soft: mirrors embed.py) ---------------

_PARSERS: dict = {}  # grammar name -> tree_sitter.Parser (built once, cached)


def available() -> bool:
    """True when the tree-sitter extractor can run: the optional deps import.
    Cheap and side-effect-free; never raises. Callers fail soft on False."""
    try:
        __import__("tree_sitter")
        __import__("tree_sitter_language_pack")
        return True
    except Exception:
        return False


def parser_issues() -> list[str]:
    """Verify representative grammars with fixed bytes, without scanning user files."""
    if not available():
        return ["The tree-sitter packages cannot be imported."]
    samples = {
        "python": b"def greet():\n    return 1\n",
        "javascript": b"function greet() { return 1; }",
        "typescript": b"function greet(): number { return 1; }",
        "rust": b"fn greet() -> i32 { 1 }",
        "go": b"package main\nfunc greet() int { return 1 }\n",
        "csharp": b"class Greeting { int Value() { return 1; } }",
        "yaml": b"greeting: hello\n",
        "embeddedtemplate": b"<p><%= greeting %></p>",
    }
    issues = []
    for language, sample in samples.items():
        try:
            tree = _parser(language).parse(sample)
            if tree.root_node.has_error or tree.root_node.end_byte != len(sample):
                issues.append(language + ": the syntax sample did not parse.")
        except Exception:  # noqa: BLE001 - Report any grammar compatibility failure through diagnostics.
            issues.append(language + ": the grammar could not load or parse.")
    return issues


def languages() -> list[str]:
    """Every grammar name the ext/name maps resolve to, sorted. (Whether a
    given grammar actually LOADS depends on the installed language pack; an
    absent one is skipped fail-soft at parse time.)"""
    return sorted(set(EXT_LANG.values()) | set(NAME_LANG.values()))


def lang_for(path: str) -> str | None:
    """Grammar name for a path, or None (unsupported). A bare-name match
    (``Dockerfile``, ``Makefile``, ``go.mod``) wins over the extension map;
    otherwise the lower-cased extension is looked up."""
    base = os.path.basename(path).lower()
    if base in NAME_LANG:
        return NAME_LANG[base]
    return EXT_LANG.get(os.path.splitext(base)[1])


def _parser(lang: str):
    """A cached ``tree_sitter.Parser`` for ``lang`` via the portable
    ``get_language`` + ``Parser`` path (the language_pack ``get_parser`` returns
    a vendored core with a different, non-portable API). Raises on a missing
    grammar; callers treat that as a fail-soft skip."""
    if lang not in _PARSERS:
        import tree_sitter
        from tree_sitter_language_pack import get_language
        _PARSERS[lang] = tree_sitter.Parser(get_language(lang))
    return _PARSERS[lang]


# -- graph accumulator ------------------------------------------------------

def _new_graph() -> dict:
    """An empty graph in graphify's networkx node-link shape."""
    return {"directed": True, "multigraph": False, "graph": {},
            "nodes": [], "links": []}


class _Acc:
    """Collect nodes and edges for one source file in a shared graph. Node
    ids are file-local counters (``<lang>:<src>#<n>``) so they are unique and
    deterministic; edges reference node ids (``contains``) or a raw
    name/path string (``references`` to something that may not be a node)."""

    def __init__(self, graph: dict, lang: str, src: str):
        self.g = graph
        self.lang = lang
        self.src = src
        self._n = 0

    def node(self, label: str, kind: str, body: str = "",
             line: int | None = None) -> str:
        self._n += 1
        nid = f"{self.lang}:{self.src}#{self._n}"
        # label hygiene (field-tested on Flask): head-line fallbacks carry
        # the opening punctuation of a multi-line body ("a {", "foo (") :
        # block-opener junk on a label is never content
        label = (label or "").strip()
        while label and label[-1] in "{(":
            label = label[:-1].rstrip()
        rec = {
            "id": nid,
            "label": label or "(unnamed)",
            "kind": kind,
            "lang": self.lang,
            "file_type": _FILE_TYPE.get(kind, "concept"),
            "body": _body(body),
            "source_file": self.src,
            "provenance": PROVENANCE,
            "confidence": CONFIDENCE,
            "_origin": "ast",  # deterministic parse: import_graphify reads this
        }
        if line is not None:
            rec["source_location"] = f"L{line}"
        self.g["nodes"].append(rec)
        return nid

    def edge(self, source: str, target: str, relation: str) -> None:
        if not target:
            return
        self.g["links"].append({"source": source, "target": target,
                                "relation": relation, "confidence": CONFIDENCE})


def _body(text) -> str:
    """Sanitize a node body: drop control chars but KEEP newlines/tabs (a body
    is free-form markdown, unlike a frontmatter value), then clip to BODY_CAP
    at a clean boundary. Empty stays empty."""
    if not text:
        return ""
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+", " ", str(text)).strip()
    if len(s) <= BODY_CAP:
        return s
    cut = s[:BODY_CAP].rsplit(None, 1)[0].rstrip(" ,;:.-")
    return (cut or s[:BODY_CAP]) + " …"


def _txt(node) -> str:
    """Decoded, stripped text of a tree-sitter node."""
    return node.text.decode("utf-8", "replace").strip()


def _unquote(s: str) -> str:
    """Strip one layer of matching quotes from a scalar/key literal."""
    s = s.strip()
    if len(s) >= 2 and s[0] in "\"'" and s[-1] == s[0]:
        return s[1:-1]
    return s


# -- markdown ---------------------------------------------------------------
#
# The block grammar can put multiple Setext headings inside one section.
# Heading levels, not section nodes, therefore define the exported hierarchy.
# Inline references use the same Markdown and wikilink parser as stored notes.

def _md_heading_text(hnode) -> str:
    for c in hnode.children:
        if c.type == "inline":
            return _txt(c)
        if c.type == "paragraph":
            return " ".join(_txt(c).split())
    first = _txt(hnode).splitlines()[0] if _txt(hnode) else ""
    return re.sub(r"^#+\s*|\s*#+\s*$", "", first).strip()


def _md_section_body(children) -> str:
    """Join one heading's content blocks without duplicating child sections."""
    parts = []
    for c in children:
        if c.type in ("atx_heading", "setext_heading", "section"):
            continue
        t = c.text.decode("utf-8", "replace").strip()
        if t:
            parts.append(t)
    return "\n\n".join(parts)


def _md_links(text: str) -> list[str]:
    """Reference targets in a section's prose: markdown link paths (external
    URLs skipped) and wikilink names, in order, de-duplicated. Reuses store's
    own link patterns: one link grammar for the whole system."""
    out: list[str] = []
    seen: set[str] = set()
    for link in store._all_markdown_links(text):
        raw = link.destination
        if "://" in raw or raw.startswith("#"):
            continue
        if raw not in seen:
            seen.add(raw)
            out.append(raw)
    for m in store._WIKILINK.finditer(text):
        # collapse internal whitespace: a `[[Backup target]]` that wraps
        # across a source line otherwise carries a newline in its name
        name = re.sub(r"\s+", " ", m.group(1)).strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _md(acc: _Acc, root) -> None:
    def blocks(node):
        for child in node.children:
            if child.type == "section":
                yield from blocks(child)
            else:
                yield child

    sections = []
    for block in blocks(root):
        if block.type in ("atx_heading", "setext_heading"):
            sections.append((block, []))
        elif sections:
            sections[-1][1].append(block)
    parents = []
    for heading, children in sections:
        title = _md_heading_text(heading)
        raw = _txt(heading).strip()
        if heading.type == "atx_heading":
            level = len(raw) - len(raw.lstrip("#"))
        else:
            level = 1 if raw.splitlines()[-1].lstrip().startswith("=") else 2
        while parents and parents[-1][0] >= level:
            parents.pop()
        body = _md_section_body(children)
        nid = acc.node(title, "heading", body, line=heading.start_point[0] + 1)
        if parents:
            acc.edge(parents[-1][1], nid, "contains")
        parents.append((level, nid))
        for tgt in _md_links(body):
            acc.edge(nid, tgt, "references")


# -- rst (Sphinx docs: where most Python repos keep their knowledge) --------

_RST_SKIP = ("comment", "target", "substitution_definition", "footnote",
             "citation")
# :doc:`target` and :doc:`Title <target>`: the cross-file reference role
_RST_DOC_REF = re.compile(r":doc:`(?:[^`<>]*<)?([^`<>]+?)>?`")
# any :role:`...`: unwrapped to its display text for note bodies, so the
# model reads prose, not Sphinx plumbing (field-tested on Flask)
_RST_ROLE = re.compile(r":[A-Za-z0-9_.:+-]+:`([^`]+?)`")


def _rst_unrole(text: str) -> str:
    """``:func:`~flask.cli.with_appcontext``` → ``with_appcontext``;
    ``:doc:`CLI Guide <cli>``` → ``CLI Guide``: what a rendered page
    shows. Applied to note bodies only; reference edges read the RAW
    text first."""
    def _sub(m) -> str:
        t = m.group(1).strip()
        if "<" in t:  # explicit title form: keep the title
            t = t.split("<", 1)[0].strip() or t
        if t.startswith("~"):  # show only the last dotted segment
            t = t[1:].rsplit(".", 1)[-1]
        return t
    return _RST_ROLE.sub(_sub, text)


def _rst(acc: _Acc, root) -> None:
    """tree-sitter-rst emits a FLAT stream: a ``section`` node holds only
    its title + adornment, and the section's content follows as
    document-level SIBLINGS until the next section. Sweep once collecting
    (title, adornment char, prose), then emit heading nodes with the
    docutils depth convention: the order each adornment character is
    first seen defines its level (``=`` before ``-`` makes ``-`` the
    subsection). Preamble prose before any section creates no node, like
    markdown's headingless preamble."""
    sections: list[dict] = []
    current: dict | None = None
    for c in root.named_children:
        if c.type == "section":
            title_node = next((k for k in c.named_children
                               if k.type == "title"), None)
            adorn = next((k.text for k in c.children
                          if k.type == "adornment"), b"=")
            title = _txt(title_node).strip() if title_node is not None else ""
            current = {"title": title,
                       "char": chr(adorn[0]) if adorn else "=",
                       "line": c.start_point[0] + 1, "parts": []}
            if title:
                sections.append(current)
            continue
        if current is None or c.type in _RST_SKIP:
            continue
        t = c.text.decode("utf-8", "replace").strip()
        if t:
            current["parts"].append(t)
    level_of: dict[str, int] = {}
    stack: list[tuple[int, str]] = []  # (level, node id)
    for s in sections:
        lvl = level_of.setdefault(s["char"], len(level_of) + 1)
        while stack and stack[-1][0] >= lvl:
            stack.pop()
        raw = "\n\n".join(s["parts"])
        nid = acc.node(s["title"], "heading", _rst_unrole(raw),
                       line=s["line"])
        if stack:
            acc.edge(stack[-1][1], nid, "contains")
        for tgt in dict.fromkeys(_RST_DOC_REF.findall(raw)):
            acc.edge(nid, tgt.strip(), "references")
        stack.append((lvl, nid))


# -- json / yaml / toml (config-as-graph) -----------------------------------

def _qualify(prefix: str, key: str) -> str:
    return f"{prefix}.{key}" if prefix else key


_KV_SUMMARY_CAP = 200  # chars of scalar-children summary in a parent's body


def _kv_summary(pairs: list[tuple[str, str]]) -> str:
    """A parent key/table's body: its single-line scalar children inlined as
    ``k: v · k: v``. Without this a parent note is a connections-only stub :
    a context pack spends focus tokens on it and learns nothing, while the
    actual values reside in child notes that the parent does not load."""
    parts = [f"{k}: {v}" for k, v in pairs
             if k and v and "\n" not in v]
    return _clip_chars(" · ".join(parts), _KV_SUMMARY_CAP)


def _clip_chars(s: str, cap: int) -> str:
    if len(s) <= cap:
        return s
    cut = s[:cap].rsplit(" · ", 1)[0]
    return (cut or s[:cap]) + " …"


def _json(acc: _Acc, root) -> None:
    val = next((c for c in root.named_children), None)
    if val is not None and val.type == "object":
        _json_object(acc, val, None, "", 0)


def _json_object(acc: _Acc, obj, parent_id, prefix, depth) -> None:
    if depth > MAX_DEPTH:
        return  # pathologically deep config: stop, never risk RecursionError
    for pair in obj.named_children:
        if pair.type != "pair":
            continue
        k = pair.child_by_field_name("key")
        v = pair.child_by_field_name("value")
        if k is None:
            continue
        key = _unquote(_txt(k))
        label = _qualify(prefix, key)
        if v is not None and v.type == "object":
            scalars = []
            for p2 in v.named_children:  # inline the scalar children
                if p2.type != "pair":
                    continue
                k2 = p2.child_by_field_name("key")
                v2 = p2.child_by_field_name("value")
                if k2 is not None and v2 is not None and v2.type not in (
                        "object", "array"):
                    scalars.append((_unquote(_txt(k2)), _unquote(_txt(v2))))
            nid = acc.node(label, "key", _kv_summary(scalars),
                           line=k.start_point[0] + 1)
            if parent_id is not None:
                acc.edge(parent_id, nid, "contains")
            _json_object(acc, v, nid, label, depth + 1)
        else:
            body = _txt(v) if v is not None else ""
            nid = acc.node(label, "key", body, line=k.start_point[0] + 1)
            if parent_id is not None:
                acc.edge(parent_id, nid, "contains")


def _yaml_top_mapping(doc):
    """Return the document's top-level mapping node, or `None`. Only the direct
    node chain (document → [block_node] → mapping) is inspected: a top-level
    sequence has no top-level keys (like a JSON array), so it is skipped, not
    dived into (which would misleadingly grab only its first item's keys)."""
    for c in doc.named_children:
        if c.type in ("block_mapping", "flow_mapping"):
            return c
        if c.type in ("block_node", "flow_node"):
            for gc in c.named_children:
                if gc.type in ("block_mapping", "flow_mapping"):
                    return gc
    return None


def _yaml(acc: _Acc, root) -> None:
    for doc in root.named_children:
        if doc.type == "document":
            mapping = _yaml_top_mapping(doc)
            if mapping is not None:
                _yaml_mapping(acc, mapping, None, "", 0)
            break  # first document only


def _yaml_child_mapping(value):
    if value is None:
        return None
    for c in value.named_children:
        if c.type in ("block_mapping", "flow_mapping"):
            return c
    return None


def _yaml_mapping(acc: _Acc, mapping, parent_id, prefix, depth) -> None:
    if depth > MAX_DEPTH:
        return  # pathologically deep config: stop, never risk RecursionError
    for pair in mapping.named_children:
        if pair.type != "block_mapping_pair":
            continue
        k = pair.child_by_field_name("key")
        v = pair.child_by_field_name("value")
        if k is None:
            continue
        key = _unquote(_txt(k))
        label = _qualify(prefix, key)
        submap = _yaml_child_mapping(v)
        if submap is not None:
            scalars = []
            for p2 in submap.named_children:  # inline the scalar children
                if p2.type != "block_mapping_pair":
                    continue
                k2 = p2.child_by_field_name("key")
                v2 = p2.child_by_field_name("value")
                if (k2 is not None and v2 is not None
                        and _yaml_child_mapping(v2) is None):
                    scalars.append((_unquote(_txt(k2)), _unquote(_txt(v2))))
            nid = acc.node(label, "key", _kv_summary(scalars),
                           line=k.start_point[0] + 1)
            if parent_id is not None:
                acc.edge(parent_id, nid, "contains")
            _yaml_mapping(acc, submap, nid, label, depth + 1)
        else:
            body = _txt(v) if v is not None else ""
            nid = acc.node(label, "key", body, line=k.start_point[0] + 1)
            if parent_id is not None:
                acc.edge(parent_id, nid, "contains")


def _toml_kv(pair):
    named = [c for c in pair.named_children]
    key = named[0] if named else None
    val = named[1] if len(named) > 1 else None
    return key, val


def _toml(acc: _Acc, root) -> None:
    for c in root.named_children:
        if c.type == "pair":
            k, v = _toml_kv(c)
            if k is None:
                continue
            acc.node(_unquote(_txt(k)), "key", _txt(v) if v else "",
                     line=c.start_point[0] + 1)
        elif c.type in ("table", "table_array_element"):
            named = c.named_children
            if not named:
                continue
            header = _txt(named[0])  # bare_key or dotted_key
            scalars = []
            for p2 in named[1:]:  # inline the table's scalar pairs
                if p2.type != "pair":
                    continue
                k2, v2 = _toml_kv(p2)
                if k2 is not None and v2 is not None:
                    scalars.append((_unquote(_txt(k2)), _unquote(_txt(v2))))
            tid = acc.node(header, "table", _kv_summary(scalars),
                           line=c.start_point[0] + 1)
            for p in named[1:]:
                if p.type != "pair":
                    continue
                k, v = _toml_kv(p)
                if k is None:
                    continue
                label = _qualify(header, _unquote(_txt(k)))
                nid = acc.node(label, "key", _txt(v) if v else "",
                               line=p.start_point[0] + 1)
                acc.edge(tid, nid, "contains")


# -- sql (schema graph) -----------------------------------------------------

def _sql_ref_name(ref) -> str:
    """Table name from an ``object_reference`` (its ``name`` field, else the
    whole reference text)."""
    if ref is None:
        return ""
    nm = ref.child_by_field_name("name")
    return _txt(nm) if nm is not None else _txt(ref)


def _sql(acc: _Acc, root) -> None:
    for ct in _find_all(root, "create_table"):
        own_ref = next((c for c in ct.children
                        if c.type == "object_reference"), None)
        tname = _sql_ref_name(own_ref)
        if not tname:
            continue
        tid = acc.node(tname, "table", "", line=ct.start_point[0] + 1)
        coldefs = next((c for c in ct.children
                        if c.type == "column_definitions"), None)
        if coldefs is None:
            continue
        for cd in coldefs.named_children:
            if cd.type == "column_definition":
                name_n = cd.child_by_field_name("name")
                type_n = cd.child_by_field_name("type")
                cname = _txt(name_n) if name_n is not None else _txt(cd)
                ctype = _txt(type_n) if type_n is not None else ""
                nid = acc.node(_qualify(tname, cname), "column", ctype,
                               line=cd.start_point[0] + 1)
                acc.edge(tid, nid, "contains")
                # inline `... REFERENCES other(col)` on the column itself
                ref = _sql_referenced(cd, own_ref)
                if ref:
                    acc.edge(nid, ref, "references")
            elif cd.type == "constraints":
                for con in cd.named_children:
                    if con.type == "constraint":
                        ref = _sql_referenced(con, own_ref)
                        if ref:
                            acc.edge(tid, ref, "references")


def _sql_referenced(node, own_ref) -> str:
    """The referenced-table name inside a column/constraint: the first
    ``object_reference`` that is not the table's own name node."""
    for n in _find_all(node, "object_reference"):
        if n is not own_ref:
            return _sql_ref_name(n)
    return ""


# -- python (light structural pass) -----------------------------------------

def _py_signature(defn) -> str:
    """The def/class header line (up to and incl. the first colon)."""
    text = defn.text.decode("utf-8", "replace")
    head = text.split("\n", 1)[0]
    return head.strip()


def _py(acc: _Acc, root, src: str) -> None:
    mod_label = os.path.basename(src)
    mid = acc.node(mod_label, "module", _py_module_doc(root),
                   line=root.start_point[0] + 1)
    _py_scope(acc, root, mid, class_name=None, depth=0)
    _semantic_call_edges(acc, root, mid, {"call"})


def _py_module_doc(root) -> str:
    """First paragraph of the module docstring, or ''. The docstring is a bare
    ``string`` statement as the module's first named child (some grammar
    versions wrap it in ``expression_statement``); both are handled."""
    for c in root.named_children:
        node = c
        if c.type == "expression_statement" and c.named_children:
            node = c.named_children[0]
        if node.type == "string":
            return _docstring_text(node)
        break  # docstring must be the very first statement
    return ""


def _docstring_text(string_node) -> str:
    """Clean text of a ``string`` node: prefer its ``string_content`` child
    (quotes already excluded), else strip quotes by hand. First paragraph."""
    content = next((c for c in string_node.named_children
                    if c.type == "string_content"), None)
    raw = _txt(content) if content is not None else _txt(string_node).strip("\"'")
    return raw.strip().split("\n\n", 1)[0].strip()


def _py_scope(acc: _Acc, scope, parent_id, class_name, depth) -> None:
    """Emit direct-child defs/classes/imports of ``scope`` (a module or a class
    body). Methods (functions inside a class) get a ``Class.method`` label."""
    if depth > MAX_DEPTH:
        return  # pathologically deep class nesting: stop recursing
    body = scope
    if scope.type == "class_definition":
        body = scope.child_by_field_name("body") or scope
    for c in body.children:
        if c.type in ("import_statement", "import_from_statement"):
            for name in _py_import_names(c):
                iid = acc.node(name, "import", "", line=c.start_point[0] + 1)
                acc.edge(parent_id, iid, "imports")
        elif c.type == "function_definition":
            name_n = c.child_by_field_name("name")
            fname = _txt(name_n) if name_n is not None else "?"
            kind = "method" if class_name else "function"
            label = f"{class_name}.{fname}" if class_name else fname
            fid = acc.node(label, kind, _py_signature(c),
                           line=c.start_point[0] + 1)
            acc.edge(parent_id, fid, "contains")
        elif c.type == "class_definition":
            name_n = c.child_by_field_name("name")
            cname = _txt(name_n) if name_n is not None else "?"
            cid = acc.node(cname, "class", _py_signature(c),
                           line=c.start_point[0] + 1)
            acc.edge(parent_id, cid, "contains")
            _py_scope(acc, c, cid, class_name=cname, depth=depth + 1)


def _py_import_names(stmt) -> list[str]:
    if stmt.type == "import_from_statement":
        mod = stmt.child_by_field_name("module_name")
        if mod is not None:
            return [_txt(mod)]
        return []
    return [_txt(n) for n in stmt.named_children
            if n.type in ("dotted_name", "aliased_import", "identifier")]


def _head_line(node) -> str:
    """The node's first source line, stripped: a compact signature for a
    def/class/method body (shared by the JS/TS and generic extractors)."""
    text = node.text.decode("utf-8", "replace")
    return text.split("\n", 1)[0].strip()


# -- javascript / typescript / tsx / jsx (rich explicit pass) ----------------
#
# The owner's real projects are TypeScript. A LIGHT structural pass mirroring
# python: module node; top-level functions, arrow-function consts, classes +
# methods, React components (Capitalized + returns JSX), exported names; and
# import/export-from/require → import nodes + `imports` edges. JSX only parses
# under the tsx/javascript grammars (a `.ts` file has no components).

_JS_LANGS = frozenset({"javascript", "typescript", "tsx"})
_JSX_TYPES = frozenset({"jsx_element", "jsx_self_closing_element",
                        "jsx_fragment"})
_JS_FN_VALUES = frozenset({"arrow_function", "function", "function_expression"})


def _js_str_value(strnode) -> str:
    """The text of a JS `string` node without quotes (prefer its
    ``string_fragment`` child, which already excludes the delimiters)."""
    frag = next((c for c in strnode.named_children
                 if c.type == "string_fragment"), None)
    return _txt(frag) if frag is not None else _unquote(_txt(strnode))


def _has_jsx(node, depth: int = 0) -> bool:
    """True if the subtree contains a JSX element/fragment: the signal that a
    Capitalized function/const is a React component. Depth-capped (a component
    body is shallow; the cap only guards pathological nesting)."""
    if depth > 60:
        return False
    if node.type in _JSX_TYPES:
        return True
    return any(_has_jsx(c, depth + 1) for c in node.children)


def _require_source(value) -> str | None:
    """The module string of a ``require("mod")`` call value, else None."""
    if value is None or value.type != "call_expression":
        return None
    fn = value.child_by_field_name("function")
    if fn is None or _txt(fn) != "require":
        return None
    args = value.child_by_field_name("arguments")
    if args is None:
        return None
    for a in args.named_children:
        if a.type == "string":
            return _js_str_value(a)
    return None


def _js_add_import(acc: _Acc, stmt, mid: str) -> None:
    """An ``import … from "mod"`` / ``export … from "mod"`` → import node +
    ``imports`` edge (the module string is the label)."""
    s = stmt.child_by_field_name("source")
    if s is None:
        return
    mod = _js_str_value(s)
    if mod:
        iid = acc.node(mod, "import", "", line=stmt.start_point[0] + 1)
        acc.edge(mid, iid, "imports")


def _js_function_kind(name: str, body) -> str:
    """`component` for a Capitalized name whose body returns JSX, else
    `function`: React's own naming convention is the whole heuristic."""
    if name[:1].isupper() and body is not None and _has_jsx(body):
        return "component"
    return "function"


def _js_declarator(acc: _Acc, d, mid: str) -> None:
    """One ``variable_declarator``: a ``require(...)`` value → import; an
    arrow/function value → function or React-component node."""
    name_n = d.child_by_field_name("name")
    value = d.child_by_field_name("value")
    mod = _require_source(value)
    if mod:
        iid = acc.node(mod, "import", "", line=d.start_point[0] + 1)
        acc.edge(mid, iid, "imports")
        return
    if value is not None and value.type in _JS_FN_VALUES:
        name = _txt(name_n) if name_n is not None else "(anon)"
        body = value.child_by_field_name("body")
        kind = _js_function_kind(name, body)
        fid = acc.node(name, kind, _head_line(d), line=d.start_point[0] + 1)
        acc.edge(mid, fid, "contains")


def _js_class(acc: _Acc, node, mid: str) -> None:
    name_n = node.child_by_field_name("name")
    cname = _txt(name_n) if name_n is not None else "(anon)"
    cid = acc.node(cname, "class", _head_line(node),
                   line=node.start_point[0] + 1)
    acc.edge(mid, cid, "contains")
    body = node.child_by_field_name("body")
    if body is None:
        return
    for m in body.named_children:
        if m.type in ("method_definition", "method_signature"):
            mn = m.child_by_field_name("name")
            mname = _txt(mn) if mn is not None else "(anon)"
            nid = acc.node(f"{cname}.{mname}", "method", _head_line(m),
                           line=m.start_point[0] + 1)
            acc.edge(cid, nid, "contains")


def _js_named(acc: _Acc, node, kind: str, mid: str) -> None:
    """A single named TS declaration (interface / enum / type alias) → a node
    of ``kind`` + a ``contains`` edge. Name from the ``name`` field, else the
    first type-identifier child."""
    nm = node.child_by_field_name("name")
    if nm is None:
        nm = next((c for c in node.named_children
                   if c.type in ("type_identifier", "identifier")), None)
    label = _txt(nm) if nm is not None else "(anon)"
    nid = acc.node(label, kind, _head_line(node), line=node.start_point[0] + 1)
    acc.edge(mid, nid, "contains")


def _js_stmt(acc: _Acc, node, mid: str) -> None:
    t = node.type
    if t == "import_statement":
        _js_add_import(acc, node, mid)
    elif t == "export_statement":
        if node.child_by_field_name("source") is not None:
            _js_add_import(acc, node, mid)  # `export … from "mod"` = dependency
            return
        decl = node.child_by_field_name("declaration")
        if decl is not None:
            _js_stmt(acc, decl, mid)        # `export function/class/const …`
            return
        for ch in node.named_children:      # bare `export { a, b }` / default
            if ch.type == "export_clause":
                for spec in ch.named_children:
                    if spec.type != "export_specifier":
                        continue
                    nm = spec.child_by_field_name("name")
                    eid = acc.node(_txt(nm) if nm is not None else _txt(spec),
                                   "export", "", line=spec.start_point[0] + 1)
                    acc.edge(mid, eid, "contains")
            elif ch.type == "identifier":   # `export default Foo`
                eid = acc.node(_txt(ch), "export", "",
                               line=ch.start_point[0] + 1)
                acc.edge(mid, eid, "contains")
    elif t in ("lexical_declaration", "variable_declaration"):
        for d in node.named_children:
            if d.type == "variable_declarator":
                _js_declarator(acc, d, mid)
    elif t in ("function_declaration", "generator_function_declaration"):
        name_n = node.child_by_field_name("name")
        name = _txt(name_n) if name_n is not None else "(anon)"
        kind = _js_function_kind(name, node.child_by_field_name("body"))
        fid = acc.node(name, kind, _head_line(node),
                       line=node.start_point[0] + 1)
        acc.edge(mid, fid, "contains")
    elif t in ("class_declaration", "abstract_class_declaration"):
        _js_class(acc, node, mid)
    elif t == "interface_declaration":      # TS: interface / enum / type alias
        _js_named(acc, node, "interface", mid)
    elif t == "enum_declaration":
        _js_named(acc, node, "enum", mid)
    elif t == "type_alias_declaration":
        _js_named(acc, node, "type", mid)
    elif t == "expression_statement":       # bare `require("side-effect")`
        for ch in node.named_children:
            mod = _require_source(ch)
            if mod:
                iid = acc.node(mod, "import", "", line=node.start_point[0] + 1)
                acc.edge(mid, iid, "imports")


def _js(acc: _Acc, root, src: str) -> None:
    mid = acc.node(os.path.basename(src), "module", "",
                   line=root.start_point[0] + 1)
    for c in root.named_children:
        _js_stmt(acc, c, mid)
    _semantic_call_edges(acc, root, mid, {"call_expression"})


# -- generic fallback for grammars without a specialized extractor -----------
#
# Guarantees ANY grammar yields structure: a module node per file plus a node
# for every "definition-like" tree-sitter node, with `contains` + import edges.
# Purely heuristic on node.type: tree-sitter names definitions consistently
# (`function_declaration`, `struct_item`, `method`, `class_specifier`, …), so a
# substring match (filtered against a use/reference denylist) covers the pack.

_DEF_KINDS = (  # (substring in node.type, emitted kind): checked in order, so
    ("function", "function"), ("subroutine", "function"),   # specific wins
    ("procedure", "function"), ("constructor", "constructor"),
    ("method", "method"), ("interface", "interface"),
    ("namespace", "namespace"), ("class", "class"), ("struct", "struct"),
    ("enum", "enum"), ("trait", "trait"), ("impl", "impl"),
    ("module", "module"), ("message", "message"),
    ("type_definition", "type"), ("table", "table"), ("rule", "rule"),
    # ("field", ...) deliberately absent: field-tested on gson/ripgrep:
    # per-field notes exploded bundles (28k notes) with stubs; the
    # struct/class is the unit of knowledge
)
# node.type fragments that mean USE/reference or a container, never a single
# definition: filters the false positives a bare substring match would catch
# (`field_expression`, `function_type`, `default_clause`, `type_identifier`,
# `field_initializer`, `field_declaration_list`, and C's `function_declarator`,
# whose parent `function_definition` already covers the function). Denied
# container nodes are still descended INTO, so their real child defs attach to
# the grandparent.
_DEF_DENY = ("expression", "identifier", "reference", "access", "argument",
             "parameter", "default", "function_type", "typeof", "_call",
             "invocation", "annotation", "_clause", "predicate",
             "constraint", "modifier",
             "_name", "_list", "initializer", "_body", "_block", "declarator",
             # field-tested on ripgrep: `tuple_struct_pattern` match arms
             # and `mutable_specifier` are uses, never definitions
             "pattern", "specifier")
# import/include/use nodes → an `imports`/`includes` edge. The three verbs are
# matched as substrings; `use`/`from`/`open` are too short to match safely, so
# those grammars' node types are listed explicitly.
_IMPORT_HINTS = ("import", "include", "require")
_IMPORT_TYPES = frozenset({
    "use_declaration", "use_clause", "using_directive", "using_declaration",
    "open_directive", "package_import", "load_statement", "with_directive"})
_NAME_CHILD = frozenset({"identifier", "constant", "name", "word",
                         "field_identifier", "property_identifier",
                         "type_identifier"})
_STRING_TYPES = frozenset({
    "string", "interpreted_string_literal", "string_literal",
    "raw_string_literal", "string_fragment", "quoted_string"})
_PATH_TYPES = frozenset({
    "dotted_name", "scoped_identifier", "identifier", "namespace",
    "package_identifier", "qualified_name", "scoped_type_identifier"})
_SEMANTIC_EDGE_CAP = 400


def _is_import_type(t: str) -> bool:
    return t in _IMPORT_TYPES or any(h in t for h in _IMPORT_HINTS)


def _def_kind(t: str, node=None) -> str | None:
    """The kind for a definition-like node.type, or None if it is not one."""
    if t in {"class_specifier", "struct_specifier"}:
        # C and C++ use these types for both definitions and references.
        # A body distinguishes a definition from a bodyless type reference.
        if node is not None and node.child_by_field_name("body") is not None:
            return t.removesuffix("_specifier")
        return None
    if any(d in t for d in _DEF_DENY):
        return None
    for frag, kind in _DEF_KINDS:
        if frag in t:
            return kind
    return None


def _first_of_types(node, types, cap: int = 300):
    """First descendant (BFS, self included) whose type is in ``types``,
    within a small node budget: a bounded search for a label child."""
    stack = [node]
    seen = 0
    while stack and seen < cap:
        n = stack.pop()
        seen += 1
        if n.type in types:
            return n
        stack.extend(reversed(n.children))
    return None


def _generic_name(node) -> str:
    """A label for a definition node: its ``name`` field, else the identifier
    inside its ``declarator`` chain (C-family: ``function_definition`` names its
    function via a nested ``function_declarator``), else the first identifier-ish
    child, else its parent's ``name`` (Go ``struct_type`` sits under a named
    ``type_spec``), else its first source line."""
    nm = node.child_by_field_name("name")
    if nm is not None:
        return _txt(nm)
    dcl = node.child_by_field_name("declarator")
    if dcl is not None:  # the identifier precedes the param list in the subtree
        ident = _first_of_types(dcl, _NAME_CHILD)
        if ident is not None:
            return _txt(ident)
    for c in node.named_children:
        if c.type in _NAME_CHILD or c.type.endswith("_name"):
            return _txt(c)
    parent = node.parent
    if parent is not None:
        pn = parent.child_by_field_name("name")
        if pn is not None:
            return _txt(pn)
    return _head_line(node)[:100]


def _generic_import_label(node) -> str:
    """A label for an import node: the imported module string, else a
    dotted/scoped path identifier, else the import's first line."""
    s = _first_of_types(node, _STRING_TYPES)
    if s is not None:
        return _unquote(_txt(s)).strip("\"'<>` ")
    ident = _first_of_types(node, _PATH_TYPES)
    if ident is not None:
        return _txt(ident)
    return _head_line(node)[:100]


def _generic_walk(acc: _Acc, node, parent_id: str, depth: int) -> None:
    if depth > MAX_DEPTH:
        return
    for c in node.named_children:
        t = c.type
        if _is_import_type(t):
            rel = "includes" if "include" in t else "imports"
            iid = acc.node(_generic_import_label(c), "import", "",
                           line=c.start_point[0] + 1)
            acc.edge(parent_id, iid, rel)
            continue  # an import is a leaf here: do not descend
        kind = _def_kind(t, c)
        if kind is not None:
            name = _generic_name(c).strip()
            # field-tested on redis: one-char/numeric names ("r", "0")
            # are shell locals and fixture keys, not landmarks: recurse
            # for real definitions inside, but emit no node
            if len(name) < 2 or name.isdigit():
                _generic_walk(acc, c, parent_id, depth + 1)
                continue
            nid = acc.node(name, kind, _head_line(c),
                           line=c.start_point[0] + 1)
            acc.edge(parent_id, nid, "contains")
            _generic_walk(acc, c, nid, depth + 1)  # nested def → child-def
        else:
            _generic_walk(acc, c, parent_id, depth + 1)


def _walk_nodes(root, cap: int = 20000):
    """Yield a bounded document-order traversal, including ``root``."""
    stack = [root]
    seen = 0
    while stack and seen < cap:
        node = stack.pop()
        seen += 1
        yield node
        stack.extend(reversed(node.named_children))


def _semantic_target(node) -> str:
    """Return the final named symbol represented by a syntax node."""
    named = node.child_by_field_name("name")
    if named is not None:
        return _txt(named).split(".")[-1]
    identifiers = [child for child in _walk_nodes(node, 100)
                   if child.type in _NAME_CHILD]
    return _txt(identifiers[-1]).split(".")[-1] if identifiers else ""


def _semantic_call_edges(acc: _Acc, root, module_id: str,
                         call_types: set[str]) -> None:
    """Emit bounded call targets for the specialized language passes."""
    targets = []
    for node in _walk_nodes(root):
        if node.type not in call_types:
            continue
        function = node.child_by_field_name("function")
        target = _semantic_target(function or node).strip()
        if target and target not in targets:
            targets.append(target)
        if len(targets) >= _SEMANTIC_EDGE_CAP:
            break
    for target in targets:
        acc.edge(module_id, target, "calls")


def _generic_semantic_edges(acc: _Acc, root, module_id: str) -> None:
    """Extract bounded Java and C# code relations from their native syntax."""
    if acc.lang not in {"java", "csharp"}:
        return
    relations: list[tuple[str, str]] = []

    def add(relation: str, target: str) -> None:
        value = target.strip()
        if value and len(relations) < _SEMANTIC_EDGE_CAP:
            relations.append((relation, value))

    for node in _walk_nodes(root):
        if len(relations) >= _SEMANTIC_EDGE_CAP:
            break
        if acc.lang == "java":
            if node.type == "superclass":
                target = _first_of_types(node, {"type_identifier"})
                if target is not None:
                    add("inherits", _txt(target))
            elif node.type == "super_interfaces":
                for target in _walk_nodes(node, 100):
                    if target.type == "type_identifier":
                        add("implements", _txt(target))
            elif node.type == "method_invocation":
                add("calls", _semantic_target(node))
            elif node.type == "type_identifier":
                add("references", _txt(node))
        else:
            if node.type == "base_list":
                for target in node.named_children:
                    add("references", _semantic_target(target))
            elif node.type == "invocation_expression":
                function = node.child_by_field_name("function")
                add("calls", _semantic_target(function or node))
            elif node.type in {"object_creation_expression",
                              "variable_declaration", "parameter"}:
                target = node.child_by_field_name("type")
                if target is not None:
                    add("references", _semantic_target(target))

    for relation, target in dict.fromkeys(relations):
        acc.edge(module_id, target, relation)


_CODE_CALL_TYPES = {
    "csharp": frozenset({"invocation_expression"}),
    "go": frozenset({"call_expression"}),
    "java": frozenset({"method_invocation"}),
    "javascript": frozenset({"call_expression"}),
    "kotlin": frozenset({"call_expression"}),
    "python": frozenset({"call"}),
    "ruby": frozenset({"call", "method_call"}),
    "rust": frozenset({"call_expression", "method_call_expression"}),
    "tsx": frozenset({"call_expression"}),
    "typescript": frozenset({"call_expression"}),
}


def code_symbol_relations(path: str, src: str | None = None) -> dict:
    """Return bounded symbol definitions and call relations for one file."""
    result = {"symbols": [], "relations": []}
    lang = lang_for(path)
    call_types = _CODE_CALL_TYPES.get(lang or "")
    if call_types is None:
        return result
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError:
        return result
    if (len(data) > MAX_FILE_BYTES or b"\x00" in data
            or _nesting_depth(data) > MAX_DEPTH):
        return result
    try:
        root = _parser(lang).parse(data).root_node
    except Exception:
        return result

    module = src if src is not None else os.path.basename(path)
    definitions = {}
    for node in _walk_nodes(root):
        kind = _def_kind(node.type, node)
        if kind is None:
            continue
        name = _generic_name(node).strip()
        if len(name) < 2 or name.isdigit():
            continue
        key = (node.start_byte, node.end_byte, node.type)
        definitions[key] = {
            "node": node,
            "name": name,
            "kind": kind,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
        }
        if len(definitions) >= MAX_FILE_NODES:
            break

    qualified = {}

    def qualified_name(key) -> str:
        if key in qualified:
            return qualified[key]
        entry = definitions[key]
        parent = entry["node"].parent
        parent_name = ""
        while parent is not None:
            parent_key = (parent.start_byte, parent.end_byte, parent.type)
            if parent_key in definitions:
                parent_name = qualified_name(parent_key)
                break
            parent = parent.parent
        value = (
            f"{parent_name}::{entry['name']}" if parent_name
            else str(entry["name"])
        )
        qualified[key] = value
        return value

    result["symbols"].append({
        "name": os.path.basename(module),
        "qualified": os.path.basename(module),
        "kind": "module",
        "start_line": 1,
        "end_line": root.end_point[0] + 1,
    })
    for key, entry in sorted(
            definitions.items(), key=lambda item: (
                item[1]["start_line"], item[1]["end_line"], item[1]["name"])):
        result["symbols"].append({
            "name": entry["name"],
            "qualified": qualified_name(key),
            "kind": entry["kind"],
            "start_line": entry["start_line"],
            "end_line": entry["end_line"],
        })

    relations = []
    seen = set()
    for node in _walk_nodes(root):
        if node.type not in call_types:
            continue
        function = (
            node.child_by_field_name("function")
            or node.child_by_field_name("method")
            or node.child_by_field_name("name")
        )
        target = _semantic_target(function or node).strip()
        if not target:
            continue
        parent = node.parent
        source = os.path.basename(module)
        while parent is not None:
            parent_key = (parent.start_byte, parent.end_byte, parent.type)
            if parent_key in definitions:
                source = qualified_name(parent_key)
                break
            parent = parent.parent
        relation = (source, target, "calls")
        if relation in seen:
            continue
        seen.add(relation)
        relations.append({
            "source": source,
            "target": target,
            "relation": "calls",
        })
        if len(relations) >= _SEMANTIC_EDGE_CAP:
            break
    result["relations"] = relations
    return result


def _generic(acc: _Acc, root, src: str) -> None:
    mid = acc.node(os.path.basename(src), "module", "",
                   line=root.start_point[0] + 1)
    _generic_walk(acc, root, mid, depth=0)
    _generic_semantic_edges(acc, root, mid)


# -- tree helpers -----------------------------------------------------------

def _find_all(node, type_name: str) -> list:
    """All descendants (and self) of ``node`` with the given type, in
    document order."""
    out = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == type_name:
            out.append(n)
        stack.extend(reversed(n.children))
    return out


_DISPATCH = {
    "markdown": _md, "rst": _rst, "json": _json, "yaml": _yaml,
    "toml": _toml,
    "sql": _sql,  # python handled specially (needs the src path for its label)
}


# -- file / folder extraction -----------------------------------------------

_OPEN = frozenset(b"{[(")
_CLOSE = frozenset(b"}])")


def _nesting_depth(data: bytes) -> int:
    """A cheap upper bound on structural nesting, from raw bytes WITHOUT
    parsing: the larger of bracket-nesting depth and the deepest line's
    leading-whitespace run (YAML/indentation nesting). Used as a pre-parse
    guard: the native parsers stack-overflow on pathological depth, and that
    crash is a segfault Python cannot catch, so a deep file must be skipped
    before it ever reaches ``parse()``."""
    max_ws = brackets = depth = 0
    for line in data.split(b"\n"):
        ws = len(line) - len(line.lstrip(b" \t"))
        if ws > max_ws:
            max_ws = ws
    for ch in data:
        if ch in _OPEN:
            brackets += 1
            if brackets > depth:
                depth = brackets
        elif ch in _CLOSE and brackets > 0:
            brackets -= 1
    return max(max_ws, depth)


def extract_file(path: str, graph: dict | None = None,
                 src: str | None = None) -> dict:
    """Parse one file into ``graph`` (a new one if None). ``src`` is the label
    stored on nodes (defaults to the path's basename). Returns the graph.
    Fails soft: an unsupported extension, an unreadable file, or a grammar
    that will not load adds nothing and does not raise."""
    graph = graph if graph is not None else _new_graph()
    lang = lang_for(path)
    if lang is None:
        return graph
    src = src if src is not None else os.path.basename(path)
    try:
        with open(path, "rb") as fh:
            # read one byte past the cap: len > cap ⇒ oversized, skip without
            # ever holding a multi-MB blob in memory (minified/generated files)
            data = fh.read(MAX_FILE_BYTES + 1)
    except OSError:
        return graph
    if len(data) > MAX_FILE_BYTES:
        return graph  # too big to be worth structural parsing: skip
    if b"\x00" in data:
        return graph  # NUL byte ⇒ binary / non-UTF8: never parse as source
    if _nesting_depth(data) > MAX_DEPTH:
        # pathologically deep: skip BEFORE the native parser can stack-overflow
        # (an uncatchable segfault). Real config/code never nests this deep.
        return graph
    try:
        parser = _parser(lang)
    except Exception:
        return graph  # grammar unavailable (pack lacks it): fail soft
    n0, e0 = len(graph["nodes"]), len(graph["links"])
    try:
        root = parser.parse(data).root_node
        acc = _Acc(graph, lang, src)
        if lang == "python":
            _py(acc, root, src)
        elif lang in _JS_LANGS:
            _js(acc, root, src)
        elif lang in _DISPATCH:
            _DISPATCH[lang](acc, root)
        else:
            _generic(acc, root, src)  # every other grammar → structure anyway
    except Exception:
        # one pathological file (an unexpected tree shape, runaway nesting)
        # must never abort a folder walk: roll back this file's partial
        # contribution and fail soft, leaving earlier files intact
        del graph["nodes"][n0:]
        del graph["links"][e0:]
    if len(graph["nodes"]) - n0 > MAX_FILE_NODES:
        # a data fixture, not knowledge: keep the first MAX_FILE_NODES of
        # THIS file's nodes and drop edges touching the truncated tail
        dropped = {n["id"] for n in graph["nodes"][n0 + MAX_FILE_NODES:]}
        del graph["nodes"][n0 + MAX_FILE_NODES:]
        graph["links"][e0:] = [e for e in graph["links"][e0:]
                               if e.get("source") not in dropped
                               and e.get("target") not in dropped]
    return graph


def _skip_walk_file(fn: str) -> bool:
    """A file never worth reading in a walk, independent of language: a
    dependency lockfile, a source map, or a minified bundle (all huge and/or
    machine-generated, near-zero structural value: and lockfiles carry a real
    ``.json``/``.yaml`` extension, so they would otherwise be swept)."""
    low = fn.lower()
    return (low in _SKIP_FILES or low.endswith(".map")
            or fnmatch.fnmatch(low, "*.min.*"))


def _excluded_by_glob(rel: str, name: str, globs) -> bool:
    """True if ``--exclude`` matches the entry's basename OR its posix
    bundle-relative path (so both ``*.test.ts`` and ``docs/*`` work)."""
    return any(fnmatch.fnmatch(name, g) or fnmatch.fnmatch(rel, g)
               for g in globs)


def extract_path(path: str, exclude: set | None = None,
                 exclude_globs: list | None = None,
                 only_exts: set | None = None) -> tuple[dict, dict]:
    """Extract one file or a recursively traversed directory.

The result contains the graph and counts of parsed files by language. Source
paths are relative to the input root and remain stable across runs. Directory
walks omit excluded, hidden, generated, oversized, binary, and secret-bearing
paths. ``exclude_globs`` matches names or relative paths, and ``only_exts``
limits extraction to the named suffixes.
    """
    graph = _new_graph()
    stats = {"files": 0, "by_lang": {}}
    globs = list(exclude_globs or ())
    exts = only_exts or set()

    def _wanted(name: str) -> bool:
        return not exts or os.path.splitext(name)[1].lower() in exts

    if os.path.isfile(path):
        lang = lang_for(path)
        name = os.path.basename(path)
        if (lang is not None and _wanted(name) and not _skip_walk_file(name)
                and not _excluded_by_glob(name, name, globs)):
            extract_file(path, graph, src=name)
            if graph["nodes"]:  # count only if it actually yielded nodes
                stats["files"] = 1
                stats["by_lang"][lang] = 1
        return graph, stats
    excl = {os.path.realpath(p) for p in (exclude or ())}
    root = os.path.abspath(path)
    for dirpath, dirnames, filenames in os.walk(root):
        drel = os.path.relpath(dirpath, root).replace(os.sep, "/")
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.startswith(".")
            and os.path.realpath(os.path.join(dirpath, d)) not in excl
            and not _excluded_by_glob(
                (f"{drel}/{d}" if drel != "." else d), d, globs))
        for fn in sorted(filenames):
            if fn.startswith("."):
                continue  # dotfiles may hold secrets: never sweep them
            if fn in store.RESERVED:
                continue  # index.md / log.md are generated OKF artifacts, not
                #           source (store skips them too): avoids a build self-scan
            if _skip_walk_file(fn) or not _wanted(fn):
                continue  # lockfile/map/minified, or filtered out by --only
            lang = lang_for(fn)
            if lang is None:
                continue
            rel = f"{drel}/{fn}" if drel != "." else fn
            if _excluded_by_glob(rel, fn, globs):
                continue  # matched a --exclude pattern
            full = os.path.join(dirpath, fn)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            before = len(graph["nodes"])
            extract_file(full, graph, src=rel)
            if len(graph["nodes"]) > before:
                stats["files"] += 1
                stats["by_lang"][lang] = stats["by_lang"].get(lang, 0) + 1
    return graph, stats


def summarize(graph: dict) -> dict:
    """Counts for a SUMMARY: total nodes/edges, nodes by kind, edges by
    relation (both sorted, deterministic)."""
    by_kind: dict[str, int] = {}
    for n in graph["nodes"]:
        by_kind[n.get("kind", "?")] = by_kind.get(n.get("kind", "?"), 0) + 1
    by_rel: dict[str, int] = {}
    for e in graph["links"]:
        by_rel[e.get("relation", "?")] = by_rel.get(e.get("relation", "?"), 0) + 1
    return {
        "nodes": len(graph["nodes"]),
        "edges": len(graph["links"]),
        "by_kind": dict(sorted(by_kind.items())),
        "by_relation": dict(sorted(by_rel.items())),
    }


CONFIG_LANGS = frozenset({"json", "yaml", "toml"})


def config_value_count(graph: dict) -> int:
    """How many config scalar VALUES were captured into node bodies
    (json/yaml/toml keys with a non-empty body). The privacy-notice signal:
    these bodies carry the config's values verbatim, so if the config held
    secrets they are now in notes (see the module PRIVACY NOTE)."""
    return sum(1 for n in graph.get("nodes", [])
               if n.get("lang") in CONFIG_LANGS and n.get("kind") == "key"
               and n.get("body"))


# -- writing extracted notes into a bundle ----------------------------------

# the shared graph→notes writer lives in ingest.py; the alias keeps this
# module's stable surface (tests exercise extract._carry_inferred directly)
from .ingest import _carry_inferred, write_notes

_COMPATIBILITY_EXPORTS = (_carry_inferred, write_notes)


# Test-origin detection (field-tested on gin/gson/sinatra): test code
# lexically mirrors every how-do-I question and swamps production notes in
# recall. Importers TAG test-origin notes; recall demotes (never hides).
_TEST_DIRS = frozenset({"test", "tests", "spec", "specs", "testdata",
                        "testing", "__tests__", "test_apps", "fixtures",
                        # field-tested on OpenClaw: qa/scenarios yaml led
                        # the index as landmarks
                        "qa", "scenarios", "e2e"})


def _is_test_path(path: str) -> bool:
    parts = str(path).replace("\\", "/").split("/")
    if any(p.lower() in _TEST_DIRS for p in parts[:-1]):
        return True
    base = parts[-1]
    stem = base.rsplit(".", 1)[0]
    if stem.endswith(("Test", "Tests", "TestCase", "Spec")):
        return True  # Java/C# convention: case-sensitive dodges "latest"
    low = base.lower()
    stem = stem.lower()
    return (stem.startswith(("test_", "test-")) or stem == "test"
            or stem.endswith(("_test", "-test", "_tests", "-tests",
                              "_spec", "-spec"))
            or ".test." in low or ".spec." in low)


def import_graph(bundle, graph: dict, subdir: str = "extracted", *,
                 scoped: bool = False) -> tuple[int, int]:
    """Write an extracted graph as OKF notes under the requested subdirectory.

Each note retains its node provenance and confidence. Typed relationships use
the shared connection grammar. Duplicate import targets merge into one note,
curated ownership remains protected, and byte-identical files are skipped.
The return value reports written and skipped notes.
    """
    nodes = {n["id"]: n for n in graph.get("nodes", [])
             if isinstance(n, dict) and isinstance(n.get("id"), (str, int))}
    edges_by_src: dict = {}
    for e in graph.get("links", []):
        edges_by_src.setdefault(e.get("source", ""), []).append(e)

    # boilerplate headings repeat across files (field-tested on OWASP:
    # 146 "Introduction" sections): a heading title used by SEVERAL
    # source files gets its doc stem, so landmarks stay distinguishable
    heading_files: dict[str, set] = {}
    for n in nodes.values():
        if _clean(n.get("kind")) == "heading" and n.get("source_file"):
            heading_files.setdefault(
                str(n.get("label", "")).strip().lower(),
                set()).add(str(n["source_file"]))

    def _heading_title(node, label: str) -> str:
        if (_clean(node.get("kind")) == "heading" and node.get("source_file")
                and len(heading_files.get(label.strip().lower(), ())) > 1):
            stem = os.path.basename(str(node["source_file"]))
            stem = stem.rsplit(".", 1)[0]
            return f"{label} ({stem})"
        return label

    import_seen: set[tuple] = set()

    def plan(nid, node, label):
        # lang is a path component: sanitize to alnum so it can never carry a
        # separator or '..' (defense-in-depth; our own graphs use fixed names)
        lang = re.sub(r"[^a-z0-9]+", "",
                      str(node.get("lang", "")).lower()) or "misc"
        # Honour each node's own provenance: the deterministic base is
        # `extracted` by default. The optional enrichment layer
        # appends `inferred` concept nodes on top while preserving provenance.
        prov = _clean(node.get("provenance")) or PROVENANCE
        conf = _clean(node.get("confidence")) or CONFIDENCE
        if _clean(node.get("kind")) == "import":
            # The same import target from several files is one knowledge item.
            # first node writes the note, the rest merge into it (each module's
            # `imports [[target]]` line still resolves to it by title)
            key = (lang, label.lower(), str(node.get("source_file", "")) if scoped else "")
            if key in import_seen:
                return None
            import_seen.add(key)
        # model-written notes live under inferred/: the folder must not
        # contradict the frontmatter for anyone browsing the vault
        eff_subdir = "inferred" if prov == "inferred" and not scoped else subdir
        if scoped:
            source_id = hashlib.sha256(str(node.get("source_file", "")).encode()).hexdigest()[:20]
            eff_subdir += "/" + source_id
            if prov == "inferred":
                eff_subdir += "/inferred"
        meta = {"type": _clean(node.get("kind")) or "concept",
                "title": _heading_title(node, label),
                "provenance": prov,
                "confidence": conf,
                "tags": (["tree-sitter", lang] if prov == "extracted"
                         else ["llm-enrich", lang])}
        if node.get("source_file"):
            meta["resource"] = _clean(node["source_file"])
            if _is_test_path(meta["resource"]):
                meta["tags"].append("test")
        body_lines = []
        content = _body(node.get("body"))
        if content:
            body_lines += [content, ""]
        return f"{eff_subdir}/{lang}", meta, body_lines

    return write_notes(bundle, nodes, edges_by_src, plan, qualify_links=scoped)


# -- the later, optional LLM enrichment layer (see enrich.py) ----------------

def enrich(graph: dict, chat=None) -> dict:
    """Return the deterministic graph with optional inferred additions.

The function imports :mod:`muninn.enrich` lazily. Extracted nodes and edges
remain unchanged, while accepted inferred concepts and cross-file edges use
weight 0.5. Missing or failed providers return the base graph.
    """
    from . import enrich as _enrich
    return _enrich.enrich(graph, chat=chat)
