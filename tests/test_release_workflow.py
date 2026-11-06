"""Verify the standalone release contract without publishing assets."""

from __future__ import annotations

import ast
import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
CI = ROOT / ".github" / "workflows" / "ci.yml"


def run_block(name: str, workflow: Path = WORKFLOW) -> str:
    """Read one literal shell block from the release workflow."""
    text = workflow.read_text(encoding="utf-8")
    step = text.split(f"      - name: {name}\n", 1)[1]
    step = step.split("\n      - ", 1)[0]
    lines = step.split("        run: |\n", 1)[1].splitlines()
    return "\n".join(line[10:] for line in lines if line.startswith("          "))


def shell_environment(workspace: Path) -> dict[str, str]:
    env = os.environ.copy()
    for name in ("MUNINN_VERSION", "MUNINN_INSTALL_DIR", "MUNINN_ASSET_DIR"):
        env.pop(name, None)
    env.update(
        GITHUB_WORKSPACE=str(workspace),
        GITHUB_REF_NAME="v0.2.1",
        GITHUB_REPOSITORY="example/muninn",
        RUNNER_TEMP=str(workspace),
        PATH=str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", ""),
    )
    return env


def test_release_only_grants_publication_write_access() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    build, publish = text.split("  publish:\n", 1)

    assert "contents: read" in build
    assert "contents: write" not in build
    assert "contents: write" in publish
    assert "id-token:" not in text
    assert "pypi" not in text.lower()
    assert "npm publish" not in text
    assert "twine upload" not in text
    assert "needs: build" in publish
    assert "types: [published]" in text
    assert "--clobber" not in text
    assert re.findall(r"uses: ([^\s]+)", text)
    assert all(
        re.fullmatch(r"[^@]+@[0-9a-f]{40}", action) or action == "./.github/workflows/parser-install.yml"
        for action in re.findall(r"uses: ([^\s]+)", text)
    )


def test_release_preserves_package_and_revision_checks() -> None:
    verify = run_block("Verify the release revision")
    distributions = run_block("Build and inspect distributions")
    standalone = run_block("Build standalone assets")

    assert "tools/check_repository.py" in verify
    assert "coverage report --fail-under=93" in verify
    assert "python -m twine check dist/*" in distributions
    assert "python tools/check_distribution.py dist/*" in distributions
    assert "--output-dir standalone" in standalone
    assert "dist/" not in standalone


def test_release_verifies_npm_before_uploading_packages() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    package = run_block("Build and verify npm package")
    assert "npm pack ./npm --pack-destination dist" in package
    assert 'python npm/check.py "dist/muninn-kb-$version.tgz"' in package
    assert package.index("npm/check.py") < package.index("sha256sum")
    assert workflow.count("name: verified-package-assets") == 2


def test_installer_default_matches_project_version() -> None:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    version = re.search(r'^version = "([^"]+)"$', project, re.MULTILINE)
    assert version is not None
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    body = installer.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    install = next(
        node for node in ast.parse(body).body
        if isinstance(node, ast.FunctionDef) and node.name == "install"
    )
    release = next(
        node.value for node in install.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "release" for target in node.targets)
    )
    assert isinstance(release, ast.BoolOp) and isinstance(release.op, ast.Or)
    assert ast.literal_eval(release.values[-1]) == version.group(1)

    result = subprocess.run(
        ["sh", str(ROOT / "install.sh"), "--version"],
        text=True, capture_output=True, check=True, timeout=10,
    )
    assert result.stdout.strip() == "Muninn installer " + version.group(1)


def test_standalone_assets_pass_isolated_smoke_checks(tmp_path: Path) -> None:
    (tmp_path / "tools").symlink_to(ROOT / "tools", target_is_directory=True)
    shutil.copyfile(ROOT / "install.sh", tmp_path / "install.sh")
    env = shell_environment(tmp_path)

    for name in ("Build standalone assets", "Verify standalone assets"):
        result = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", run_block(name)],
            cwd=tmp_path,
            env=env,
            check=False,
            timeout=30,
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    assert {path.name for path in (tmp_path / "standalone").iterdir()} == {
        "muninn-0.2.1.pyz",
        "muninn-0.2.1.pyz.sha256",
        "install.sh",
    }
    assert "python -I -S" in run_block("Verify standalone assets")
    assert "mktemp -d" in run_block("Verify standalone assets")


