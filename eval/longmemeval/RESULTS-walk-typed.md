# LongMemEval typed-walk results

The typed multi-cue walk met its stated criterion. It answered 40 of 121
multi-session questions correctly, compared with 36 for flat retrieval, and
3 of 30 preference questions correctly, compared with zero for flat
retrieval.

The experiment used 151 questions from `longmemeval_s_cleaned`: 121
multi-session questions and 30 single-session preference questions. Each
condition received a 1,200-token budget and at most six notes. Claude Haiku
answered the questions and judged the answers with the official LongMemEval
prompts.

| Condition | Accuracy | Mean pack tokens | Errors |
|---|---:|---:|---:|
| Flat retrieval | 36/151 | 1,167 | 0 |
| Typed multi-cue Muninn walk | 43/151 | 1,135 | 0 |

## Success criterion

The typed multi-cue walk had to exceed flat retrieval on both multi-session
and single-session preference questions at the same 1,200-token budget. It
met both conditions.

| Question type | Flat retrieval | Typed multi-cue walk | Difference |
|---|---:|---:|---:|
| Multi-session | 36/121 | 40/121 | +4 |
| Single-session preference | 0/30 | 3/30 | +3 |

## Runtime and estimated full-run cost

The experiment completed 302 answer calls and 302 judge calls in 15 minutes
with four workers, or approximately 5.9 seconds per call. Pack construction
required six seconds.

A 500-question run with the same two conditions would require approximately
2,000 model calls and 0.8 hours with four workers at the observed rate.
Ingest would require approximately three additional minutes, based on the
measured 16-second ingest of 55 bundles and an estimated 125,000 notes. At
approximately 2,000 input tokens per call, the projected input volume is 4
million tokens rather than the 6 million stated in the original generated
report. A proportional output estimate is 0.2 million tokens. Under the
contemporaneous Haiku 4.5 prices stated in that report, the corrected total is
approximately USD 5. Subscription-based Claude CLI use incurred no marginal
charge.

## Limitations

- The sample contains 151 of 500 questions. The 30-question preference cell
  remains imprecise, and each changed judgment moves it by approximately 3.3
  percentage points.
- Claude Haiku applied the official LongMemEval prompts, but its human
  agreement was not measured in this experiment. The LongMemEval paper used
  GPT-4o and reported approximately 97 percent human agreement. These results
  are therefore not directly comparable with the paper's leaderboard.
- Six notes within 1,200 tokens truncate many focus notes because individual
  chat turns often contain approximately 500 tokens. Both conditions received
  the same constraint, but the budget limits absolute accuracy.
- Each note represents one user-to-assistant exchange, and retrieval was
  lexical.
- The experiment replayed chronology once by touching each note and
  consolidating between sessions. It did not apply goals, pins, outcomes, or
  supersession events.
