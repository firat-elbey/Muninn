# Muninn v0.1: specification

Muninn is a portable format for **personal knowledge that gets stronger with
use**. It separates two things every prior system fuses:

- **Knowledge**: an [OKF v0.2](https://github.com/GoogleCloudPlatform/open-knowledge-format)-conformant
  bundle of markdown notes with YAML frontmatter and wikilinks. Human-editable,
  git-diffable, shareable. The knowledge is the files.
- **Memory**: a *separable, per-consumer* memory sidecar (`.muninn/`):
  an append-only event ledger from which recall strength is derived. Using a
  note strengthens it, corrections outrank what they fix, strong outcomes
  mark recent work, and disuse lets a note fade. The memory is a consumer's
  usage record over the shared knowledge; it is not part of the knowledge.

**The interop invariant:** delete `.muninn/` and a valid Muninn bundle is a
valid OKF bundle. Any OKF/markdown tool can read the knowledge; only
muninn-aware consumers get usage-strengthened recall.

## 1. The bundle

A directory tree of `.md` files. OKF v0.2 rules apply:

- Every non-reserved `.md` file has YAML frontmatter with a non-empty `type`
  (the only always-required key in v0.2).
- `index.md` and `log.md` are reserved (directory listing / change log).
- Notes muninn writes stamp `generated: { by: muninn/<version>, at: <ISO> }`
  (v0.2 Section 5.2, actor convention Section 7) instead of the retired v0.1 `timestamp`;
  muninn as a consumer falls back to `timestamp` (v0.2 Section 13.1), so v0.1
  bundles load unchanged.
- The v0.2 trust/lifecycle families are parsed, preserved, and consumed:
  `status: deprecated` joins the superseded set (excluded from default
  recall: the producer-declared spelling of "no longer current"); a note
  past `stale_after` is served but its *why loaded* line says
  `STALE since <date>`; `sources`, `verified`, and `usage_window` round-trip
  verbatim (flow mappings parse to dicts).
- Links: `[[wikilinks]]` (resolved by title or basename) and standard
  markdown links. Broken links are tolerated. These links define the recall
  graph.
- Consumers preserve unknown fields and types. Files outside the resource
  limits below cause an explicit error rather than partial recall.

The Muninn consumer may exclude generated material through a root
`.muninnignore` file. Each UTF-8 entry is either an exact root-relative file
or, with a trailing slash, a root-relative directory tree. Blank lines and
lines beginning with `#` are comments, and surrounding whitespace is ignored.
Wildcards, negation, absolute paths, and parent traversal are invalid. The
file must be regular and no larger than 65,536 bytes. Muninn rejects an invalid
exclusion file and any attempt to write a note beneath an excluded path. This
consumer boundary does not change the underlying OKF bundle.

The default reader limits one note to 8 MiB and all loaded note files to 64
MiB. It also limits a bundle to 20,000 notes, 20,000 directories, 200,000
filesystem entries, and 64 directory levels. Mounted directories count toward
the same limits. Crossing a limit fails the load; it never produces an
unreported partial bundle.

The Muninn writer stores canonical root-relative `.md` paths that the
reader can load. It normalizes safe separators and `.` components before writing.
It rejects reserved files, hidden parent directories, drive
paths, filesystem aliases, and destinations that traverse a symbolic link.
Mounted directories and per-file symbolic-link views can contribute regular
Markdown files. Non-regular files are not notes, and every symbolic-link view
is read-only through the writer. The writer commits a verified private file by
atomic replacement. It refuses destinations with multiple hard links, preserving
the shared inode and every linked path.
Index generation uses the same replacement path. If the platform cannot pin
the destination directory through a descriptor, the writer fails before it
creates or replaces a file.

### Typed links

A body list line that is *exactly* a relation, a wikilink, and an optional
confidence declares a **typed, weighted edge**:

```
- <relation> [[Target]] (<confidence>)      e.g.  - imports [[Parser]] (extracted)
```

Both `<relation>` and `(<confidence>)` are optional: a bare `- [[Target]]`
means `related_to` / `extracted`. Confidence is a **single word**; a
multi-word parenthetical (`- read [[X]] (old draft)`) is prose, not a typed
link. The confidence word sets the edge weight (matched case-insensitively):

| confidence | weight |
|---|---|
| `extracted` | 1.0 |
| `inferred` | 0.5 |
| `ambiguous` | 0.2 |
| anything else | 0.5 |

Any other `[[wikilink]]` in a body, including a link in prose or on a line
with trailing text, stays a plain link with implicit weight 1.0. Typed targets
are also plain links. Typing adds information without hiding an edge from a
link-only consumer. This is the line grammar the Graphify
importer writes under `# Connections`, so imported typed relations round-trip.
Graph export is an interchange view, not a backup of note bodies, arbitrary
metadata, or the usage ledger. Keep the Markdown bundle for lossless note storage.
Formalized as [OKF-M Section 2.7](docs/OKF-M.md).

### Muninn frontmatter extensions (all optional; OKF consumers ignore them)

| field | values | meaning |
|---|---|---|
| `provenance` | `curated` \| `extracted` \| `inferred` | identifies whether a person, deterministic extractor, or model wrote the note. Importers must never overwrite `curated`. |
| `stability` | `durable` \| `perishable` | perishable facts (ports, versions, addresses) should be re-verified when recalled after long dormancy |
| `pinned` | bool | gives policy and safety notes a recall floor regardless of use |
| `supersedes` | list of bundle-relative paths | identifies notes that this note corrects and defines the file-level correction graph |
| `aliases` | list of strings | alternative titles for wikilink resolution |
| `valid_at` / `invalid_at` | ISO 8601 | optional bi-temporal claim validity (world time), à la Graphiti |
| `guards` | list of paths | identifies files associated with a lesson note (`type: lesson`). A primed pack includes the lesson when changed files overlap these paths by exact match, boundary suffix, or directory prefix. A lesson without `guards` applies to every session. |

Supersession is **append-only**: the old note is never edited or deleted;
the new note declares `supersedes`. History stays in git; consumers must
prefer the superseding note and may exclude the superseded one from recall.

The [OKF-M profile](docs/OKF-M.md) formalizes these extensions.

## 2. The sidecar (`.muninn/`)

Per-consumer state. Two files:

- **`ledger.jsonl`**: the source of truth. Append-only JSON events:

  | kind | fields | meaning |
  |---|---|---|
  | `touch` | `note`, `ts`, `valence?` | the note was used (read, edited, cited) |
  | `recall` | `note`, `ts`, `session?` | the note was returned by recall; this event gives a small bounded adjustment but does not record use |
  | `encode` | `note`, `ts`, `valence?` | the note was created/updated |
  | `outcome` | `valence` (−1..1), `why?`, `ts` | a signed result: incident, fix, correction, win |
  | `pin` | `note`, `value` | set/clear the safety floor |
  | `supersede` | `note` (old), `by` (new) | usage-level correction (weakens the old note) |
  | `goal` | `text`, `weight` | declare (weight>0) or retire (weight=0) a current concern |
  | `intent` | `branch`, `paths?`, `goal?`, `ttl?`, `done?` | announces in-progress work by branch without creating a lock. Re-declaring refreshes the intent; `done: true` retires it. An unrefreshed intent expires `ttl` seconds after `ts` (default 86400), with filtering at read time to preserve deterministic replay. |
  | `consolidate` | `ts` | records one decay tick, invoked by the ambient `consolidate-if-due` path or manually |
  | `session-begin` | `session`, `ts`, `cue?` | a Sense marked an agent session opening (claims the session window); `cue` keeps the prime cue so the reflection loop can learn from this session's misses |
  | `assoc` | `note`, `toks`, `w` | records a bounded and decaying cue-to-note association when review finds that the agent used a note that the pack omitted |
  | `gap` | `path`, `repository?` | records one use of an unmapped repository-relative path. The repository identifier is the SHA-256 digest of its canonical local root. |
  | `review` | `session`, `hits`, `misses`, `waste`, `built` | seals one session review, prevents duplicate review, counts unused results, and records observability data |
  | `feedback` | `text`, `domain`, `polarity`, `session?` | records one scrubbed and bounded preference observation when the user corrects, rewrites, or evaluates an output |
  | `rule` | `id`, `action` (`promote`/`demote`), `domain`, `text`, `toks`, `polarity`, `count`, `sessions` | records preference-rule promotion and demotion. Three or more events across two or more sessions promote a rule into `style-learned/`. Repeated contrary evidence supersedes the dated rule without deleting it. |

  Touch-family events MAY carry a `session` identifier or use the
  `MUNINN_SESSION` environment variable. Notes used in the same session gain
  a weighted association in the sidecar. Recall uses these associations in
  addition to authored links.

  Consumers MUST preserve and ignore unknown event kinds.

  Feedback uses an explicit `--session` value or `MUNINN_SESSION`.
  Missing session identities do not count toward the two-session promotion threshold.
  The most recent session in a shared ledger does not identify the caller.

- **`state.json`**: derived cache of per-note dynamics. Rebuildable by
  replaying the ledger; never authoritative; safe to delete.

- **`embeddings.json`** *(optional)*: a derived cache of note vectors for
  the optional semantic recall channel (`embed.py`), keyed by note path +
  content sha256. The channel is endpoint-configurable via `MUNINN_EMBED_URL`
  through an OpenAI-compatible `/v1/embeddings` server. The default permits
  only a loopback host,
  so content stays on the machine (e.g. llama.cpp `llama-server --embeddings`);
  a remote/cloud endpoint is refused unless `MUNINN_EMBED_ALLOW_REMOTE` is set.
  Present only once used; rebuildable, never authoritative, safe to delete.

The bundle may be shared through Git, while the sidecar is personal and
ignored by Git by default. Three multi-consumer modes are available. In team
mode, consumers that point to the same root share one sidecar, so one agent's
use affects every agent's recall. The ledger remains authoritative; Muninn
rebuilds a `state.json` file that has processed fewer bytes than the ledger
contains. In per-agent mode, each consumer keeps a separate `.muninn/` sidecar
over the same knowledge. A consumer without a sidecar treats every note at
baseline strength. Fleet mode uses `muninn sync` to union ledgers through a
shared Git remote when agents do not share a filesystem.

### Synchronizing a shared sidecar

`muninn sync` moves the ledger over a dedicated Git reference,
`refs/muninn/ledger` by default. It never creates a commit on a working branch.
The merge is a line-set union over append-only JSONL. Identical events collapse,
and the union orders events by `ts` and then by line text, so every consumer
replays the same sequence. Each synchronization fetches, unions, replays, and
pushes. If a concurrent writer updates the reference first, Muninn fetches and unions
again within a bounded retry loop. Synchronization publishes a personal ledger
to everyone who can read the remote and therefore requires explicit consent.

## 3. Recall dynamics

Four forces set a note's recall strength (inspired by how repetition,
emotion, and goals shape human memory):

