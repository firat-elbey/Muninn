#!/usr/bin/env python3
"""Continuity eval: does a FRESH session see what the last one decided?

The session-amnesia problem: context dies with the session, so a new
session answers "what did we decide about X?" from nothing. This eval
measures the fix deterministically (no LLM): build a workspace with
distractor repo notes + real threads (decisions, rationale, next steps
journaled across sessions), then for each question compose exactly what
a fresh session would be handed at boot :

  cold      the context pack alone (pre-episodic muninn)
  threads   "Where we left off" + the pack (episodic muninn)

: and score whether the ANSWER SPAN (the decision, the reason, the next
step) appears verbatim in the served text, within budget. Answer-in-
context availability is the relevant proxy because a model cannot recall what it was never
shown.

    python3 eval/continuity_eval.py          # prints the table
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))
from muninn import journal                       # noqa: E402
from muninn.dynamics import Dynamics             # noqa: E402
from muninn.recall import context_pack, estimate_tokens  # noqa: E402
from muninn.store import Bundle                  # noqa: E402

BUDGET = 900

# (thread, head/state, episode): decisions carry distinctive spans so
# containment can't false-positive off distractors
THREADS = [
    ("Cache Layer",
     "DECIDED: warthog-cache over blimp-cache (eviction jitter under "
     "bursts). NEXT: wire the ttl sweeper. OPEN: cold-start warmup.",
     "Benchmarked both; blimp-cache showed 40ms eviction jitter under "
     "burst load, warthog steady. Rejected blimp-cache."),
    ("Auth Flow",
     "DECIDED: magic links only, no passwords (support burden). "
     "NEXT: token store moves to redis with 15m expiry.",
     "User pushed back on passwords twice; settled on magic links. "
     "Redis chosen for the token store, 15 minute expiry."),
    ("Pack Format",
     "DECIDED: token shares proportional to activation, floored at 80. "
     "NEXT: coverage-first selection for multi-part questions.",
     "Tried fixed per-note budgets first: too fiddly, reverted."),
    ("Billing Webhooks",
     "DECIDED: idempotency keys on every handler (double-charge bug). "
     "NEXT: replay tooling for dropped events.",
     "The stripe retry storm double-charged 3 users; every webhook "
     "handler now requires an idempotency key."),
    ("Search Migration",
     "DECIDED: postpone opensearch move until Q4 (index rebuild cost). "
     "NEXT: shim the query API so the move is a flag flip.",
     "Estimated 30h index rebuild; decided to postpone to Q4 and build "
     "the query shim now."),
    ("Onboarding Emails",
     "DECIDED: drip of 3 emails max (unsubscribe spike at 5). "
     "NEXT: A/B the day-2 subject line.",
     "The 5-email drip spiked unsubscribes to 4 percent; capped at 3."),
]

# (question a fresh session gets asked, the span the served text must contain)
QUESTIONS = [
    ("what did we decide for the cache layer and why",
     "warthog-cache over blimp-cache"),
    ("why did we reject blimp cache", "eviction jitter"),
    ("what's next on the cache work", "ttl sweeper"),
    ("what did we settle on for auth passwords or magic links",
     "magic links only"),
    ("where does the auth token store live now", "redis"),
    ("how are pack token shares computed", "proportional to activation"),
    ("what happened with fixed per-note budgets", "too fiddly"),
    ("why do billing webhooks need idempotency keys", "double-charge"),
    ("what's the plan for the opensearch migration", "postpone"),
    ("why postpone the search migration", "index rebuild"),
    ("how many onboarding emails do we send", "3"),
    ("what's next for onboarding emails", "day-2 subject"),
]


def build_workspace(root: str) -> None:
    b = Bundle(root)
    for i in range(40):  # distractor "repo" notes with overlapping words
        b.write_note(f"src/mod{i}.md",
                     {"title": f"Module {i}"},
                     f"module {i} handles cache auth search email webhook "
                     f"pack utilities for subsystem {i}.")
    b = Bundle(root)
    d = Dynamics(root)
    import time
    base = time.time() - len(THREADS) * 86400  # one thread per "day",
    for i, (name, state, episode) in enumerate(THREADS):  # last = freshest
        journal.add_episode(b, d, name, episode, state=state,
                            when=base + i * 86400)


def served_text(root: str, question: str, with_threads: bool) -> str:
    b, d = Bundle(root), Dynamics(root)
    parts = []
    if with_threads:
        parts.append(journal.threads_section(b, d, cue=question,
                                             session=None))
    parts.append(context_pack(b, d, question, budget=BUDGET,
                              reactivate=False))
    return "\n".join(parts)


# Scenario B: session BOOT: no question yet, just the situation. The
# "one agent" feel lives here: does the boot text already carry the
# freshest thread's state and next step? (The last thread journaled is
# Onboarding Emails.) A vague situation cue can't lexically single out
# the thread: this is exactly what the fallback-to-freshest serves.
BOOT_CUE = "myapp main src work"
BOOT_SPANS = ("drip of 3 emails max", "day-2 subject")


def main() -> None:
    root = tempfile.mkdtemp(prefix="muninn-cont-")
    try:
        build_workspace(root)
        print(f"budget {BUDGET} tokens\n")
        print("A. pointed questions about prior-session decisions "
              f"({len(QUESTIONS)} questions):")
        for cond, with_threads in (("episodes in bundle, pack only", False),
                                   ("+ where-we-left-off section", True)):
            hits, toks = 0, 0
            misses = []
            for q, answer in QUESTIONS:
                text = served_text(root, q, with_threads)
                toks += estimate_tokens(text)
                if answer.lower() in text.lower():
                    hits += 1
                else:
                    misses.append(q)
            print(f"  {cond:34s} answer-in-context "
                  f"{hits}/{len(QUESTIONS)}  (~{toks // len(QUESTIONS)} "
                  "tok avg)")
            for m in misses:
                print(f"      miss: {m}")
        print("\nB. session boot: situation cue only, no question asked "
              f"(cue: {BOOT_CUE!r}):")
        for cond, with_threads in (("pack only (pre-episodic boot)", False),
                                   ("+ where-we-left-off section", True)):
            text = served_text(root, BOOT_CUE, with_threads)
            got = sum(1 for s in BOOT_SPANS if s.lower() in text.lower())
            print(f"  {cond:34s} freshest thread's state+next in boot: "
                  f"{got}/{len(BOOT_SPANS)}  (~{estimate_tokens(text)} tok)")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