def test_publish_uploads_only_verified_release_assets(tmp_path: Path) -> None:
    script = run_block("Publish verified release assets")
    assets = tmp_path / "standalone"
    assets.mkdir()
    archive = assets / "muninn-0.2.1.pyz"
    archive.write_bytes(b"verified archive")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (assets / "muninn-0.2.1.pyz.sha256").write_text(
        f"{digest}  {archive.name}\n", encoding="utf-8"
    )
    (assets / "install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    packages = tmp_path / "dist"
    packages.mkdir()
    package_names = ["muninn_kb-0.2.1-py3-none-any.whl", "muninn_kb-0.2.1.tar.gz",
                     "muninn-kb-0.2.1.tgz"]
    checksums = []
    for name in package_names:
        package = packages / name
        package.write_bytes(name.encode())
        checksums.append(f"{hashlib.sha256(package.read_bytes()).hexdigest()}  {name}\n")
    (packages / "SHA256SUMS").write_text("".join(checksums), encoding="utf-8")
    env = shell_environment(tmp_path)
    stub = 'gh() { printf "%s\\n" "$@"; }\n'

    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", stub + script],
        cwd=tmp_path,
        env=env,
        check=False,
        timeout=30,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines()[-11:] == [
        "release",
        "upload",
        "v0.2.1",
        "standalone/muninn-0.2.1.pyz",
        "standalone/muninn-0.2.1.pyz.sha256",
        "standalone/install.sh",
        *["dist/" + name for name in package_names],
        "--repo",
        "example/muninn",
    ]
    archive.write_bytes(b"changed archive")

    rejected = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", stub + script],
        cwd=tmp_path,
        env=env,
        check=False,
        timeout=30,
        text=True,
        capture_output=True,
    )

    assert rejected.returncode != 0
    assert "upload" not in rejected.stdout

    archive.write_bytes(b"verified archive")
    for name in package_names:
        package = packages / name
        package.write_bytes(b"modified after verification")
        rejected = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", stub + script],
            cwd=tmp_path, env=env, text=True, capture_output=True, timeout=30,
        )
        assert rejected.returncode != 0, name
        assert "upload" not in rejected.stdout
        package.write_bytes(name.encode())


def test_ci_runs_offline_installer_smoke_checks(tmp_path: Path) -> None:
    script = run_block("Test the standalone installer offline", CI)
    (tmp_path / "tools").symlink_to(ROOT / "tools", target_is_directory=True)
    shutil.copyfile(ROOT / "install.sh", tmp_path / "install.sh")

    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=tmp_path,
        env=shell_environment(tmp_path),
        check=False,
        timeout=30,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "MUNINN_ASSET_DIR=" in script
    assert "curl " not in script
    assert "setup" not in script
    assert '"$smoke_dir/bin/muninn" --help' in script
    assert '"$smoke_dir/bin/muninn" demo' in script


def test_required_package_check_fails_when_parser_matrix_does_not_pass(tmp_path):
    package = CI.read_text().split("  package:\n", 1)[1]
    assert "needs: parsers" in package
    assert "if: always()" in package
    script = run_block("Require successful parser installation checks", CI)
    for state in ("success", "failure", "cancelled", "skipped", ""):
        result = subprocess.run(["bash", "-euo", "pipefail", "-c", script], cwd=tmp_path,
                                env=dict(os.environ, PARSER_RESULT=state), check=False)
        assert (result.returncode == 0) == (state == "success")


def test_artifact_handoff_fails_closed_on_missing_files(tmp_path: Path) -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    build = workflow.split("  publish:\n", 1)[0]
    step = run_block("Select verified files for transfer")
    outputs = tmp_path / "outputs"
    env = dict(shell_environment(tmp_path), GITHUB_OUTPUT=str(outputs))
    packages = ["dist/muninn_kb-0.2.1-py3-none-any.whl", "dist/muninn_kb-0.2.1.tar.gz",
                "dist/muninn-kb-0.2.1.tgz", "dist/SHA256SUMS"]
    standalone = ["standalone/muninn-0.2.1.pyz", "standalone/muninn-0.2.1.pyz.sha256",
                  "standalone/install.sh"]
    (tmp_path / "dist").mkdir()
    (tmp_path / "standalone").mkdir()
    for relative in packages + standalone + ["dist/extra.txt", "standalone/extra.txt"]:
        (tmp_path / relative).touch()

    def run():
        return subprocess.run(["bash", "-euo", "pipefail", "-c", step],
                              cwd=tmp_path, env=env, capture_output=True, timeout=10)

    assert run().returncode == 0
    assert outputs.read_text().splitlines() == [
        "packages<<FILES", *packages, "FILES", "standalone<<FILES", *standalone, "FILES"]
    uploads = build.split("      - uses: actions/upload-artifact@")[1:]
    assert len(uploads) == 2
    for upload, key in zip(uploads, ("packages", "standalone")):
        assert "path: ${{ steps.transfer.outputs." + key + " }}" in upload
        assert "if-no-files-found: error" in upload

    outputs.unlink()
    for relative in packages + standalone:
        asset = tmp_path / relative
        asset.unlink()
        assert run().returncode != 0, relative
        assert not outputs.exists()
        asset.touch()
