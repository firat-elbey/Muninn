"""Verify that independent local reviewers apply each session once."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

SOURCE = str(Path(__file__).resolve().parents[1] / "src")
sys.path.insert(0, SOURCE)

from muninn import review
from muninn.dynamics import REVIEWED_KEYS, Dynamics, sidecar_lock
from muninn.store import Bundle

WORKER = r"""
import json
from pathlib import Path
import sys
import time
from muninn import review
from muninn.dynamics import Dynamics
from muninn.store import Bundle

root, barrier, mode, label = sys.argv[1:]
barrier = Path(barrier)
dynamics = Dynamics(root)
bundle = Bundle(root)
original = review.session_metrics

def wait_for(path):
    deadline = time.monotonic() + 15
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(str(path))
        time.sleep(0.005)

def synchronized_metrics(*args, **kwargs):
    metrics = original(*args, **kwargs)
    (barrier / ("measured-" + label)).touch()
    wait_for(barrier / "release")
    return metrics

def record_gap_build(*args, **kwargs):
    with (barrier / "gap-builds").open("a") as handle:
        handle.write(label + "\n")
    return []

review.session_metrics = synchronized_metrics
review._build_gaps = record_gap_build
(barrier / ("ready-" + label)).touch()
wait_for(barrier / "start")
if mode == "usage":
    result = review.review_usage_session(dynamics, "shared-session")
else:
    result = review.review_session(bundle, dynamics, "shared-session")
