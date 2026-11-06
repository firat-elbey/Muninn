# Hybrid retrieval for source code and personal knowledge

- **Status:** The default lexical path and persistent index are implemented.
- **Scope:** Source indexing, candidate retrieval, graph expansion, personal ranking, and context assembly.
- **Decision:** Use BM25F and exact symbols for ordinary code localization. Use the code graph for related context and explicitly relational queries. Apply learned usage only after retrieval, within fixed bounds.

## Problem

Muninn currently retrieves Markdown notes through field-weighted token overlap and a bounded graph walk. This design works when notes already contain the right words and relations. It performs poorly when a new repository contains raw code but no authored knowledge graph. The existing build process also copies extracted symbols into Markdown notes. One monorepo produced 225,000 notes, although the source files already contained the authoritative code.

A combined system must preserve Muninn's portable files and separable personal memory while adding the retrieval quality of a conventional search index and the relational reach of a code graph. These mechanisms must remain testable in isolation. A graph, embedding model, or usage history must not become a hidden requirement for basic retrieval.

## Goals

The system must locate relevant source files from ordinary task descriptions, retrieve connected files for multi-file reasoning, improve from independent evidence of use, explain each selection, and remain useful without a model or remote service. Source files and Markdown notes must remain authoritative. Every index and learned score must be rebuildable or replayable.

The first release will not attempt autonomous patch generation, unrestricted model-written memory, enterprise access control, or a single score trained across private user data.

## System boundary

Muninn will treat a mounted repository as a source, not as material that must be copied into the knowledge bundle. A rebuildable source index will store file statistics, symbols, locations, and typed relations. The existing `.muninn/` ledger will continue to store per-consumer events such as use, outcomes, corrections, goals, and co-use.

The query path has six stages:

1. The query is normalized into words, identifiers, quoted spans, and optional relational cues.
2. Independent retrieval arms produce candidate rankings.
3. Reciprocal-rank fusion combines lexical and exact candidates without comparing incompatible raw scores.
4. The graph supplies related context or a relational ranking when the query requests a relationship.
5. Personal signals reorder candidates within documented limits.
6. The pack contains primary files, related files, exact excerpts, and an explanation for each selection.

This boundary separates shared facts from personal relevance. Two agents can index the same repository while retaining different usage histories.

## Derived source index

The source index contains one record per file and structural records for detected symbols. File fields include the relative path, symbols, documentation, headings, and body text. Structural edges include `contains`, `imports`, `calls`, `inherits`, `implements`, `references`, and `configures`. Every edge records its source and provenance.

The standard-library profile indexes paths and text and extracts Python structure with `ast`. The optional code profile uses the existing tree-sitter layer across supported languages. Deeper Graphify-compatible imports may add call and community edges. Optional model enrichment may add inferred concepts, but it may not rewrite deterministic nodes or edges.

The index is stored under the derived sidecar rather than represented as generated Markdown notes. The implementation uses SQLite from the Python standard library and an internal compressed inverted index; it does not require FTS5. The database can be deleted and rebuilt. The ledger remains the only source of learned state.

## Retrieval arms

BM25F is the default lexical arm. It accounts for term rarity, repeated evidence, document length, and field importance. Paths and symbols receive more weight than source bodies. Test and fixture files remain retrievable but receive the existing bounded demotion unless the query explicitly concerns tests.

Exact retrieval ranks filenames, paths, classes, functions, configuration keys, and other identifiers. This arm catches literal names that may be diluted in a long issue description.

Graph retrieval begins from lexical or exact seeds and follows typed edges with fixed damping. It is not a substitute for lexical localization. The complete evaluation found that direct graph fusion increased hit@10 from 80.0% to 80.7%, but the paired difference was not reliable: six gains and four losses produced a two-sided exact p-value of 0.7539. Graph fusion reduced hit@1 from 44.7% to 25.3%, improving 18 cases and harming 76. The graph therefore serves two narrower purposes: it supplies companion files after a primary file is found, and it answers queries that explicitly ask about calls, dependencies, ownership, data flow, or another relationship.

Optional semantic retrieval remains an independent arm. A local embedding model may recover paraphrases, but semantic-only candidates cannot displace a strong literal match. Remote embedding requires explicit consent.

