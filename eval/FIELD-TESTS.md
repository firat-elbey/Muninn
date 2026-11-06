# Field tests across 11 repositories

The four-round study identified 14 systematic defects, added regression tests
for each defect, and increased the test suite from 386 to 418 tests. It also
produced the reflection loop (`muninn review`) and guarded lessons (`muninn
lesson`).

For each repository, the harness created a shallow clone, built a bundle, and
measured the index landmarks, top-three recall for three realistic questions,
stub ratio, and build time. Each identified defect was fixed with a regression
test before the repository was measured again. The harness is [field_probe.py](field_probe.py).

The subjects were Flask for Python and Sphinx reStructuredText; Express for
JavaScript with documentation outside the repository; Gin for test-heavy Go;
ripgrep for macro-heavy Rust; Sinatra for Ruby and Minitest; Gson for Java
with a large test tree; Zod for TypeScript; OWASP Cheat Sheet Series for
Markdown; Redis for C, Tcl, and 447 JSON fixtures; Docker Awesome Compose for
YAML infrastructure; and OpenClaw for a 22,000-file TypeScript, Kotlin, and Swift monorepository.

## Round 1: Flask

The detailed report is [FIELD-TEST-flask.md](FIELD-TEST-flask.md).

| Finding | Correction |
|---|---|
| reStructuredText files produced file-name stubs and omitted conceptual documentation. | An `_rst` dispatcher reconstructed flat-stream sections, Docutils depth, and `:doc:` references. |
| A query for `teardown` could not retrieve `teardown_request`. | `tokens()` retained each identifier and added subtokens at camel-case boundaries and separator characters. |
| A goal explanation could end within a word. | Truncation now ends at a word boundary. |
| Alphabetical ordering placed CSS stubs among index landmarks. | The index tail now uses connectivity order. |
| Sphinx roles remained in note bodies. | The extractor replaces each role with its display text. |
| CSS produced labels such as `a {`. | `_Acc.node` now applies label validation centrally. |

## Round 2 results

| Finding | Correction |
|---|---|
| Test notes such as `TestMiddlewareNoRoute`, `BeforeFilterTest`, and `CustomTypeAdaptersTest` ranked first for implementation questions. | Importers identify test paths with `_is_test_path`. `TEST_DAMP` multiplies their lexical seed by 0.5, while an explicit `test` cue restores their relevance through the tag field. |
| Substring matching classified `mutable_specifier` as a table and `Some`, `Err`, and `None` match arms as structures. | `_DEF_DENY` now includes `pattern` and `specifier`. |
| Creating one note per class or structure field produced 28,000 Gson notes and 9,000 ripgrep notes. | `_DEF_KINDS` no longer includes `field`; the containing structure is the knowledge unit. |
| Highly connected import nodes such as `supertest`, `node:assert`, and `../` became index landmarks. | The index excludes import notes and orders test notes after production notes. |

After these changes, Gin's landmarks became `context.go`, `gin.go`, and
`routergroup.go`. Production `Abort()` and `AbortWithError()` notes displaced
test stubs. Sinatra ranked `before-filter.md` first, and Zod retrieved
`objectschema.md`. Documentation ranked above tests wherever both were
present.

## Round 3 results

| Finding | Correction |
|---|---|
| Processing 447 JSON test fixtures produced 276,852 notes, 94 percent stubs, a 278-second build, and a 1.1 GB bundle. | `MAX_FILE_NODES` limits each file to 200 nodes and prunes corresponding edges consistently. |
| Bash and Tcl local names such as `0`, `r`, and `1` became highly connected landmarks. | The generic walk omits one-character and numeric labels while continuing to inspect their descendants. |
| The OWASP corpus contained 146 indistinguishable `Introduction` headings. | Duplicate headings from different files include the document stem, as in `Introduction (SQL_Injection)`. |

After correction, Redis produced 58,925 notes, a 79 percent reduction; built
in 24.6 seconds, a 91 percent reduction; and occupied 233 MB, a 79 percent
reduction. The same three questions still retrieved `aeEventLoop`,
`scanDatabaseForDeletedKeys`, and `replication.c`.

