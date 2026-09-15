"""Run the MYL-11 and MYL-14 LongMemEval comparisons.

Conditions (same token budget for every context-bearing condition):
  none         no memory at all (floor; only abstention questions can score)
  flat         context_pack mode='flat': lexical retrieval over the same bundle
  muninn       context_pack mode='muninn': retrieval with bounded usage signals
  muninn-walk  context_pack mode='muninn-walk': multi-cue facets from the
               question (cues_from_query; situation absent), walk on,
               facet-coverage pack allocation (MYL-14, the measured walk)

Overridable via env: SAMPLE (manifest path), CONDITIONS (comma-separated),
BUDGET, K, MODEL, JUDGE_MODEL, TAG. The MYL-14 walk run:
  python3 ingest.py --strata multi-session,single-session-preference --out walk-sample.json
  SAMPLE=walk-sample.json TAG=walk CONDITIONS=flat,muninn,muninn-walk python3 run.py

Each question is answered by `claude -p --model haiku` strictly from the pack
(+ the question_date), then scored by an LLM judge using LongMemEval's own
per-type judge prompts (ported verbatim from the official
src/evaluation/evaluate_qa.py; the paper's protocol is LLM-judged QA
accuracy). These experiments use Haiku as the judge rather than the paper's
GPT-4o judge. The reports state this limitation.

Prereq: python3 eval/longmemeval/ingest.py --sample 55   (builds sample.json + bundles)
Writes results-pilot.json + RESULTS-pilot.md.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "src"))

from muninn.dynamics import Dynamics  # noqa: E402
from muninn.recall import context_pack, estimate_tokens  # noqa: E402
from muninn.store import Bundle  # noqa: E402

SAMPLE = os.path.join(HERE, os.environ.get("SAMPLE", "sample.json"))
BUDGET = int(os.environ.get("BUDGET", "1200"))
K = int(os.environ.get("K", "6"))
MODEL = os.environ.get("MODEL", "haiku")
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "haiku")
TAG = os.environ.get("TAG", "pilot")
CONDITIONS = [c.strip() for c in
              os.environ.get("CONDITIONS", "none,flat,muninn").split(",")
              if c.strip()]
# the local embedding endpoint for the muninn-walk-embed condition (captured
# once; each embed build re-enables it so the lexical conditions stay lexical)
EMBED_URL = os.environ.get("MUNINN_EMBED_URL", "")


def build_pack(bundle: Bundle | None, dyn: Dynamics | None, cond: str,
               cue: str) -> str:
    if cond == "none":
        return ""
    if cond == "muninn-walk-embed":
        # the measured walk PLUS the local semantic seed channel: activate
        # reads MUNINN_EMBED_URL live, so enable it for THIS build only and
        # the plain muninn-walk condition in the same process stays lexical.
        os.environ["MUNINN_EMBED_URL"] = EMBED_URL
        try:
            return context_pack(bundle, dyn, cue, budget=BUDGET, k=K,
                                mode="muninn-walk", reactivate=False)
        finally:
            os.environ.pop("MUNINN_EMBED_URL", None)
    if cond in ("flat", "muninn", "muninn-walk"):  # mode == condition name
        os.environ.pop("MUNINN_EMBED_URL", None)  # keep the channel OFF
        return context_pack(bundle, None if cond == "flat" else dyn, cue,
                            budget=BUDGET, k=K, mode=cond, reactivate=False)
    raise ValueError(cond)


def claude(prompt: str, model: str) -> str:
    """Run one `claude -p` call and retry one empty or failed response.

    A second failure raises an exception so the report counts the error.
    """
    last = ""
    for attempt in range(2):
        r = subprocess.run(["claude", "-p", prompt, "--model", model],
                           capture_output=True, text=True, timeout=180)
        out = (r.stdout or "").strip()
        if out:
            return out
        last = (r.stderr or "").strip()[:200]
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"claude returned no output: {last}")


def ask(pack: str, question: str, question_date: str) -> str:
    if pack:
        prompt = (
            "You are an assistant with memory of your past chat sessions with "
            "this user, given below as excerpts (each excerpt's title is its "
            f"session date). Today's date: {question_date}.\n\n"
            "Answer the user's question using ONLY these memory excerpts. Be "
            "concise (at most two sentences). If the excerpts do not contain "
            "the information needed, say you don't have that information from "
            "your past conversations.\n\n"
            f"<memory>\n{pack}\n</memory>\n\nQuestion: {question}")
    else:
        prompt = (
            f"You are an assistant. Today's date: {question_date}. You have no "
            "memory of past conversations with this user. Answer the question "
            "concisely (at most two sentences). If answering requires knowledge "
            "of your past conversations with this user, say you don't have "
            f"that information.\n\nQuestion: {question}")
    return claude(prompt, MODEL)


# The judge prompts below are copied verbatim from LongMemEval. Their language
# is retained because changing it would change the evaluation protocol.
# github.com/xiaowu0162/LongMemEval src/evaluation/evaluate_qa.py
# (get_anscheck_prompt); label rule is theirs too: 'yes' in reply.lower().

def anscheck_prompt(task: str, question: str, answer: str, response: str,
                    abstention: bool) -> str:
    if abstention:
        template = "I will give you an unanswerable question, an explanation, and a response from a model. Please answer yes if the model correctly identifies the question as unanswerable. The model could say that the information is incomplete, or some other information is given but the asked information is not.\n\nQuestion: {}\n\nExplanation: {}\n\nModel Response: {}\n\nDoes the model correctly identify the question as unanswerable? Answer yes or no only."
    elif task in ("single-session-user", "single-session-assistant", "multi-session"):
        template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
    elif task == "temporal-reasoning":
        template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. In addition, do not penalize off-by-one errors for the number of days. If the question asks for the number of days/weeks/months, etc., and the model makes off-by-one errors (e.g., predicting 19 days when the answer is 18), the model's response is still correct. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
    elif task == "knowledge-update":
        template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response contains some previous information along with an updated answer, the response should be considered as correct as long as the updated answer is the required answer.\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
    elif task == "single-session-preference":
        template = "I will give you a question, a rubric for desired personalized response, and a response from a model. Please answer yes if the response satisfies the desired response. Otherwise, answer no. The model does not need to reflect all the points in the rubric. The response is correct as long as it recalls and utilizes the user's personal information correctly.\n\nQuestion: {}\n\nRubric: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
    else:
        raise NotImplementedError(task)
    return template.format(question, answer, response)


def judge(q: dict, response: str) -> tuple[bool, str]:
    prompt = anscheck_prompt(q["question_type"], q["question"],
                             str(q["answer"]), response, q["abstention"])
    out = claude(prompt, JUDGE_MODEL)
    return "yes" in out.lower(), out


def stratum(q: dict) -> str:
    return "abstention" if q["abstention"] else q["question_type"]


def project_full_run(result_count: int, sample_count: int) -> tuple[int, float, float]:
    """Project calls and token volume from a completed sample to 500 questions."""
    if result_count < 0 or sample_count <= 0:
        raise ValueError("result_count must be nonnegative and sample_count must be positive")
    calls = round(result_count * 2 * 500 / sample_count)
    return calls, calls * 2_000 / 1_000_000, calls * 100 / 1_000_000


def main() -> None:
    if not os.path.exists(SAMPLE):
        sys.exit(f"{SAMPLE} was not found. Build it with ingest.py "
                 "(use --strata/--out for an oversampled set)")
    questions = json.load(open(SAMPLE, encoding="utf-8"))
    if not questions:
        sys.exit(f"{SAMPLE} is empty; no evaluation can run")

    t_pack = time.time()
    jobs = []
    for q in questions:
        root = os.path.join(HERE, q["bundle"])
        bundle = dyn = None
        if any(c != "none" for c in CONDITIONS):  # one load per question
            bundle = Bundle(root)
            if not bundle.notes:  # A missing bundle is an error, not a zero score.
                sys.exit(f"bundle missing or empty: {root}. Rerun ingest.py "
                         "(--force rebuilds a partial bundle)")
            dyn = Dynamics(root)
        for cond in CONDITIONS:
            pack = build_pack(bundle, dyn, cond, q["question"])
            jobs.append({"cond": cond, "qid": q["question_id"], "q": q,
                         "pack": pack,
                         "pack_tokens": estimate_tokens(pack) if pack else 0})
    t_pack = time.time() - t_pack
    print(f"built {len(jobs)} packs in {t_pack:.0f}s", flush=True)

    def run_answer(job):
        t0 = time.time()
        try:
            job["answer"] = ask(job["pack"], job["q"]["question"],
                                job["q"]["question_date"])
            job["error"] = ""
        except Exception as exc:  # Count timeouts and other failures as errors.
            job["answer"] = ""
            job["error"] = f"answer: {exc}"
        job["answer_secs"] = round(time.time() - t0, 1)
        return job

    def run_judge(job):
        t0 = time.time()
        if job["error"]:
            job["correct"], job["judge_raw"] = False, "<skipped: answer errored>"
        else:
            try:
                job["correct"], job["judge_raw"] = judge(job["q"], job["answer"])
            except Exception as exc:
                job["correct"], job["judge_raw"] = False, ""
                job["error"] = f"judge: {exc}"
        job["judge_secs"] = round(time.time() - t0, 1)
        return job

    t_llm = time.time()
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(run_answer, jobs))
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(run_judge, results))
    t_llm = time.time() - t_llm
    print(f"answered+judged {len(results)} jobs in {t_llm:.0f}s", flush=True)

    by_cond = {c: [r for r in results if r["cond"] == c] for c in CONDITIONS}
    strata = sorted({stratum(q) for q in questions})
    errors = [r for r in results if r["error"]]

    def pct(rs):
        return f"{sum(r['correct'] for r in rs)}/{len(rs)}" if rs else "-"

    strata_counts = ", ".join(
        f"{s} {sum(1 for q in questions if stratum(q) == s)}" for s in strata)
    lines = [f"# LongMemEval {TAG}: MYL-11 and MYL-14",
             "",
             f"Dataset: `longmemeval_s_cleaned`, which contains 500 "
             f"instances and approximately 48 sessions and 120,000 history "
             f"tokens per question. This sample contains {len(questions)} "
             f"questions: "
             f"{strata_counts}",
             f"Each context condition used a {BUDGET}-token budget and "
             f"`k={K}`. Claude {MODEL} answered each question. Claude "
             f"{JUDGE_MODEL} applied LongMemEval's official question-type "
             f"judge prompts.",
             "",
             "| condition | accuracy | avg pack tokens | errors |",
             "|---|---|---|---|"]
    for cond in CONDITIONS:
        rs = by_cond[cond]
        avg_tok = round(sum(r["pack_tokens"] for r in rs) / max(1, len(rs)))
        errs = sum(1 for r in rs if r["error"])
        lines.append(f"| {cond} | {pct(rs)} | {avg_tok} | {errs} |")

    def _acc(cond: str, s: str) -> tuple[int, int]:
        rs = [r for r in by_cond.get(cond, []) if stratum(r["q"]) == s]
        return sum(r["correct"] for r in rs), len(rs)

    if "muninn-walk-embed" in CONDITIONS and "muninn-walk" in CONDITIONS:
        # Compare the semantic seed channel with the lexical graph walk.
        s = "single-session-preference"
        wc, wn = _acc("muninn-walk", s)
        ec, en = _acc("muninn-walk-embed", s)
        lines += ["", "## Success criterion (stated up front)", "",
                  "The embedding condition must exceed the graph-walk "
                  "condition on "
                  f"single-session-preference at the same {BUDGET}-token "
                  "budget. Otherwise, the result does not support enabling "
                  "the embedding channel for this question type.", ""]
        if wn == 0 or en == 0:
            met = False
            lines.append(f"- {s}: This sample omits the question type, so the criterion "
                         "cannot be satisfied")
        else:
            met = (ec / en) > (wc / wn)
            result = "exceeds" if met else "does not exceed"
            lines.append(f"- {s}: muninn-walk {wc}/{wn}; muninn-walk-embed "
                         f"{ec}/{en}. The embedding condition {result} the graph walk.")
        lines += ["", f"**Verdict: success criterion "
                      f"{'MET' if met else 'NOT MET'}.**"]
    elif "muninn-walk" in CONDITIONS and "flat" in CONDITIONS:
        lines += ["", "## Success criterion (stated up front)", "",
                  "The graph-walk condition must exceed flat retrieval on "
                  "both multi-session and "
                  f"single-session-preference at the same {BUDGET}-token "
                  "budget. Otherwise, the result does not support further "
                  "development of the graph walk.", ""]
        met = True  # Both question types must be present and improved.
        for s in ("multi-session", "single-session-preference"):
            fc, fn = _acc("flat", s)
            wc, wn = _acc("muninn-walk", s)
            if fn == 0 or wn == 0:
                met = False
                lines.append(f"- {s}: This sample omits the question type, so the "
                             "criterion cannot be satisfied")
                continue
            ok = (wc / wn) > (fc / fn)
            met = met and ok
            result = "exceeds" if ok else "does not exceed"
            lines.append(f"- {s}: flat {fc}/{fn}; muninn-walk {wc}/{wn}. "
                         f"The graph walk {result} flat retrieval.")
        lines += ["", f"**Verdict: success criterion "
                      f"{'MET' if met else 'NOT MET'}.**"]

    lines += ["", "## Accuracy by question type", "",
              "| condition | " + " | ".join(strata) + " |",
              "|---" * (len(strata) + 1) + "|"]
    for cond in CONDITIONS:
        cells = [pct([r for r in by_cond[cond] if stratum(r["q"]) == s])
                 for s in strata]
        lines.append(f"| {cond} | " + " | ".join(cells) + " |")

    n_pilot = max(1, len(questions))
    calls = len(results) * 2
    per_call = t_llm * 4 / max(1, calls)  # 4 workers
    projected_calls, input_millions, output_millions = project_full_run(
        len(results), n_pilot
    )
    full_secs = projected_calls * per_call / 4
    lines += [
        "", "## Estimated full-run cost and duration", "",
        f"- The measured run made {len(results)} answer calls and "
        f"{len(results)} judge calls in {t_llm / 60:.0f} minutes with four "
        f"workers. Mean call duration was approximately {per_call:.1f} "
        f"seconds, and pack construction took {t_pack:.0f} seconds.",
        f"- A 500-question run under the same conditions would require "
        f"approximately {projected_calls} Haiku calls and "
        f"{full_secs / 3600:.1f} hours with four workers. Ingesting all 500 "
        "bundles would add approximately three minutes, based on the "
        f"55-bundle measurement. The estimated API volume is {input_millions:g} "
        f"million input tokens and {output_millions:g} million output tokens.",
    ]

    lines += [
        "", "## Limitations", "",
        f"- **Sample size.** n={n_pilot} of 500 ({strata_counts}). One "
        "changed answer moves a cell of size m by 100/m percentage points. "
        "Small cells therefore provide directional evidence only.",
        f"- **Judge noise.** Scored by claude {JUDGE_MODEL} with LongMemEval's "
        "official prompts, but the paper's reference judge is GPT-4o (~97% "
        "human agreement there; Haiku's agreement is unmeasured here). "
        "Absolute accuracy is therefore not directly comparable with the "
        "paper's leaderboard.",
        "- **`none` condition.** A model without memory should abstain. This "
        "condition therefore approaches zero on answerable questions and "
        "scores higher on the abstention group by construction.",
        f"- **Tight budget.** {BUDGET} tokens over k={K} notes truncates most "
        "focus notes (chat turns run ~500 tokens each); both flat and muninn "
        "use the same budget. Absolute accuracy remains budget-limited.",
        "- **Adapter granularity.** Notes are user->assistant round trips; "
        "retrieval is lexical by default, with the optional local-embedding "
        "seed channel exercised only by the muninn-walk-embed condition; "
        "question_date arithmetic for temporal-reasoning must come from "
        "session dates in note titles surviving truncation.",
        "- **One replay of usage.** Chronology is replayed once (touch per "
        "note and consolidation between sessions). The adapter provides no "
        "goals, pins, outcomes, or supersession events. The dynamics layer "
        "must detect updates.",
    ]

    if errors:
        lines += ["", "## Errors (counted as wrong)", ""]
        lines += [f"- {r['cond']} {r['qid']}: {r['error'][:140]}" for r in errors]

    out = "\n".join(lines) + "\n"
    slim = [{k: v for k, v in r.items() if k not in ("pack", "q")} for r in results]
    with open(os.path.join(HERE, f"results-{TAG}.json"), "w", encoding="utf-8") as fh:
        json.dump(slim, fh, indent=1)
    with open(os.path.join(HERE, f"RESULTS-{TAG}.md"), "w", encoding="utf-8") as fh:
        fh.write(out)
    print(out)


if __name__ == "__main__":
    main()
