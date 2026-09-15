# Evidence and claim limits

Muninn has measured improvements on bounded knowledge loading, source-file
localization, and dependency reconstruction. It has not established broad
superiority over every memory or retrieval system. Each result applies only to
the dataset, revision, metric, and system configuration stated in its report.

## Results used by the current implementation

| Capability | Evidence | Result | Product decision |
|---|---|---|---|
| Bounded bundle loading | One reproduced 669-note knowledge base | The prior reader exceeded 2.1 GB without returning. The bounded reader loaded the notes in 0.26 seconds with 17.2 MB maximum resident memory. | Enforce explicit note, byte, directory, entry, and depth limits. |
| Source-file localization | 300 SWE-bench Lite issues in 12 Python repositories | The BM25F-led hybrid achieved 0.447 hit@1 and 0.800 hit@10. Token overlap achieved 0.200 and 0.603. | Use BM25F for the first result and combine BM25F with exact retrieval below it. |
| Persistent ranking parity | The same 300 SWE-bench Lite cases | SQLite reproduced every in-memory lexical ranking. Median reopen time was 0.354 ms. The measured index occupied 1.40 times the source bytes. | Use the persistent index, retain atomic rebuilding, and report storage as an unresolved efficiency limit. |
| Directed dependency reconstruction | 288 DependEval confirmation cases in eight languages | The optional hybrid reached 0.611 edge F1. Graphify reached 0.566 and the internal extractor reached 0.396. | Retain the dependency resolver as an optional capability. Do not use graph fusion as the ordinary localization default. |
| Passive entity context | 555 fixed BrainBench page-selection decisions | The public `volunteer` path reproduced all 555 decisions with no mismatch. | Serve at most one page by default after exact unique identity matching, and suppress repeated service within a session. |
| Compact memory excerpts | 18 public questions and four diagnostic fixtures, each at three budgets | Compact output retained 51/54 original and 9/9 diagnostic complete evidence cases. The baseline retained 51/54 and 6/9. Compact output used 18.31% fewer `cl100k_base` tokens than the baseline, but 0.99% more than the existing no-index option. | Keep compact selection optional. Evaluate complete continuation cost before changing the default. |

The complete reports and machine-readable artifacts are available here:

- [Bounded bundle loading](../eval/RESULTS-scalable-bundle-loading.md)
- [SWE-bench Lite source localization](../eval/code_retrieval/RESULTS-swebench-lite-300.md)
- [Persistent source-index parity](../eval/code_retrieval/RESULTS-persistent-parity-swebench-lite-300.md)
- [DependEval confirmation](../eval/competitive/RESULTS-dependeval-series5-confirmation.md)
- [BrainBench integration equivalence](../eval/competitive/RESULTS-BRAINBENCH-VOLUNTEER-INTEGRATION-EQUIVALENCE-v1.md)
- [Paired context efficiency](../eval/RESULTS-context-efficiency-2026-09.md)

The context-efficiency result measures available source evidence, not generated
answers. Its four diagnostic fixtures are development cases. Compact output
exceeded the nominal budget in nine of 66 packs under `cl100k_base`, despite
meeting Muninn's character-estimate limit. Later source reads and model costs
are not measured. The [September review](REVIEW-2026-09.md) records broader
verification limits and remaining work.

## Rejected default changes

Direct graph fusion raised hit@10 from 0.800 to 0.807 on SWE-bench Lite, but
the paired difference was not reliable and hit@1 fell from 0.447 to 0.253.
Graph results therefore remain separate from the ordinary primary ranking.

An evidence-gated agent pilot also failed its preregistered threshold. Its
final-file F1 exceeded the prefix control by 0.0056, below the required 0.020,
and trailed the selective control by 0.0208. The candidate did not proceed to
validation. The [negative result](../eval/competitive/RESULTS-ARB-SERIES11-evidence-gated-pilot-v6.md)
is retained to prevent the rejected method from becoming a default without new
evidence.

Chronological exact-file and familiar-directory learning changed rankings but
did not improve the registered SWE-bench recall target. The implementation
retains bounded personal-ranking functions for further research, but the
public `source search` command does not apply them.

## Unsupported claims

The current evidence does not establish patch correctness, end-to-end agent
task completion, multilingual file localization, longitudinal learning gains,
or superiority across all competitors. Public claims must not infer those
outcomes from the results above.
