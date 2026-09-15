"""Run the recall experiment: does the muninn pack beat the baselines?

Conditions (same token budget for every context-bearing condition):
  none    no context at all (floor)
  naive   flat lexical retrieval, blind to supersession: plain RAG-over-files
  format  flat lexical + supersedes-aware exclusion: the FORMAT claim
  muninn  format + potentiation dynamics: the DYNAMICS claim
  dump    alphabetical concatenation truncated at budget (naive stuffing)

Each question is answered by `claude -p --model haiku` strictly from the
pack; scored by expected-substring match. Writes results.json + RESULTS.md.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from muninn.dynamics import Dynamics  # noqa: E402
from muninn.recall import context_pack, estimate_tokens  # noqa: E402
from muninn.store import Bundle  # noqa: E402

CORPUS = os.path.join(HERE, "corpus")
BUDGET = int(os.environ.get("BUDGET", "900"))
K = int(os.environ.get("K", "5"))
MODEL = os.environ.get("MODEL", "haiku")
CONDITIONS = ["none", "naive", "format", "muninn", "dump"]


def build_pack(bundle: Bundle, dyn: Dynamics, cond: str, cue: str) -> str:
    if cond == "none":
        return ""
    if cond == "naive":
        return context_pack(bundle, None, cue, budget=BUDGET, k=K,
                            mode="flat", include_stale=True)
    if cond == "format":
        return context_pack(bundle, None, cue, budget=BUDGET, k=K, mode="flat")
    if cond == "muninn":
        return context_pack(bundle, dyn, cue, budget=BUDGET, k=K,
                            mode="muninn", reactivate=False)
    if cond == "dump":
        return context_pack(bundle, None, cue, budget=BUDGET, mode="dump")
    raise ValueError(cond)


def ask(pack: str, question: str) -> str:
    if pack:
        prompt = ("Answer the question using ONLY the context below. Reply with "
                  "a short answer (one sentence max). If the context does not "
                  "contain the answer, reply exactly UNKNOWN.\n\n<context>\n"
                  f"{pack}\n</context>\n\nQuestion: {question}")
    else:
        prompt = ("Answer in one short sentence. You have no context about this "
                  "private homelab; if you cannot know the answer, reply exactly "
                  f"UNKNOWN.\n\nQuestion: {question}")
    r = subprocess.run(["claude", "-p", prompt, "--model", MODEL],
                       capture_output=True, text=True, timeout=120)
    return (r.stdout or "").strip()


def main() -> None:
    bundle = Bundle(CORPUS)
    dyn = Dynamics(CORPUS)
    questions = json.load(open(os.path.join(HERE, "questions.json")))

    jobs = []
    for cond in CONDITIONS:
        for i, q in enumerate(questions):
            pack = build_pack(bundle, dyn, cond, q["q"])
            jobs.append({"cond": cond, "i": i, "q": q, "pack": pack,
                         "pack_tokens": estimate_tokens(pack) if pack else 0})

    def run(job):
        try:
            job["answer"] = ask(job["pack"], job["q"]["q"])
        except Exception as exc:  # timeout etc.: count as wrong, loudly
            job["answer"] = f"<ERROR: {exc}>"
        a = job["answer"].lower()
        job["correct"] = any(e.lower() in a for e in job["q"]["expect"])
        job["unknown"] = "unknown" in a and not job["correct"]
        return job

    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(run, jobs))

    by_cond: dict[str, list] = {c: [] for c in CONDITIONS}
    for r in results:
        by_cond[r["cond"]].append(r)

    lines = ["# Experiment 1: recall quality at a fixed context budget", "",
             f"corpus: {len(bundle.notes)} notes; questions: {len(questions)}; "
             f"budget: {BUDGET} tokens; k={K}; answerer: claude {MODEL}", "",
             "| condition | accuracy | unknown | wrong | avg pack tokens |",
             "|---|---|---|---|---|"]
    for cond in CONDITIONS:
        rs = by_cond[cond]
        acc = sum(r["correct"] for r in rs)
        unk = sum(r["unknown"] for r in rs)
        wrong = len(rs) - acc - unk
        avg_tok = sum(r["pack_tokens"] for r in rs) // len(rs)
        lines.append(f"| {cond} | {acc}/{len(rs)} | {unk} | {wrong} | {avg_tok} |")

    kinds = sorted({q["kind"] for q in questions})
    lines += ["", "## By question kind (accuracy)", "",
              "| condition | " + " | ".join(kinds) + " |",
              "|---" * (len(kinds) + 1) + "|"]
    for cond in CONDITIONS:
        cells = []
        for kind in kinds:
            rs = [r for r in by_cond[cond] if r["q"]["kind"] == kind]
            cells.append(f"{sum(r['correct'] for r in rs)}/{len(rs)}")
        lines.append(f"| {cond} | " + " | ".join(cells) + " |")

    lines += ["", "## Failures (context-bearing conditions)", ""]
    for cond in CONDITIONS:
        for r in by_cond[cond]:
            if not r["correct"] and cond != "none":
                lines.append(f"- **{cond}** [{r['q']['kind']}] {r['q']['q']} -> "
                             f"`{r['answer'][:110]}`")

    out = "\n".join(lines) + "\n"
    with open(os.path.join(HERE, f"RESULTS-b{BUDGET}k{K}.md"), "w", encoding="utf-8") as fh:
        fh.write(out)
    slim = [{k: v for k, v in r.items() if k != "pack"} for r in results]
    with open(os.path.join(HERE, f"results-b{BUDGET}k{K}.json"), "w", encoding="utf-8") as fh:
        json.dump(slim, fh, indent=1)
    print(out)


if __name__ == "__main__":
    main()
