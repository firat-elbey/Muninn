# Persistent source-index evaluation

The persistent index reproduced all in-memory rankings across 300 SWE-bench Lite cases. The four lexical conditions matched the earlier source artifact exactly.

## Setup

Each case was exported at its recorded base commit and scanned once. The in-memory and SQLite indexes received the same source records. The SQLite index stored compressed postings and typed edges but not source bodies.

## Ranking and latency

| condition | source parity | hit@1 | hit@10 | MRR | memory p50 | persistent p50 | persistent p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| overlap | 300/300 | 0.200 | 0.603 | 0.324 | 6.0 ms | 11.6 ms | 34.0 ms |
| bm25f | 300/300 | 0.447 | 0.773 | 0.561 | 49.3 ms | 38.2 ms | 136.8 ms |
| lexical-hybrid | 300/300 | 0.397 | 0.797 | 0.541 | 50.4 ms | 50.1 ms | 174.7 ms |
| hybrid | 300/300 | 0.447 | 0.800 | 0.577 | 51.3 ms | 48.7 ms | 187.1 ms |
| graph-fusion | 19/300 | 0.263 | 0.803 | 0.447 | 54.5 ms | 73.3 ms | 202.1 ms |

## Build, open, and storage

| measure | result |
|---|---:|
| source scan p50 | 2.636 s |
| in-memory build p50 | 23.623 s |
| persistent build p50 | 24.205 s |
| persistent reopen p50 | 0.354 ms |
| persistent reopen p95 | 0.762 ms |
| source bytes | 4,106,956,669 |
| index bytes | 5,748,092,928 |
| index-to-source ratio | 1.40 |

## Graph change

The graph condition is not a parity target because the candidate adds cross-language module aliases and relation labels. Relative to the earlier graph, hit@1 changed by +0.010, hit@10 by -0.003, and MRR by +0.006. These values are diagnostic; the graph cannot enter primary ranking without the separate relational test.

## Decision

Retain the persistent index as the operational candidate. Do not change the public default until the complete case set and the relational, longitudinal, and agent-level gates pass.

## Artifacts

- persistent source run: `RESULTS-persistent-parity-swebench-lite-300.json.gz`, SHA-256 `b0866f622cff7e36b8d0ff3ad2a0c26c6172c05f9559bb690468d64f617174fa`.
- original source run: `RESULTS-swebench-lite-300.json.gz`, SHA-256 `032cdedabcd2c51cd49fb1e97b0951d348bb7685cb0c6dee7841051ef43ac640`.
