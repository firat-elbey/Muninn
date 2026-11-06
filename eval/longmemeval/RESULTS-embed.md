# LongMemEval semantic-seed pilot results

The local semantic seed channel answered 3 of 30 preference questions
correctly, compared with 2 for the lexical Muninn walk. This one-answer
difference is insufficient evidence of an accuracy improvement.

The experiment used all 30 single-session preference questions in its sample
from `longmemeval_s_cleaned`. Each condition received a 1,200-token budget and
at most six notes. Claude Haiku answered the questions and judged the answers
with the official LongMemEval prompts.

| Condition | Accuracy | Mean pack tokens | Errors |
|---|---:|---:|---:|
| Flat retrieval | 1/30 | 1,169 | 0 |
| Lexical Muninn walk | 2/30 | 1,144 | 0 |
| Muninn walk with semantic seeds | 3/30 | 1,144 | 0 |

## Success criterion

The semantic-seed condition had to exceed the lexical walk on preference
questions at the same 1,200-token budget. It exceeded the lexical walk by one
answer, but the result is within the resolution of this 30-question sample
and a nondeterministic judge. The experiment therefore demonstrated that the
local channel operated without reducing measured accuracy; it did not demonstrate a meaningful preference-recall improvement.

The local CPU service used `llama-server` with
`nomic-embed-text-v1.5` through a loopback connection and processed
approximately 3,800 requests. The lexical conditions remained lexical. The
run did not persist an `embeddings.json` cache, so it repeated bundle
embeddings. This is an efficiency defect rather than an accuracy defect. A
larger preference sample and an independently validated judge are required before enabling the channel by default.

## Runtime and estimated full-run cost

The experiment completed 90 answer calls and 90 judge calls in six minutes
with four workers, or approximately 7.4 seconds per call. Pack construction
required five seconds.

A 500-question run with the same three conditions would require approximately
3,000 model calls and 1.5 hours with four workers at the observed rate. Ingest
would require approximately three additional minutes, based on the measured
16-second ingest of 55 bundles and an estimated 125,000 notes. The
contemporaneous API estimate assumed approximately 6 million input tokens,
0.3 million output tokens, and USD 9 under the stated Haiku 4.5 prices of USD
1 and USD 5 per million tokens, respectively. Subscription-based Claude CLI
use incurred no marginal charge.

## Limitations

- The sample contains 30 questions. Each changed judgment moves the result by
  approximately 3.3 percentage points.
- Claude Haiku applied the official LongMemEval prompts, but its human
  agreement was not measured in this experiment. The LongMemEval paper used
  GPT-4o and reported approximately 97 percent human agreement. These results
  are therefore not directly comparable with the paper's leaderboard.
- Six notes within 1,200 tokens truncate many focus notes because individual
  chat turns often contain approximately 500 tokens. All conditions received
  the same constraint, but the budget limits absolute accuracy.
- Each note represents one user-to-assistant exchange. Retrieval was lexical
  except for the optional semantic seed channel in its named condition.
- The experiment replayed chronology once by touching each note and
  consolidating between sessions. It did not apply goals, pins, outcomes, or
  supersession events.
