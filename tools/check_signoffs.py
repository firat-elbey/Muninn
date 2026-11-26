"""Check author sign-offs using commit objects and Git's trailer parser.

Pull requests use the trusted target workflow and the fixed repository's pull
ref. The checker never checks out proposed files. Pushes check before..after.
New branches and manual ROOT runs check every commit reachable from the head.
Other manual runs check base..GITHUB_SHA. Empty or incomplete ranges fail.
Every commit requires certification, including merge commits and bot authors.
GitHub Dependabot trailers and clean GitHub pull-request or branch-update merge
commits over certified branches require cryptographic verification against
GitHub's pinned web-flow PGP public key.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

IDENTITY_HEADER = re.compile(r"([^<>\r\n]+) <([^<>\s@]+@[^<>\s@]+)> -?\d+ [+-]\d{4}")
GITHUB_WEBFLOW_COMMITTER = "GitHub <noreply@github.com>"
DEPENDABOT_AUTHOR = "dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>"
DEPENDABOT_SIGNOFF = "dependabot[bot] <support@github.com>"
GITHUB_PR_MERGE_SUBJECT = re.compile(r"^Merge pull request #[1-9][0-9]* from [^\r\n]+$")
GITHUB_BRANCH_UPDATE_SUBJECT = re.compile(r"^Merge branch '[^\r\n']+' into [^\r\n]+$")
GITHUB_WEBFLOW_FINGERPRINTS = frozenset({"968479A1AFF927E37D1A566BB5690EEEBB952194"})
GITHUB_WEBFLOW_PGP_KEY = """\
-----BEGIN PGP PUBLIC KEY BLOCK-----

