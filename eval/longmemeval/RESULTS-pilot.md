# LongMemEval pilot results

Muninn answered 34 of 55 questions correctly, compared with 33 for flat
retrieval and 4 without memory. The one-answer difference between Muninn and
flat retrieval does not establish an overall advantage.

The pilot used a stratified 55-question sample from `longmemeval_s_cleaned`,
which contains 500 instances with approximately 48 sessions and 120,000
tokens of chat history per question. The sample emphasized knowledge-update
questions and used seed 11. Each condition received a 1,200-token budget and
at most six notes. Claude Haiku answered the questions and judged the answers
with the official LongMemEval prompts.

| Condition | Accuracy | Mean pack tokens | Errors |
|---|---:|---:|---:|
| No memory | 4/55 | 0 | 0 |
| Flat retrieval | 33/55 | 1,161 | 0 |
| Muninn | 34/55 | 1,143 | 0 |

## Accuracy by question type

| Condition | Abstention | Knowledge update | Multi-session | Single-session assistant | Single-session preference | Single-session user | Temporal reasoning |
|---|---:|---:|---:|---:|---:|---:|---:|
| No memory | 4/5 | 0/15 | 0/8 | 0/6 | 0/5 | 0/8 | 0/8 |
| Flat retrieval | 4/5 | 10/15 | 1/8 | 2/6 | 0/5 | 8/8 | 8/8 |
| Muninn | 3/5 | 13/15 | 1/8 | 2/6 | 0/5 | 7/8 | 8/8 |

## Runtime and estimated full-run cost

The pilot completed 165 answer calls and 165 judge calls in seven minutes
with four workers, or approximately 5.0 seconds per call. Pack construction
required five seconds.

A 500-question run with the same three conditions would require approximately
3,000 model calls and one hour with four workers at the observed rate. Ingest
would require approximately three additional minutes, based on the measured
16-second ingest of 55 bundles and an estimated 125,000 notes. The
contemporaneous API estimate assumed approximately 6 million input tokens,
0.3 million output tokens, and USD 9 under the stated Haiku 4.5 prices of USD
1 and USD 5 per million tokens, respectively. Subscription-based Claude CLI
use incurred no marginal charge.

## Observations

Knowledge-update questions produced four disagreements that favored Muninn
and one that favored flat retrieval. In the inspected cases, flat retrieval
returned superseded values such as a 3-2 record and a Thursday class, whereas
Muninn returned the updated 5-2 record, Friday class, Premier Silver status,
and 70-200 mm lens. Muninn obtained this result through the recency gradient
created by consolidation decay and same-session co-activation. The adapter
did not deduplicate repeated facts, so per-note recurrence remained one.
Results for the other question types differed by no more than one answer.

Multi-session accuracy was 1/8 under both retrieval conditions, and preference
accuracy was 0/5. Transcript inspection found that the required evidence
often spanned more than the six notes permitted by the budget. Temporal
reasoning reached 8/8 under both conditions because note titles retained
session dates and the prompt included the question date. The eight-question
cell is too small for a stable estimate.

Manual review identified two Muninn responses that appeared consistent with
the available evidence but received negative judgments. One response gave
the supported duration and added date arithmetic. The other named the known
taxi price and stated that the bus fare was absent, as the rubric required.
This review is qualitative and does not replace independent annotation. All
165 judge outputs began with `yes` or `no`; no answer was empty, and no
subprocess failed.

## Limitations

- The sample contains 55 of 500 questions. Each type contains 5 to 15
  questions, so one changed judgment moves a type-level result by 7 to 20
  percentage points. The approximate 95 percent interval for overall
  accuracy spans 13 percentage points on either side of the estimate.
- Claude Haiku applied the official LongMemEval prompts, but its human
  agreement was not measured in this experiment. The LongMemEval paper used
  GPT-4o and reported approximately 97 percent human agreement. These results
  are therefore not directly comparable with the paper's leaderboard.
- The no-memory condition necessarily abstains on answerable questions and
  performs well on abstention questions. It provides a floor rather than a competitive retrieval condition.
- Six notes within 1,200 tokens truncate many focus notes because individual
  chat turns often contain approximately 500 tokens. Flat retrieval and
  Muninn received the same constraint, but the budget limits absolute
  accuracy.
- Each note represents one user-to-assistant exchange. Retrieval was lexical,
  and temporal calculations depended on dates that survived truncation.
- The experiment replayed chronology once by touching each note and
  consolidating between sessions. It did not apply goals, pins, outcomes, or
  supersession events. The resulting dynamics signal was primarily recency
  and co-activation rather than repeated strengthening of one note.
