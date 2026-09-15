# ARB Series 11 evidence-gated retrieval pilot

The evidence-gated candidate did not pass the preregistered pilot gate.

| Condition | Final-file F1 | Useful context | Read tokens | Model input tokens | Failures |
| --- | ---: | ---: | ---: | ---: | ---: |
| candidate-prefix | 0.1861 | 0.6250 | 4959.8 | 223461.2 | 0.0000 |
| evidence-gated-leads | 0.1917 | 0.6250 | 5376.0 | 217543.2 | 0.0000 |
| selective-leads | 0.2125 | 0.7500 | 5654.9 | 223790.8 | 0.0000 |

The candidate-minus-prefix F1 difference is 0.0056; its one-sided 95 percent lower bound is -0.1461.

The candidate-minus-selective-control F1 difference is -0.0208; its one-sided 95 percent lower bound is -0.1022.

| Gate requirement | Result |
| --- | --- |
| baseline mean f1 gain at least 0.020 | fail |
| baseline one sided 95 lower bound above zero | fail |
| baseline repository weighted f1 gain positive | pass |
| component mean f1 gain at least 0.005 | fail |
| component one sided 95 lower bound above zero | fail |
| component repository weighted f1 gain positive | fail |
| component useful context not lower | fail |
| baseline useful context loss within 0.02 | pass |
| baseline read tokens within 110 percent | pass |
| baseline model input tokens within 110 percent | pass |
| component read tokens within 110 percent | pass |
| component model input tokens within 110 percent | pass |
| failure rate increase within 0.025 | pass |
| integrity validated | pass |

This adaptive pilot cannot establish superiority. A passing result only authorizes the registered untouched validation.
