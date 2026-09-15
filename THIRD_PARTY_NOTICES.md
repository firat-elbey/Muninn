# Third-party notices

Muninn is licensed under the MIT License except for the third-party material
identified below. The corresponding license governs that material.

## LongMemEval

The following files contain material from the
[LongMemEval](https://github.com/xiaowu0162/LongMemEval) project:

- `eval/longmemeval/run.py` contains adapted evaluation prompts.
- `eval/longmemeval/pref-sample.json`, `sample.json`, and `walk-sample.json`
  contain benchmark question and answer records.

LongMemEval is Copyright 2024 Di Wu and is available under the MIT License.
The complete license is in
[`LICENSES/LongMemEval-MIT.txt`](LICENSES/LongMemEval-MIT.txt).

The project source and license were verified on 26 August 2026 at revision
`9e0b455f4ef0e2ab8f2e582289761153549043fc`. The selected question and answer
records came from the project's `longmemeval_s_cleaned` Hugging Face dataset.
The source dataset is available at
[xiaowu0162/longmemeval-cleaned](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned).
Their committed SHA-256 digests are:

- `pref-sample.json`: `16292d7276f8f54b789f9f14053e9f5b6af3196c4e31afacda503f486cd954e5`.
- `sample.json`: `c7ecbb3aa9f83f7dba37d43e38f0f789ce04abd050d317e2617b0c826c4358ad`.
- `walk-sample.json`: `791987e59e2d1a92dadb05a4207b9005aee51df200e7ec7ef9cb780b0f4c329e`.

The generated result files in `eval/longmemeval/` record Muninn experiments
against this material. Their inclusion does not imply endorsement by the
LongMemEval authors.