**Use strengthens.** Each `touch`/`encode` bumps a note's use count and
refreshes how recently it was used. Being recalled is NOT use: `recall`
events add only a small bounded priority term (`+0.1 × min(10,
recalls)/10`) and move strength at most `+0.01` per recall, never past the
note's earned priority (recalls cannot undo decay); they never bump the
use count and never enter the outcome-capture window. A read-only query
therefore cannot amplify an irrelevant note. A recall with an explicit
`session` identifier, including `prime --session` and agent-hook recalls, can
form a co-use link. An ordinary query does not form such a link, and no recall
adds use. At each `consolidate`, unused strength fades multiplicatively by
`DECAY = 0.98`. A note below `FLOOR = 0.15` becomes dormant but is not deleted.

**Big outcomes mark what preceded them.** `outcome` events with |valence| ≥
`CAPTURE_DELTA = 0.5` reach back and boost the last `CAPTURE_WINDOW = 12`
touched notes (`+ min(1,|v|) × 0.5`, capped at 2.0). The system learns the
*precursors* of wins and incidents, not just the events themselves.

**Corrections weaken what they fix.** A superseded note keeps only
`LTD_FACTOR = 0.25` of its strength, so a correction demotes the stale note.

**Pinned notes hold a floor.** A pinned note never falls below
`PIN_FLOOR = 0.5`. This rule prevents infrequently used policy and safety
notes from becoming dormant.

