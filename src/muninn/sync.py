"""Synchronize append-only ledgers through a dedicated Git reference.

``muninn sync`` reads ``refs/muninn/ledger``, unions unique JSONL events,
orders them by timestamp and line text, replays the local state, and pushes a
fast-forward child. A concurrent update causes a new fetch, union, and bounded
retry. The operation does not create commits on a working branch.

Synchronization shares note paths, session identifiers, outcomes, feedback,
and intents with every reader of the remote. It must therefore use an
authorized private remote.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time

from .dynamics import Dynamics, sidecar_lock

SYNC_REF = "refs/muninn/ledger"
_INCOMING = "refs/muninn/incoming"  # scratch ref for the fetched remote state
LEDGER_NAME = "ledger.jsonl"
PUSH_RETRIES = 5
PUSH_BACKOFF = 0.2  # seconds x attempt between lost races: a herd of
#                     racing agents must not re-collide in lockstep
# commit-tree needs an identity; syncing must not depend on user git config
_IDENT = ("-c", "user.name=muninn", "-c", "user.email=muninn@localhost")


class SyncError(RuntimeError):
    """A sync step that cannot proceed (not a repo, push kept losing)."""


def _git(repo: str, *args: str, data: str | None = None) -> tuple[int, str]:
    """Run one git command in ``repo``; (returncode, stdout)."""
    try:
        r = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                           text=True, timeout=60, input=data)
        return r.returncode, r.stdout.strip()
    except Exception:
        return 1, ""


def _event_ts(line: str) -> float:
    try:
        ts = json.loads(line).get("ts", 0)
        return float(ts) if isinstance(ts, (int, float)) else 0.0
    except (ValueError, TypeError, RecursionError):
        return 0.0


def union_ledgers(*texts: str) -> str:
    """The conflict-free merge: unique non-empty lines from all ledgers,
    ordered by event ts (ties by line text: deterministic everywhere).
    Idempotent: union(a, a) == normalize(a)."""
    lines = {ln for t in texts for ln in t.splitlines() if ln.strip()}
    if not lines:
        return ""
    return "\n".join(sorted(lines, key=lambda ln: (_event_ts(ln), ln))) + "\n"


def _read_remote(repo: str, sha: str) -> str:
    code, out = _git(repo, "show", f"{sha}:{LEDGER_NAME}")
    return out + "\n" if code == 0 and out else ""


def _commit_union(repo: str, merged: str, parent: str | None) -> str:
    code, blob = _git(repo, "hash-object", "-w", "--stdin", data=merged)
    if code != 0:
        raise SyncError("git hash-object failed")
    code, tree = _git(repo, "mktree",
                      data=f"100644 blob {blob}\t{LEDGER_NAME}\n")
    if code != 0:
        raise SyncError("git mktree failed")
    args = [*_IDENT, "commit-tree", tree, "-m", "muninn sync"]
    if parent:
        args += ["-p", parent]
    code, commit = _git(repo, *args)
    if code != 0 or not commit:
        raise SyncError("git commit-tree failed")
    return commit


def _merge_local(root: str, incoming: str) -> tuple[str, int]:
    """Merge against current local events while excluding concurrent appends."""
    directory = os.path.join(root, ".muninn")
    ledger_path = os.path.join(directory, LEDGER_NAME)
    with sidecar_lock(root):
        try:
            with open(ledger_path, encoding="utf-8") as handle:
                local = handle.read()
        except FileNotFoundError:
            local = ""
        merged = union_ledgers(local, incoming)
        learned = len(set(merged.splitlines()) - set(local.splitlines()))
        changed = merged != local
        if changed:
            descriptor, temporary = tempfile.mkstemp(
                prefix="ledger-", suffix=".tmp", dir=directory)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(merged)
                os.replace(temporary, ledger_path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
    if changed:
        Dynamics(root).replay()
    return merged, learned


def sync(root: str, remote: str = "origin", ref: str = SYNC_REF,
         push: bool = True, repo: str | None = None) -> dict:
    """Fetch the team ledger, union it into the local sidecar, push the
    union back. Returns a summary dict:

      pulled  events learned from the remote (0 when none / unreachable)
      pushed  True when the union landed on the remote
      events  total events in the merged ledger
      fetched False when the remote ref could not be read (first sync on
              a fresh remote, or offline: the local union still happens)

    Raises SyncError when ``repo`` is not a git repository or a requested
    push keeps losing the fetch/union/push race."""
    root = os.path.abspath(root)
    repo = os.path.abspath(repo) if repo else root
    if not os.path.isdir(repo):
        raise SyncError(f"{repo} does not exist: point --root (or --repo) "
                        "at the bundle inside the repo with the shared "
                        "remote")
    if _git(repo, "rev-parse", "--git-dir")[0] != 0:
        raise SyncError(f"{repo} is not a git repository: sync moves the "
                        "ledger over a git ref; run it from (or --repo at) "
                        "the repo that has the shared remote")
    pulled = 0
    pushed = False
    remote_sha = None
    fetched = _git(repo, "fetch", remote, f"+{ref}:{_INCOMING}")[0] == 0
    if fetched:
        code, sha = _git(repo, "rev-parse", _INCOMING)
        remote_sha = sha if code == 0 and sha else None
    remote_text = _read_remote(repo, remote_sha) if remote_sha else ""
    merged, pulled = _merge_local(root, remote_text)
    merged_lines = {ln for ln in merged.splitlines() if ln.strip()}

    if push:
        remote_lines = {ln for ln in remote_text.splitlines() if ln.strip()}
        if merged_lines == remote_lines and remote_sha:
            pushed = True  # nothing new for the remote: already in sync
        elif not merged_lines and not remote_sha:
            pushed = True  # nothing anywhere: publishing an empty ledger
        else:              # would be pure ref noise
            for attempt in range(PUSH_RETRIES):
                commit = _commit_union(repo, merged, remote_sha)
                _git(repo, "update-ref", ref, commit)
                if _git(repo, "push", remote, f"{commit}:{ref}")[0] == 0:
                    pushed = True
                    break
                time.sleep(PUSH_BACKOFF * (attempt + 1))
                # lost the race (or first contact failed): re-fetch, re-union
                if _git(repo, "fetch", remote, f"+{ref}:{_INCOMING}")[0] != 0:
                    break  # unreachable remote: retrying cannot help
                code, sha = _git(repo, "rev-parse", _INCOMING)
                remote_sha = sha if code == 0 and sha else None
                remote_text = _read_remote(repo, remote_sha) if remote_sha else ""
                merged, learned = _merge_local(
                    root, union_ledgers(merged, remote_text))
                pulled += learned
                merged_lines = {ln for ln in merged.splitlines() if ln.strip()}
            if not pushed:
                raise SyncError(f"could not push {ref} to {remote}: remote "
                                "unreachable or rejecting; the local union "
                                f"is intact ({len(merged_lines)} events)")
    return {"pulled": pulled, "pushed": pushed,
            "events": len(merged_lines), "fetched": fetched}
