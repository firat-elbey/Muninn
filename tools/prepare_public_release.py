#!/usr/bin/env python3
"""Create a one-commit public repository from the reviewed source tree."""

from __future__ import annotations

import argparse
import io
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath


NO_REPLY = re.compile(
    r"^(?:[0-9]+\+)?[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"@users\.noreply\.github\.com$"
)


class ReleaseError(RuntimeError):
    """The source tree cannot produce a safe public candidate."""


def isolated_environment() -> dict[str, str]:
    """Prevent inherited Git routing, identity, and configuration overrides."""
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_TERMINAL_PROMPT="0")
    return environment


def run(cwd: Path, *command: str, capture: bool = True,
        env: dict[str, str] | None = None) -> str:
    try:
        result = subprocess.run(
            list(command),
            cwd=cwd,
            check=True,
            capture_output=capture,
            text=True,
            env=isolated_environment() if env is None else env,
        )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "no diagnostic").strip()
        raise ReleaseError(f"command failed: {' '.join(command)}: {detail}") from error
    return result.stdout.strip() if capture else ""


def validate_author_email(email: str) -> str:
    if not NO_REPLY.fullmatch(email):
        raise ReleaseError("the public commit must use a GitHub no-reply address")
    return email


def resolve_symlink_target(member: tarfile.TarInfo) -> PurePosixPath:
    """Resolve one relative archive link without consulting the file system."""
    link = PurePosixPath(member.linkname)
    if link.is_absolute():
        raise ReleaseError(f"archive link is absolute: {member.name}")
    parts = list(PurePosixPath(member.name).parent.parts)
    for part in link.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise ReleaseError(f"archive link escapes the repository: {member.name}")
            parts.pop()
            continue
        parts.append(part)
    return PurePosixPath(*parts)


def validate_archive_members(members: list[tarfile.TarInfo]) -> None:
    entries = {PurePosixPath(member.name): member for member in members}
    for member in members:
        path = PurePosixPath(member.name)
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise ReleaseError(f"archive contains an unsafe path: {member.name}")
        if member.issym():
            resolved = resolve_symlink_target(member)
            target = entries.get(resolved)
            if target is None or not target.isfile():
                raise ReleaseError(
                    f"archive link does not name a regular archived file: {member.name}"
                )
            continue
        if member.islnk():
            raise ReleaseError(f"archive contains a hard link: {member.name}")
        if not (member.isdir() or member.isfile()):
            raise ReleaseError(f"archive contains an unsupported entry: {member.name}")


def extract_archive(archive: bytes, destination: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
        members = source.getmembers()
        validate_archive_members(members)
        for member in members:
            if member.issym():
                continue
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            archived = source.extractfile(member)
            if archived is None:
                raise ReleaseError(f"archive file has no content: {member.name}")
            with target.open("wb") as output:
                output.write(archived.read())
            os.chmod(target, member.mode & 0o777)
        for member in members:
            if not member.issym():
                continue
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(member.linkname)


def verify_source(source: Path, destination: Path) -> str:
    if not source.is_dir():
        raise ReleaseError(f"source repository does not exist: {source}")
    if destination.exists():
        raise ReleaseError(f"destination already exists: {destination}")
    try:
        destination.relative_to(source)
    except ValueError:
        pass
    else:
        raise ReleaseError("destination must be outside the source repository")
    if run(source, "git", "rev-parse", "--is-inside-work-tree") != "true":
        raise ReleaseError(f"source is not a Git working tree: {source}")
    status = run(source, "git", "status", "--porcelain", "--untracked-files=normal")
    if status:
        raise ReleaseError("source working tree is not clean")
    branch = run(source, "git", "branch", "--show-current")
    if branch != "main":
        raise ReleaseError(f"source branch is {branch or 'detached'}, not main")
    checker = source / "tools" / "check_repository.py"
    if not checker.is_file():
        raise ReleaseError("source does not contain tools/check_repository.py")
    run(source, sys.executable, str(checker))
    return run(source, "git", "rev-parse", "HEAD")


def verify_candidate(destination: Path) -> str:
    if run(destination, "git", "rev-list", "--all", "--count") != "1":
        raise ReleaseError("public candidate does not contain exactly one commit")
    references = run(
        destination, "git", "for-each-ref", "--format=%(refname)"
    ).splitlines()
    if references != ["refs/heads/main"]:
        raise ReleaseError(f"public candidate contains unexpected references: {references}")
    if run(destination, "git", "remote"):
        raise ReleaseError("public candidate inherited a remote")
    if run(destination, "git", "status", "--porcelain"):
        raise ReleaseError("public candidate is not clean")
    run(destination, sys.executable, "tools/check_repository.py")
    return run(destination, "git", "rev-parse", "HEAD")


def prepare_public_release(
    source: Path,
    destination: Path,
    *,
    author_name: str,
    author_email: str,
) -> tuple[str, str]:
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    author_email = validate_author_email(author_email)
    if not author_name.strip():
        raise ReleaseError("the public commit author name cannot be empty")
    source_revision = verify_source(source, destination)
    archive = subprocess.run(
        ["git", "archive", "--format=tar", "HEAD"],
        cwd=source,
        check=True,
        capture_output=True,
        env=isolated_environment(),
    ).stdout

    destination.mkdir(parents=True)
    extract_archive(archive, destination)
    run(destination, "git", "init", "--quiet", "--initial-branch=main")
    run(destination, "git", "config", "user.name", author_name.strip())
    run(destination, "git", "config", "user.email", author_email)
    run(destination, "git", "config", "commit.gpgsign", "false")
    run(destination, "git", "config", "core.hooksPath", "/dev/null")
    run(destination, "git", "add", "--all")
    identity_env = isolated_environment()
    for role in ("AUTHOR", "COMMITTER"):
        identity_env[f"GIT_{role}_NAME"] = author_name.strip()
        identity_env[f"GIT_{role}_EMAIL"] = author_email
    run(
        destination,
        "git",
        "commit",
        "--quiet",
        "--signoff",
        "--message",
        "Initial public release",
        env=identity_env,
    )
    public_revision = verify_candidate(destination)
    identities = run(destination, "git", "log", "-1", "--format=%ae%n%ce").splitlines()
    if identities != [author_email, author_email]:
        raise ReleaseError("public commit identity does not match the requested no-reply address")
    return source_revision, public_revision


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser(
        description=(
            "Create a one-commit public repository without private history, "
            "remotes, or custom references."
        )
    )
    argument_parser.add_argument("destination", type=Path)
    argument_parser.add_argument("--source", type=Path, default=Path.cwd())
    argument_parser.add_argument("--author-name", required=True)
    argument_parser.add_argument("--author-email", required=True)
    return argument_parser


def main() -> int:
    arguments = parser().parse_args()
    try:
        source_revision, public_revision = prepare_public_release(
            arguments.source,
            arguments.destination,
            author_name=arguments.author_name,
            author_email=arguments.author_email,
        )
    except (OSError, ReleaseError, subprocess.SubprocessError, tarfile.TarError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"Source revision: {source_revision}")
    print(f"Public revision: {public_revision}")
    print(f"Candidate: {arguments.destination.expanduser().resolve()}")
    print("Verified: one commit, main only, no remote, clean tree, audit passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
