# Multi-source knowledge design

- **Status:** deferred design; this interface is not implemented.
- **Objective:** query several independent knowledge bundles through one
  consumer without merging their authoritative files.

## Decision

Each source remains an independent OKF bundle. A per-consumer registry records
the available sources and selects a bounded set for each query. Muninn composes
the selected notes in memory under source-prefixed paths and uses one shared
consumer sidecar. This design preserves each bundle's files and history while
allowing cross-source associations.

The alternative of importing every source into one bundle would require each
query to scan and tokenize the complete collection. Separate bundles with a
second global overlay would split usage evidence between two ledgers. The
registry avoids both conditions: it selects sources before recall and records all consumer evidence in one ledger.

## Registry

The proposed `$MUNINN_HOME/sources.json` file is consumer configuration, not
knowledge. Each entry contains:

| Field | Meaning |
|---|---|
| `id` | Stable source identifier and note-path prefix. |
| `path` | Absolute path to the authoritative source. |
| `type` | `code` or `docs`, which selects the build path. |
| `notes_root` | Path to the source's OKF notes. |
| `global` | Whether the source joins every query scope. |
| `last_built` | Git revision or deterministic tree fingerprint. |
| `built_at` | Time of the last completed build. |

`muninn source add <path>` would register and build one source. A refresh would
skip an unchanged source by revision or fingerprint and retain the existing
per-note content-hash check. A Markdown source could be registered in place;
code or other documents would use a derived notes directory.

## Query scope

The current working directory determines the focus source by matching its Git
root against registry paths. The default scope contains the focus source and
small sources marked `global`. An explicit `--source` option may select several
sources, while `--all` permits an intentionally unbounded query.

Cross-source recall requires evidence. If notes from two sources are used in
one session, the shared sidecar can record their association. A later query in
the first source may then admit the associated note from the second source
within the existing graph-walk limit. This rule avoids loading every source for ordinary single-project work.

## Composition and provenance

Composition prefixes each note path with its source identifier. Therefore,
`repo-a/notes/x.md` and `repo-b/notes/x.md` cannot collide. The resolver then
builds links over the selected union. Source paths and each note's existing
`resource` and `provenance` fields retain attribution.

Conceptually similar notes remain separate because they describe different
source files. A curated note may link or supersede them, but import does not
merge them automatically. This rule prevents the composition layer from
creating a fact with no authoritative source.

One shared sidecar records source-prefixed note paths, goals, outcomes, and
co-use. This enables cross-source learning but makes usage export a separate
operation. A future `source export` command would select one source's events
from the ledger. Deleting the shared sidecar still leaves every constituent bundle valid.

## Required validation

An implementation must first pass a two-source test with colliding relative
paths. The test must verify distinct composed identifiers, current-directory
scope, global-source inclusion, budget compliance, and cross-source recall only
after independent co-use. It must also show that explicit multi-source scope
does not change the ranking rules inside the selected union.

Large-source scaling remains unresolved. A 100,000-note source may require
internal sharding even when the registry selects only that source. The design
must be evaluated before it becomes part of the public command interface.
