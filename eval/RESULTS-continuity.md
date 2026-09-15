# Continuity eval: does a fresh session pick up where the last left off?

Harness: [continuity_eval.py](continuity_eval.py): deterministic
answer-in-context scoring (a model cannot recall what it was never
shown), 6 threads x journaled decisions/rationale/next-steps over 40
distractor notes, budget 900 tokens.

```
A. pointed questions about prior-session decisions (12):
   episodes in bundle, pack only      12/12  (~439 tok avg)
   + where-we-left-off section        12/12  (~550 tok avg)

B. session boot: situation cue only, no question asked:
   pack only (pre-episodic boot)       0/2   (~469 tok)
   + where-we-left-off section         2/2   (~564 tok)
```

Two findings:

1. **Encoding is the fix for questions.** Once sessions are journaled
   as episode notes, ordinary recall answers "what did we decide about
   X and why" perfectly: the knowledge stopped dying with the context
   window the moment it became notes.
2. **The boot section is the fix for the first minute.** At session
   start no question exists yet, so lexical retrieval has nothing to
   bite on: 0/2. "Where we left off" (situation-matched thread, else
   freshest-within-30d) puts the current state and next step in the
   model's first screen for ~95 tokens: the difference between a
   stranger and a colleague who remembers Friday.
