"""Exercise the standalone distribution without downloads or package installation."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ASSET = "muninn-0.1.0.pyz"


def builder():
    spec = importlib.util.spec_from_file_location("build_zipapp", ROOT / "tools/build_zipapp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def assets(tmp_path):
    directory = tmp_path / "release assets"
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/build_zipapp.py"), "--output-dir", str(directory)],
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    return directory


def install(tmp_path, assets, *arguments, **overrides):
    environment = dict(os.environ)
    environment.update(HOME=str(tmp_path / "user home"), MUNINN_ASSET_DIR=str(assets))
    environment["PATH"] = str(Path(sys.executable).parent) + os.pathsep + environment["PATH"]
    environment.pop("MUNINN_INSTALL_DIR", None)
    environment.pop("MUNINN_VERSION", None)
    environment.update(overrides)
    return subprocess.run(
        ["/bin/sh", str(ROOT / "install.sh"), *arguments], env=environment,
        capture_output=True, text=True, timeout=20, check=False,
    )


def write_checksum(directory, name=ASSET):
    digest = hashlib.sha256((directory / name).read_bytes()).hexdigest()
    (directory / (name + ".sha256")).write_text(f"{digest}  {name}\n", encoding="ascii")


def installer_source():
    return (ROOT / "install.sh").read_text().split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]


def test_archive_is_deterministic_complete_and_runs_without_site_packages(assets, tmp_path):
    second = tmp_path / "second"
    builder().build(second)
    archive = assets / ASSET
    assert archive.read_bytes() == (second / ASSET).read_bytes()
    assert (assets / (ASSET + ".sha256")).read_text() == (
        f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {ASSET}\n"
    )
    with zipfile.ZipFile(archive) as package:
        assert package.comment == b"muninn-kb-standalone-v1"
        required = {"__main__.py", "muninn-standalone.json", "LICENSE", "NOTICE",
                    "THIRD_PARTY_NOTICES.md", "LICENSES/LongMemEval-MIT.txt"}
        modules = {f"muninn/{path.name}" for path in (ROOT / "src/muninn").glob("*.py")}
        assert set(package.namelist()) == required | modules
        for name in required - {"__main__.py", "muninn-standalone.json"}:
            assert package.read(name) == (ROOT / name).read_bytes()
        assert package.read("LICENSE").startswith(b"MIT License\n\nCopyright (c) 2026 Firat Elbey\n")
        assert package.read("LICENSES/LongMemEval-MIT.txt").startswith(
            b"MIT License\n\nCopyright (c) 2024 Di Wu\n"
        )
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in package.infolist())
        metadata = json.loads(package.read("muninn-standalone.json"))
        assert metadata == {"format": 1, "project": "muninn-kb", "version": "0.1.0"}
    for argument in ("--help", "--version", "demo"):
        result = subprocess.run([sys.executable, "-I", "-S", str(archive), argument],
                                cwd=tmp_path, capture_output=True, text=True, timeout=20,
                                check=False)
        assert result.returncode == 0, result.stderr
        if argument == "--version":
            assert result.stdout.strip() == "muninn 0.1.0"


def test_installer_preserves_configuration_and_supports_spaces_and_reinstall(assets, tmp_path):
    home = tmp_path / "user home"
    home.mkdir()
    profile = home / ".zshrc"
    profile.write_text("User configuration.\n")
    first = install(tmp_path, assets)
    assert first.returncode == 0, first.stderr
    executable = home / ".local/bin/muninn"
    assert executable.read_bytes() == (assets / ASSET).read_bytes()
    assert executable.stat().st_mode & 0o111
    assert profile.read_text() == "User configuration.\n"
    before = executable.stat().st_ino
    assert install(tmp_path, assets).returncode == 0
    assert executable.stat().st_ino == before
    assert not (home / ".claude").exists()
    assert not (home / ".codex").exists()
    custom = tmp_path / "custom bin"
    result = install(tmp_path, assets, MUNINN_INSTALL_DIR=str(custom))
    assert result.returncode == 0, result.stderr
    assert (custom / "muninn").read_bytes() == executable.read_bytes()


@pytest.mark.parametrize("kind", ["plain", "symlink", "broken-symlink", "directory", "fifo"])
def test_installer_preserves_unmanaged_and_nonregular_destinations(assets, tmp_path, kind):
    directory = tmp_path / "bin"
    directory.mkdir()
    target = directory / "muninn"
    original = tmp_path / "original"
    original.write_bytes(b"User executable.\n")
    if kind == "plain":
        target.write_bytes(original.read_bytes())
    elif kind == "symlink":
        target.symlink_to(original)
    elif kind == "broken-symlink":
        target.symlink_to(tmp_path / "absent")
    elif kind == "directory":
        target.mkdir()
    else:
        os.mkfifo(target)
    before = target.lstat()
    result = install(tmp_path, assets, MUNINN_INSTALL_DIR=str(directory))
    assert result.returncode != 0
    assert "existing" in result.stderr.lower()
    assert target.lstat().st_ino == before.st_ino
    assert original.read_bytes() == b"User executable.\n"


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
@pytest.mark.parametrize("name", [ASSET, ASSET + ".sha256"])
def test_installer_rejects_nonregular_assets_before_reading(assets, tmp_path, kind, name):
    archive = assets / name
    original = assets / "original.pyz"
    archive.rename(original)
    if kind == "symlink":
        archive.symlink_to(original)
    else:
        os.mkfifo(archive)
    result = install(tmp_path, assets)
    assert result.returncode != 0
    assert not (tmp_path / "user home/.local/bin/muninn").exists()


def test_installer_rejects_corruption_and_keeps_previous_install(assets, tmp_path):
    assert install(tmp_path, assets).returncode == 0
    installed = tmp_path / "user home/.local/bin/muninn"
    before = installed.read_bytes()
    (assets / ASSET).write_bytes(b"Corrupt download.\n")
    result = install(tmp_path, assets)
    assert result.returncode != 0
    assert "checksum" in result.stderr.lower()
    assert installed.read_bytes() == before


@pytest.mark.parametrize("mode", [0o644, 0o641])
def test_reinstall_repairs_missing_execute_permission(assets, tmp_path, mode):
    assert install(tmp_path, assets).returncode == 0
    target = tmp_path / "user home/.local/bin/muninn"
    target.chmod(mode)
    result = install(tmp_path, assets)
    assert result.returncode == 0, result.stderr
    assert target.stat().st_mode & 0o777 == 0o755


def test_installer_refuses_an_executable_changed_during_upgrade(assets, tmp_path, monkeypatch):
    previous = tmp_path / "previous release"
    module = builder()
    module.ENTRY_POINT += b"\n"
    module.build(previous)
    assert install(tmp_path, previous).returncode == 0
    target = tmp_path / "user home/.local/bin/muninn"
    original_open = os.open

    def changed_before_write(path, *args, **kwargs):
        if str(path).startswith(".muninn-"):
            target.write_bytes(b"User replacement.\n")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", changed_before_write)
    monkeypatch.setenv("MUNINN_ASSET_DIR", str(assets))
    monkeypatch.setenv("MUNINN_INSTALL_DIR", str(target.parent))
    monkeypatch.setenv("MUNINN_VERSION", "0.1.0")
    with pytest.raises(SystemExit, match="changed"):
        # Run reviewed source in-process to inject a deterministic filesystem race.
        exec(compile(installer_source(), str(ROOT / "install.sh"), "exec"), {})  # noqa: S102
    assert target.read_bytes() == b"User replacement.\n"


def test_installer_rejects_version_mismatch_and_wrong_checksum_name(assets, tmp_path):
    alternate = "muninn-0.1.1.pyz"
    shutil.copyfile(assets / ASSET, assets / alternate)
    write_checksum(assets, alternate)
    result = install(tmp_path, assets, MUNINN_VERSION="0.1.1")
    assert result.returncode != 0
    assert "version" in result.stderr.lower()
    (assets / (ASSET + ".sha256")).write_text("0" * 64 + "  another.pyz\n")
    result = install(tmp_path, assets)
    assert result.returncode != 0
    assert "checksum" in result.stderr.lower()


def test_installer_upgrades_only_recognized_archives(assets, tmp_path):
    assert install(tmp_path, assets).returncode == 0
    source = tmp_path / "source"
    shutil.copytree(ROOT / "src", source / "src")
    for name in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md", "LICENSES", "pyproject.toml"):
        path = ROOT / name
        if path.is_dir():
            shutil.copytree(path, source / name)
        else:
            shutil.copyfile(path, source / name)
    for name in ("pyproject.toml", "src/muninn/__init__.py"):
        path = source / name
        path.write_text(path.read_text().replace('"0.1.0"', '"0.1.1"'))
    builder().build(assets, root=source)
    result = install(tmp_path, assets, MUNINN_VERSION="0.1.1")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "user home/.local/bin/muninn").read_bytes() == (
        assets / "muninn-0.1.1.pyz").read_bytes()


def test_installer_downloads_only_versioned_https_assets(assets, tmp_path):
    commands = tmp_path / "commands"
    commands.mkdir()
    (commands / "python3").symlink_to(sys.executable)
    log = tmp_path / "curl.jsonl"
    curl = commands / "curl"
    curl.write_text(
        "#!/usr/bin/env python3\nimport json, os, pathlib, shutil, sys\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['CURL_LOG'], 'a') as handle: handle.write(json.dumps(args)+'\\n')\n"
        "destination = args[args.index('--output') + 1]\n"
        "name = args[-1].rsplit('/', 1)[-1]\n"
        "shutil.copyfile(pathlib.Path(os.environ['CURL_ASSETS']) / name, destination)\n"
    )
    curl.chmod(0o755)
    result = install(tmp_path, "", MUNINN_ASSET_DIR="", CURL_LOG=str(log),
                     CURL_ASSETS=str(assets), PATH=str(commands))
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(calls) == 2
    for call in calls:
        assert call[call.index("--proto") + 1] == "=https"
        assert call[call.index("--proto-redir") + 1] == "=https"
        assert call[-1].startswith("https://github.com/firat-elbey/muninn/releases/download/v0.1.0/")


def test_download_failure_and_missing_runtime_leave_no_install(assets, tmp_path):
    commands = tmp_path / "commands"
    commands.mkdir()
    curl = commands / "curl"
    curl.write_text("#!/bin/sh\nexit 22\n")
    curl.chmod(0o755)
    result = install(tmp_path, "", MUNINN_ASSET_DIR="",
                     PATH=str(commands) + os.pathsep + os.environ["PATH"])
    assert result.returncode != 0
    assert "download" in result.stderr.lower()
    result = install(tmp_path, assets, PATH=str(commands))
    assert result.returncode != 0
    assert "Python 3.10" in result.stderr
    assert not (tmp_path / "user home/.local/bin/muninn").exists()


def test_installer_help_and_version_do_not_download_or_install(tmp_path):
    for argument in ("--help", "--version"):
        result = install(tmp_path, tmp_path / "missing", argument, PATH="/nonexistent")
        assert result.returncode == 0, result.stderr
        assert "0.1.0" in result.stdout
    for argument in ("--unknown",):
        assert install(tmp_path, "", argument).returncode != 0
    assert install(tmp_path, "", MUNINN_VERSION="../invalid").returncode != 0
    assert not (tmp_path / "user home").exists()


def test_installer_rejects_an_older_python_before_any_install(monkeypatch):
    monkeypatch.setattr(sys, "version_info", (3, 9, 0))
    with pytest.raises(SystemExit, match="Python 3.10"):
        # Run reviewed source in-process to substitute the runtime version before imports.
        exec(compile(installer_source(), str(ROOT / "install.sh"), "exec"), {})  # noqa: S102


def test_installer_ignores_pythonpath_shadow_modules(assets, tmp_path):
    shadow = tmp_path / "untrusted modules"
    shadow.mkdir()
    (shadow / "json.py").write_text("raise RuntimeError('Shadow module executed.')\n")
    environment = dict(os.environ, PYTHONPATH=str(shadow), MUNINN_ASSET_DIR=str(assets),
                       HOME=str(tmp_path / "user home"))
    unisolated = tmp_path / "unisolated.sh"
    unisolated.write_text((ROOT / "install.sh").read_text().replace(
        "exec python3 -I -S -", "exec python3 -"))
    result = subprocess.run(["/bin/sh", str(unisolated)], env=environment,
                            capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode != 0
    assert "Shadow module executed." in result.stderr
    result = install(tmp_path, assets, PYTHONPATH=str(shadow))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("conflicting", [False, True])
def test_archive_setup_hooks_use_the_running_archive_without_path_dependency(assets, tmp_path, conflicting):
    assert install(tmp_path, assets).returncode == 0
    home = tmp_path / "user home"
    executable = home / ".local/bin/muninn"
    commands = tmp_path / "commands"
    commands.mkdir()
    (commands / "python3").symlink_to(sys.executable)
    if conflicting:
        other = commands / "muninn"
        other.write_text("#!/bin/sh\nexit 99\n")
        other.chmod(0o755)
    environment = dict(os.environ, HOME=str(home), PATH=str(commands))
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(executable), "setup", "--brain", str(tmp_path / "brain")],
        env=environment, capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    for relative in (".claude/settings.json", ".codex/hooks.json", ".grok/hooks/muninn.json"):
        hooks = json.loads((home / relative).read_text())["hooks"]
        for groups in hooks.values():
            for group in groups:
                for hook in group["hooks"]:
                    assert shlex.split(hook["command"])[:2] == [sys.executable, str(executable)]
        if relative == ".claude/settings.json":
            command = hooks["SessionStart"][0]["hooks"][0]["command"]
            arguments = shlex.split(command.split(" 2>/dev/null", 1)[0])
            result = subprocess.run(arguments, env=environment, input='{"session_id":"archive-test"}',
                                    capture_output=True, text=True, timeout=20, check=False)
            assert result.returncode == 0, result.stderr
            assert "Context pack" in result.stdout