Per-note strength:

```
base   = 0.3 + 0.4·min(10, recurrence)/10 + 0.3·min(1, affect_mag) + captured
strength = base × LTD_FACTOR   (if superseded)
strength = max(strength, PIN_FLOOR)   (if pinned)
```

### The reflection loop (`muninn review`)

Every hooked session leaves two streams: what packs SERVED (`recall`
events with the session id) and what the agent really USED (`touch`/
`encode`: recalls are never use). At session end, `review-if-due`
compares them once per session. It records a **hit** when a served note was
used, a **miss** when an unserved note was used, **waste** when a served note
was not used, and a **gap** when a used path has no note. It then applies the
following bounded changes:

- a **miss** writes an `assoc` event: the session's situation-cue tokens
  associate with the missed note. Recall seeds it like the semantic
  channel: capped at the walk-orphan ceiling (0.4× the top lexical
  hit), needing ≥2 shared tokens, so a learned lift carries a note into
  candidacy but can never manufacture a top hit. Served notes that ride
  an association show `learned from a past miss` in *why loaded*.
- **waste** counts per note (via the `review` event); after 3
  consecutive wasted serves the score takes ONE bounded 0.8× step. Any
  real touch resets it instantly.
- **gaps** sighted twice are mapped deterministically: the tree-sitter
  extractor runs on exactly those files (no LLM, ≤8 per review).
  Automatic extraction requires the recorded repository digest to match the
  current source root. Historical unscoped gaps remain readable but cannot
  trigger extraction. Moving a repository requires fresh gap observations.