mQINBGWmxXYBEACyN+4viFQM6QQoKr0A2W0rGdMobTJwOZso2QPpewbyBsuL3rNW
5OmHrWwXAhPKNqUIyOzdq8MoSxoTTuqLksoahixEL/X2nyhOBxR9GkYz/oI9R3nY
cLRaFQoSJoVfOt61opkLUzbWAehpbgT8EKln8JsENq0+0nDlWQi0h2Q9oGmqlgVz
skwmVZ8Leyv4Mg7hN6swyZ7moZfkkpD5+U7Z2XVurCzkSSfg4zb2lMRLJos2eCAc
749ECsX0t7OBftF+YqgjIXixXsm2RrUqvU47OkOtZeAhvAYenbC3pr9Fha5NxoBU
Ea+11MK9W6OcRhwvxVCUrMUR6FTSZyC//VpXTTtrRlOqpU5wGMbP3zpn9geqOXCl
8rF7+1gAPG/o+QFQTBsVEruwi4JWogiQuQyOwAIlFe/7dvaxWZGpv/yW2+L3guL7
xaHKFVGsayhlitQQ5Xa+P1iSgKSXDyReCbWotfqAempPySI25LHh3ScXI6NgdHSr
SBaFojwAfgxbPTEQ6adIsKHCQofrnLrNa3UOeGDGiOOK0aYV3jiEDGAouatkNf2q
85Eosj1f9laCqAH3YLJD7dcSne1iChK5qRTByMvIyeSD0NbNnVMFOGpXySyWtKb2
ldpu8AWBQJsJs9FmYBcWAGBA2pp+IxaCn6rBIHIsUVFRN8OVZKsEsBkWywARAQAB
tBtHaXRIdWIgPG5vcmVwbHlAZ2l0aHViLmNvbT6JAk4EEwEKADgWIQSWhHmhr/kn
430aVmu1aQ7uu5UhlAUCZabFdgIbAwULCQgHAgYVCgkICwIEFgIDAQIeAQIXgAAK
CRC1aQ7uu5UhlIMuEAClvVwC+Neoiq0AdixJZsagKHpx1QrMJWrtMRi4eXVTTaeX
+P1unhC/AmSO4Xxd3uRoejHvfWh4F0gitUJ8XKgiejnmuGcq7Dbt5OoO1JuXGlW2
BQ+MiGoYVw2B0sOhWDNrIBWOO/WL4LykcGnAtrRXwoS0Wx4MCydztXQY5lcnCWaW
8rvu7WmduoOikH4HI97rqN5896dc4iBKSx8LZf+46DRCCD/5SfACplBz4hs5zen8
TL8zd+zxjFrXbzota0jSDEGK9WGO4z55S2xScC6zv6v3Bj1OR8Bs5aodGtmamHZ7
sE9w0RJoCfNx+9cR/rE82SrOaBpVU7urLe4lg7zaaNhqDdNV8ymuXGmIJarDgrme
iB5bHS+dLFzLUkTgot4RFlPa9bFiJuJN6Tc9tMu5RJQ9l/zKmxDHIKWsAle5R65u
zEq04LugTQBdEorGxfQCsF2ga9ncKTDMiAThWTvZpOP3NJ/athZRmOBpG4B9iR6r
pRU8F/+MokG4fIMwnvtOhWQFiEzdTkJ7U5JAkPtTAmT3/mznwtPEU7DrFWSGAdqg
IMOlxNCBeGvjwLR0qGH7cB9qHDGNoDLkjaUFpu5tPv4/ivkQaHlHJxjT0ILM6jet
CAzKpKh48rm65tmrJX6KVpj0r2kKMscFf7s7XaPlCNCFds/YA+0puPbzJKWKfA==
=NRlX
-----END PGP PUBLIC KEY BLOCK-----
"""
_KEYRING_CACHE: dict[str, str] = {}


class SignoffError(ValueError):
    """The checker cannot establish a complete, valid commit range."""


def _clean_env() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("GIT_") and key != "GNUPGHOME"}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_GRAFT_FILE=os.devnull, GIT_TERMINAL_PROMPT="0")
    return environment


def git(repo: Path, *args: str, input_text: str | None = None) -> str:
    """Ignore inherited Git controls and disable altered ancestry."""
    environment = _clean_env()
    command = ["git", "--no-pager", "--no-replace-objects"]
    if args[0] == "interpret-trailers":
        command.append(f"--git-dir={os.devnull}")
    result = subprocess.run(
        [*command, *args], cwd=repo,
        input=input_text, capture_output=True, text=True, encoding="utf-8",
        env=environment, timeout=120,
        check=False,
    )
    if result.returncode:
        raise SignoffError(f"Git {args[0]} failed. Check that the complete commit range is available.")
    return result.stdout


def _keyring_dir(keyring_asc: str) -> str | None:
    cached = _KEYRING_CACHE.get(keyring_asc)
    if cached and os.path.isdir(cached):
        return cached
    if not shutil.which("gpg"):
        return None
    homedir = tempfile.mkdtemp(prefix="muninn-dco-gpg-")
    os.chmod(homedir, 0o700)
    atexit.register(shutil.rmtree, homedir, True)
    proc = subprocess.run(
        ["gpg", "--batch", "--quiet", "--homedir", homedir, "--import"],
        input=keyring_asc,
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=_clean_env(),
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        shutil.rmtree(homedir, ignore_errors=True)
        return None
    _KEYRING_CACHE[keyring_asc] = homedir
    return homedir


def verify_webflow_signature(
    repo: Path,
    sha: str,
    keyring_asc: str = GITHUB_WEBFLOW_PGP_KEY,
    allowed_fingerprints: frozenset[str] = GITHUB_WEBFLOW_FINGERPRINTS,
) -> bool:
    """Verify a commit signature cryptographically against an isolated PGP keyring."""
    sha = commit_sha(sha)
    homedir = _keyring_dir(keyring_asc)
    if not homedir:
        return False
    env = _clean_env()
    env["GNUPGHOME"] = homedir
    result = subprocess.run(
        ["git", "--no-pager", "--no-replace-objects", "-c", "gpg.program=gpg",
         "verify-commit", "--raw", sha],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        return False
    for line in result.stderr.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "[GNUPG:]" and parts[1] == "VALIDSIG":
            primary_fpr = parts[11] if len(parts) >= 12 else parts[2]
            if parts[2].upper() in allowed_fingerprints and primary_fpr.upper() in allowed_fingerprints:
                return True
    return False


def commit_sha(value: object) -> str:
    """Accept only a complete, nonzero commit object identifier."""
    if (not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value)
            or not value.strip("0")):
        raise SignoffError("A complete nonzero commit SHA is required.")
    return value


def proposed_commits(repo: Path, base: str, head: str) -> list[str]:
    """Enumerate every proposed commit, including commits on merged branches."""
    head = commit_sha(head)
    if base != "ROOT":
        base = commit_sha(base)
    if git(repo, "rev-parse", "--is-shallow-repository").strip() != "false":
        raise SignoffError("The repository is shallow. Fetch the complete history.")
    for sha in [head] + ([] if base == "ROOT" else [base]):
        if git(repo, "cat-file", "-t", sha).strip() != "commit":
            raise SignoffError("The range must name commit objects.")
    if base != "ROOT":
        git(repo, "merge-base", base, head)
    revision = head if base == "ROOT" else f"{base}..{head}"
    commits = git(repo, "rev-list", "--reverse", "--topo-order", revision, "--").splitlines()
    if not commits:
        raise SignoffError("The proposed commit range is empty.")
    return [commit_sha(sha) for sha in commits]


def _inspect_commit_direct(
    repo: Path,
    sha: str,
    keyring_asc: str,
    allowed_fingerprints: frozenset[str],
) -> tuple[bool | None, list[str]]:
    """Return ``(verdict, deps)`` for one commit without Python recursion."""
    raw = git(repo, "cat-file", "commit", commit_sha(sha))
    headers, separator, message = raw.partition("\n\n")
    header_lines = headers.splitlines()
    trees = [line[5:] for line in header_lines if line.startswith("tree ")]
    raw_parents = [line[7:] for line in header_lines if line.startswith("parent ")]
    authors = [line[7:] for line in header_lines if line.startswith("author ")]
    committers = [line[10:] for line in header_lines if line.startswith("committer ")]
    if not separator or len(trees) != 1 or len(authors) != 1 or len(committers) != 1:
        return False, []
    try:
        tree_id = commit_sha(trees[0])
        parents = [commit_sha(p) for p in raw_parents]
    except SignoffError:
        return False, []
    author = IDENTITY_HEADER.fullmatch(authors[0])
    committer = IDENTITY_HEADER.fullmatch(committers[0])
    if not author or not committer:
        return False, []
    identity = f"{author[1]} <{author[2]}>"
    committer_identity = f"{committer[1]} <{committer[2]}>"
    trailer_input = message if message.endswith("\n") else message + "\n"
    trailers = git(
        repo, "interpret-trailers", "--no-divider", "--parse", input_text=trailer_input
    )
    signoffs = set()
    for line in trailers.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.casefold() == "signed-off-by":
            signoffs.add(value.strip())
    if identity in signoffs:
        return True, []
    if (
        identity == DEPENDABOT_AUTHOR
        and committer_identity == GITHUB_WEBFLOW_COMMITTER
        and DEPENDABOT_SIGNOFF in signoffs
        and verify_webflow_signature(repo, sha, keyring_asc, allowed_fingerprints)
    ):
        return True, []
    subject = message.splitlines()[0] if message else ""
    is_pr_merge = bool(GITHUB_PR_MERGE_SUBJECT.fullmatch(subject))
    is_branch_update = bool(GITHUB_BRANCH_UPDATE_SUBJECT.fullmatch(subject))
    if (
        len(parents) == 2
        and committer_identity == GITHUB_WEBFLOW_COMMITTER
        and (is_pr_merge or is_branch_update)
        and verify_webflow_signature(repo, sha, keyring_asc, allowed_fingerprints)
    ):
        try:
            merged_tree = git(
                repo, "merge-tree", "--write-tree", "--end-of-options", parents[0], parents[1]
            ).strip()
            rev_range = (
                f"{parents[0]}..{parents[1]}"
                if is_pr_merge
                else f"{parents[0]}...{parents[1]}"
            )
            branch_revs = git(
                repo, "rev-list", "--reverse", "--topo-order", rev_range, "--"
            ).splitlines()
        except SignoffError:
            return False, []
        if merged_tree != tree_id:
            return False, []
        revs = [commit_sha(rev) for rev in branch_revs]
        if is_pr_merge:
            deps = revs or [parents[1]]
        else:
            deps = list(dict.fromkeys([parents[0], parents[1], *revs]))
        return None, deps
    return False, []


def has_author_signoff(
    repo: Path,
    sha: str,
    *,
    memo: dict[str, bool] | None = None,
    keyring_asc: str = GITHUB_WEBFLOW_PGP_KEY,
    allowed_fingerprints: frozenset[str] = GITHUB_WEBFLOW_FINGERPRINTS,
) -> bool:
    """Require an exact author identity or cryptographically verified GitHub certification."""
    target = commit_sha(sha)
    cache = memo if memo is not None else {}
    if target in cache:
        return cache[target]
    pending_deps: dict[str, list[str]] = {}
    in_progress: set[str] = set()
    stack = [target]
    while stack:
        cur = stack[-1]
        if cur in cache:
            stack.pop()
            in_progress.discard(cur)
            continue
        if cur in pending_deps:
            cache[cur] = all(cache.get(dep, False) for dep in pending_deps.pop(cur))
            stack.pop()
            in_progress.discard(cur)
            continue
        if cur in in_progress:
            cache[cur] = False
            stack.pop()
            in_progress.discard(cur)
            continue
        in_progress.add(cur)
        verdict, deps = _inspect_commit_direct(repo, cur, keyring_asc, allowed_fingerprints)
        if verdict is not None:
            cache[cur] = verdict
            stack.pop()
            in_progress.discard(cur)
            continue
        missing = [dep for dep in deps if dep not in cache]
        if not missing:
            cache[cur] = all(cache[dep] for dep in deps)
            stack.pop()
            in_progress.discard(cur)
        else:
            pending_deps[cur] = deps
            stack.extend(reversed(missing))
    return cache[target]


def invalid_signoffs(
    repo: Path,
    commits: list[str],
    *,
    keyring_asc: str = GITHUB_WEBFLOW_PGP_KEY,
    allowed_fingerprints: frozenset[str] = GITHUB_WEBFLOW_FINGERPRINTS,
) -> list[str]:
    """Return each commit that lacks its author's sign-off."""
    memo: dict[str, bool] = {}
    return [
        sha for sha in commits
        if not has_author_signoff(
            repo, sha, memo=memo,
            keyring_asc=keyring_asc, allowed_fingerprints=allowed_fingerprints,
        )
    ]