## Round 4: OpenClaw (a real application at monorepo scale)

The repository contained 22,132 files in 10 languages, including 18,400
TypeScript files as well as Kotlin, Swift, and Go files. Muninn produced
225,000 notes in 94 seconds. All three contributor questions retrieved the
correct source first without prior use: `adding-a-channel.md`,
`routing-rules-how-an-agent-is-chosen.md`, and the Kotlin
`CronJobManagement` module. This result extends the documentation finding below to a large application repository.

| Finding | Correction |
|---|---|
| YAML files under `qa/scenarios/` became index landmarks because the path classifier did not identify these test directories. | `_TEST_DIRS` now includes `qa`, `scenarios`, and `e2e`. |
| Note count increased linearly with repository size and reached 225,000. | Recall remained correct in the three tested questions, but the result identified bundle size and build time as limits that may require an aggregate budget. |

Round 4 also introduced guarded lessons through `muninn lesson`. A failure,
such as a change that breaks dark mode, can become a pinned note with affected
paths in `guards:`. A primed pack includes that note when the current changes
overlap those paths. Lessons without guards describe process rules and appear
in every session. The agent records a lesson after a relevant correction or
failure because Muninn does not read conversations. Serving a lesson records
only a recall event; the reflection loop separately measures whether the
agent used it.

## Final state by repository

| Repository | Type | Notes | Build time | First-ranked results |
|---|---|---|---|---|
| flask | py + rst | 1.7k | 0.7s | Context documentation and the relevant corrected team note rank first. |
| express | JavaScript | 392 | 0.4s | Conceptual retrieval remained limited because the documentation resides at expressjs.com. |
| gin | Go | 2.8k | 1.5s | Documentation and the production `Abort()` implementation rank first. |
| ripgrep | Rust | 5.1k | 3.0s | GUIDE sections and `search.rs` rank first. |
| sinatra | Ruby | 3.8k | 2.0s | The production filter and relevant README sections rank first. |
| gson | Java | 18.5k | 5.9s | The troubleshooting document and production code rank first. |
| zod | TypeScript | 3.9k | 2.6s | The production schema and README `refine` section rank first. |
| OWASP | markdown | 4.1k | 2.9s | Exact cheat-sheet sections rank first, with 6 percent stubs. |
| redis | C, Tcl, and JSON | 58.9k | 24.6s | `aeEventLoop`, expiration code, and `replication.c` rank first. |
| compose | YAML | 2.7k | 1.4s | Exact service definitions and the health-check key rank first. |
| OpenClaw | TypeScript, Kotlin, and Swift | 225k | 94s | Exact channel and routing documents and the Kotlin scheduling module rank first. |

## Durable lessons

1. **Repository documentation was the strongest observed predictor of pack
   quality.** Markdown and reStructuredText sources in OWASP, Gin, and
   ripgrep answered conceptual questions directly. Code-only repositories
   answered identifier-specific questions more accurately than conceptual
   questions. Express documentation resides outside its repository, so a
   curated team layer was required to address the missing context.
2. **Test code caused the most consistent lexical interference.** In every
   test-heavy repository, test notes initially ranked first because their
   names contained the question's terms. A 0.5 multiplier lowered these
   notes without excluding them, and explicit testing questions still
   retrieved them.
3. **Excessive extraction caused the largest failures.** Fields, local
   variables, fixtures, and patterns interpreted as definitions produced too
   many notes. Per-file limits, denied fragments, and consistent labels
   mattered more than extracting every syntax node.
4. **Landmarks require distinct identities.** Document-stem suffixes
   distinguished repeated headings. Excluding imports and ordering by
   connectivity kept the index focused on knowledge rather than structural
   dependencies.
5. **Top-three recall sometimes survived noise, but resource use did not.**
   The correct note often ranked among the first three results even when 94
   percent of notes were stubs. However, index size, disk use, build time, and
   token use increased with irrelevant notes. Extraction quality therefore
   affects both retrieval and resource limits.

## Reproduce

```
python3 eval/field_probe.py gin https://github.com/gin-gonic/gin \
    "bind json request body to a struct" \
    "middleware that runs before every route" \
    "context abort vs next"
```