Tests enforce five limits on adaptation. Serving is never evidence; only
independent agent behavior affects review. Every adjustment is bounded and
decays without reinforcement, with association weights dissolving after about
two weeks of ticks. Each session is reviewed once. Replay reconstructs the
same state. Every change is a readable ledger event, and
`MUNINN_NO_REVIEW=1` disables ambient review.

## 4. Recall and context packs

Every recall is one pass over one merged graph: cues seed it, a bounded
walk spreads activation over it, and bounded multipliers finish the score.

- **Cues.** The query is split into weighted facets: the whole question,
  quoted spans, clauses, groups of rare tokens, so a two-part question
  becomes several cues instead of one blurry one. The situation (see
  priming below) can join as extra cues the same way; active goals never
  seed cues: they only tilt notes the query itself reached (below).
- **Seeding.** Each cue scores every note by field-weighted lexical match
  (title ×3, tags ×3, description ×2, headings ×2, body ×1). The notes a
  facet hits become the walk's starting activation, and the facets that
  hit are kept as evidence.
- **The walk.** Activation spreads a fixed few damped steps over ONE
  merged graph containing authored links, backlinks, and learned co-use
  pairs. A typed authored edge carries the weight defined in Section 1:
  `extracted` 1.0, `inferred` 0.5, and `ambiguous` 0.2. A plain link has weight
  1.0. When several edges connect the same pair, the highest weight applies.
  A learned pair has weight `min(1, w/3)`. Each
  step a note passes half its activation to its neighbors, split by edge
  weight × neighbor strength (pinned floor honored): usage biases where
  activation flows, and a note tied to a hit can surface with zero
  shared words.
