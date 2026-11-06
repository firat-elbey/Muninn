from __future__ import annotations

import importlib.util
import subprocess
import tarfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_release_module():
    path = ROOT / "tools" / "prepare_public_release.py"
    spec = importlib.util.spec_from_file_location("prepare_public_release", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


@pytest.mark.parametrize(
    "email",
    [
        "56897870+firat-elbey@users.noreply.github.com",
        "firat-elbey@users.noreply.github.com",
    ],
)
def test_github_no_reply_addresses_are_accepted(email: str) -> None:
    release = load_release_module()

    assert release.validate_author_email(email) == email


@pytest.mark.parametrize(
    "email",
    ["person@example.com", "noreply@github.com", "invalid"],
)
def test_other_addresses_are_rejected(email: str) -> None:
    release = load_release_module()

    with pytest.raises(release.ReleaseError, match="GitHub no-reply"):
        release.validate_author_email(email)


@pytest.mark.parametrize("inherited", ["identity", "routing", "config", "objects"])
def test_clean_export_has_one_commit_and_no_inherited_refs(
    tmp_path: Path, monkeypatch, inherited: str,
) -> None:
    release = load_release_module()
    source = tmp_path / "source"
    destination = tmp_path / "public"
    (source / "tools").mkdir(parents=True)
    (source / "README.md").write_text("# Example\n", encoding="utf-8")
    (source / "AGENTS.md").write_text("# Instructions\n", encoding="utf-8")
    (source / "CLAUDE.md").symlink_to("AGENTS.md")
    (source / "tools" / "check_repository.py").write_text(
        "raise SystemExit(0)\n", encoding="utf-8"
    )
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "Private author")
    git(source, "config", "user.email", "private@example.com")
    git(source, "add", "--all")
    git(source, "commit", "-m", "Private history")
    git(source, "update-ref", "refs/muninn/ledger", "HEAD")
    original = {name: (source / ".git" / name).read_bytes()
                for name in ("config", "HEAD", "index")}
    source_revision = git(source, "rev-parse", "HEAD")

    # The requested public identity must override inherited private Git identity.
    with monkeypatch.context() as environment:
        for role in ("AUTHOR", "COMMITTER"):
            environment.setenv(f"GIT_{role}_NAME", "Private environment author")
            environment.setenv(f"GIT_{role}_EMAIL", "private@example.invalid")
        if inherited == "routing":
            environment.setenv("GIT_DIR", str(source / ".git"))
            environment.setenv("GIT_WORK_TREE", str(source))
            environment.setenv("GIT_COMMON_DIR", str(source / ".git"))
            environment.setenv("GIT_INDEX_FILE", str(source / ".git" / "index"))
        elif inherited == "config":
            environment.setenv("GIT_CONFIG_COUNT", "1")
            environment.setenv("GIT_CONFIG_KEY_0", "core.bare")
            environment.setenv("GIT_CONFIG_VALUE_0", "true")
        elif inherited == "objects":
            environment.setenv("GIT_OBJECT_DIRECTORY", str(source / ".git" / "objects"))
            environment.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", str(source / ".git" / "objects"))
            environment.setenv("GIT_NAMESPACE", "private")

        release.prepare_public_release(
            source,
            destination,
            author_name="Public author",
            author_email="56897870+firat-elbey@users.noreply.github.com",
        )

    assert original == {name: (source / ".git" / name).read_bytes() for name in original}
    assert git(source, "rev-parse", "HEAD") == source_revision
    assert git(source, "status", "--porcelain") == ""
    assert git(source, "rev-parse", "HEAD^{tree}") == git(destination, "rev-parse", "HEAD^{tree}")
    assert not (destination / ".git" / "objects" / "info" / "alternates").exists()

    assert git(destination, "rev-list", "--all", "--count") == "1"
    assert git(destination, "for-each-ref", "--format=%(refname)").splitlines() == [
        "refs/heads/main"
    ]
    assert git(destination, "remote") == ""
    assert git(destination, "status", "--porcelain") == ""
    assert "Private history" not in git(destination, "log", "--format=%B")
    assert (
        git(destination, "log", "-1", "--format=%ae")
        == "56897870+firat-elbey@users.noreply.github.com"
    )
    assert "Signed-off-by: Public author" in git(
        destination, "log", "-1", "--format=%B"
    )
    assert git(destination, "log", "-1", "--format=%ce") == (
        "56897870+firat-elbey@users.noreply.github.com")
    assert "private@example.invalid" not in git(destination, "cat-file", "-p", "HEAD")
    assert (destination / "CLAUDE.md").is_symlink()
    assert (destination / "CLAUDE.md").readlink() == Path("AGENTS.md")


@pytest.mark.parametrize("linkname", ["/etc/passwd", "../../outside"])
def test_unsafe_archive_symlinks_are_rejected(linkname: str) -> None:
    release = load_release_module()
    target = tarfile.TarInfo("CLAUDE.md")
    target.type = tarfile.SYMTYPE
    target.linkname = linkname

    with pytest.raises(release.ReleaseError, match="archive link"):
        release.validate_archive_members([target])
