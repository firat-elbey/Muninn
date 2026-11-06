"""Manage persistent source indexes and render bounded retrieval results."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from .code_context import CodeContextPack, assemble_code_context
from .code_search import (CodeHit, MAX_SOURCE_BYTES, SKIP_DIRECTORIES,
                          SOURCE_EXTENSIONS)
from .persistent_code_search import PersistentCodeSearchIndex


INDEX_DIRECTORY = "source-indexes"
PARSER_DISTRIBUTIONS = (
    "tree-sitter",
    "tree-sitter-language-pack",
    "tree-sitter-c-sharp",
    "tree-sitter-embedded-template",
    "tree-sitter-yaml",
)


@dataclass(frozen=True)
class SourceSearchResult:
    """Describe one source search and the index that produced it."""

    source_root: str
    database: str
    rebuilt: bool
    document_count: int
    hits: tuple[CodeHit, ...]
    pack: CodeContextPack


def resolve_source_root(path: str | os.PathLike[str]) -> str:
    """Return an existing source directory as a canonical absolute path."""
    root = os.path.realpath(os.fspath(path))
    if not os.path.isdir(root):
        raise NotADirectoryError(root)
    return root


def source_database(bundle_root: str | os.PathLike[str],
                    source_root: str | os.PathLike[str]) -> str:
    """Place one repository index in the consumer's private sidecar."""
    source = resolve_source_root(source_root)
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(source).name).strip("-")
    label = label or "source"
    identity = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
    return os.path.join(
        os.path.realpath(os.fspath(bundle_root)),
        ".muninn",
        INDEX_DIRECTORY,
        f"{label}-{identity}.sqlite",
    )


def _structural_profile() -> tuple[str, bool]:
    """Identify parser readiness and exact dependencies for a separate cache."""
    from . import extract

    ready = not extract.parser_issues()
    versions = {}
    for name in PARSER_DISTRIBUTIONS:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    profile = json.dumps(
        {"revision": 1, "ready": ready, "versions": versions},
        sort_keys=True,
    )
    return hashlib.sha256(profile.encode("utf-8")).hexdigest()[:16], ready


