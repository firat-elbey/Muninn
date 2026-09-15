# Bounded bundle-loading experiment

## Question

This experiment tested whether an explicit bundle boundary prevents generated
benchmark trees from exhausting memory during recall. It also tested whether
the boundary preserves ordinary notes, mounted knowledge, and safe writes.

## Setup

The final implementation was commit
`8ecb47b5a8bd5182f87d82d94b21f79f051fd7ce` on
`codex/scalable-state-rebuild`. The measurements ran on 7 August 2026 with
macOS 15.5 on Arm64. The test suite used Python 3.12.13. Timing used macOS
`/usr/bin/time -l` and reports one run per condition.

The scale fixture was a temporary directory containing symbolic links to every
visible top-level entry in the live Muninn knowledge base. The fixture copied the
repository's `.muninnignore` but did not copy the live `.muninn/` sidecar. This
kept the knowledge tree realistic while ensuring that the test could not write
to live memory. The pack command used a 900-token budget and
`--no-reactivate`:

```bash
muninn --root <fixture> pack \
  "How does Muninn combine lexical, graph, and learned retrieval?" \
  --budget 900 --no-reactivate
```

The unbounded baseline was the installed reader before this change. One live
pack attempt exceeded 2.1 GB of resident memory without returning and was
interrupted. It was not repeated because another run would add resource cost
without resolving the observed failure. The baseline is therefore a censored
failure, not a timed comparison.

## Result

| Condition | Notes loaded | Wall time | Maximum resident memory | Output |
|---|---:|---:|---:|---:|
| Unbounded installed reader | Not reached | No result | More than 2.1 GB | None |
| Final `Bundle` load | 669 | 0.26 seconds | 17,170,432 bytes | 669 notes |
| Final 900-token pack | 669 | 0.30 seconds | 38,436,864 bytes | 370 words; 3,162 bytes |

The integrated release candidate passed 940 tests without optional
dependencies, with 71 parser-dependent tests skipped. It passed 1,011 tests
with tree-sitter enabled. Both configurations also passed 735 subtests. Branch
coverage was 93 percent, which meets the repository floor. Repository-wide
pyflakes checks passed for `src` and `tests`.

Two independent code reviews passed the final implementation. Earlier reviews
rejected a 428-line Git-ignore parser because its matching semantics diverged
from Git and its write boundary was incomplete. The replacement uses one
bounded root `.muninnignore` with literal file and directory entries. Later
adversarial passes found write races involving symbolic links, FIFOs, and hard
links. The final writer creates a private file, verifies it, and atomically
replaces the destination through an open parent-directory descriptor.

## Interpretation

The result establishes that configured exclusions prevent the reproduced
generated-tree failure in this knowledge base while preserving 669 loadable notes. It
does not establish a universal memory bound for an arbitrary unconfigured
bundle. Muninn still reads every eligible Markdown file outside the configured
boundary. The timing also represents one machine, one filesystem state, and
one run; it does not estimate run-to-run variation.

## Integration status and remaining test

The bounded reader is integrated with hybrid source retrieval in the release
candidate. The combined configurations and coverage check reported above
passed on 23 August 2026. A later synthetic study should vary eligible note
count, excluded directory size, mount count, and rule count across repeated
runs. That study would measure scaling and variation without using a private live bundle.
