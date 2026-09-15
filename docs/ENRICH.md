# Optional model enrichment (`muninn enrich`)

Muninn first extracts deterministic nodes and typed edges with no model. The
optional enrichment step then adds inferred concepts and cross-file edges
without rewriting extracted facts. [extract.py](../src/muninn/extract.py)
implements the deterministic pass, and [enrich.py](../src/muninn/enrich.py)
implements enrichment.

```
BUILD (non-LLM)      files ──tree-sitter/graphify──▶ extracted nodes + typed edges   (weight 1.0)
      │
ENRICH (optional LLM)   base graph ──model──▶ + inferred CONCEPT nodes (themes)      (weight 0.5)
      │                                       + inferred cross-file EDGES
      ▼
REMEMBER / SERVE     usage-strengthened recall over the whole graph, budgeted packs
```

## Added information

Given the extracted base graph, the model proposes two kinds of addition, both
stamped `provenance: inferred` / `confidence: inferred` (edge weight **0.5**):

1. **Concept nodes** represent themes that a parser cannot identify, such as
   an authentication flow or deployment pipeline. Each concept groups existing
   nodes, preferably across files, and becomes an OKF note under
   `inferred/concept/`. Its body contains a summary and
   `- relates_to [[member]] (inferred)` links to the base nodes it groups.
   This is the concept/entity page of Karpathy's LLM-Wiki pattern.
2. **Cross-file edges** represent inferred relationships such as `depends_on`,
   `relates_to`, and `documents` between existing nodes in different files.

The extracted base is never rewritten or removed.
Enrichment only appends. An extracted note may gain an *additional*
`(inferred)` edge line, but its frontmatter, prose, and extracted (weight-1.0)
edges remain unchanged. A Git diff therefore separates deterministic output
from model output, and the inferred layer remains reversible.

## Provider selection

Muninn selects the first configured provider in the following order.

**1. Offline agent handshake.** An agent that already has repository access
can request the task and apply a response. This path requires no server or API
key and sends no data itself.

```bash
muninn enrich ~/project --request              # prints the task: contract +
                                               # node handles + a digest
# the agent writes response.json (concepts + edges + the echoed digest), then:
muninn enrich ~/project --apply response.json --into ~/knowledge
# or as one pipe, with any prompt-in/reply-out CLI standing in as the agent:
muninn enrich ~/project --request | claude -p | \
    muninn enrich ~/project --apply - --into ~/knowledge
```

`--apply` extracts the base again. An unchanged tree produces the same handles.
Muninn rejects a response whose digest does not match because a source change
could cause a stale handle to identify the wrong node.
An explicit `--apply` exits with status 1 for unreadable, malformed, or stale responses.
This refusal leaves the destination notes and JSON output unchanged.
After a successful response check, `--into` creates a missing destination directory.

**2. Agent command.** A configured command supports scheduled or automated
runs.

```bash
export MUNINN_ENRICH_CMD='claude -p'          # or codex exec, ollama run …
export MUNINN_ENRICH_TIMEOUT=300              # seconds (default 300)
muninn enrich ~/project --into ~/knowledge
```

Muninn parses the command with `shlex`, starts it without a shell, writes the
prompt to standard input, and reads the reply from standard output. Graph text
therefore cannot be interpreted as a shell command.

**3. Local OpenAI-compatible server.**

```bash
# e.g. llama.cpp:  llama-server -m model.gguf --port 8080
export MUNINN_ENRICH_URL=http://127.0.0.1:8080/v1/chat/completions
export MUNINN_ENRICH_MODEL=your-model         # optional
export MUNINN_ENRICH_TEMPERATURE=0            # default 0 → deterministic re-runs
muninn enrich ~/project --into ~/knowledge
muninn build  ~/project --enrich              # one command: base + graphify + enrich
```

Without a provider, the command writes the deterministic base and reports
`+0 inferred`. Enrichment is optional. Every provider uses the same response
validation.

## Privacy: local by default, remote only by opt-in

The model receives handles, kinds, source paths, and labels. It does not
receive note bodies. The following controls govern this structural data.

- The handshake performs no network request; the selected agent already has
  repository access.
- `MUNINN_ENRICH_URL` must point at a **loopback** host (127.0.0.1 / localhost /
  ::1). A non-local host raises `PrivacyError` at config time. Override
  deliberately with `MUNINN_ENRICH_ALLOW_REMOTE=1` (prints one stderr line
  naming the host on each run). For a local endpoint the request refuses HTTP
  proxies and 3xx redirects, so content cannot egress via a configured proxy
  or a redirect off the pinned host.
- `MUNINN_ENRICH_CMD` runs a user-selected executable. Muninn cannot control
  whether that executable sends data elsewhere. Configuring the command is
  explicit consent to its behavior. Muninn selects the command before it
  validates a configured URL, so an unused URL cannot block the command or become an implicit fallback.

## Response validation

Muninn validates the reply before writing. It removes wikilink and frontmatter
metacharacters from labels and relations, requires edges to reference existing
base nodes, drops invalid handles and self-edges, caps additions with
`MAX_CONCEPTS` and `MAX_EDGES`, and ignores duplicates. Malformed or empty
output leaves the base graph unchanged.
Automatic provider failures can still produce the deterministic base.
Explicit `--apply` refusals do not write that base.
Explicit application also rejects malformed collections, non-object items,
and incorrect types in supported fields before writing outputs.
Optional fields can be omitted. Invalid handles remain filtered rather than
creating references to absent nodes.

## Implementation boundary

`extract.enrich(graph, chat=None)` imports this module lazily, which preserves
the standard-library core when enrichment is unused. `prompt_for()` supplies
one prompt to every provider. `resolve_chat()` selects the provider, while
`render_request()` and `apply_response()` implement the handshake. Every reply
passes through the same `_apply` sanitizer. Tests can inject any
`fn(system, user) -> str` implementation. [test_enrich.py](../tests/test_enrich.py)
covers a loopback HTTP server, subprocess providers, and the complete request
and apply sequence. `extract.import_graph` preserves each node's provenance and
confidence, and `store.py` parses inferred edges with weight 0.5.