def _git(root: str, *arguments: str) -> bytes | None:
    """Run one bounded, non-interactive Git query and return its output."""
    try:
        result = subprocess.run(
            ["git", "-C", root, *arguments],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _relevant_file(path: Path) -> bool:
    """Return whether the standard source reader would index this file."""
    try:
        return (
            path.suffix.lower() in SOURCE_EXTENSIONS
            and not path.is_symlink()
            and path.is_file()
            and path.stat().st_size <= MAX_SOURCE_BYTES
        )
    except OSError:
        return False


def _hash_changed_file(digest, root: str, relative: bytes) -> None:
    """Add the current form of one changed source path to a snapshot."""
    path_text = os.fsdecode(relative)
    digest.update(relative)
    digest.update(b"\0")
    candidate = Path(root, path_text)
    if not _relevant_file(candidate):
        digest.update(b"absent-or-excluded\0")
        return
    try:
        digest.update(candidate.read_bytes())
    except OSError:
        digest.update(b"unreadable\0")
    digest.update(b"\0")


def _git_snapshot(root: str) -> str | None:
    """Fingerprint Git's committed tree and the current relevant changes."""
    head = _git(root, "rev-parse", "--verify", "HEAD")
    if head is None:
        return None
    changed = _git(
        root,
        "diff",
        "--name-only",
        "-z",
        "--relative",
        "HEAD",
        "--",
        ".",
    )
    untracked = _git(
        root,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
        ".",
    )
    if changed is None or untracked is None:
        return None
    digest = hashlib.sha256()
    digest.update(b"git\0")
    digest.update(os.fsencode(root))
    digest.update(b"\0")
    digest.update(head.strip())
    digest.update(b"\0")
    # The reader also indexes ignored source files. Git status cannot detect
    # their edits, so include the same source inventory used outside Git.
    digest.update(_tree_snapshot(root).encode())
    digest.update(b"\0")
    paths = {value for value in (changed + untracked).split(b"\0") if value}
    for relative in sorted(paths):
        _hash_changed_file(digest, root, relative)
    return "git:" + digest.hexdigest()


def _tree_snapshot(root: str) -> str:
    """Fingerprint source metadata when the directory is not a Git tree."""
    digest = hashlib.sha256()
    digest.update(b"tree\0")
    digest.update(os.fsencode(root))
    digest.update(b"\0")
    for directory, names, files in os.walk(root):
        names[:] = sorted(
            name for name in names
            if name not in SKIP_DIRECTORIES and not name.startswith(".git")
        )
        for name in sorted(files):
            path = Path(directory, name)
            if not _relevant_file(path):
                continue
            try:
                stat = path.stat()
                relative = path.relative_to(root).as_posix()
            except (OSError, ValueError):
                continue
            digest.update(relative.encode("utf-8", "surrogateescape"))
            digest.update(b"\0")
            digest.update(str(stat.st_size).encode())
            digest.update(b"\0")
            digest.update(str(stat.st_mtime_ns).encode())
            digest.update(b"\0")
    return "tree:" + digest.hexdigest()


def source_snapshot(source_root: str | os.PathLike[str]) -> str:
    """Return a deterministic freshness marker for one source directory."""
    root = resolve_source_root(source_root)
    return _git_snapshot(root) or _tree_snapshot(root)


def build_source_index(bundle_root: str | os.PathLike[str],
                       source_root: str | os.PathLike[str]
                       ) -> PersistentCodeSearchIndex:
    """Build one source index atomically and return its open reader."""
    source = resolve_source_root(source_root)
    database = source_database(bundle_root, source)
    snapshot = source_snapshot(source)
    return PersistentCodeSearchIndex.build(
        source,
        database,
        source_snapshot=snapshot,
    )


def ensure_source_index(bundle_root: str | os.PathLike[str],
                        source_root: str | os.PathLike[str], *,
                        refresh: bool = True,
                        force: bool = False,
                        structural: bool = False
                        ) -> tuple[PersistentCodeSearchIndex, bool]:
    """Open a current index, rebuilding it when required or requested."""
    source = resolve_source_root(source_root)
    database = source_database(bundle_root, source)
    parser_ready = False
    if structural:
        # Probe even without source refresh: installing or changing parsers
        # must not reuse facts extracted under a different capability.
        profile, parser_ready = _structural_profile()
        path = Path(database)
        database = str(path.with_name(f"{path.stem}-structural-{profile}.sqlite"))
    current_snapshot = source_snapshot(source) if refresh or force else ""
    if not force and os.path.isfile(database):
        try:
            index = PersistentCodeSearchIndex(database)
        except (OSError, ValueError):
            index = None
        if index is not None:
            same_root = index.metadata.get("source_root") == source
            same_snapshot = (
                not refresh
                or index.metadata.get("source_snapshot") == current_snapshot
            )
            if same_root and same_snapshot:
                return index, False
            index.close()
    index = PersistentCodeSearchIndex.build(
        source,
        database,
        structural=parser_ready,
        source_snapshot=current_snapshot or source_snapshot(source),
    )
    return index, True


def _read_indexed_source(source_root: str, relative: str) -> str | None:
    """Read one indexed file without following it outside the source root."""
    unresolved = Path(source_root, relative)
    try:
        candidate = unresolved.resolve()
        if unresolved.is_symlink() or not candidate.is_file():
            return None
        if os.path.commonpath((source_root, str(candidate))) != source_root:
            return None
        data = candidate.read_bytes()
    except (OSError, ValueError):
        return None
    if len(data) > MAX_SOURCE_BYTES:
        return None
    return data.decode("utf-8", "replace")


def search_source(bundle_root: str | os.PathLike[str],
                  source_root: str | os.PathLike[str], query: str, *,
                  budget: int = 1200, limit: int = 20,
                  mode: str = "hybrid", refresh: bool = True
                  ) -> SourceSearchResult:
    """Return a bounded source context from the validated default ranking."""
    if not query.strip():
        raise ValueError("the source query must not be empty")
    source = resolve_source_root(source_root)
    index, rebuilt = ensure_source_index(
        bundle_root,
        source,
        refresh=refresh,
        structural=mode in {"symbol", "structural-fusion"},
    )
    try:
        hits = index.search(query, mode=mode, limit=limit)
        records = {}
        for hit in hits:
            text = _read_indexed_source(source, hit.path)
            if text is not None:
                records[hit.path] = text
        pack = assemble_code_context(
            records,
            hits,
            query,
            budget=budget,
        )
        return SourceSearchResult(
            source_root=source,
            database=str(index.database),
            rebuilt=rebuilt,
            document_count=index.document_count,
            hits=tuple(hits),
            pack=pack,
        )
    finally:
        index.close()
