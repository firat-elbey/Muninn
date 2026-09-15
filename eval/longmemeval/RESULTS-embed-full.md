# LongMemEval semantic-seed comparison results

The local semantic seed channel did not meet its stated criterion. It
answered no preference questions correctly, compared with one for the lexical
Muninn walk, while total accuracy remained 44 of 151 under both conditions.

The experiment used 151 questions from `longmemeval_s_cleaned`: 121
multi-session questions and 30 single-session preference questions. Each
condition received a 1,200-token budget and at most six notes. Claude Haiku
answered the questions and judged the answers with the official LongMemEval
prompts.

| Condition | Accuracy | Mean pack tokens | Errors |
|---|---:|---:|---:|
| Lexical Muninn walk | 44/151 | 1,135 | 0 |
| Muninn walk with semantic seeds | 44/151 | 1,135 | 0 |

## Success criterion

The semantic-seed condition had to exceed the lexical walk on preference
questions at the same 1,200-token budget. It did not meet that criterion.

| Question type | Lexical Muninn walk | Semantic-seed walk | Difference |
|---|---:|---:|---:|
| Multi-session | 43/121 | 44/121 | +1 |
| Single-session preference | 1/30 | 0/30 | -1 |

The opposite one-answer changes canceled in the aggregate. This experiment
does not support enabling semantic seeds by default.

## Runtime and estimated full-run cost

The experiment completed 302 answer calls and 302 judge calls in 14 minutes
with four workers, or approximately 5.7 seconds per call. Pack construction
required 20 seconds.

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
- Each note represents one user-to-assistant exchange. Retrieval was lexical
  except for the optional semantic seed channel in its named condition.
- The experiment replayed chronology once by touching each note and
  consolidating between sessions. It did not apply goals, pins, outcomes, or
  supersession events.
