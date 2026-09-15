# Muninn

Muninn gives configured agents on the same system shared, persistent memory
in Markdown. It takes its name from
[Muninn, Odin's raven whose name means "memory"](https://myndir.uvic.ca/MunN01.html).

All agents that use the same Muninn knowledge-base root share recorded
decisions, corrections, preferences, session journals, and usage history.
This shares saved knowledge, not live context windows or every conversation
automatically. [Agent integration](docs/ADAPTERS.md) supports hooks and explicit
commands.

The core runs locally with Python's standard library.
It requires no account, API key, model service, or database server.

## Installation

Give a coding agent this repository and the following request:

> Read AGENTS.md and docs/INSTALL.md. Install Muninn. Configure it for this
> machine. Reuse existing knowledge. Preserve my instructions.
> Check the result with doctor and prime.

The [repository instructions](AGENTS.md) define setup and checks. Reading them alone does not authorize home-directory changes.

The default installation includes tree-sitter parsers and their grammars.
It requires CPython 3.10 through 3.14 with `venv`, macOS or Linux, and curl.
The parser environment is isolated. No manual pip command is required.

Version 0.2.0 is in preparation. Its release assets are not yet verified for public installation.

Download the versioned installer:

```bash
muninn_installer_dir=$(mktemp -d)
curl --fail --show-error --location --proto '=https' --proto-redir '=https' \
  https://github.com/firat-elbey/muninn/releases/download/v0.2.0/install.sh \
  --output "$muninn_installer_dir/install.sh"
```

Read the downloaded script before running it. Then install version 0.2.0.
Run the demonstration:

```bash
sh "$muninn_installer_dir/install.sh"
"$HOME/.local/bin/muninn" demo
"$HOME/.local/bin/muninn" doctor --parsers
```

Parser diagnostics must report `parsers: ready`.

The installer downloads a Python executable archive and checks its SHA-256
checksum before installation. Both files come from the same HTTPS release.
This detects corruption, not a compromised release publisher. Installation
does not change shell profiles, agent configuration, or existing knowledge.
An unrelated executable or symbolic link at the destination causes an error.
The installer downloads pinned binary parsers from PyPI and checks their recorded hashes.
It verifies syntax parsing before replacing the executable. It does not modify system Python packages.
After installation, parsing uses local grammars without a network request or model.

For memory without syntax parsing, use `sh "$muninn_installer_dir/install.sh" --core-only`.
This explicit option retains the dependency-free core and does not require `venv`.

The demonstration uses temporary memory.
The [installation guide](docs/INSTALL.md) covers source builds, upgrades, offline use, and parser requirements.

## Initial setup

Before setup, make this installation available as `muninn` in each agent's `PATH`.
The installer does not change `PATH` or configure agents.

`muninn setup` creates `~/muninn` by default.
It preserves existing canonical personal instructions but replaces its reserved skill file.

```text
~/muninn/
|- projects/<name>/       Per-project knowledge.
|- threads/               Current state and dated session episodes.
|- lessons/               Corrective notes with optional path guards.
|- style/                 An optional user-owned style repository.
|- style-learned/         Preferences promoted from repeated feedback.
`- .muninn/               Private ledger, cache, and room registry.
```

Setup connects Claude Code, Codex, Gemini, and Grok to `~/AGENTS.md`.
It adds supported hooks and preserves unrelated configuration.
The [adapter guide](docs/ADAPTERS.md) lists paths and preservation rules. `muninn uninstall` removes only managed content.

Codex requires a one-time hook review through `/hooks`.
Grok does not add startup-hook output to model context, so its protocol
requires an explicit `prime` call.

Use `muninn setup --dry-run` to inspect changes. After setup, run `muninn doctor --home`.

Transcript import and [style adoption](docs/STYLE_PROTOCOL.md) require separate
requests. Import applies secret scrubbing. Learned preferences remain local
and cannot override adopted writing rules.

## Code mapping during coding work

The installed protocol tells agents to map code for implementation, debugging, reviews, tests, and source questions.
It tells agents to skip source scans during ordinary conversation, writing, and non-coding research.
General memory remains available for those tasks. The agent selects the task type. Muninn does not classify conversations automatically.

For an unfamiliar codebase, an agent maps the smallest relevant source folder:

```bash
muninn doctor --parsers
muninn --root ~/kb extract ./src --into ~/kb
muninn --root ~/kb source search "Where is ledger state restored?" --path ./src --budget 900
```

Extraction saves project-scoped structural notes and relationships. Source search returns bounded excerpts with paths and line references.
For a known symbol, use `source search "SymbolName" --mode symbol` with the same root and source path.
Agents reuse existing maps and refresh affected folders after structural changes.
Parsing does not execute the project or prove its runtime behavior.
Only code and documentation approved for the selected knowledge base belong in a saved map.

## Building and querying knowledge

The deterministic path requires no model:

```bash
muninn --root ~/kb init
muninn --root ~/kb build ~/my-project
muninn --root ~/kb pack "Where is ledger state restored?" --budget 900
muninn --root ~/kb viz
```

Use `pack --compact --budget 400` to request shorter, source-backed excerpts.
The default format remains available. Budgets use a character-count estimate,
not a model tokenizer. The [compact pack guide](docs/CONTEXT-PACKS.md)
defines selection, recovery, and limits.

An agent can add cross-file concepts through a bounded request and response
exchange:

```bash
muninn --root ~/kb enrich ~/my-project --request
# The agent writes the requested JSON to response.json.
muninn --root ~/kb enrich ~/my-project --apply response.json --into ~/kb
```

Inferred concepts receive weight 0.5. Deterministically extracted facts retain
weight 1.0. The [enrichment guide](docs/ENRICH.md) defines validation,
provider order, and privacy controls.

Source retrieval stores a rebuildable SQLite index under `.muninn/` without copying source into Markdown notes.
The default hybrid preserves the highest BM25F result. `--json` returns structured results.

## Why choose Muninn?

- Knowledge and usage history remain separate. Markdown works without
  Muninn, while the append-only ledger explains changes to retrieval.
- Corrections preserve the original note and identify its replacement.
  Bounded use signals change ranking without deleting eligible knowledge.
- Packs explain each selection. Compact packs retain exact excerpts and
  source references without a summarization model.
- Journals, guarded lessons, and repeated feedback preserve working state
  through explicit agent commands and narrow lifecycle hooks.

[GBrain](https://github.com/garrytan/gbrain) and
[Basic Memory](https://github.com/basicmachines-co/basic-memory) also support
local, editable knowledge. Locality and cross-agent use are not unique to
Muninn. Neither a service nor model-based ingestion is required.

Muninn does not provide hosted accounts, authenticated user isolation, or
automatic conversation compression. It has no matched evidence of better
answer quality or lower total cost than competing memory products.
The [product comparison](docs/COMPARISON-2026-09.md) distinguishes design
choices from benchmark claims.

The [specification](SPEC.md) defines the separation between portable
[OKF v0.2](https://github.com/GoogleCloudPlatform/open-knowledge-format) knowledge, usage events, and rebuildable caches.

## Evaluation results

| Capability | Result | Limit |
|---|---|---|
| Use-adaptive pack recall | At a 400-token budget, adaptive retrieval answered 16 of 18 questions. Flat lexical retrieval answered 15, and a whole-corpus dump answered 6. | The [evaluation](eval/RESULTS-b400k2.md) used 40 synthetic notes and one small answer model. |
| Long-session recall | A typed Muninn walk answered 43 of 151 LongMemEval questions. Equal-budget flat retrieval answered 36. Preference results were 3 of 30 and 0 of 30, respectively. | Claude Haiku judged the [experiment](eval/longmemeval/RESULTS-walk-typed.md). The experiment did not measure agreement with human annotators, so the result is not leaderboard-comparable. |
| Bounded bundle loading | The bounded reader loaded a reproduced 669-note bundle in 0.26 seconds with 17.2 MB maximum resident memory. The prior reader exceeded 2.1 GB without returning. | The [measurement](eval/RESULTS-scalable-bundle-loading.md) covers one bundle on one machine. |
| Source localization | The selected hybrid reached 44.7 percent hit@1 and 80.0 percent hit@10 on 300 SWE-bench Lite issues. Token overlap reached 20.0 and 60.3 percent. | The [labels](eval/code_retrieval/RESULTS-swebench-lite-300.md) identify accepted patch files, not correct patches or answers. The experiment did not establish a statistically significant gain over stronger BM25F retrieval. |
| Persistent source indexing | SQLite reproduced all 300 lexical rankings. Median reopen time was 0.354 ms, and the index occupied 1.40 times the source bytes. | The [evaluation](eval/code_retrieval/RESULTS-persistent-parity-swebench-lite-300.md) did not resolve storage efficiency or incremental-update cost. |
| Dependency reconstruction | The optional hybrid reached 0.611 edge F1 across 288 DependEval cases. Graphify reached 0.566, and the internal extractor reached 0.396. | The hybrid itself uses Graphify for several languages. The [comparison](eval/competitive/RESULTS-dependeval-series5-confirmation.md) measures named dependency edges, not semantic retrieval or task completion. The repository excludes unlicensed upstream data and raw archives. |
| Session continuity | Journaled state supplied every required fact for 12 of 12 questions. Session-opening context improved from 0 of 2 cases to 2 of 2. | The [evaluation](eval/RESULTS-continuity.md) measures context availability rather than model reasoning. |
| Repository extraction | Four rounds across 11 repositories identified 14 systematic defects and added a regression test for each. The largest subject produced 225,000 notes from 22,132 files in 94 seconds. | The [field study](eval/FIELD-TESTS.md) used three retrieval questions per repository and does not estimate task completion. |

A preregistered evidence-gated retrieval candidate did not meet its
improvement threshold. Muninn does not use this candidate. The [negative
result](eval/competitive/RESULTS-ARB-SERIES11-evidence-gated-pilot-v6.md)
remains with the successful results. [Evidence and claim
limits](docs/EVIDENCE.md) define the supported public statements.

## Learning from use

Independent use provides bounded ranking signals. Session review compares served context with independently used context.
Serving a pack is not evidence of use. Supersession excludes obsolete notes without deleting them.

Repeated feedback can promote a local style rule after three observations
across at least two identified sessions. The [specification](SPEC.md) defines
the scoring, thresholds, corrections, and ledger replay rules.

## Coordination and privacy

`muninn intent` announces current work with affected paths. Another agent can
see an overlap. The announcement does not prevent changes.
Announcements expire after 24 hours by default.

`muninn sync` unions append-only ledger events through
`refs/muninn/ledger`. It does not create commits on working branches.
Use this feature only with a private remote. The ledger can contain note paths,
session identifiers, feedback, outcomes, and intent text.

Ambient hooks pass only a session identifier and file path beyond intake.
Muninn does not read conversations automatically. Remote model endpoints are
disabled until configured, and nonlocal embedding endpoints require a
separate opt-in. [SECURITY.md](SECURITY.md) defines the trust boundaries and
reporting process.

## Project status and license

Muninn is alpha software. Interfaces can change before version 1.0. The test
suite runs with and without optional extraction dependencies.
The full suite must pass coverage.py's 93 percent combined statement-and-branch
coverage gate. This measures test execution, not memory accuracy.

[CONTRIBUTING.md](CONTRIBUTING.md) defines the test, audit, and Developer
Certificate of Origin requirements. The [MIT License](LICENSE) governs
Muninn's original work. [Third-party notices](THIRD_PARTY_NOTICES.md) identify
benchmark and parser material. [Evaluation sources and
licenses](eval/README.md) document excluded upstream data.

The [release procedure](docs/PUBLIC-RELEASE.md) preserves the boundary between public source and private history.
[Prior art](docs/PRIOR-ART.md) documents related systems.
