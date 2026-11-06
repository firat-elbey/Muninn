# Competitive landscape, August 2026

This review compares documented mechanisms, not product quality. It does not
place unlike systems on one benchmark or infer answer quality from an
architectural feature. The source revisions below were reviewed on 26 August
2026.

| System | Reviewed revision |
|---|---|
| [GBrain](https://github.com/garrytan/gbrain) | `77bb9d8c2165a8eb3f15e117462fcf1164fc4c0a` |
| [Graphify](https://github.com/Graphify-Labs/graphify) | `43d54acbfa9e731f7a592bb582c1f4b9d48ed73e` |
| [Basic Memory](https://github.com/basicmachines-co/basic-memory) | `c0bd87c6d5a4a58034b1d6c8c5018e443b0bd048` |
| [Graphiti](https://github.com/getzep/graphiti) | `683a8539c8925de69071a1305dc8bf0e52e17c65` |

## Mechanism comparison

The comparison uses six properties: whether editable files are authoritative;
whether independent use changes later retrieval; whether source structure is
mapped without a model; whether adaptive state is separable by consumer;
whether retrieval is bounded and explained; and whether a useful local mode
works without an API key. `Partial` means that the system implements a related mechanism with a different boundary.

| System | Files authoritative | Independent use changes retrieval | Deterministic source map | Separate adaptive state | Bounded explained retrieval | Local mode without key |
|---|---|---|---|---|---|---|
| Muninn | Yes | Yes | Yes | Yes | Yes | Yes |
| GBrain | Yes; a Markdown repository is the system of record, with PGLite or Postgres for retrieval | No documented access-based adjustment | Partial; deterministic links and schema rules | No documented per-consumer ranking state | Yes | Yes; local PGLite supports keyword retrieval |
| Graphify | No; `graph.json` is derived output | Partial; recorded outcomes produce a learning overlay | Yes; code uses local tree-sitter analysis | No | Yes | Yes for code-only analysis |
| Basic Memory | Yes; Markdown is paired with a derived SQLite or Postgres index | No documented access-based adjustment | Partial; Markdown relations and wikilinks form the graph | No | Partial | Yes; SQLite and local models are supported |
| Graphiti | No; a graph database is authoritative | Partial; temporal invalidation changes current facts, not access preference | No; graph construction uses a model | No | Partial | Partial; self-hosting still requires graph and model infrastructure |

## Findings

The reviewed systems now overlap more than an earlier survey indicated.
GBrain supports keyless local operation and explained hybrid retrieval.
Graphify records explicit query outcomes and converts them into a learning
overlay. Basic Memory supports local semantic search and reranking. These are
material capabilities and are reflected in the table.

Muninn's narrower distinction is the combination of portable OKF Markdown with
a removable, per-consumer usage ledger. Independent reads and edits affect
later ranking within fixed bounds. The ledger also records corrections,
co-use, outcomes, and goals, while each selected note states why it was
included. None of the reviewed systems documents that complete combination.
This observation does not establish that Muninn answers questions better.

The systems also optimize for different tasks. GBrain includes synthesis,
connectors, jobs, and a database-backed graph. Graphify provides broad and
deep source analysis. Basic Memory provides a mature Markdown and MCP
workflow. Graphiti provides explicit temporal fact history. Muninn favors a
small standard-library core, auditable adaptive retrieval, and a sidecar that
can be deleted without changing the knowledge files.

## Claim limits

Public claims should report measured Muninn behavior. The committed evidence
supports the following statements:

- A 400-token adaptive pack answered 16 of 18 questions in the repository's
  40-note synthetic evaluation; the whole-corpus condition answered 6.
- The selected source retriever reached 44.7 percent hit@1 and 80.0 percent
  hit@10 on 300 SWE-bench Lite file-localization cases.
- Direct graph fusion reduced hit@1 and therefore did not become the default.

These results do not establish end-to-end task completion, answer quality on
unseen corpora, or superiority over the systems above. The
[evidence report](EVIDENCE.md) states the measured results and their limits.
