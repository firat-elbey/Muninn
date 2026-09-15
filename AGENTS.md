# Agent instructions for Muninn

This file routes installation requests and defines the requirements for
changes to Muninn. `CLAUDE.md` and `GEMINI.md` link to this file.
Read [README.md](README.md) for usage and [SPEC.md](SPEC.md) for the behavioral contract.

## Installation requests

If the user requests installation or agent setup, read [docs/INSTALL.md](docs/INSTALL.md) before acting.
An executable installation alone does not create global instructions or hooks.
Only an authorized setup request permits those home-level changes.
Inspecting, evaluating, or contributing to this repository does not authorize setup.

1. Check the existing executable, knowledge-base root, and agent configuration.
   Reuse the intended installation and existing memory.
2. If an executable is missing, build from the reviewed checkout or use the documented release installer.
   Do not replace an unrelated executable or move existing memory.
3. Before setup, read the guide's preservation checks.
   Inspect `setup --dry-run` with the selected root.
   If the proposed paths exceed the user's authority, stop and ask.
4. Within the approved scope, run setup.
   Check the selected executable in the actual agent environment.
   Do not infer the desktop agent's `PATH` from a terminal session.
5. Run `doctor --home` with the same root.
   Then run `prime` in the current session.
   Report changed paths, preserved conflicts, and required harness approvals or restarts.

Do not import transcripts, adopt a style repository, or change unrelated agent configuration without separate authorization.
If custom instruction locations or overrides prevent safe setup, report the conflict before changing those files.
The remaining sections govern repository contributions, not home installation.

## Architecture

| Path | Responsibility |
|---|---|
| `src/muninn/store.py` | OKF v0.2 notes, frontmatter, links, and indexes |
| `src/muninn/dynamics.py` | Append-only usage ledger and rebuildable state cache |
| `src/muninn/activate.py` | Cues, graph traversal, strength, and goal signals |
| `src/muninn/recall.py` | Ranking, context packs, and selection reasons |
| `src/muninn/observe.py` | Hook intake and the privacy boundary |
| `src/muninn/review.py` | Per-session comparison of served and used context |
| `src/muninn/evolve.py` | Feedback aggregation and learned style rules |
| `src/muninn/journal.py` | Threads, episodes, and transcript import |
| `src/muninn/home.py` | Project rooms, registration, adoption, and pointers |
| `src/muninn/style.py` | User-owned style contracts and managed instruction blocks |
| `src/muninn/extract.py` | Optional deterministic tree-sitter extraction |
| `src/muninn/code_search.py` | In-memory BM25F, exact, and relationship retrieval |
| `src/muninn/persistent_code_search.py` | Rebuildable SQLite source index |
| `src/muninn/source_retrieval.py` | Index freshness, storage, and bounded excerpts |
| `src/muninn/enrich.py` | Optional model enrichment and offline agent exchange |
| `src/muninn/ingest.py` | Graphify `graph.json` import |
| `src/muninn/sync.py` | Ledger union through `refs/muninn/ledger` |
| `src/muninn/cli.py` | Command-line interface over the modules above |
| `tests/` | Regression suites; `tests/test_okf2.py` verifies the format |
| `docs/`, `eval/` | Design records, evaluations, and field reports |

## Verification

Write a failing regression test before changing behavior. Run the suite with
and without the optional extraction dependencies. Core Muninn uses only the Python standard library.

```bash
python -m pip install -r requirements-test.txt
python -m pip install -e .
python -m pytest -q
python -m pip install -e ".[extract]"
python -m pytest -q

# This check forces the standard-library profile after tree-sitter has been
# installed in the same environment.
mkdir -p /tmp/no-ts && printf 'raise ImportError("stubbed")\n' \
    > /tmp/no-ts/tree_sitter.py
cp /tmp/no-ts/tree_sitter.py /tmp/no-ts/tree_sitter_language_pack.py
PYTHONPATH=/tmp/no-ts python -m pytest -q
```

Continuous integration runs the standard-library suite on Python 3.10, 3.12,
and 3.14 and the complete suite on Python 3.11. The complete suite must pass
coverage.py's 93 percent combined statement-and-branch coverage gate.

```bash
python -m pyflakes src tests tools eval
python -m coverage run --branch --source=src/muninn -m pytest -q
python -m coverage report --fail-under=93
python tools/check_repository.py
```

The repository audit validates tracked files, local documentation links,
legal metadata, compressed results, and reproducible evaluation reports. A
release also requires the package and clean-export checks in
[the public release procedure](docs/PUBLIC-RELEASE.md).

## Invariants

A change to any invariant requires a regression test that proves the invariant
still holds.

1. **Adaptive signals reorder but do not filter.** Strength, goals,
   associations, and damping are bounded ranking multipliers. Supersession and
   `status: deprecated` exclude obsolete knowledge. `.muninnignore` excludes
   configured non-knowledge. A stale note remains available with a label.
2. **Serving is not evidence of use.** Reflection and evolution use
   independent reads, edits, touches, and outcomes. A context pack cannot
   strengthen itself. Tests G1 through G8 in `tests/test_review.py` enforce
   this rule.
3. **Hook intake preserves the privacy boundary.** The adapter passes only
   `session_id` and `tool_input.file_path` beyond intake. It reads `tool_name`
   only to classify the event and does not store the raw value. Muninn does
   not read conversations automatically. It records gap paths relative to the
   repository and scrubs user text at write boundaries.
4. **The ledger is authoritative and state is rebuildable.** New behavior
   appends new event types. Replay must reproduce the same state after
   `state.json` is deleted. A semantic change to an event requires a
   `STATE_RULES` increment in `dynamics.py`.
5. **Hooks do not obstruct the host agent.** `muninn hook` and `muninn
   observe` always exit with status 0, emit at most one diagnostic line, and
   complete promptly. A damaged bundle cannot block an agent session.
6. **Instruction updates preserve unmanaged prose.** Installers add a two-line
   pointer or refresh a marker-delimited block. They leave damaged markers and
   unmanaged prose unchanged. Setup replaces the reserved Muninn skill file.
   Follow the installation guide's preservation checks before setup.
7. **The core has no required third-party dependency.** A required dependency
   is an architectural decision. An unavailable optional feature reports its absence without breaking core operation.
8. **Derived output is deterministic.** Extraction, clustering, promotion,
   and style routing produce stable output. Paths resolve ties. Tests inject
   time instead of waiting for the clock.

## Writing standard

All repository prose, command output, code comments, commit messages, and pull
request text must use complete, formal, concise English. State the controlling
point first. Give each sentence one purpose and each paragraph one claim. Keep
evidence, scope, and limitations beside the claim they qualify. Use literal
technical terms instead of idiom, promotional language, or conversational
shorthand. Do not use contractions or em dashes. Remove any sentence that does
not change what the reader knows or does.

Comments explain constraints that the code cannot express. Module docstrings
may explain a design, but they must not narrate the implementation line by
line. Tables are reserved for information that readers need to compare or
retrieve. Every commit must carry the sign-off required by [CONTRIBUTING.md](CONTRIBUTING.md).