- **Bounded blending: reorder, never gate.** Graph support may lift a
  note the cue already hit by at most +50%. Only a note with NO lexical
  match may score from the walk alone, capped at 0.4× the strongest
  direct hit: the graph reorders near-peers; it never buries a stronger
  match or manufactures a top hit. Usage strength then multiplies by a
  band bounded to 0.8–1.2 (it breaks ties but cannot bury a strongly
  relevant untouched note: a wider range measurably buried them, see
  eval), and an active goal may tilt an aligned note by at most 1.3×
  (below). Superseded notes (frontmatter- or ledger-level) are excluded
  unless explicitly requested.

Fixed steps, sorted edges, rounded scores: the same bundle and sidecar
state always recall the same notes.

**What matters now (goals).** A `goal` ledger event ({`text`, `weight`})
declares a current concern: `muninn goal "complete the migration" --weight
0.8`. While a goal is active, a note whose text lines up with it gets a
small, bounded nudge in recall (up to 1.3x, via `GOAL_SPAN = 0.3`): the
same reorder-never-gate rule as strength, so a goal can lift aligned notes
toward the top but never hide anything. Alignment is stricter than query
relevance: at least two distinct matching tokens, or one that occurs
outside URL-ish text (a lone "docs" inside a URL in a title is a path
fragment, not an alignment). `muninn goal` with no argument lists active
goals; `--off` (weight 0) retires one: `muninn goal --off` alone retires
the single active goal (with several it lists them and exits nonzero).
Goals live in the ledger and replay like any other event.

**Priming (no-query recall).** `muninn prime` builds the cue from the
*situation* instead of a query: working directory, git branch, changed
files, last commit subject, and the titles of recently-touched notes (what
you were just doing pulls up related notes). Deterministic, no LLM, fails
soft outside a git repo. Wire it to a session-start hook and recall becomes
ambient: the situation surfaces what you will likely need, no query
required.

**Lessons (corrective knowledge).** `muninn lesson "<headline>"
--guards <paths> --body "<how to avoid it>"` writes a pinned, curated
`type: lesson` note and logs a negative outcome (capture marks what was
being changed when the failure occurred). Primed packs append a
`## Lessons for this change` section. It includes guarded lessons whose paths
overlap the session's uncommitted changes and process rules without guards.
These items produce recall events, which let review measure subsequent use.
Muninn never reads conversations. The agent records the lesson when the user
identifies a failure or correction.

**In flight (intent awareness).** When the ledger holds active `intent`
events announced with `muninn intent "<goal>" --paths a.py,b.py` and shared
through a common sidecar or `muninn sync`, primed packs append a
bounded `## In flight` section listing them, and flag any intent whose
claimed paths collide with the situation's changed files, compared directly
and through the file-to-note mapping. An intent informs agents but does not block work.

**Context pack.** Recall returns a budgeted Markdown document:

```
# Context pack: <cue>
## Index (whole bundle, low-res)     ≤ 25% of budget
- Title (path): description         ordered by strength, [superseded] flagged
## Focus (recalled for this cue)     the rest of the budget
### Title (path)
*why loaded: cue-match 2.1; via [[Backup target]]; strength 0.62*
<body, truncated to fit>
```

Focus notes are selected for coverage. Before Muninn admits near-duplicates,
each selected note must cover a query facet that earlier notes did not cover.
Token shares are proportional to activation, with a body allowance of at
least 80 estimated tokens when space permits. Short complete bodies need
less space. Later notes are omitted when the remaining space cannot meet
this minimum. Headers and selection explanations count toward the total
pack budget. Only results present in the output receive recall events.
Every focused note carries a one-line
**why loaded** statement, including the link that selected it (`via [[Backup
target]]` or `used-with restore-db`). This statement makes incorrect recall
correctable. Packs are deterministic for a given state. The Index is the
low-res whole (progressive disclosure); Focus is the high-res close-up.

