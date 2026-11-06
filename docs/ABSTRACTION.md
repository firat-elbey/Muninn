# The learning substrate

- **Status:** implemented architecture.
- **Components:** OKF notes, deterministic extraction, the OKF-M profile,
  use-adaptive recall, and a separable `.muninn/` sidecar.

## Definition

Muninn is a learning substrate for agent knowledge. Markdown files remain the
authoritative knowledge. Deterministic parsers can derive structure from source
files, and optional model enrichment can add explicitly inferred structure. A
separate sidecar records how one consumer uses the knowledge and changes recall
order within fixed bounds. Removing every derived component leaves a readable OKF bundle.

This design separates three classes of state. Shared knowledge belongs in
Markdown. Rebuildable indexes belong in derived storage. Per-consumer evidence
of use belongs in the sidecar. This separation makes provenance, deletion, and sharing behavior explicit.

## Architecture

The substrate has five layers.

1. **Knowledge files.** An OKF v0.2 bundle stores Markdown, YAML frontmatter,
   and wikilinks. People can edit the files directly, Git can compare them, and
   Obsidian can open the bundle without conversion.
2. **Deterministic extraction.** Tree-sitter and format-specific parsers derive
   symbols, headings, configuration keys, and typed relationships. Extracted
   nodes have `provenance: extracted` and edge weight 1.0. Optional Graphify
   import can add deeper structural relationships.
3. **Optional model enrichment.** A model may add concepts and cross-file
   edges with `provenance: inferred` and edge weight 0.5. Enrichment is additive
   and cannot replace extracted or curated content.
4. **Per-consumer memory.** The `.muninn/` sidecar records use, outcomes,
   corrections, goals, and co-use. The append-only ledger is authoritative;
   state and indexes are rebuildable. The sidecar can be removed without
   changing the knowledge bundle.
5. **Recall and serving.** Lexical evidence seeds a bounded walk over authored,
   extracted, inferred, and learned relationships. Fixed multipliers then
   reorder candidates. A context pack contains a compact index, selected notes, and an explanation for each selection.

## Governing properties

- **Files remain authoritative.** Extraction, indexes, usage state, and packs
  are derived from the files or ledger and can be rebuilt.
- **Model use remains optional.** Deterministic extraction and lexical recall
  work without a model, API key, or remote service.
- **Provenance remains visible.** Curated, extracted, and inferred content use
  separate labels and weights. An importer cannot overwrite curated or
  unlabeled notes.
- **Learning changes order within bounds.** Usage, goals, and graph support may
  reorder eligible notes but cannot hide a stronger direct match. Corrections
  can exclude obsolete notes because they change validity rather than
  preference.
- **Personal state remains separable.** Two consumers may use the same files
  with different sidecars. Sharing the files does not disclose either
  consumer's usage history.
- **Every selection remains inspectable.** A context pack states the cue,
  relationship, strength, or learned association that selected each note.

## Layer interactions

The layers exchange bounded, attributable signals. A new parser adds extracted
relationships to the graph, which can improve reach without changing recall
code. Notes used together gain a sidecar association, which can retrieve a note
that shares no words with a later cue. A superseding note changes admissibility
and weakens the obsolete note. A high-magnitude outcome strengthens recently
used evidence. Optional enrichment adds lower-weight relationships to the same
walk without changing extracted facts.

The design therefore supports progressive capability. Plain Markdown provides
the minimum useful form. Deterministic structure improves retrieval when a
grammar is available. Inferred structure adds concepts when a model is
configured. Usage evidence personalizes ranking after independent use. Each
additional layer can be removed without invalidating the layers beneath it.

The [specification](../SPEC.md) defines the runtime contract. The
[OKF-M profile](OKF-M.md) defines the interoperable extensions, and the
[evidence report](EVIDENCE.md) states which measured claims the current
implementation supports.
