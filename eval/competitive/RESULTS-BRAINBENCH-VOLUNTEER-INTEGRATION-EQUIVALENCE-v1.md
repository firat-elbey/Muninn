# BrainBench volunteer integration equivalence

## Result

The production `volunteer` command reproduced every fixed page-selection decision from the confirmed entity-session candidate. The comparison covered 555 unique decisions and found no mismatch.

| Split | Decisions | Mismatches |
|---|---:|---:|
| Development | 474 | 0 |
| Holdout | 81 | 0 |
| Total | 555 | 0 |

The production implementation is commit `5aabbed`, with source tree `ddda2f9477e478e81da7532977f487b71c85f522`. The harness compared each ordered injected-page list and each cross-source list with fixed references. An independent verifier then seeded fresh bundles and reproduced every decision through the public command-line interface. Both checks passed.

## Interpretation

This result establishes behavioral equivalence for BrainBench page selection. The integrated command therefore retains the candidate behavior that passed the separate development and holdout quality gates: exact entity matching, one page by default, and suppression after the page has been served in the same session.

The result does not measure answer quality again. It does not establish performance on entities or corpora outside BrainBench, and it does not support replacing Muninn's broader explicit retrieval path.

## Evidence

- [Protocol](BRAINBENCH-VOLUNTEER-INTEGRATION-equivalence-protocol.md)
- [Primary result](evidence/brainbench-volunteer-integration-equivalence-v1/result.json)
- [Independent command-line verification](evidence/brainbench-volunteer-integration-equivalence-v1/independent-verification.json)