The optional `--compact` format omits the index and selects complete body
blocks from the same ranked notes. It uses only the current cue and retains
source order, adjacent blocks, and ancestor ATX headings. Short notes remain
complete when they fit. Output includes paths and one-based body line
references, which do not include frontmatter. A reference can remain when
no complete excerpt fits. A no-match response does not load fallback notes.

Compact selection is not a guarantee of semantic preservation. Distant
qualifications can be omitted. Source recovery remains necessary when the
excerpt is insufficient. No model or automatic conversation capture is
part of this format. The [pack guide](docs/CONTEXT-PACKS.md) defines its limits.

All pack formats use the existing four-characters-per-token estimate.
Rendered pack text stays within four characters per budget unit, including
truncation markers. A model tokenizer can report a different count. The
`prime` command appends separate continuity and situational sections, so its
complete output is not bounded by the pack budget.

## 4b. Home knowledge base

One Muninn installation MAY organize every project with `muninn install
--home`. A home knowledge base is an ordinary bundle marked by
`.muninn/home.json` and organized as follows:

```
~/muninn/                  visible on purpose ($MUNINN_HOME overrides)
  home.md                  what this is (pinned)
  projects/<slug>/         one room per project: derived knowledge
  threads/                 episodic memory across ALL projects
  lessons/                 corrective knowledge with cross-project guards
  style/                   adopted, user-owned style repository
  style-learned/           local overlay from repeated feedback
  .muninn/                 private sidecar: ledger, state, rooms.json
```

Rules:

- **Rooms grow organically.** The first observed activity in a project
  (a hook event, a session start, or an explicit `muninn adopt`)
  registers its room: slug from the project's git-toplevel basename,
  short-hash suffixed on collision, stable forever after. Nothing is
  pre-built and nothing prompts.
