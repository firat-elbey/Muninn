#!/usr/bin/env python3
"""Field probe: build a muninn bundle from one real repository and measure
what the model would see: bundle size/stub ratio, the index head (the
low-res map's landmarks), and top-3 recall for realistic questions.

This is the harness behind eval/FIELD-TESTS.md (10 repos, 3 fix rounds).

    python3 eval/field_probe.py <name> <git-url> "<question>" [...]

Writes reports/<name>.txt next to the working clone, then deletes the
clone (keeps the bundle for inspection). Requires tree-sitter extras.
"""
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))
from muninn import extract                      # noqa: E402
from muninn.dynamics import Dynamics            # noqa: E402
from muninn.recall import context_pack, recall  # noqa: E402
from muninn.store import Bundle                 # noqa: E402


def probe(name: str, url: str, questions: list[str], base: str) -> str:
    work = os.path.join(base, "work-" + name)
    repo, kb = os.path.join(work, "repo"), os.path.join(work, "kb")
    os.makedirs(work, exist_ok=True)
    rep_path = os.path.join(base, "reports", name + ".txt")
    os.makedirs(os.path.dirname(rep_path), exist_ok=True)
    out: list[str] = []

    if not os.path.isdir(repo):
        subprocess.run(["git", "clone", "-q", "--depth", "1", url, repo],
                       check=True, timeout=600)
    t0 = time.time()
    b = Bundle(kb)
    graph, stats = extract.extract_path(repo)
    extract.import_graph(b, graph)
    build_s = time.time() - t0
    b, d = Bundle(kb), Dynamics(kb)

    out.append(f"== {name}  ({url})")
    out.append(f"files parsed: {stats['files']}  by_lang: "
               + json.dumps(stats["by_lang"], sort_keys=True))
    out.append(f"notes: {len(b.notes)}  build: {build_s:.1f}s")
    bodies = [n.body.strip() for n in b.notes.values()]
    stubs = sum(1 for x in bodies if len(x) < 40)
    out.append(f"stub ratio (body<40ch): {stubs}/{len(bodies)} "
               f"({100 * stubs / max(1, len(bodies)):.0f}%)")

    out.append("\n-- index head (the model's low-res map, first 10):")
    pack = context_pack(b, d, "zzz-no-match-zzz", budget=1200, k=0,
                        reactivate=False)
    out += [ln for ln in pack.splitlines() if ln.startswith("- ")][:10]

    for q in questions:
        out.append(f"\n-- Q: {q}")
        for note, score, why in recall(b, d, q, k=3, reactivate=False):
            body1 = " ".join(note.body.split())[:90]
            out.append(f"  {score:5.2f}  {note.path}")
            out.append(f"         [{why}]")
            out.append(f"         {body1}")

    with open(rep_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    shutil.rmtree(repo, ignore_errors=True)  # keep kb, drop the clone
    return rep_path


if __name__ == "__main__":
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    print("report ->", probe(sys.argv[1], sys.argv[2], sys.argv[3:],
                             os.getcwd()))
