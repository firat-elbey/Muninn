# Flask repository field test

The Flask field test identified and corrected two extraction and retrieval
defects. The subject was a shallow clone of `pallets/flask` at `HEAD` with 198
parseable files: 83 Python files, 85 reStructuredText or Markdown documents,
and TOML, YAML, HTML, and CSS files.

The test built a new bundle, submitted realistic questions, added curated
team notes, simulated three development cycles of usage, and measured the
result again. The usage history included sessions, an incident, a correction,
an active goal, and decay. A final check started a new agent in the repository
while another intent affected the same path.

## Build

```
muninn --root kb init && muninn --root kb build flask
# 198 files → 2095 nodes / 1913 edges → 1668 notes, in ~0.7s (warm) / ~4s (cold)
```

## Finding 1: reStructuredText files produced empty stubs

Flask stores conceptual documentation about application context, request
context, blueprints, and signals in Sphinx reStructuredText. Before the
correction, each reStructuredText file passed through the generic extractor
and produced one module note containing only the file name. The retriever
could not access the document's substantive content.

```
recall "how does the application context work"      # BEFORE
  0.29  extracted/toml/project-2.md                  ← config noise
```

The extractor lacked a reStructuredText dispatcher, and tree-sitter-rst
emits sections as a flat stream. A `section` node contains its title and
adornment, while the section content appears in subsequent document-level
siblings. The `extract._rst` correction performs one pass, attaches prose to
the active section, derives depth from the Docutils adornment convention, and
creates `references` edges from `:doc:` roles.

```
recall "how does the application context work"      # AFTER
  2.35  extracted/rst/how-the-context-works.md
  2.27  extracted/rst/application-context.md
  2.23  extracted/rst/the-app-and-request-context.md
```

## Finding 2: prose could not retrieve compound identifiers

A curated note containing `teardown_request` or `teardown_appcontext` could
not be retrieved with the word `teardown` because the tokenizer retained
compound identifiers as single tokens. The corrected `tokens()` function
splits camel case before lowercasing and splits identifiers at `_`,
periods, and hyphens. It retains both the parts and the original token for
queries and notes, which preserves exact-identifier matching.

```
recall "connections leak teardown background task"
  ...
  1.67  team/db-teardown.md  (cue-match 1.00; matters now: harden request
        lifecycle…; strength 1.69; preceded a strong outcome)
```

## Layered-system results

- **Corrections rank above superseded notes.** `team/db-teardown.md` scored
  2.70 for its query and excluded the superseded note from recall.
- **Outcome capture.** The notes touched before the simulated staging
  outage carry "preceded a strong outcome" in every why-loaded line.
- **Goals tilt.** With `goal "harden request lifecycle and teardown paths"`
  active, aligned notes carry `matters now`. This bounded signal does not displace a stronger lexical match.
- **Ambient capture resolves repository paths.** `observe --kind touch --note
  …/flask/src/flask/ctx.py` mapped through the `resource:` suffix index
  to the note derived from that file: hooks would train recall from
  ordinary file reads and edits without an additional command.
- **Session initialization includes relevant continuity.** Inside the
  repository on a new branch with an
  uncommitted edit to `ctx.py`, `muninn prime` (no query) surfaced the
  team's corrected teardown note first. Because another
  agent had announced `intent … --paths src/flask/ctx.py`, the pack ended
  with an In-flight section that identified an overlap with `src/flask/ctx.py`.

## Limitations

- Broad-vocabulary queries ("database connections leak from background
  jobs") still rank title-matching tutorial notes above the curated
  note. Lexical relevance dominates, and usage strength only reorders
  results within a 0.8 to 1.2 multiplier. This bound preserves relevant
  unused notes. A query containing terms from the incident does retrieve the
  curated note.
- One `resource:` file maps to one note for ambient touches. The first
  matching note is selected, so reading `ctx.py` strengthens one of its
  approximately 20 derived notes rather than all of them.
- Graphify was not installed, so this field test did not exercise its call-graph layer.

## Reproduce

```
git clone --depth 1 https://github.com/pallets/flask
muninn --root kb init && muninn --root kb build flask
muninn --root kb recall "how does the application context work" --no-reactivate
# … then layer notes/usage as above; the history simulation is ~20 lines
# of Dynamics calls (touch/outcome/goal/consolidate): see git history of
# this file's branch for the exact script.
```
