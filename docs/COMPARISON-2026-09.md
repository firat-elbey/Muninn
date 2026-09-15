# Muninn competitor and context review, 7 September 2026

Muninn now offers optional compact note excerpts and stricter pack budgets. These changes retain its standard-library core and existing privacy boundary. Full conversation compression requires a separate, explicit input contract.

## Method and product identity

This review inspected first-party repositories and documentation on 7 September 2026. It also read Muninn's existing landscape and prior-art reports. Competitor benchmark numbers below are publisher reports. The competitive research did not execute competitor systems, reproduce their numbers, or send private data to them. Links to moving branches describe the inspected documentation, not a pinned runtime comparison.

GBrain resolves to [garrytan/gbrain](https://github.com/garrytan/gbrain). The user's name "Graphy" remains ambiguous. [Graphy SDK](https://docs.graphy.dev/agents/overview) describes chart agents. [Graphy AI Brain](https://graphy.com/us/ai-brain/) describes creator knowledge shared across its products. Graphify is the relevant code-graph comparator already used by Muninn. It is included explicitly as that interpretation, without asserting that the user meant it. Graphiti is a separate temporal-memory system.

## Current capabilities and evidence

| System and first-party source | Documented capability relevant to Muninn | Published result and limit | Useful design lesson |
|---|---|---|---|
| [GBrain](https://github.com/garrytan/gbrain), inspected 7 September 2026. | Markdown authority, PGLite or PostgreSQL retrieval, typed graph links, synthesis with citations and gaps, and remote client scoping. | Its README reports 49.1% P@5 and 97.9% R@5 on a 240-page model-generated corpus. These are retrieval scores, not task completion or a Muninn comparison. | Retrieval can report missing evidence explicitly. A narrow agent interface reduces tool-selection overhead. |
| [GBrain memory protocol](https://github.com/garrytan/gbrain/blob/master/docs/protocol/MEMORY_VERBS_v1.md), inspected 7 September 2026. | Seven MCP verbs include deterministic context packs and change delivery. Responses expose consumed budget, dropped items, provenance, and errors. Change cursors advance only through delivered records. | The protocol distinguishes conformance tests from ranking quality. No token-saving result was independently reproduced here. | Add inspectable budget metadata. If incremental delivery is added, preserve undelivered items and namespace cursors by authenticated client and session. |
| [Graphify](https://github.com/Graphify-Labs/graphify), inspected 7 September 2026. [Benchmark report](https://github.com/Graphify-Labs/graphify/blob/v8/BENCHMARKS.md), dated 5 July 2026. | Deterministic source parsing, scoped subgraphs, paths, source references, confidence labels, broad language support, and assistant skills. | It reports 45.3% QA on 300 LoCoMo questions and 76% on 50 LongMemEval-S questions. Its memory harness uses Kimi K2.6. Scores do not establish performance on the complete datasets. | Reuse bounded source structure where Muninn's existing evaluations support it. Preserve the strongest lexical result rather than making graph expansion universal. |
| [Graphiti and Zep](https://github.com/getzep/graphiti), inspected 7 September 2026. | Graphiti supports temporal fact validity, provenance episodes, incremental construction, and hybrid retrieval. Zep adds managed user, thread, and context assembly services. | Its repository states that Graphiti deployment performance depends on the chosen implementation. Managed Zep latency claims do not transfer to self-hosted Graphiti. | Preserve historical facts and distinguish event time from recording time. Muninn already has relevant validity and supersession fields, so test their behavior before adding another representation. |
| [Mem0 algorithm report](https://mem0.ai/blog/mem0-the-token-efficient-memory-algorithm), dated 16 April 2026. | Single-pass additive extraction, historical fact retention, entity linking, and fused semantic, keyword, and entity retrieval. | It reports 92.5% LoCoMo with 6,956 mean tokens and 94.4% LongMemEval with 6,787. The report explicitly attributes these scores to managed-platform optimizations absent from the open-source SDK. | Evaluate evidence coverage per token. Keep factual changes append-only and make current validity explicit. |
| [Mastra observational memory](https://mastra.ai/docs/memory/observational-memory), inspected 7 September 2026. | An Observer compresses messages and a Reflector consolidates observations. Stable appended observations support prompt caching. Optional recall recovers exact source-message ranges without a vector store. | The documentation reports typical compression of 5 to 40 times. Activation thresholds are not strict caps. Model work and storage remain part of the total cost. | Keep exact recoverable sources behind compact views. Preserve a stable prefix and append recent material when practical. |
| [Mastra research announcement](https://mastra.ai/blog/observational-memory), dated 9 February 2026. | The published experiment studies the observation architecture on LongMemEval. | It reports 94.87% with gpt-5-mini and 84.23% with gpt-4o. These model-dependent scores are not directly comparable with Graphify's subset or Mem0's managed deployment. | Run the same answering model, data split, and token budget across variants before attributing gains to memory. |
| [Letta Agent SDK memory](https://docs.letta.com/agent-sdk/memory), inspected 7 September 2026. | MemFS stores Markdown in an agent-owned Git repository. System files remain in context, while other files are opened on demand. Background dreaming can follow step or compaction triggers. | No comparable measured compression result was established by the inspected page. Older memory-block documentation belongs to the legacy SDK. | A compact directory or manifest can defer detailed retrieval. Current comparison prose should include MemFS instead of describing only legacy blocks. |
| [Basic Memory](https://github.com/basicmachines-co/basic-memory), inspected 7 September 2026. | Markdown authority, MCP, optional semantic retrieval and reranking, optional cloud service, and progressive tool discovery. | The README does not establish a directly comparable token-saving result. | Reduce the tool schema and initial instruction surface as well as returned note text. |
| [AJNT](https://ajnt.ai/), inspected 7 September 2026. | A model gateway claims task-aware history selection, repeated-read removal, task restoration, and cache-aware context placement. | The landing page claims 47% fewer tokens and 1.3 times completion on SWE-bench Verified. Its full-report link returned the same landing page section. Sample size, run artifacts, and complete methodology were not established. | Evaluate duplicate tool-output removal and complete net cost. Treat the numerical claims as unverified marketing evidence until a reproducible report is available. |

## Changes implemented in this pass

The optional `pack --compact` and `prime --compact` modes select exact note-body excerpts without including the bundle index. They retain complete short notes when the budget permits. Longer notes use complete blocks, adjacent context, and preceding ATX heading ancestry. Each excerpt identifies its note and body-line range. These are lines within the parsed note body, not absolute file-line numbers. Selection reasons remain visible. The implementation is in [recall.py](../src/muninn/recall.py).

Compact selection performs no model calls. A primary block can use additional available space when its initial allocation is insufficient. When no complete excerpt fits, the output attempts to provide a recovery reference. That reference identifies the note and explains that a full read is required. Very small budgets can omit even this reference and produce empty output. Excerpts remain data. Block selection cannot guarantee preservation of every distant qualification, and heading recognition does not cover every Markdown heading form.

The renderer now includes headings, selection reasons, indexes, and truncation notices within the pack budget. Its contract uses the existing estimate of four characters per token. This estimate is not a model tokenizer. Recall events are recorded only for admitted note entries, rather than every ranked candidate.

The pack budget does not bound the complete output of `prime` or the SessionStart hook. Continuity, corrective lessons, learned preferences, and concurrent-work notices are separate sections. They can increase the injected context beyond the requested pack budget. A shared budget across those sections remains a recommendation.

[Regression tests](../tests/test_compact_pack.py) cover compact evidence, adjacent qualifications, heading scope, large primary blocks, no-fit disclosure, and rendered budget limits. The separate [context-efficiency evaluation](../eval/context_efficiency.py) compares pack sizes and required evidence spans. It reports character estimates and optional exact counts for the named `cl100k_base` tokenizer. It does not measure generated-answer accuracy, billed usage, complete operational cost, or agent task completion.

## Remaining recommendations

The implemented excerpt mode provides a bounded first retrieval step. The following improvements still require implementation or further evaluation.

1. Expose consumed budget, omitted evidence, the estimation method, and available recovery paths as machine-readable metadata. Allocate a shared budget across complete priming output.
2. Add an explicit expansion interface for source references. Measure whether smaller initial excerpts cause additional reads that offset their savings.
3. Evaluate session-aware duplicate suppression using source content hashes and context generations. Reset delivery state after compaction. A delivery cursor must preserve omitted items and remain separate from evidence of independent use.
4. Expose a small stable tool surface through adapters. Keep administration and graph maintenance discoverable on demand. Measure tool-schema tokens separately from memory-pack tokens.
5. Evaluate cache behavior alongside context size. Stable prefixes may reduce billed input cost even when their literal token count remains unchanged.

The context compiler could consume Muninn's bounded evidence through a shared interface. Its separate opt-in input could contain user-approved session messages and tool events. It could retain exact task constraints and recent messages while reducing older material. Such an integration must preserve role boundaries, tool-call identifiers, source references, and current corrections.

Muninn's ordinary hooks currently accept only a narrow file-event surface. Adding automatic raw transcript collection would change a documented invariant. Model observation, background dreaming, and a hosted proxy therefore need a distinct mode and an explicit authority boundary. This comparison supports designing that integration. It does not establish that blanket transcript capture improves Muninn.

## Muninn evidence and comparison limits

Muninn's previously reproduced retrieval and dependency results are recorded in the [evidence report](EVIDENCE.md). They use different tasks and conditions from the publisher benchmarks above. The updated [SWE-bench analysis](../eval/code_retrieval/RESULTS-swebench-lite-300.md) was regenerated from existing result records. That regeneration was not a new retrieval run.

The new context-efficiency harness measures evidence availability and rendered-pack token counts on public synthetic fixtures. Its results must remain separate from competitor QA scores and end-to-end task completion. Named-tokenizer counts describe the measured strings, not every model's token usage. A claim that Muninn lowers complete operational cost or outperforms another product requires matched execution and complete cost accounting.

## Evaluation requirements

Use matched baselines and report gains with their corresponding quality changes. A smaller prompt can cause repeated retrieval, longer answers, or failed tasks.

| Evaluation | Required comparison and measurements |
|---|---|
| Note evidence selection | Compare prefix truncation, full-note retrieval, and focused excerpts at fixed budgets. Measure answer-bearing evidence recall, exact fact retention, output size, and latency. Include late facts, repeated boilerplate, tables, code, negation, and superseded decisions. |
| Session continuation | Compare exact history, baseline compaction, compiler-only context, and compiler plus Muninn evidence. Measure task success, correction retention, chronology, resumption after compaction, and exact-source recovery. |
| Net token cost | Count primary input, cached input, output, compiler or observer calls, retrieval expansion, retries, and tool schemas. Report ingest cost separately from per-query cost. |
| Isolation and authority | Test separate users and sessions, changed source hashes, stale cursors, forged references, untrusted retrieved instructions, and suppression reset after compaction. |
| Competitive comparison | Pin each adapter and dependency. Use the same corpus, answering model, judge, tool budget, and token accounting. Publish failed runs and unsupported configurations. |

The published competitor scores identify mechanisms worth testing. They do not establish a product ranking. Muninn's public claims should remain limited to its reproduced evaluations until the matched comparisons exist.
