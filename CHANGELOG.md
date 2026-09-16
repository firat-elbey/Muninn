# Changelog

This file records user-visible changes to Muninn. The project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) after version 0.1.0.

## 0.2.0

In version 0.2.0, the default curl installation includes tree-sitter parsers and local grammars.

- The installer uses an isolated environment with exact package versions and verified binary hashes.
  Syntax checks must pass before it replaces the executable. `--core-only` remains an explicit option.
- `doctor --parsers` checks eight grammar samples without scanning source or changing memory.
- The installed protocol requires source mapping for relevant coding work, not ordinary conversation or non-coding research.
  General memory remains available in both cases.
- Existing personal instruction files retain their prose and receive a route to the full protocol.
  Setup and explicit project adoption refresh recognized legacy pointers.
- Saved extraction maps separate projects and source files. Git subfolder scans retain stable repository-relative paths.
  Existing unscoped notes are preserved, not automatically migrated or deleted.
- Explicit symbol and structural-fusion searches use separate parser-capability profiles.
  The evaluated default hybrid index and ranking remain unchanged.
- Standalone hooks retain isolated Python invocation. Public CI checks default installation across macOS, Linux, Intel, and Arm.
  Parser checks must pass before package validation or release publication.

Native parsers remain outside the core archive. Default installation requires CPython 3.10 through 3.14 with `venv` and compatible binary wheels.
No account, model service, or background source scan is added.

## 0.1.0

The pre-release review adds the following changes:

- A versioned standalone archive and checksum support installation through
  curl without pip. Installation leaves agent configuration unchanged.
- Standalone setup binds lifecycle hooks to its archive and Python interpreter,
  even when another Muninn installation appears first on `PATH`.
- The release workflow publishes standalone GitHub assets without a PyPI
  prerequisite. The README defines supported strengths and comparison limits.
- Optional compact packs select query-focused excerpts with exact source
  text and body line references. All pack formats account for rendered overhead.
- Recall events identify only results that the pack delivers. Session-end
  review honors the supplied session identifier.
- Ledger synchronization preserves events written during a remote fetch.
  Concurrent writes use bounded locks and separate temporary cache files.
- Journal writes preserve concurrent episodes and numeric episode order.
  Explicit transcript import scrubs generated titles before truncation.
- Source indexes detect changes to included Git-ignored files. Directory
  extraction excludes escaping file symlinks and non-regular files.
- Instruction updates reject ambiguous managed markers. Codex hook metadata
  uses the documented command-handler fields.
- Learned-rule promotion requires identified sessions. Gap observations retain
  repository scope before they can trigger automatic extraction.
- A paired context-efficiency evaluation measures source evidence and output
  size. SWE-bench analysis derives its report from case-level results.

The initial public release provides the following behavior:

- Muninn stores knowledge as OKF v0.2 Markdown and records consumer-specific
  usage in a removable append-only sidecar.
- Bounded ranking signals represent independent use, supersession, co-use,
  outcomes, review, and active goals.
- Journals preserve session state. Guarded lessons surface prior failures.
  Repeated feedback can produce local style rules.
- Deterministic extraction and optional enrichment create source-linked knowledge without making model output authoritative.
- Persistent BM25F, path, and symbol retrieval returns bounded source excerpts.
- Merge-safe setup connects Claude Code, Codex, and Gemini to one knowledge
  base, and uninstall removes only managed content.
- Optional ledger synchronization and expiring intents support several agents.
