# SWE-bench Lite file-localization evaluation

## Result

Across 300 completed cases, replacing overlap with the selected hybrid changed hit@1 from 20.0% to 44.7%, hit@5 from 46.7% to 73.3%, hit@10 from 60.3% to 80.0%.

At hit@10, the hybrid recorded 60 gains and 1 loss against overlap, with two-sided exact p-value <0.0001. Against BM25F, it recorded 14 gains and 6 losses, with two-sided exact p-value 0.1153.

## Method

The evaluation used 300 issues from SWE-bench Lite across 12 Python repositories. The issue statement was the query, and implementation files modified by the accepted patch provided relevance labels. No language model generated candidates or graded results.

The dataset revision was `6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2`. The evaluated Muninn revision was `f050095bc13bd9543d564f5f8f5d0dc910b42c36`. The count of cases with primary relevance labels outside the bounded index was 0. The run records 0 failed evaluation cases.

## Aggregate results

| Condition | hit@1 | hit@5 | hit@10 | MRR | Median query |
|---|---:|---:|---:|---:|---:|
| overlap | 0.200 | 0.467 | 0.603 | 0.324 | 5.6 ms |
| bm25f | 0.447 | 0.710 | 0.773 | 0.561 | 45.8 ms |
| lexical-hybrid | 0.397 | 0.720 | 0.797 | 0.541 | 49.8 ms |
| hybrid | 0.447 | 0.733 | 0.800 | 0.577 | 49.8 ms |
| graph-fusion | 0.253 | 0.683 | 0.807 | 0.441 | 51.8 ms |

The lexical hybrid fused BM25F and exact rankings at every position. The selected hybrid preserved the first BM25F result and fused the remaining ranks. Graph fusion added the two-step Python graph directly to that competition.

## Paired analysis

| Comparison | Metric | Gains | Losses | Ties | Exact p |
|---|---|---:|---:|---:|---:|
| hybrid vs. overlap | hit@1 | 87 | 13 | 200 | <0.0001 |
| hybrid vs. overlap | hit@5 | 85 | 5 | 210 | <0.0001 |
| hybrid vs. overlap | hit@10 | 60 | 1 | 239 | <0.0001 |
| hybrid vs. bm25f | hit@1 | 0 | 0 | 300 | 1.0000 |
| hybrid vs. bm25f | hit@5 | 14 | 7 | 279 | 0.1892 |
| hybrid vs. bm25f | hit@10 | 14 | 6 | 280 | 0.1153 |
| graph-fusion vs. hybrid | hit@1 | 18 | 76 | 206 | <0.0001 |
| graph-fusion vs. hybrid | hit@5 | 6 | 21 | 273 | 0.0059 |
| graph-fusion vs. hybrid | hit@10 | 6 | 4 | 290 | 0.7539 |

Direct graph fusion recorded 18 gains and 76 losses against the hybrid at hit@1. The paired results measure file localization and do not establish downstream task completion.

## Repository consistency

| Repository | Cases | overlap hit@10 | BM25F hit@10 | hybrid hit@10 | graph hit@10 |
|---|---:|---:|---:|---:|---:|
| astropy/astropy | 6 | 0.333 | 0.833 | 0.833 | 0.833 |
| django/django | 114 | 0.640 | 0.833 | 0.833 | 0.833 |
| matplotlib/matplotlib | 23 | 0.652 | 0.739 | 0.870 | 0.870 |
| mwaskom/seaborn | 4 | 0.750 | 1.000 | 1.000 | 1.000 |
| pallets/flask | 3 | 1.000 | 1.000 | 1.000 | 1.000 |
| psf/requests | 6 | 0.833 | 0.833 | 0.833 | 1.000 |
| pydata/xarray | 5 | 1.000 | 0.800 | 1.000 | 1.000 |
| pylint-dev/pylint | 6 | 0.167 | 0.500 | 0.333 | 0.333 |
| pytest-dev/pytest | 17 | 0.529 | 0.647 | 0.647 | 0.647 |
| scikit-learn/scikit-learn | 23 | 0.565 | 0.870 | 0.913 | 0.870 |
| sphinx-doc/sphinx | 16 | 0.562 | 0.625 | 0.562 | 0.562 |
| sympy/sympy | 77 | 0.558 | 0.714 | 0.779 | 0.805 |

The repository-macro hit@10 changed from 0.633 to 0.800. Against overlap, the repository counts were 8 improved, 0 worsened, and 4 tied.

## Operational profile

The median checkout contained 1,374 indexed Python files and 14,070 undirected graph edges. The in-memory index took 5.34 seconds at the median and 12.37 seconds at the 95th percentile. Hybrid queries took 49.8 ms at the median and 174.2 ms at the 95th percentile.

These build times measure repeated in-memory construction. They do not establish persistent-index storage or update cost.

## Failure analysis

Hybrid hit@10 losses against overlap occurred in `matplotlib__matplotlib-23314`.

Graph fusion hit@10 gains occurred in `django__django-13220`, `django__django-14016`, `psf__requests-2674`, `sympy__sympy-21612`, `sympy__sympy-17630`, `sympy__sympy-13031`.

Graph fusion hit@10 losses occurred in `django__django-10914`, `django__django-14752`, `scikit-learn__scikit-learn-13584`, `sympy__sympy-13043`.

## Limits and next evaluations

This evaluation measures file localization in Python. It does not measure dependency reconstruction, symbol or excerpt localization, patch correctness, answer quality, personal learning, persistent index updates, or another language. Accepted patch files are useful but imperfect relevance labels.

The next retrieval evaluation should test relational and multi-file queries so that typed graph edges can be assessed directly. A longitudinal evaluation should train usage only on earlier sessions and test later paraphrased tasks. An agent-level evaluation should hold the model, repository revision, context budget, and tool budget constant while comparing overlap, BM25F, and the selected hybrid.

## Reproduction

```bash
python3 eval/code_retrieval/run_swebench.py --instances-per-repo 999 --seed muninn-code-retrieval-v1
python3 eval/code_retrieval/analyze_swebench.py eval/code_retrieval/RESULTS-swebench-lite-300.json.gz --output report.md
```