## Default ordering

Ordinary code localization follows a precision-first rule. BM25F supplies the first result because it raised hit@1 from 20.0% under distinct-token overlap to 44.7%. Reciprocal-rank fusion between BM25F and exact retrieval supplies the remaining candidates. This composition preserved the BM25F first result and raised hit@10 from 60.3% under overlap to 80.0%, with 60 paired gains and one loss. Its hit@10 difference from BM25F alone was smaller and was not statistically established: 14 gains and six losses produced a two-sided exact p-value of 0.1153. The hybrid is therefore the practical candidate, not a proven improvement over BM25F.

Graph-related files appear in a separate part of the context pack. This presentation tells the agent which file directly matched the task and which files are present because of a structural relationship. A later relational-intent rule may promote the graph arm, but only after a dedicated relational benchmark demonstrates that the rule improves retrieval.

## Personal learning

Personal learning acts after candidate generation. A source file or symbol receives a full-strength use event only when the agent independently reads, cites, edits, or verifies it. Serving the file is not evidence. Repeated misses may create a bounded query-to-file association. Repeated waste may apply one bounded demotion. Co-used files may gain a personal edge. Successful outcomes may strengthen recently used evidence.

These signals must not hide an untouched lexical match. Corrections and deprecations may exclude obsolete knowledge because they change admissibility rather than preference. Every learned adjustment remains visible in the explanation and replayable from the ledger.

## Context assembly

A code pack separates three forms of evidence:

1. **Primary files** contain the strongest direct matches.
2. **Related files** are connected by a named structural or learned relation.
3. **Knowledge notes** contain relevant decisions, lessons, constraints, or prior outcomes.

Each file excerpt includes its repository-relative path, symbol, line range, retrieval arm, and relation where applicable. The token allocator first protects the best primary excerpt, then covers distinct query facets, then adds related context. Duplicate implementations and test mirrors receive lower priority unless the query requests them.

## Evaluation requirements

The completed evaluation uses all 300 SWE-bench Lite issue descriptions and accepted patch files. It records the fixed dataset revision, complete top-20 outputs, paired tests, repository-level results, timings, and failure cases. The Lite split contains one patch file per case, so this evidence supports single-file localization only. RepoBench or an equivalent cross-file benchmark must measure structural retrieval. A longitudinal benchmark must train usage only on earlier sessions and test paraphrased, held-out tasks. An agent-level evaluation must hold the model, tool budget, repository revision, and context budget constant.

The report must include hit and recall at fixed ranks, mean reciprocal rank, token cost, build time, query latency, storage, graph coverage, and negative results. Source retrieval and answer generation must be reported separately.

## Implementation status and unresolved decisions

`muninn source search` now provides source retrieval without changing note
recall. It stores one index per canonical source directory under the
consumer's `.muninn/` sidecar, checks source freshness, rebuilds atomically,
and emits bounded Markdown or JSON. The installed agent protocol directs
agents to query note memory first and source retrieval second.

Graph fusion and personal ranking remain outside the default path. Direct
graph fusion did not improve ordinary file localization reliably, and the
tested personal file and directory signals did not improve chronological
SWE-bench recall. A later change may route explicitly relational queries to a
graph arm, but only after a dedicated evaluation supports that rule. The
complete file-localization result is recorded in
[`eval/code_retrieval/RESULTS-swebench-lite-300.md`](../eval/code_retrieval/RESULTS-swebench-lite-300.md).

## References

- Cormack, Clarke, and Buettcher, [Reciprocal Rank Fusion Outperforms Condorcet and Individual Rank Learning Methods](https://cormack.uwaterloo.ca/cormacksigir09-rrf.pdf), SIGIR 2009.
- Jimenez et al., [SWE-bench: Can Language Models Resolve Real-World GitHub Issues?](https://arxiv.org/abs/2310.06770), ICLR 2024.
- GBrain, [retrieval architecture](https://github.com/garrytan/gbrain/blob/master/docs/architecture/RETRIEVAL.md).
- Graphify, [repository and benchmark documentation](https://github.com/Graphify-Labs/graphify).
