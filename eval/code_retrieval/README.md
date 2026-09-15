# Code-retrieval evaluation

This evaluation measures whether Muninn can locate implementation files from
real issue descriptions before an agent reads or edits a repository. It uses
the official SWE-bench Lite dataset and accepted patch files as relevance
labels. No language model generates or grades the result.

Run a stratified 24-instance development sample with:

```bash
python3 eval/code_retrieval/run_swebench.py --instances-per-repo 2
```

The conditions isolate four retrieval stages:

1. `overlap` uses the former field-weighted distinct-token score at file level.
2. `bm25f` adds term rarity, frequency, field weight, and length normalization.
3. `lexical-hybrid` fuses BM25F with exact path and symbol matching.
4. `hybrid` preserves BM25F's first result, then uses lexical fusion for recall.
5. `graph-fusion` adds a two-step walk over Python import and unambiguous
   symbol edges directly to the file ranking.

The script downloads dataset metadata from the Hugging Face dataset server,
caches bare repositories under `cache/`, checks out each recorded base commit
through `git archive`, and writes complete top-20 rankings under `runs/`.
Generated repositories and runs remain untracked. Add a selected result to the
permanent experiment record after its code revision is committed.

Run all 300 Lite cases and create the permanent analysis with:

```bash
python3.14 eval/code_retrieval/run_swebench.py --instances-per-repo 999
python3.14 eval/code_retrieval/analyze_swebench.py \
  eval/code_retrieval/runs/swebench-lite-300.json \
  --output eval/code_retrieval/RESULTS-swebench-lite-300.md
gzip -n -9 -c eval/code_retrieval/runs/swebench-lite-300.json \
  > eval/code_retrieval/RESULTS-swebench-lite-300.json.gz
```

The complete evaluation is recorded in
[`RESULTS-swebench-lite-300.md`](RESULTS-swebench-lite-300.md). The compressed
[`raw result`](RESULTS-swebench-lite-300.json.gz) contains every top-20 ranking
and the metadata needed for another analysis without rebuilding the
repositories. Its SHA-256 digest is
`032cdedabcd2c51cd49fb1e97b0951d348bb7685cb0c6dee7841051ef43ac640`.

Verify the persistent SQLite index against the same cases with:

```bash
python3.14 eval/code_retrieval/run_persistent_parity.py \
  eval/code_retrieval/RESULTS-swebench-lite-300.json.gz --workers 4
```

The parity run builds the in-memory and persistent indexes from one source
scan. It requires identical top-20 rankings for every retrieval mode and
records source-run parity, build time, reopen time, query latency, and index
size. The SQLite file stores compressed postings and typed, provenance-marked
edges. It does not copy source bodies.
