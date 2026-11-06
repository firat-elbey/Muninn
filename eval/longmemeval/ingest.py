"""MYL-11: LongMemEval -> muninn bundle adapter.

Builds ONE bundle per benchmark question from its haystack of multi-session
chat history (github.com/xiaowu0162/LongMemEval, file longmemeval_s from the
official HF mirror xiaowu0162/longmemeval-cleaned):

  data/longmemeval_s_cleaned.json   (public benchmark; gitignored)
  bundles/<question_id>/            one OKF bundle + .muninn sidecar each

Mapping:
  * each user->assistant round trip (a small chunk of turns) becomes a note:
    title = session date + index, body = the turn text, date in frontmatter
  * chronology is replayed as USAGE: every note is touched once with
    session=<session_id> in date order, with a consolidation between
    sessions: early-session content decays through more consolidations, so
    strength forms a recency gradient and a re-mentioned fact lives in a
    newer, stronger note (the adapter never dedups facts into one note, so
    per-note recurrence stays 1; cross-note association comes only from
    same-session co-activation)
  * NO supersede events and no has_answer oracle flags: detecting updates
    is the system's job, not the adapter's

Usage:
  python3 eval/longmemeval/ingest.py --sample 55        # pilot (writes sample.json)
  python3 eval/longmemeval/ingest.py --all              # all 500 questions
  python3 eval/longmemeval/ingest.py --qid e47becba     # specific question(s)
  python3 eval/longmemeval/ingest.py \
      --strata multi-session,single-session-preference \
      --out walk-sample.json   # oversample: ALL answerable questions of
                               # the named types (MYL-14 walk eval)

Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "src"))

from muninn.dynamics import Dynamics  # noqa: E402
from muninn.store import Bundle  # noqa: E402

DATA = os.path.join(HERE, "data", "longmemeval_s_cleaned.json")
BUNDLES = os.path.join(HERE, "bundles")
SAMPLE = os.path.join(HERE, "sample.json")

# pilot strata: heavier on knowledge-update (the fading/strengthening story);
# abstention (_abs question_ids) is its own stratum, as LongMemEval reports it
PILOT_PLAN = {
    "knowledge-update": 15,
    "multi-session": 8,
    "temporal-reasoning": 8,
    "single-session-user": 8,
    "single-session-assistant": 6,
    "single-session-preference": 5,
    "abstention": 5,
}

_DATE = re.compile(r"\s*(\d{4})/(\d{2})/(\d{2}).*?(\d{2}):(\d{2})")


def is_abstention(inst: dict) -> bool:
    """LongMemEval marks unanswerable variants by an _abs question-id
    suffix; they are reported as their own stratum."""
    return "_abs" in inst["question_id"]


def date_key(s: str) -> tuple:
    """Sortable key for '2023/05/20 (Sat) 02:21' (locale-free). haystack
    sessions are not reliably date-sorted in the raw data (211 of 500 are not)."""
    m = _DATE.match(s or "")
    if m:
        return tuple(int(g) for g in m.groups())
    return (9999, 12, 31, 23, 59)  # unparseable sorts last, deterministically


def chunk_turns(turns: list[dict]) -> list[list[dict]]:
    """Group consecutive turns into user->assistant round trips: flush at
    each assistant turn, so a chunk is typically one exchange."""
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    for t in turns:
        content = (t.get("content") or "").strip()
        if not content:
            continue
        cur.append({"role": str(t.get("role", "user")), "content": content})
        if cur[-1]["role"] == "assistant":
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def build_bundle(inst: dict, root: str, force: bool = False) -> dict:
    """Write the notes and replay chronology as usage. Idempotent via
    --force (a partial/old bundle is removed and rebuilt)."""
    if os.path.isdir(root):
        if not force:
            return {"skipped": True}
        shutil.rmtree(root)
    os.makedirs(root, exist_ok=True)
    bundle = Bundle(root)

    if not (len(inst["haystack_dates"]) == len(inst["haystack_session_ids"])
            == len(inst["haystack_sessions"])):
        raise ValueError(f"{inst['question_id']}: ragged haystack lists: "
                         "zip would silently drop/misalign sessions")
    triples = list(zip(inst["haystack_dates"], inst["haystack_session_ids"],
                       inst["haystack_sessions"]))
    triples.sort(key=lambda t: date_key(t[0]))  # chronological replay order

    per_session: list[tuple[str, list[str]]] = []  # (session_id, note paths)
    notes = 0
    for si, (date, sid, turns) in enumerate(triples):
        paths: list[str] = []
        for ci, chunk in enumerate(chunk_turns(turns)):
            body = "\n\n".join(f"{t['role'].capitalize()}: {t['content']}"
                               for t in chunk)
            rel = f"s{si:03d}/t{ci:02d}.md"
            bundle.write_note(rel, {
                "type": "note",
                "title": f"{date} · {si:02d}.{ci:02d}",
                "date": date,
                "session": str(sid),
            }, body)
            paths.append(rel)
            notes += 1
        per_session.append((str(sid), paths))

    dyn = Dynamics(root)
    for i, (sid, paths) in enumerate(per_session):
        for rel in paths:
            dyn.touch(rel, session=sid)
        if i < len(per_session) - 1:
            dyn.consolidate()  # between sessions: un-reused content fades
    return {"skipped": False, "sessions": len(per_session), "notes": notes}


def pick_sample(data: list[dict], n_plan: dict[str, int], seed: int) -> list[dict]:
    rng = random.Random(seed)
    strata: dict[str, list[dict]] = {k: [] for k in n_plan}
    for inst in data:
        key = "abstention" if is_abstention(inst) else inst["question_type"]
        if key in strata:
            strata[key].append(inst)
    picked: list[dict] = []
    for key, want in n_plan.items():
        pool = sorted(strata[key], key=lambda x: x["question_id"])
        picked += rng.sample(pool, min(want, len(pool)))
    return picked


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default=DATA)
    ap.add_argument("--sample", type=int, default=0,
                    help="stratified pilot sample size: approximate unless it "
                         "matches PILOT_PLAN's total (per-stratum rounding)")
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--all", action="store_true", help="ingest all questions")
    ap.add_argument("--qid", action="append", default=[],
                    help="ingest specific question id(s)")
    ap.add_argument("--strata", default="",
                    help="comma-separated question types: ingest ALL "
                         "answerable (non-abstention) questions of these "
                         "types: the oversampler for low-accuracy strata; "
                         "requires --out")
    ap.add_argument("--out", default=None,
                    help="manifest path to write (default: sample.json; a "
                         "relative path resolves against this script's "
                         "directory, matching run.py's SAMPLE)")
    ap.add_argument("--force", action="store_true", help="rebuild existing bundles")
    args = ap.parse_args()

    if not os.path.exists(args.data):
        sys.exit(f"dataset not found: {args.data}\nfetch it with:\n  curl -L -o "
                 f"{args.data} https://huggingface.co/datasets/xiaowu0162/"
                 "longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json")
    with open(args.data, encoding="utf-8") as fh:
        data = json.load(fh)

    if args.qid:
        chosen = [x for x in data if x["question_id"] in set(args.qid)]
    elif args.strata:
        if not args.out:
            sys.exit("--strata requires --out (refusing to overwrite the "
                     "default sample.json)")
        want = {s.strip() for s in args.strata.split(",") if s.strip()}
        known = {x["question_type"] for x in data}
        if want - known:  # An invalid type must raise instead of selecting no questions.
            sys.exit(f"unknown question type(s): {sorted(want - known)}; "
                     f"valid: {sorted(known)}")
        chosen = sorted((x for x in data if x["question_type"] in want
                         and not is_abstention(x)),
                        key=lambda x: x["question_id"])
    elif args.all:
        chosen = data
    else:
        plan = dict(PILOT_PLAN)
        if args.sample and args.sample != sum(plan.values()):
            scale = args.sample / sum(plan.values())
            plan = {k: max(1, round(v * scale)) for k, v in plan.items()}
        chosen = pick_sample(data, plan, args.seed)
    if not chosen:
        sys.exit("selection matched no questions: nothing ingested, "
                 "no manifest written")

    manifest = []
    t0 = time.time()
    for i, inst in enumerate(chosen):
        qid = re.sub(r"[^A-Za-z0-9_.-]", "_", inst["question_id"])
        root = os.path.join(BUNDLES, qid)
        stats = build_bundle(inst, root, force=args.force)
        manifest.append({
            "question_id": inst["question_id"],
            "question_type": inst["question_type"],
            "abstention": is_abstention(inst),
            "question": inst["question"],
            "answer": inst["answer"],
            "question_date": inst["question_date"],
            "bundle": os.path.relpath(root, HERE),
        })
        flag = "skip" if stats.get("skipped") else \
            f"{stats['sessions']} sessions, {stats['notes']} notes"
        print(f"[{i + 1}/{len(chosen)}] {inst['question_id']} "
              f"({inst['question_type']}): {flag}", flush=True)

    # a real selection (pilot, strata, or full) defines a run set; --qid
    # alone is a bundle rebuild unless an explicit --out asks for a manifest
    if args.out or not args.qid:
        out = os.path.join(HERE, args.out) if args.out else SAMPLE
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=1)
        print(f"wrote {out} ({len(manifest)} questions)")
    print(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
