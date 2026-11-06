# OKF-M memory profile for OKF v0.2

- **Status:** draft 0.1.
- **Base format:** [OKF v0.2](https://github.com/GoogleCloudPlatform/open-knowledge-format).
- **Reference implementation:** [Muninn](../SPEC.md).

The terms MUST, MUST NOT, SHOULD, SHOULD NOT, and MAY are interpreted as
described in [BCP 14](https://www.rfc-editor.org/info/bcp14) when they appear in
capital letters.

## 1. Scope

OKF v0.2 defines Markdown knowledge files, YAML frontmatter, wikilinks,
reserved files, and permissive handling of extensions. OKF-M adds optional
signals for correction precedence, provenance, fact stability, consumer-side
usage, and budgeted serving. A consumer may ignore the complete profile and
continue to read a conforming bundle as ordinary OKF.

The profile addresses four conditions that representation alone does not
resolve. A bundle may contain both an obsolete fact and its correction. Human,
deterministic, and model-written notes may require different treatment. Some
facts expire faster than others. Finally, evidence about one consumer's use
must not alter shared knowledge.

In Muninn's 55-question LongMemEval-S pilot, supersession-aware retrieval
answered 13 of 15 knowledge-update questions, compared with 10 of 15 for flat
retrieval over the same notes and 0 of 15 without context. This small sample
supports the correction mechanism on its named task; it does not establish a general accuracy improvement.

## 2. Profile fields

All fields are optional. Producers MAY emit any subset, and consumers MUST NOT
reject a bundle because it contains them.

### 2.1 `supersedes`

`supersedes` is a list of bundle-relative paths that the current note corrects.

- A producer MUST record a correction in a new note and MUST NOT edit or delete
  the old note solely to record that correction.
- A consumer MUST prefer the superseding note and SHOULD exclude superseded
  notes from default serving. It MAY include them when the request concerns
  history.
- A consumer MUST tolerate an unresolved target or cycle.

```yaml
type: note
title: DB port moved to 7433
supersedes: [infra/db-port.md]
```

### 2.2 `provenance`

`provenance` is `curated`, `extracted`, or `inferred`.

- An importer MUST label deterministic output `extracted` and model output
  `inferred`. It MUST NOT label imported output `curated`.
- An importer MUST NOT overwrite a `curated` or unlabeled note. An absent value
  is treated as `curated` for ownership.
- A prompt assembler SHOULD treat extracted and inferred bodies as data rather
  than instructions and MAY display provenance in selection evidence.

```yaml
type: concept
title: Payment retry flow
provenance: extracted
```

### 2.3 `pinned`

`pinned` is a boolean.

- An L2 consumer MUST keep a pinned note at or above its documented recall
  floor.
- Pinning MUST NOT admit a note that is unrelated to the cue.

```yaml
type: policy
title: Friday deployment restriction
pinned: true
```

### 2.4 `stability`

`stability` is `durable` or `perishable`. An absent value makes no claim.

- A producer SHOULD mark rapidly changing facts, including ports, versions,
  addresses, and prices, as `perishable`.
- An L2 consumer SHOULD identify a perishable note recalled after prolonged
  dormancy so that the fact can be verified. It MUST NOT suppress the note without reporting the suppression rule.

```yaml
type: note
title: Aurora DB port
stability: perishable
```

### 2.5 `valid_at` and `invalid_at`

These fields are ISO 8601 bounds on the interval during which a claim is true
in world time. Git and the sidecar continue to record when the claim was
written or observed.

- A producer SHOULD close an expired claim with `invalid_at` or a superseding note instead of deleting the claim.
- A consumer MAY exclude or annotate a note whose interval does not cover the
  requested time. It MUST NOT reject the note because either field is present.

```yaml
type: note
valid_at: 2026-01-01
invalid_at: 2026-06-30
```

### 2.6 `aliases`

`aliases` is a list of alternative titles.

- A consumer that resolves wikilinks SHOULD match aliases in addition to the title and basename.
- If several notes claim one alias, the consumer MUST resolve the ambiguity
  deterministically or leave it unresolved. It MUST NOT reject the bundle.

```yaml
type: concept
title: Kubernetes
aliases: [k8s, kube]
```

### 2.7 Typed links

A list line that contains only a relation, a wikilink, and an optional
parenthesized confidence word declares a typed edge.

```markdown
- imports [[Parser]] (extracted)
- refines [[Old design]] (inferred)
- [[Glossary]]
```

- A missing relation defaults to `related_to`, and a missing confidence value
  defaults to `extracted`.
- A confidence value MUST be one word. A parenthetical phrase that contains
  whitespace remains ordinary prose.
- The weights are 1.0 for `extracted`, 0.5 for `inferred`, and 0.2 for
  `ambiguous`. An unknown value SHOULD receive weight 0.5 and MUST NOT cause
  rejection.
- An unresolved target MUST NOT cause rejection.
- The target MUST remain visible as an ordinary wikilink to an L0 consumer.
- A producer SHOULD write one edge per line to preserve readable diffs and deterministic parsing.

## 3. Per-consumer sidecar

Consumer usage belongs in a dot-directory named for the consumer, such as the
reference `.muninn/` directory. It MUST NOT be written into shared knowledge
files.

1. Deleting the sidecar MUST leave a valid OKF bundle. A consumer without a
   sidecar MUST read every note at baseline strength.
2. The sidecar ledger MUST be append-only and authoritative. A reversal is a
   new event, not an edit to an earlier event.
3. A tool MUST preserve and ignore unknown event kinds.
4. Derived state MUST be rebuildable by replaying the ledger and safe to
   delete.
5. A sidecar SHOULD remain uncommitted by default. Different consumers MAY
   retain different sidecars over the same knowledge.
6. The sidecar MUST NOT contain OKF knowledge files.

The profile does not require Muninn's event vocabulary. A consumer may define
its own events if it preserves the rules above.

## 4. Budgeted context packs

An L1 or L2 consumer serves a Markdown context pack under an explicit token
budget. The pack contains a compact bundle index and detailed notes selected for the cue.

```markdown
# Context pack: <cue>
## Index
- Title (path): description
## Focus
### Title (path)
*why loaded: cue-match 2.1; strength 0.62*
<body, truncated to fit>
```

- The index SHOULD identify relevant bundle contents beyond the focused notes
  and SHOULD use no more than about one quarter of the budget.
- Every focused note MUST state why it was included. The statement SHOULD name
  the match, relationship, strength, or history that affected selection.
- Focus MUST exclude a superseded note by default. The index SHOULD label it
  `[superseded]`, and a historical request MAY include it.
- Usage signals SHOULD reorder relevant candidates but MUST NOT make a stronger direct match unreachable.
- A pack SHOULD be deterministic for a fixed bundle and sidecar state.

## 5. Conformance levels

- **L0 reader.** The consumer reads OKF files and ignores profile fields. Every
  profile-conforming bundle MUST remain readable at L0.
- **L1 pack consumer.** The consumer serves budgeted context and honors
  `supersedes`. It MUST NOT answer from a note known to be superseded unless the
  request concerns history.
- **L2 memory writer.** The consumer maintains a sidecar, preserves unknown
  events, rebuilds derived state, honors pinned floors, and identifies
  perishable facts that require verification.

## 6. Compatibility and versioning

Every field in Section 2 uses OKF v0.2's extension mechanism. Removing the
fields and sidecar leaves ordinary OKF. The profile does not edit note prose;
corrections are new notes and usage remains in the sidecar.

The root `index.md` MAY declare `okf_m: "0.1"` beside `okf_version`, but a
consumer MUST NOT require the declaration. A minor profile version may add
fields or event kinds if an older consumer can preserve and ignore them.
