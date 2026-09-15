# Security policy

## Supported versions

Security fixes apply to the current `main` branch until the first release.
After release, the latest minor release and `main` will receive security fixes.
Older minor releases will not receive separate backports unless a security
advisory states otherwise.

## Reporting a vulnerability

Report vulnerabilities privately through GitHub Security Advisories. Open the
repository's **Security** tab and choose **Report a vulnerability**. Do not
open a public issue for a suspected vulnerability.

Include the affected revision, reproduction steps, expected behavior, observed
behavior, and any known impact. Do not include credentials or personal data
that are unnecessary to reproduce the issue.

## Security boundaries

The following boundaries define the expected security properties:

- Core operation uses no server, API key, or telemetry. Remote enrichment and
  embedding require explicit configuration. The local attack surface includes
  the command line, hook adapter, imported data, and files Muninn reads.
- The hook adapter passes only `session_id` and `tool_input.file_path` beyond
  its intake function. It reads `tool_name` only to classify the event as a
  read or write and never stores the raw value. Any wider flow from hook JSON
  is a vulnerability, not a feature request.
- Note content with `provenance: extracted` or `inferred` is data, not
  instructions; the same applies to imported graphs and transcripts.
  A path by which such content changes Muninn's behavior (marker
  injection, path traversal via note paths, frontmatter smuggling) is
  in scope and prior fixes of that shape are pinned by tests.
- `journal.scrub` replaces recognized credential patterns in authored note
  titles, descriptions, tags, and bodies.
  It also covers lessons, journals, imported transcript text, feedback, goals,
  intent descriptions, outcome reasons, and session cues.
  A bypass of these write boundaries is in scope. Scrubbing is pattern-based,
  not a guarantee that arbitrary confidential text is detected.
- Source indexes, extracted code and configuration, imported graphs, and
  direct library writes preserve source evidence. They are not secret-scrubbed
  exports. Explicit paths, identifiers, and existing files are not rewritten.
  Review these inputs before retrieval, remote enrichment, synchronization,
  or publication. Never supply credentials in paths or identifiers.
