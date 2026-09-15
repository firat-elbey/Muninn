"""Verify the arithmetic used in LongMemEval full-run projections."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_runner():
    path = ROOT / "eval" / "longmemeval" / "run.py"
    spec = importlib.util.spec_from_file_location("longmemeval_run", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("result_count", "sample_count", "expected"),
    [
        (165, 55, (3_000, 6.0, 0.3)),
        (453, 151, (3_000, 6.0, 0.3)),
        (302, 151, (2_000, 4.0, 0.2)),
    ],
)
def test_project_full_run_scales_with_condition_count(
    result_count: int,
    sample_count: int,
    expected: tuple[int, float, float],
) -> None:
    runner = load_runner()

    assert runner.project_full_run(result_count, sample_count) == expected


@pytest.mark.parametrize("arguments", [(-1, 10), (1, 0)])
def test_project_full_run_rejects_invalid_counts(arguments: tuple[int, int]) -> None:
    runner = load_runner()

    with pytest.raises(ValueError):
        runner.project_full_run(*arguments)
