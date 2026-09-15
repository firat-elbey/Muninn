# Public context-efficiency evaluation

This deterministic evaluation measures pack size and answer-span availability. It does not measure generated-answer accuracy or agent task completion.

The fixed corpus contains all 18 Aurora questions and four adversarial cases. Each variant runs at 200, 400, and 900 estimated tokens with five candidate notes.

The no-index condition uses the existing index omission option. It isolates savings from that option before adding compact extraction.

Expected spans remain outside retrieval inputs. Complete-evidence scoring requires every fact group. The separate legacy score accepts any original answer substring.

| Variant | Aurora complete | Legacy any span | Adversarial complete | Estimated tokens | Budget violations | Forbidden evidence |
|---|---:|---:|---:|---:|---:|---:|
| baseline-standard | 51/54 | 51/54 | 6/9 | 21570 | 1 | 0 |
| current-standard | 51/54 | 51/54 | 6/9 | 21806 | 0 | 0 |
| current-no-index | 51/54 | 51/54 | 6/9 | 16868 | 0 | 0 |
| current-compact | 51/54 | 51/54 | 9/9 | 15808 | 0 | 0 |

| Comparison | Estimated reduction | Evidence gains | Evidence losses |
|---|---:|---:|---:|
| current-standard vs baseline-standard | -1.09% | 0 | 0 |
| current-compact vs baseline-standard | 26.71% | 3 | 0 |
| current-compact vs current-no-index | 6.28% | 3 | 0 |
| current-compact vs current-standard | 27.51% | 3 | 0 |

The following counts use `cl100k_base` from tiktoken 0.14.0.

| Variant | Tokenizer tokens | Tokenizer budget exceedances |
|---|---:|---:|
| baseline-standard | 21454 | 5 |
| current-standard | 21684 | 5 |
| current-no-index | 17354 | 4 |
| current-compact | 17525 | 9 |

| Comparison | Tokenizer reduction |
|---|---:|
| current-standard vs baseline-standard | -1.07% |
| current-compact vs baseline-standard | 18.31% |
| current-compact vs current-no-index | -0.99% |
| current-compact vs current-standard | 19.18% |

These counts are exact for the named tokenizer. They remain a proxy for models with a different tokenizer.

Tokenizer budget exceedances show where the production character estimate is insufficient. They are separate from violations of the declared estimate budget.

The same questions repeat at three budgets, so the 66 observations are not independent samples. The four new fixtures are development diagnostics, not an untouched confirmation set.

Muninn's character-based token estimate is the budget contract. It is not the active model tokenizer. The artifact also records UTF-8 bytes, per-case outputs, and one-run retrieval latency.

Complete blocks can exceed a small budget. An omitted fact is scored as unavailable, even when a source reference allows a later read. Follow-up read costs are not measured.

The qualification fixture uses one paragraph. These results do not establish preservation of qualifications across separate paragraphs.

The baseline revision is `33c5acd3cba2db192d0a3f6329e8d2e4383f4048`. The frozen label SHA-256 is `9f0848dd5184a28b1e4577e7647788416b09c9257dcfef9ded56194a8d59e00d`.

Reproduce with an immutable baseline source export and the following command:

```bash
python eval/context_efficiency.py --baseline-source /tmp/muninn-baseline/src --output /tmp/context-efficiency.json --report /tmp/context-efficiency.md --tokenizer cl100k_base
```
