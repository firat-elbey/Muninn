# LongMemEval walk results

The multi-cue walk met its stated criterion. It answered 44 of 121
multi-session questions correctly, compared with 36 for flat retrieval, and
3 of 30 preference questions correctly, compared with zero for flat
retrieval.

The experiment used 151 questions from `longmemeval_s_cleaned`: all 121
answerable multi-session questions and all 30 single-session preference
questions. Each condition received a 1,200-token budget and at most six
notes. Claude Haiku answered the questions and judged the answers with the
official LongMemEval prompts.

| Condition | Accuracy | Mean pack tokens | Errors |
|---|---:|---:|---:|
| Flat retrieval | 36/151 | 1,167 | 0 |
| Single-cue Muninn walk | 40/151 | 1,136 | 0 |
| Multi-cue Muninn walk | 47/151 | 1,135 | 0 |

## Success criterion

The multi-cue walk had to exceed flat retrieval on both multi-session and
single-session preference questions at the same 1,200-token budget. It met
both conditions.

| Question type | Flat retrieval | Multi-cue walk | Difference |
|---|---:|---:|---:|
| Multi-session | 36/121 | 44/121 | +8 |
| Single-session preference | 0/30 | 3/30 | +3 |

## Accuracy by question type

| Condition | Multi-session | Single-session preference |
|---|---:|---:|
| Flat retrieval | 36/121 | 0/30 |
| Single-cue Muninn walk | 40/121 | 0/30 |
| Multi-cue Muninn walk | 44/121 | 3/30 |

## Runtime and estimated full-run cost

The experiment completed 453 answer calls and 453 judge calls in 22 minutes
with four workers, or approximately 5.8 seconds per call. Pack construction
required eight seconds.

A 500-question run with the same three conditions would require approximately
3,000 model calls and 1.2 hours with four workers at the observed rate. Ingest
would require approximately three additional minutes, based on the measured
16-second ingest of 55 bundles and an estimated 125,000 notes. The
contemporaneous API estimate assumed approximately 6 million input tokens,
0.3 million output tokens, and USD 9 under the stated Haiku 4.5 prices of USD
1 and USD 5 per million tokens, respectively. Subscription-based Claude CLI
use incurred no marginal charge.

## Observations

The paired multi-session results contained 14 cases in which the multi-cue
walk was correct and flat retrieval was wrong, and six cases with the reverse
outcome. The net difference was eight answers among 121 questions. A
one-sided McNemar test gave an approximate p-value of 0.06, which did not
cross the 0.05 threshold. Preference results contained three disagreements
that favored the multi-cue walk and none that favored flat retrieval; the
approximate one-sided p-value was 0.13.

The single-cue Muninn walk added four correct multi-session answers relative
to flat retrieval and added no preference answers. Multi-cue facets and
coverage allocation added four further multi-session answers and all three
preference answers. These components therefore accounted for half of the
observed multi-session difference and all of the observed preference
difference in this sample.

The `muninn` condition in this experiment used the measured walk with one
cue. It did not use the pilot experiment's one-hop maximum-inheritance method.
The pilot and this experiment also used different samples, so their
type-level results are not directly comparable.

The dataset contained 133 multi-session questions rather than the 32
estimated during planning. The experiment included all 121 answerable
multi-session questions and excluded 12 abstention variants so that the two
criterion cells contained only answerable questions.

The degree-normalized walk divides a matching note's outgoing weight among
the 10 to 30 notes in its same-session group. This design bounds the effect
of any one relationship. Convergent weight from several cue facets can still
raise a related note. Preference accuracy remained low at 3/30, which
motivated a separate test of an optional local semantic seed channel.

## Limitations

- The sample contains 151 of 500 questions. The multi-session cell is
  substantial, but the 30-question preference cell remains imprecise.
- Claude Haiku applied the official LongMemEval prompts, but its human
  agreement was not measured in this experiment. The LongMemEval paper used
  GPT-4o and reported approximately 97 percent human agreement. These results
  are therefore not directly comparable with the paper's leaderboard.
- Six notes within 1,200 tokens truncate many focus notes because individual
  chat turns often contain approximately 500 tokens. All conditions received
  the same constraint, but the budget limits absolute accuracy.
- Each note represents one user-to-assistant exchange. Retrieval was lexical,
  and temporal calculations depended on dates that survived truncation.
- The experiment replayed chronology once by touching each note and
  consolidating between sessions. It did not apply goals, pins, outcomes, or
  supersession events.