- **The registry is private.** `rooms.json` maps local ABSOLUTE project
  paths → slugs; it lives in the sidecar and must never be exported to
  a note (a shared knowledge base must not contain a machine's filesystem layout).
- **Room-scoped mapping.** In a home knowledge base, path→note resource mapping
  consults the observed file's room FIRST, then falls back to the
  global rule. Therefore, `src/app.py` in two projects strengthens two
  different notes. Outside a home knowledge base, behavior is byte-identical to a single-project bundle.
- **Wiring never overrides.** `install --home` writes the full managed
  protocol block only into files it creates; an EXISTING canonical
  global file or project AGENTS.md gains at most a two-line pointer
  (`<!-- muninn:home ... -->` + one instruction line). The pointer requires
  rooted `prime` and `skill` commands. Setup refreshes recognized legacy
  pointers and managed blocks while preserving personal prose. Other content
  requires review. `muninn doctor --home` verifies the full protocol route
  and exits with status 1 on drift.
- **Global slots are complete.** Setup connects Claude Code, Codex, Gemini,
  and Grok to the canonical file. Home doctor fails when any supported slot is
  missing or differs from the canonical file.
- **Hook delivery is explicit.** Setup merges Muninn-owned hooks into the
  Claude, Codex, and Grok global hook files. It preserves unrelated JSON and
  keeps one exact backup before the first change. Claude and Codex can receive
  the startup pack through hook output. Grok cannot, so the adapter treats its
  startup envelope as record-only. Its loaded protocol requires an explicit
  `prime` call. Native and Claude-compatible Grok hooks remain identical so
  the host can deduplicate them.

### The style contract (`muninn style adopt <repo>`)

The adopted repository MUST contain `muninn-style.json` version 1. The
manifest names one Markdown core, explicit document guides, and optional
templates. Every declared path MUST be relative, remain inside the repository,
and exist. Route identifiers MUST be unique. Muninn MUST reject an invalid
contract before it mounts the repository.

The core is mandatory, limited to 32 KiB, and embedded verbatim in both the
canonical global agent file and the generated style skill. Muninn MUST route
only manifest entries. It MUST reject more than 48 routes rather than omit any
entry. A document guide may add structure but cannot override the core.

The repository mounts read-only by policy as the home bundle's `style/`. Promoted
preferences are written to `style-learned/<domain>.md`, a separate local
overlay with lower precedence. Muninn MUST NOT modify or commit the adopted
repository. Legacy learning found in a real, unmounted `style/learned/`
directory migrates to the overlay without overwriting an existing file.

`muninn style refresh` rebuilds every managed surface and records the content
hash and local source revision. `--pull` first performs `git pull --ff-only`.
`doctor --home` fails when the embedded block, generated skill, content hash,
recorded revision, or local upstream status is stale. Doctor does not contact
a remote; callers fetch or use `style refresh --pull` when they need current
remote state. The full manifest schema is defined in
`docs/STYLE_PROTOCOL.md`.

## 5. Interoperability

- **OKF**: a Muninn bundle IS an OKF v0.2 bundle (extensions like
  `provenance`, `pinned`, and `guards` ride the unknown-keys rule; the
  v0.2 trust/lifecycle families are honored as specified in section 1).
- **llms.txt**: `index.md` maps 1:1 onto an `llms.txt` (H1 + link lists);
  a context pack is the `llms-ctx` analogue, with usage strength replacing
  the hand-maintained `Optional` tier.
- **graphify**: `muninn.ingest.import_graphify` imports concept/document/
  rationale nodes from `graph.json` as `provenance: extracted|inferred`
  notes; curated notes are never overwritten.
- **Obsidian & friends**: plain markdown + wikilinks: bundles open as
  vaults unmodified.

### Conformance levels

- **L0: reader**: any OKF/markdown consumer. No muninn awareness.
- **L1: pack consumer**: reads context packs; honors `supersedes`.
- **L2: dynamics writer**: appends ledger events (touch/outcome/...),
  preserves unknown events, treats `state.json` as a rebuildable cache.

## Default installation and coding scope

Release 0.2.0 installs native syntax parsers by default through the curl installer.
The core archive remains standard-library-only. `--core-only` explicitly omits parser provisioning.
The default installation uses an isolated environment, exact dependency versions, binary-only packages, and required wheel hashes.
Parser verification must pass before the installer replaces an existing executable.
The installer does not modify system Python packages, agent configuration, or knowledge.

`doctor --parsers` parses fixed samples without scanning files or changing memory.
The installed protocol limits automatic source mapping and indexing to coding work.
The agent chooses the task type and relevant source scope. General memory retrieval remains available for non-coding tasks.
An explicit graph-building request can still select approved documents.
Installation does not start a background source scan or execute project code.

Saved extraction maps use a digest of the canonical project root and a digest of each relative source path.
Git subfolder scans retain repository-relative resource paths. Home bundles place these notes in the project's room.
This separates matching file and symbol names across projects and partial scans.
Existing unscoped notes remain unchanged. Moving a source root creates a separate map namespace.

Explicit `symbol` and `structural-fusion` searches use separate parser-backed indexes.
If parser diagnostics fail, these modes use a separate core profile.
Python definitions and filename matches remain available. Definitions in other languages require working parsers.
Their cache profiles include parser readiness and the exact versions of all five parser packages.
Changing parser capability selects another profile, including with `--no-refresh`.
The default hybrid retains its parser-independent index and evaluated ranking.

## 6. Versioning

This is muninn v0.1, targeting OKF v0.2. The root `index.md` declares
`okf_version: "0.2"`; sidecar events may carry `muninn: "0.1"`. Minor
versions add fields/event kinds (backward-compatible); consumers attempt
best-effort on unknown versions: permissive consumption, like OKF.