print(json.dumps({"applied": result is not None}))
"""


@pytest.fixture
def session(tmp_path):
    root = tmp_path / "brain"
    root.mkdir()
    bundle = Bundle(str(root))
    for name in ("hit.md", "miss.md", "waste.md"):
        bundle.write_note(name, {"title": name}, "Synthetic review evidence.")
    dynamics = Dynamics(str(root))
    dynamics.assoc("hit.md", ["synthetic", "review"], w=0.3)
    dynamics.session_begin("shared-session", cue="synthetic review")
    for name in ("hit.md", "waste.md"):
        dynamics.touch(name, kind="recall", session="shared-session")
    for name in ("hit.md", "miss.md"):
        dynamics.touch(name, session="shared-session")
    return root, Bundle(str(root)), dynamics


def events(root):
    return [json.loads(line) for line in
            (root / ".muninn" / "ledger.jsonl").read_text().splitlines()]


def environment(tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {"HOME": str(home), "PATH": os.defpath, "PYTHONPATH": SOURCE,
            "PYTHONDONTWRITEBYTECODE": "1"}


def await_files(directory, prefix, count, timeout=10):
    deadline = time.monotonic() + timeout
    while len(list(directory.glob(prefix + "*"))) < count:
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.005)
    return True


@pytest.mark.parametrize("mode", ["notes", "usage", "mixed"])
def test_independent_processes_apply_one_review_and_feedback(session, tmp_path, mode):
    root, bundle, dynamics = session
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    processes = []
    try:
        for index in range(8):
            selected = ("usage" if index % 2 else "notes") if mode == "mixed" else mode
            processes.append(subprocess.Popen(
                [sys.executable, "-c", WORKER, str(root), str(barrier),
                 selected, str(index)], cwd=tmp_path, env=environment(tmp_path),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        assert await_files(barrier, "ready-", 8), "Review workers did not start."
        (barrier / "start").touch()
        assert await_files(barrier, "measured-", 1), "No reviewer read the ledger."
        # Unlocked reviewers can all read before release. Locked reviewers
        # intentionally leave seven processes outside the metrics read.
        await_files(barrier, "measured-", 8, timeout=0.5)
        (barrier / "release").touch()
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == 0, (stdout, stderr)
            results.append(json.loads(stdout))
    finally:
        (barrier / "release").touch()
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate()

    assert sum(result["applied"] for result in results) == 1
    recorded = events(root)
    assert len([event for event in recorded if event["kind"] == "review"]) == 1
    assert len([event for event in recorded if event["kind"] == "assoc"]) == 3
    fresh = Dynamics(str(root))
    assert fresh.assocs["miss.md"]["w"] == 0.5
    assert fresh.assocs["hit.md"]["w"] == 0.5
    assert fresh.serve_miss == {"waste.md": 1}
    gap_log = barrier / "gap-builds"
    gap_count = len(gap_log.read_text().splitlines()) if gap_log.exists() else 0
    if mode == "mixed":
        assert gap_count <= 1
    else:
        assert gap_count == (1 if mode == "notes" else 0)

    # The caller's Dynamics instance predates the competing review.
    before = (root / ".muninn" / "ledger.jsonl").read_bytes()
    assert review.review_session(bundle, dynamics, "shared-session", build_gaps=False) is None
    assert review.review_usage_session(dynamics, "shared-session") is None
    assert (root / ".muninn" / "ledger.jsonl").read_bytes() == before
    os.unlink(fresh.state_path)
    replayed = Dynamics(str(root))
    assert replayed.assocs == fresh.assocs
    assert replayed.serve_miss == fresh.serve_miss
    assert replayed.reviewed == fresh.reviewed


def test_review_remains_once_after_bounded_cache_eviction(session):
    root, bundle, dynamics = session
    review.review_session(bundle, dynamics, "shared-session", build_gaps=False)
    for index in range(REVIEWED_KEYS):
        dynamics.review_mark(f"later-{index}", [], [], [])
    assert "shared-session" not in dynamics.reviewed
    before = (root / ".muninn" / "ledger.jsonl").read_bytes()
    assert review.review_usage_session(dynamics, "shared-session") is None
    assert review.review_session(bundle, dynamics, "shared-session", build_gaps=False) is None
    assert (root / ".muninn" / "ledger.jsonl").read_bytes() == before


@pytest.mark.parametrize("mode", ["notes", "usage"])
def test_review_lock_timeout_does_not_write_feedback(session, mode):
    root, bundle, dynamics = session
    before = (root / ".muninn" / "ledger.jsonl").read_bytes()
    started = time.monotonic()
    with (sidecar_lock(str(root), "review"),
          mock.patch.object(review, "REVIEW_LOCK_TIMEOUT", 0.03, create=True),
          pytest.raises(TimeoutError, match="review write lock is busy")):
        if mode == "usage":
            review.review_usage_session(dynamics, "shared-session")
        else:
            review.review_session(bundle, dynamics, "shared-session")
    assert time.monotonic() - started < 1
    assert (root / ".muninn" / "ledger.jsonl").read_bytes() == before


def test_session_end_hook_remains_fail_soft_when_review_lock_is_busy(session, tmp_path):
    root, _bundle, _dynamics = session
    before = (root / ".muninn" / "ledger.jsonl").read_bytes()
    started = time.monotonic()
    with sidecar_lock(str(root), "review"):
        result = subprocess.run(
            [sys.executable, "-m", "muninn.cli", "--root", str(root), "hook", "session-end"],
            input='{"session_id":"shared-session"}', text=True, capture_output=True,
            env=environment(tmp_path), cwd=tmp_path, timeout=6, check=False)
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr.splitlines() == ["muninn hook: Muninn review write lock is busy"]
    assert time.monotonic() - started < 4
    assert (root / ".muninn" / "ledger.jsonl").read_bytes() == before


def test_metrics_does_not_hold_the_ledger_append_lock(session):
    root, bundle, dynamics = session
    original = review.session_metrics

    def check_lock_order(*args, **kwargs):
        with sidecar_lock(str(root), timeout=0.01):
            return original(*args, **kwargs)

    with mock.patch.object(review, "session_metrics", side_effect=check_lock_order):
        assert review.review_session(bundle, dynamics, "shared-session", build_gaps=False)


def test_gap_failure_releases_review_lock_without_feedback(session):
    root, bundle, dynamics = session
    before = (root / ".muninn" / "ledger.jsonl").read_bytes()
    with (mock.patch.object(review, "_build_gaps", side_effect=OSError("Synthetic failure.")),
          pytest.raises(OSError, match="Synthetic failure")):
        review.review_session(bundle, dynamics, "shared-session")
    assert (root / ".muninn" / "ledger.jsonl").read_bytes() == before
    assert review.review_session(bundle, dynamics, "shared-session", build_gaps=False)
