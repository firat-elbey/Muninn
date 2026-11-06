# November 2026 public benchmark results

## LongMemEval

Each question uses a 1200-token estimated budget and up to 6 notes. claude-haiku-4-5-20251001 answered and judged with tools disabled and a structured yes/no verdict. The run recorded 743 question-condition scores and 0 provider errors. Overlapping manifests reuse the same judgment for an identical question and pack. The preference manifest is a subset of the multi-session and preference manifest.

| Manifest | Questions | No memory | Flat | Muninn | Walk | Walk with embeddings |
|---|---:|---:|---:|---:|---:|---:|
| Pilot | 55 | 5 | 33 | 32 |  |  |
| Multi-session and preferences | 151 |  | 42 | 46 | 45 | 49 |
| Preferences | 30 |  | 2 |  | 0 | 2 |

The embedding condition used Nomic Embed Text v1.5 Q8_0 with an 8,192-token window.

### Bundle opening

Across the 193 public bundles and 47,893 notes, median opening time changed from 3.109 s to 0.250 s with 8 workers. All 193 link graphs and 579 rendered packs matched the control. These timings exclude answering and judging.

## SWE-bench Lite

The hybrid returned an accepted file at rank one on 134/300 cases, in the top five on 221/300, and in the top ten on 240/300. The labels measure source-file localization.

## DependEval

Across 288 cases, native extraction reached 0.3956 macro edge F1, the hybrid reached 0.6113, and standalone Graphify reached 0.5985. The hybrid gained on 21 cases, lost on 18 and tied on 249 (paired sign-test p=0.749). The historical confirmation criterion failed on this replay. The [registered report](competitive/RESULTS-dependeval-series5-confirmation.md) specifies the fold and explains the upstream redistribution limit.

The [measurement file](public-benchmarks-2026-11.json.gz) records dataset fingerprints, LongMemEval scoring decisions and pack digests, loader measurements and the two code benchmark aggregates. Rebuild this report with `python eval/public_benchmarks.py`.
