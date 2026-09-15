# Evaluation sources and licenses

This directory separates Muninn's original evaluation code and reports from
third-party benchmarks. Each report states its dataset, revision, metric,
configuration, and limits.

| Evaluation | External source | Material included in this repository | License treatment |
|---|---|---|---|
| Pack recall, continuity, loading, and field tests | None | Original fixtures, programs, and results | MIT License |
| LongMemEval | [LongMemEval](https://github.com/xiaowu0162/LongMemEval) and its `longmemeval_s_cleaned` dataset | Adapted judge prompts, selected question and answer records, and generated results | The included benchmark material retains its MIT License. The [third-party notices](../THIRD_PARTY_NOTICES.md) record the verified source revision and sample digests. |
| SWE-bench Lite | [SWE-bench](https://github.com/SWE-bench/SWE-bench) | Dataset identifiers, revision metadata, accepted patch paths, retrieval rankings, and generated analysis | The repository does not include issue statements or source repositories. The harness downloads the dataset and repositories from their original sources. |
| DependEval | [DependEval](https://github.com/ink7-sudo/DependEval) | An original harness and an aggregate report | The upstream revision does not declare a redistribution license. Muninn therefore excludes the dataset and raw result archives. Reproduction requires an independently obtained copy of the upstream data. |
| BrainBench integration | [GBrain](https://github.com/garrytan/gbrain) | Protocols, hashes, page-selection decisions, and generated verification results | The GBrain corpus is not included. The harness records the exact external revision. |

The compressed SWE-bench result files contain identifiers, hashes, paths,
rankings, and measurements. They do not contain issue statements or repository
source bodies. Generated caches, downloaded datasets, repository checkouts,
and evaluation workspaces are excluded through `.gitignore`.

Removing an external benchmark from the repository does not invalidate an
aggregate result. It changes the reproduction procedure: the researcher must
obtain the named revision from its owner and verify the recorded digest before
running the original Muninn harness.

## Context efficiency

The [context-efficiency report](RESULTS-context-efficiency-2026-09.md) compares
four pack formats on original public fixtures. Its [machine-readable results](RESULTS-context-efficiency-2026-09.json)
retain every output and score. The evaluator requires no model calls or
external dataset downloads.

The baseline is revision `33c5acd3cba2db192d0a3f6329e8d2e4383f4048`.
The evaluator verifies its exported source digest. Frozen labels never enter
the retrieval subprocess. Candidate source must remain unchanged during a run.

With an export of that revision at `/tmp/muninn-baseline`, run:

```bash
python eval/context_efficiency.py --baseline-source /tmp/muninn-baseline/src \
  --output /tmp/context-efficiency.json --report /tmp/context-efficiency.md
```

The report distinguishes repeated-budget observations from independent
questions. It measures evidence availability and a character-count token
estimate, not answer accuracy or complete continuation cost.

With the optional `tiktoken` package installed, add `--tokenizer cl100k_base`
to count rendered tokens separately. This dependency belongs to evaluation,
not the Muninn core. The report records its version and tokenizer budget
exceedances. Counts for one tokenizer do not establish costs for every model.
