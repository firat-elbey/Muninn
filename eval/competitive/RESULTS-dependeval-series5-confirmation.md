# DependEval dependency recognition, series 5 confirmation

The hybrid candidate reached 0.611 macro edge F1, compared with 0.396 for Muninn tree extraction and 0.566 for Graphify. The candidate passed confirmation on this named dependency task.

## Primary result

| method | edge F1 | precision | recall | exact graph | node F1 | p50 ms | p95 ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| Muninn hybrid | 0.611 | 0.620 | 0.649 | 0.247 | 0.740 | 127.8 | 458.7 |
| Muninn tree | 0.396 | 0.400 | 0.421 | 0.149 | 0.498 | 15.8 | 40.2 |
| Graphify | 0.566 | 0.602 | 0.580 | 0.243 | 0.675 | 232.2 | 514.0 |

## Paired comparisons

| candidate minus | difference | gains | losses | ties | exact p | 95% paired bootstrap interval |
|---|---:|---:|---:|---:|---:|---:|
| Muninn tree | +0.216 | 100 | 1 | 187 | 8.046e-29 | [+0.177, +0.255] |
| Graphify | +0.046 | 32 | 13 | 243 | 0.006609 | [+0.025, +0.068] |

## Language results

| language | hybrid | Muninn tree | Graphify | hybrid minus Muninn |
|---|---:|---:|---:|---:|
| c | 0.556 | 0.000 | 0.541 | +0.556 |
| c# | 0.827 | 0.308 | 0.827 | +0.520 |
| c++ | 0.523 | 0.000 | 0.511 | +0.523 |
| java | 0.245 | 0.245 | 0.208 | +0.000 |
| javascript | 0.902 | 0.902 | 0.890 | +0.000 |
| php | 0.115 | 0.000 | 0.115 | +0.115 |
| python | 0.834 | 0.834 | 0.515 | +0.000 |
| typescript | 0.887 | 0.874 | 0.887 | +0.013 |

## Scope and decision

The comparison contains 288 cases from folds [4] and eight languages. Every system received the same named files and source text. Each run recorded zero network calls and zero model calls.

Confirmation ran once, as preregistered.

The hybrid uses Graphify for C, C++, C#, PHP, and TypeScript source files. It uses Muninn for Java, JavaScript, and Python. Graphify is optional; an unavailable provider returns the internal graph and records the error.

The candidate passed confirmation on this named dependency task.

This result measures directed dependency reconstruction. It does not establish semantic retrieval, longitudinal learning, or agent task success.

## Reproduction record

- DependEval revision: `7c5f15bbd7ba5e032082bfe4595327fbc5d126b9`.
- Muninn revision: `83fd35ac22f029bf42c672dca7314f233ee5e060`.
- Candidate raw-result digest: SHA-256 `f91abf1797f85dde236c17d6def0d6801a9607734fb170e7b7bbed14da56e1e7`.
- Muninn raw-result digest: SHA-256 `7a3aeac8101ae0b89049669fca206fee7de84f604a5fb23237ef84b7892c6ca6`.
- Graphify raw-result digest: SHA-256 `6f157da9384014f7b306fbd4a32d629e2d3cf524020c7e247a9b8e9087847334`.

The public repository excludes the three raw-result archives because the
named DependEval revision does not declare a redistribution license. The
digests preserve the identity of the analyzed files. Reproduction requires an
independently obtained copy of the upstream data and a new local execution of
`run_dependeval.py` followed by `analyze_dependeval.py`.
