"""Check author sign-offs using commit objects and Git's trailer parser.

Pull requests use the trusted target workflow and the fixed repository's pull
ref. The checker never checks out proposed files. Pushes check before..after.
New branches and manual ROOT runs check every commit reachable from the head.
Other manual runs check base..GITHUB_SHA. Empty or incomplete ranges fail.
Every commit requires certification, including merge commits and bot authors.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


class SignoffError(ValueError):
    """The checker cannot establish a complete, valid commit range."""


def git(repo: Path, *args: str, input_text: str | None = None) -> str:
    """Ignore inherited Git controls and disable altered ancestry."""
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_GRAFT_FILE=os.devnull, GIT_TERMINAL_PROMPT="0")
    command = ["git", "--no-pager", "--no-replace-objects"]
    if args[0] == "interpret-trailers":
        # --parse still applies trailer aliases from repository configuration.
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


def has_author_signoff(repo: Path, sha: str) -> bool:
    """Require an exact author identity in the parsed trailer block."""
    raw = git(repo, "cat-file", "commit", commit_sha(sha))
    headers, separator, message = raw.partition("\n\n")
    authors = [line[7:] for line in headers.splitlines() if line.startswith("author ")]
    if not separator or len(authors) != 1:
        return False
    author = re.fullmatch(r"([^<>\r\n]+) <([^<>\s@]+@[^<>\s@]+)> -?\d+ [+-]\d{4}", authors[0])
    if not author:
        return False
    identity = f"{author[1]} <{author[2]}>"
    trailers = git(repo, "interpret-trailers", "--parse", input_text=message)
    for line in trailers.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.casefold() == "signed-off-by" and value.strip() == identity:
            return True
    return False


def invalid_signoffs(repo: Path, commits: list[str]) -> list[str]:
    """Return each commit that lacks its author's sign-off."""
    return [sha for sha in commits if not has_author_signoff(repo, sha)]


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
            subprocess.TimeoutExpired) as error:
        print(f"DCO check failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
