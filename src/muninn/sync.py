"""Synchronize append-only ledgers through a dedicated Git reference.

``muninn sync`` reads ``refs/muninn/ledger``, merges JSONL event identities,
preserves append order, replays the local state, and pushes a
fast-forward child. A concurrent update causes a new fetch, union, and bounded
retry. The operation does not create commits on a working branch.

Synchronization shares note paths, session identifiers, outcomes, feedback,
and intents with every reader of the remote. It must therefore use an
authorized private remote.
"""

from __future__ import annotations

import heapq
import json
import math
import os
import re
import subprocess
import tempfile
import time
import uuid
from collections import Counter, defaultdict
from urllib.parse import urlsplit

from .dynamics import Dynamics, ledger_stamp, sidecar_lock

SYNC_REF = "refs/muninn/ledger"
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
        config = (["-c", "http.followRedirects=false"]
                  if args and args[0] in ("fetch", "push") else [])
        r = subprocess.run(["git", "-C", repo, *config, *args], capture_output=True,
                           text=True, timeout=60, input=data)
        return r.returncode, r.stdout.removesuffix("\n")
    except Exception:
        return 1, ""


def _github_visibility(project: str) -> str:
    """Ask GitHub about the selected project without reading credentials."""
    try:
        result = subprocess.run(
            ["gh", "api", "--hostname", "github.com", "repos/" + project,
             "--jq", ".visibility"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _ssh_route(host: str, user: str | None, port: int | None) -> tuple[str, str] | None:
    """Check the endpoint selected by OpenSSH without opening a connection."""
    args = ["ssh", "-G"]
    if user:
        args.extend(("-l", user))
    if port is not None:
        args.extend(("-p", str(port)))
    try:
        result = subprocess.run([*args, "--", host], capture_output=True,
                                text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    options = dict(line.split(" ", 1) for line in result.stdout.splitlines()
                   if " " in line)
    if (result.returncode or not options.get("hostname") or not options.get("port")
            or options.get("proxycommand", "none") != "none"
            or options.get("proxyjump", "none") != "none"):
        return None
    return options["hostname"].lower().rstrip("."), options["port"]


def _check_private_remote(repo: str, remote: str) -> str:
    """Resolve Git's effective push destination, including URL rewrites."""
    if not remote or remote.startswith("-") or any(c in remote for c in "\r\n\0"):
        raise SyncError("Choose a private Git remote.")
    for key in ("vcs", "receivepack", "uploadpack", "mirror"):
        status, _ = _git(repo, "config", "--get", f"remote.{remote}.{key}")
        if status != 1:
            raise SyncError("Use a dedicated private Git remote with standard transport.")
    code, output = _git(repo, "remote", "get-url", "--push", "--all", "--", remote)
    if code:
        # Direct network URLs must use a named remote so Git can report its
        # effective fetch and push destinations. A direct local path may not
        # be rewritten in either direction.
        try:
            parts = urlsplit(remote)
            local_file = parts.scheme == "file" and parts.netloc in ("", "localhost")
        except ValueError as error:
            raise SyncError("Invalid ledger remote URL.") from error
        if (not local_file and (parts.scheme or parts.netloc or ":" in remote)
                or remote.startswith("//") or parts.fragment or parts.query):
            raise SyncError("Configure a named private Git remote for a network URL.")
        if not local_file and not os.path.isdir(os.path.join(repo, remote)):
            raise SyncError("Configure a named private Git remote or use a local repository path.")
        code, resolved = _git(repo, "ls-remote", "--get-url", "--", remote)
        cfg, rewrites = _git(repo, "config", "--null", "--get-regexp",
                             r"^url\..*\.pushinsteadof$")
        if (code or resolved != remote or cfg not in (0, 1)
                or any(remote.startswith(row.partition("\n")[2])
                       for row in rewrites.split("\0") if row)):
            raise SyncError("Use a named private remote for a rewritten local path.")
        return remote

    urls = output.splitlines()
    if len(urls) != 1 or not urls[0] or output != urls[0]:
        raise SyncError("Ledger sync requires exactly one private push destination.")
    url = urls[0]
    if url.startswith("-") or "::" in url or any(c in url for c in "\r\n\0"):
        raise SyncError("Unsupported ledger transport. Choose a private Git remote.")
    scp = re.fullmatch(r"(?:[^/@:]+@)?([^/:]+):(.+)", url) if "://" not in url else None
    try:
        parts = urlsplit(url)
        host = scp[1].lower() if scp else parts.hostname
    except ValueError as error:
        raise SyncError("Invalid ledger remote URL.") from error
    github_hosts = ("github.com", "ssh.github.com")
    github = bool(host and host.rstrip(".") in github_hosts)
    uses_ssh = scp is not None or parts.scheme == "ssh"
    if uses_ssh:
        custom, _ = _git(repo, "config", "--get", "core.sshCommand")
        if custom != 1 or "GIT_SSH" in os.environ or "GIT_SSH_COMMAND" in os.environ:
            raise SyncError("Use standard SSH or HTTPS for the private ledger remote.")
        username = url.split(":", 1)[0].rpartition("@")[0] if scp else parts.username
        try:
            port = None if scp else parts.port
        except ValueError as error:
            raise SyncError("Invalid SSH port.") from error
        route = _ssh_route(host, username, port) if host else None
        if route is None:
            raise SyncError("Use a direct SSH endpoint or private HTTPS remote.")
        github = github or route[0] in github_hosts
        if github and route not in {("github.com", "22"), ("ssh.github.com", "443")}:
            raise SyncError("Use a direct GitHub SSH endpoint or private HTTPS remote.")
    local_path = not parts.scheme and not parts.netloc
    local_file_url = parts.scheme == "file" and host in (None, "localhost")
    if not scp and (local_path or local_file_url):
        pass
    elif (scp is None and parts.scheme not in ("https", "ssh")) or not host:
        raise SyncError("Use a local path or a private HTTPS or SSH ledger remote.")
    elif github:
        if not scp and (parts.query or parts.fragment):
            raise SyncError("Use the canonical URL of a private GitHub repository.")
        project = (scp[2] if scp else parts.path).strip("/").removesuffix(".git")
        if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", project)
                or any(part in (".", "..") for part in project.split("/"))
                or _github_visibility(project) != "private"):
            raise SyncError("Ledger push refused. GitHub must report a private repository. "
                            "Use an authenticated gh CLI or choose a private local remote.")
    else:
        _, trusted = _git(repo, "config", "--local", "--get-all", "muninn.privateSyncUrl")
        if url not in trusted.splitlines():
            raise SyncError("Verify the remote is private, then record its exact push URL "
                            "with git config --local --add muninn.privateSyncUrl <url>.")
    if parts.scheme == "https":
        code, redirects = _git(repo, "-c", "http.followRedirects=false", "config",
                               "--get-urlmatch", "http.followRedirects", url)
        if code or redirects.lower() != "false":
            raise SyncError("Ledger sync requires HTTP redirects to be disabled.")
    code, fetch_url = _git(repo, "remote", "get-url", "--all", "--", remote)
    if code or fetch_url != url:
        raise SyncError("Use one private ledger remote with matching fetch and push URLs.")
    return url


def _legacy_clock(event: object) -> float:
    ts = event.get("ts", 0) if isinstance(event, dict) else 0
    try:
        return ts * 1000 if isinstance(ts, (int, float)) and math.isfinite(ts) else 0
    except OverflowError:
        return 0


def _union(*texts: str) -> tuple[str, int]:
    # Native keys use the logger's random ID. Historical identical lines
    # have no identity beyond their occurrence number within each replica.
    lines: dict[tuple, str] = {}
    priorities: dict[tuple, int | float] = {}
    children: dict[tuple, set] = defaultdict(set)
    indegree: Counter = Counter()

    def precedes(earlier: tuple, later: tuple) -> None:
        if later not in children[earlier]:
            children[earlier].add(later)
            indegree[later] += 1

    local_count = 0
    for index, text in enumerate(texts):
        occurrences: Counter = Counter()
        previous = None
        for line in text.split("\n"):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (ValueError, RecursionError):
                event = None
            stamp = ledger_stamp(event)
            if stamp:
                clock, identity = stamp
                key = ("event", identity)
                if key in lines and line != lines[key]:
                    if event != json.loads(lines[key]):
                        raise SyncError("Conflicting contents for one ledger event ID.")
                    line = min(line, lines[key])
            else:
                occurrences[line] += 1
                key = ("legacy", line, occurrences[line])
                clock = _legacy_clock(event)
            lines[key] = line
            priorities[key] = clock
            # Each native clock already places a writer after observed native
            # events. Old records need their original predecessor relationships,
            # including when an older writer appends to an upgraded ledger.
            if previous is not None and (previous[0] == "legacy" or key[0] == "legacy"):
                precedes(previous, key)
            previous = key
        if index == 0:
            local_count = len(lines)

    def priority(key: tuple) -> tuple:
        return priorities[key], key

    if not children:
        ordered = sorted(lines, key=priority)
    else:
        # A legacy predecessor can delay a native event beyond its priority.
        # Keep its native successors behind it even when their clocks are ready.
        native = sorted((key for key in lines if key[0] == "event"), key=priority)
        for earlier, later in zip(native, native[1:]):
            precedes(earlier, later)

        ready = [priority(key) for key in lines if not indegree[key]]
        heapq.heapify(ready)
        ordered = []
        while ready:
            _, key = heapq.heappop(ready)
            ordered.append(key)
            for child in children.get(key, ()):
                indegree[child] -= 1
                if not indegree[child]:
                    heapq.heappush(ready, priority(child))
        if len(ordered) != len(lines):
            raise SyncError("Legacy ledger histories disagree on event order.")
    return "".join(lines[key] + "\n" for key in ordered), len(lines) - local_count


def union_ledgers(*texts: str) -> str:
    """Merge identified events and the maximum observed legacy multiplicity."""
    return _union(*texts)[0]


def _fetch_remote(repo: str, remote: str, ref: str) -> str | None:
    """Resolve this fetch independently of simultaneous syncs in the same repo."""
    incoming = "refs/muninn/incoming-" + uuid.uuid4().hex
    try:
        if _git(repo, "fetch", "--no-tags", "--no-recurse-submodules", "--refmap=",
                "--", remote, f"+{ref}:{incoming}")[0]:
            return None
        code, sha = _git(repo, "rev-parse", incoming)
        if code or not sha:
            raise SyncError("The fetched ledger reference could not be resolved.")
        return sha
    finally:
        _git(repo, "update-ref", "-d", incoming)


def _read_remote(repo: str, sha: str) -> str:
    code, out = _git(repo, "show", f"{sha}:{LEDGER_NAME}")
    if code:
        raise SyncError("The fetched ledger could not be read.")
    return out + "\n" if out else ""


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
        merged, learned = _union(local, incoming)
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
    if (not ref.startswith("refs/muninn/") or ref == "refs/muninn/incoming"
            or ref.startswith(("refs/muninn/incoming/", "refs/muninn/incoming-"))
            or _git(repo, "check-ref-format", ref)[0] != 0):
        raise SyncError("Choose a valid non-scratch ledger ref under refs/muninn/.")

    # Pass the original remote to Git. Reusing a resolved URL here could
    # apply a second insteadOf rule to a previously checked destination.
    destination = _check_private_remote(repo, remote)

    def verify_destination() -> None:
        if _check_private_remote(repo, remote) != destination:
            raise SyncError("The ledger destination changed. Run sync again.")

    pulled = 0
    pushed = False
    remote_sha = _fetch_remote(repo, remote, ref)
    fetched = remote_sha is not None
    remote_text = _read_remote(repo, remote_sha) if remote_sha else ""
    merged, pulled = _merge_local(root, remote_text)

    if push:
        if merged == remote_text and remote_sha:
            pushed = True  # nothing new for the remote: already in sync
        elif not merged and not remote_sha:
            pushed = True  # nothing anywhere: publishing an empty ledger
        else:              # would be pure ref noise
            for attempt in range(PUSH_RETRIES):
                commit = _commit_union(repo, merged, remote_sha)
                _git(repo, "update-ref", ref, commit)
                verify_destination()
                if _git(repo, "push", "--no-follow-tags", "--no-recurse-submodules",
                        "--", remote, f"{commit}:{ref}")[0] == 0:
                    pushed = True
                    break
                time.sleep(PUSH_BACKOFF * (attempt + 1))
                # lost the race (or first contact failed): re-fetch, re-union
                verify_destination()
                remote_sha = _fetch_remote(repo, remote, ref)
                if not remote_sha:
                    break
                fetched = True
                remote_text = _read_remote(repo, remote_sha)
                merged, learned = _merge_local(root, remote_text)
                pulled += learned
                if merged == remote_text:
                    pushed = True
                    break
            if not pushed:
                events = merged.count("\n")
                raise SyncError(f"could not push {ref} to {remote}: remote "
                                "unreachable or rejecting; the local union "
                                f"is intact ({events} events)")
    return {"pulled": pulled, "pushed": pushed,
            "events": merged.count("\n"), "fetched": fetched}
