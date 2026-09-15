# Draft proposal for an OKF-M profile

This draft proposes optional Open Knowledge Format extensions for corrections,
provenance, and consumer-side memory. It does not change OKF v0.2 file rules.
An OKF consumer may ignore every proposed field and continue to read the Markdown bundle.

The [OKF-M profile](OKF-M.md) contains the normative draft. Muninn is the reference implementation.

## Proposed fields and conventions

### 1. Corrections

`supersedes` lists bundle-relative notes that a new note corrects. A consumer
should prefer the superseding note and exclude the obsolete note from default
serving. The old note remains unchanged, which preserves history.

This field addresses a specific failure: a bundle can contain both an old fact
and its correction without defining precedence. In Muninn's LongMemEval-S
knowledge-update sample, supersession-aware retrieval answered 13 of 15
questions, compared with 10 of 15 for flat retrieval over the same notes.

### 2. Provenance

`provenance` takes one of three values: `curated`, `extracted`, or `inferred`.
An importer must not overwrite a curated or unlabeled note. This distinction
lets a consumer separate human statements, deterministic extraction, and model
inference. It also prevents a repeated import from replacing authored content.

### 3. Typed links

A list line containing a relation, wikilink, and optional confidence word may
declare a typed edge. For example:

```markdown
- imports [[Parser]] (extracted)
```

The proposed confidence weights are 1.0 for `extracted`, 0.5 for `inferred`,
and 0.2 for `ambiguous`. A consumer that does not understand typed edges still
sees an ordinary wikilink.

### 4. Consumer sidecars

A reserved `.<consumer>/` directory may contain an append-only event ledger
and rebuildable derived state. The sidecar stores consumer-specific evidence,
including use, outcomes, corrections, and goals. Deleting it leaves a valid
OKF bundle. Unknown event kinds must be preserved and ignored.

### 5. Budgeted context packs

A context pack is a Markdown document assembled under an explicit token
budget. It contains a compact bundle index and detailed notes selected for the
cue. Each selected note states why it was included, and superseded notes are
excluded by default.

In Muninn's 40-note synthetic evaluation, a 400-token adaptive pack answered
16 of 18 questions. A whole-corpus condition answered 6 of 18 while using more
than twice as many tokens. This result applies only to the named synthetic task and answer model.

### 6. Recall policy

`pinned: true` sets a floor below which a policy or safety note cannot decay.
Pinning does not force the note into an unrelated result.

`stability` distinguishes `durable` and `perishable` facts. A consumer should
identify a perishable note recalled after prolonged dormancy so that the fact
can be verified.

`valid_at` and `invalid_at` record optional ISO 8601 bounds for world-time
validity. Git and the sidecar continue to record when the claim was written or
observed.

`aliases` lists alternative titles for wikilink resolution.

### 7. Bundle composition

A knowledge space may be the query-time union of several OKF bundles. The
union is not a new format. Each constituent remains independently valid, and
the composition layer carries source identity without modifying the files.

## Implementation findings

Three constraints proved necessary in the reference implementation.

1. Signals above lexical relevance must remain bounded. An earlier unbounded
   usage multiplier hid relevant untouched notes.
2. Model additions must remain additive. Re-running deterministic extraction
   must retain inferred content without allowing it to replace extracted or
   curated facts.
3. Serving cannot count as evidence of usefulness. Only independent actions,
   such as reading, editing, citing, or recording an outcome, may change later
   retrieval.

The evidence does not show that every proposed field belongs in OKF core. It
shows that these fields can coexist without changing the base representation.

## Questions for upstream review

1. Should `supersedes`, `provenance`, and `aliases` be core fields or profile
   fields?
2. Should OKF define a typed-link line that remains an ordinary wikilink to a
   basic consumer?
3. Should OKF reserve consumer dot-directories, or should each profile define
   its own sidecar?
4. Should the Open Knowledge Format repository maintain a registry of compatible
   profiles?