def object_field(parent: dict, name: str) -> dict:
    """Reject incomplete event envelopes before using their fields."""
    value = parent.get(name)
    if not isinstance(value, dict):
        raise SignoffError(f"The event is missing the {name} object.")
    return value


def fetch_pull_request(repo: Path, repository: str, number: int, head: str) -> None:
    """Fetch one validated pull ref from the public base repository."""
    git(repo, "fetch", "--no-tags", "--no-recurse-submodules", "--no-auto-maintenance",
        f"https://github.com/{repository}.git", f"refs/pull/{number}/head")
    if git(repo, "rev-parse", "--verify", "FETCH_HEAD").strip() != head:
        raise SignoffError("The pull request head changed. Run the check for the current event.")


def event_range(repo: Path, name: str, event: dict, repository: str, event_sha: str) -> tuple[str, str]:
    """Derive bounded revisions from the authenticated workflow event."""
    if (not isinstance(event, dict) or not isinstance(repository, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            or object_field(event, "repository").get("full_name") != repository):
        raise SignoffError("The event repository does not match the workflow repository.")
    event_sha = commit_sha(event_sha)
    if name == "pull_request_target":
        pr = object_field(event, "pull_request")
        number = event.get("number")
        if type(number) is not int or number <= 0 or pr.get("number") != number:
            raise SignoffError("The event must contain a positive pull request number.")
        base_data = object_field(pr, "base")
        if object_field(base_data, "repo").get("full_name") != repository:
            raise SignoffError("The pull request targets a different repository.")
        base = commit_sha(base_data.get("sha"))
        head = commit_sha(object_field(pr, "head").get("sha"))
        fetch_pull_request(repo, repository, number, head)
    elif name == "push":
        head = commit_sha(event.get("after"))
        if head != event_sha or event.get("deleted"):
            raise SignoffError("The push does not match the checked workflow revision.")
        before = event.get("before")
        if before == "0" * len(head) and event.get("created") is True:
            base = "ROOT"
        else:
            base = commit_sha(before)
    elif name == "workflow_dispatch":
        base = object_field(event, "inputs").get("base")
        base = "ROOT" if base == "ROOT" else commit_sha(base)
        head = event_sha
    else:
        raise SignoffError("Use push, pull_request_target, or workflow_dispatch for the DCO check.")
    return base, head


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--base", help="Set the full base SHA, or ROOT for all reachable commits.")
    parser.add_argument("--head", help="Set the full head commit SHA.")
    parser.add_argument("--event", help="Set the GitHub workflow event name.")
    parser.add_argument("--event-path", type=Path, help="Read the GitHub workflow event JSON.")
    args = parser.parse_args(argv)
    try:
        if args.event:
            if args.base or args.head or not args.event_path:
                raise SignoffError("Event mode requires only the event name and event file.")
            event_sha = commit_sha(os.environ.get("GITHUB_SHA"))
            if git(args.repo, "rev-parse", "HEAD").strip() != event_sha:
                raise SignoffError("The checkout does not match the trusted workflow revision.")
            event = json.loads(args.event_path.read_text(encoding="utf-8"))
            base, head = event_range(args.repo, args.event, event,
                                    os.environ.get("GITHUB_REPOSITORY", ""), event_sha)
        else:
            if not args.base or not args.head or args.event_path:
                raise SignoffError("Local mode requires an explicit base and head.")
            base, head = args.base, args.head
        commits = proposed_commits(args.repo, base, head)
        invalid = invalid_signoffs(args.repo, commits)
        for sha in invalid:
            print(f"Commit {sha} lacks a matching author Signed-off-by trailer.")
        if invalid:
            print("Add the author's sign-off to each listed commit and run the check again.")
            return 1
        print(f"DCO sign-offs are valid for all {len(commits)} proposed commits.")
        return 0
    except (SignoffError, OSError, UnicodeError, json.JSONDecodeError,
            RecursionError, subprocess.TimeoutExpired) as error:
        print(f"DCO check failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
