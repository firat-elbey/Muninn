# Prior art

This review identifies mechanisms that influenced Muninn's design. It is not a
market ranking. Product capabilities change, so the dated
[landscape review](LANDSCAPE-2026-08.md) governs current comparative claims.

## Portable knowledge files

[Open Knowledge Format](https://github.com/GoogleCloudPlatform/open-knowledge-format)
defines a portable Markdown bundle with YAML frontmatter and links. Its main
contribution is interoperability: a producer can add fields while a basic
consumer continues to read the files. Muninn adopts OKF as its knowledge
format and adds consumer behavior through the OKF-M profile.

[llms.txt](https://llmstxt.org/) defines a compact Markdown index of resources
for model consumption. Its optional section provides a simple priority signal
when context is limited. Muninn uses the same general separation between a
compact index and selected detailed content, but it derives selection from a
cue, graph structure, and bounded usage evidence.

[Karpathy's LLM Wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
compiles sources into an interlinked Markdown wiki under model-maintained
rules. Implementations such as
[Pratiyush/llm-wiki](https://github.com/Pratiyush/llm-wiki) and
[nashsu/llm_wiki](https://github.com/nashsu/llm_wiki) apply this pattern to
session histories and personal documents. These systems show the value of
persistent synthesis, source links, and human review. Muninn uses a narrower
model role: enrichment may add inferred concepts and edges, but it cannot
replace deterministic or curated content.

[Basic Memory](https://github.com/basicmachines-co/basic-memory) also treats
Markdown as authoritative and derives an index from links and observations.
It demonstrates direct interoperability between editable notes and agent
tools. Muninn differs by storing per-consumer usage separately and applying that evidence to later ranking.

## Deterministic and temporal graphs

[Graphify](https://github.com/Graphify-Labs/graphify) derives a graph from
source files through tree-sitter and can add model-inferred concepts,
relationships, and communities. It distinguishes extracted, inferred, and
ambiguous edges. Muninn adopts the separation between deterministic and
inferred structure, imports Graphify output, and assigns explicit weights to
provenance classes. Muninn keeps the graph rebuildable rather than
authoritative.

[Graphiti](https://github.com/getzep/graphiti) stores episodes, entities, and
temporally valid fact edges in a graph database. New evidence can invalidate a
fact without deleting its history. This distinction between record time and
world validity informs Muninn's `supersedes`, `valid_at`, and `invalid_at`
fields. Graphiti addresses temporal correctness; Muninn separately records
whether one consumer used a note.

[GBrain](https://github.com/garrytan/gbrain) combines a Markdown corpus,
typed links, reciprocal-rank fusion, PostgreSQL-backed retrieval, and scheduled
model maintenance. Its compiled summary plus append-only history demonstrates
a useful answer-and-evidence layout. Muninn shares the file-first and graph
principles but uses local rebuildable indexes for ordinary operation and keeps usage in a separate sidecar.

## Agent memory systems

[Engram](https://github.com/Gentleman-Programming/engram) stores structured
agent observations and sessions in SQLite with full-text search. Stable topic
keys let repeated observations update one conceptual record, while append-only
synchronization supports several environments. Its explicit observation types
and source references inform Muninn's continuity and provenance design.

[Letta](https://github.com/letta-ai/letta) separates always-visible memory
blocks from searchable external memory. The model decides when to edit or
retrieve memory, and context pressure determines what remains immediately
available. This design demonstrates the importance of a bounded working set.
Muninn instead makes selection deterministic and records user or agent actions
as explicit ledger events.

[Mem0](https://github.com/mem0ai/mem0),
[Cognee](https://github.com/topoteretes/cognee), and hosted memory services
use vector or graph stores to extract, consolidate, and retrieve memories.
These systems reduce application integration work and can use model judgment
to curate facts. Muninn chooses a different authority boundary: users can read
and edit the knowledge files directly, and no remote store is required.

## Mechanisms adopted by Muninn

The review supports six design choices.

1. **A portable knowledge layer needs a narrow format.** Markdown,
   frontmatter, and links remain useful without Muninn. Unknown extensions do not invalidate a bundle.
2. **Derived structure needs provenance.** Deterministic edges, inferred
   edges, and curated facts have different evidential status and must remain
   distinguishable.
3. **Corrections need explicit precedence.** Append-only history is useful
   only when a consumer can identify the current claim. `supersedes` provides
   that rule without deleting the old note.
4. **Context requires a budgeted serving contract.** Returning an index and
   selected evidence is more useful than exposing a database or an unbounded
   corpus. Each selection should state why it was included.
5. **Usage is consumer state.** Frequency, outcomes, goals, and co-use belong
   to the consumer that generated them. They should not alter shared knowledge or silently affect another consumer.
6. **Learning must remain bounded and auditable.** Serving is not evidence of
   usefulness. Independent use may adjust order, but the adjustment must be
   inspectable, replayable, and unable to hide a stronger direct match.

## Limits of the comparison

This note compares architecture, not answer quality or total cost. The systems
use different corpora, models, deployment assumptions, and evaluation tasks.
Muninn's committed evaluations establish results only for the named datasets
and configurations in [EVIDENCE.md](EVIDENCE.md). Claims about another system
should be checked against its current primary documentation before
publication.
