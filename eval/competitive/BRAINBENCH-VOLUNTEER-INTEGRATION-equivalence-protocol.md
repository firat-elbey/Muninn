# BrainBench volunteer integration equivalence protocol

## Question

Does the production `volunteer` implementation at commit `5aabbed` reproduce the fixed entity-session candidate's page decisions on every BrainBench development and holdout retrieval turn?

## Locked inputs

The reference development output is the byte-identical candidate file with SHA-256 `7058120c6b334d54dce33e26be65a964ff680574a1f5ce03c93a93fc343c5ccf`. The reference holdout output has SHA-256 `d1920a6e24d70756ca480e5f488e88d2274b1ea8433ad7a08ff76b8a34b6d7e8`.

The corpus remains GBrain 0.44.0.0 at commit `75fae742d55ade4b29c1a574eb7b62bf5b053518`, with fixture identity `36867b644b699cc4dd4c0552005476417bc062f7e90c75716f88b6d2ba4feadc`. The comparison reads fixture turns and reference page decisions. It does not score or tune against gold.

## Procedure

For each unique condition, fixture, and user turn represented in the two reference outputs, the harness rebuilds the same fixture-scoped source bundle. Continuity readers also receive the paired writer's public seed pages. It calls the production `volunteer_explain` function with the registered cap and a per-fixture served-path set. Dynamics and reactivation remain disabled, as in the confirmed candidate.

The harness compares the ordered injected slug list and cross-source list with the reference. Duplicate suite rows must agree before they are collapsed into one retrieval decision. Development and holdout references must cover disjoint fixture identifiers.

## Decision

The integration passes only if every ordered slug list and every cross-source list is identical across both splits. Any mismatch rejects the production port. A pass establishes behavioral equivalence for BrainBench page selection; it does not re-estimate the already confirmed quality metrics or establish behavior outside this corpus.
