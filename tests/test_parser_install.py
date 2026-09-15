"""Regressions for the parser-complete default installation."""

import ast
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from muninn import cli, extract, home, skill


@pytest.fixture
def release(tmp_path):
    assets = tmp_path / "assets"
    subprocess.run([sys.executable, str(ROOT / "tools/build_zipapp.py"),
                    "--output-dir", str(assets)], check=True, capture_output=True)
    return assets


def run_install(tmp_path, release, *args):
    return subprocess.run(
        ["sh", str(ROOT / "install.sh"), *args],
        env=dict(os.environ, HOME=str(tmp_path / "home"),
                 MUNINN_ASSET_DIR=str(release), MUNINN_VERSION="",
                 MUNINN_INSTALL_DIR=str(tmp_path / "bin")),
        capture_output=True, text=True, timeout=60, check=False,
    )


def test_core_only_is_explicit_and_preserves_memory(tmp_path, release):
    result = run_install(tmp_path, release, "--core-only")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "bin/muninn").is_file()
    assert not (tmp_path / "home").exists()


def test_default_offline_install_requires_parsers_before_replacement(tmp_path, release):
    destination = tmp_path / "bin"
    destination.mkdir()
    executable = destination / "muninn"
    executable.write_bytes(next(release.glob("*.pyz")).read_bytes())
    before = executable.read_bytes()
    result = run_install(tmp_path, release)
    assert result.returncode != 0
    assert "parser" in result.stderr.lower()
    assert executable.read_bytes() == before


def test_core_parser_diagnostic_reports_missing_without_scanning(tmp_path, release):
    archive = next(release.glob("*.pyz"))
    result = subprocess.run([sys.executable, "-I", "-S", str(archive),
                             "doctor", "--parsers"], cwd=tmp_path,
                            capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 1
    assert "parsers: unavailable" in result.stdout
    assert not (tmp_path / ".muninn").exists()


def test_parser_health_checks_parsing_not_only_imports(monkeypatch, capsys):
    monkeypatch.setattr(extract, "available", lambda: True)
    samples = []

    def parser(language):
        def parse(sample):
            samples.append(language)
            return SimpleNamespace(root_node=SimpleNamespace(has_error=False, end_byte=len(sample)))
        return SimpleNamespace(parse=parse)

    monkeypatch.setattr(extract, "_parser", parser)
    assert extract.parser_issues() == []
    assert {"python", "typescript", "yaml", "csharp", "embeddedtemplate"} <= set(samples)
    cli.main(["doctor", "--parsers"])
    assert "parsers: ready" in capsys.readouterr().out
    monkeypatch.setattr(extract, "_parser", lambda language: SimpleNamespace(
        parse=lambda sample: SimpleNamespace(root_node=SimpleNamespace(has_error=True, end_byte=0))))
    assert len(extract.parser_issues()) == 8

    def broken(language):
        raise ValueError("Incompatible grammar")

    monkeypatch.setattr(extract, "_parser", broken)
    with pytest.raises(SystemExit) as error:
        cli.main(["doctor", "--parsers"])
    assert error.value.code == 1
    assert "grammar could not load" in capsys.readouterr().out
    monkeypatch.setattr(extract, "available", lambda: False)
    assert extract.parser_issues() == ["The tree-sitter packages cannot be imported."]


@pytest.mark.skipif(not extract.available(), reason="Optional parsers are absent in the core profile")
def test_real_parser_health_and_rendered_mapping_command(tmp_path):
    import shlex
    assert extract.parser_issues() == []
    source = tmp_path / "source files"
    source.mkdir()
    (source / "app.py").write_text("def map_customer():\n    return 42\n")
    brain = tmp_path / "memory files"
    protocol = skill.render(str(brain))
    command = next(line.strip() for line in protocol.splitlines() if " extract <code-path> " in line)
    arguments = shlex.split(command.replace("<code-path>", shlex.quote(str(source))))
    cli.main(arguments[1:])
    assert any("map_customer" in path.read_text() for path in brain.rglob("*.md"))


def installer_functions():
    source = (ROOT / "install.sh").read_text().split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    tree = ast.parse(source)
    tree.body = [node for node in tree.body if isinstance(node, (
        ast.Import, ast.ImportFrom, ast.Assign, ast.FunctionDef))]
    namespace = {}
    exec(compile(tree, str(ROOT / "install.sh"), "exec"), namespace)  # noqa: S102
    return namespace


@pytest.mark.parametrize("failure", ["venv", "pip", "versions", "syntax", None])
def test_parser_provisioning_isolated_pinned_and_failure_atomic(tmp_path, release, monkeypatch, failure):
    namespace = installer_functions()
    data = next(release.glob("*.pyz")).read_bytes()
    (release / "fake.whl").write_bytes(b"not executed")
    destination = tmp_path / "install"
    destination.mkdir()
    calls = []
    monkeypatch.setenv("PIP_TARGET", str(tmp_path / "unrelated"))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "untrusted"))

    def run(command, **kwargs):
        calls.append(command)
        assert "PIP_TARGET" not in kwargs["env"]
        assert "PYTHONPATH" not in kwargs["env"]
        assert kwargs["env"]["PIP_CONFIG_FILE"] == os.devnull
        stage = "venv" if "venv" in command else "pip" if "pip" in command else "versions" if "-c" in command else "syntax"
        if stage == "pip":
            assert {"--isolated", "--only-binary=:all:", "--require-hashes", "--no-deps", "--no-index"} <= set(command)
            assert "--index-url" not in command
        if stage == failure:
            if kwargs["check"]:
                raise subprocess.CalledProcessError(1, command)
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    if failure:
        with pytest.raises(ValueError, match="No executable was replaced"):
            namespace["parser_runtime"](data, destination, release, None)
        assert list(destination.iterdir()) == []
    else:
        installed, runtime = namespace["parser_runtime"](data, destination, release, None)
        assert installed.endswith(data)
        assert "bin/python3" in installed[:500].decode(errors="ignore")
        assert runtime.parent == destination
        assert len(calls) == 4


def test_setup_refreshes_coding_scope_across_supported_agents(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    brain = home / "memory"
    canonical = home / "AGENTS.md"
    canonical.write_text("Preserve user instructions.\n" + cli.PROTOCOL_BEGIN + "\n"
                         + cli.PROTOCOL_HEADING + "\nOld protocol.\n" + cli.PROTOCOL_END + "\n")
    cli.main(["setup", "--brain", str(brain)])
    text = canonical.read_text()
    assert text.startswith("Preserve user instructions.")
    assert "Old protocol." not in text
    assert "For ordinary conversation, writing, or non-coding research, skip code mapping and source indexing." in text
    for slot in cli.GLOBAL_SLOTS:
        assert Path(os.path.expanduser(slot)).read_text() == text
    assert (home / ".claude/skills/muninn/SKILL.md").read_text() == skill.render(str(brain))


@pytest.mark.skipif(not extract.available(), reason="Optional parsers are absent in the core profile")
@pytest.mark.parametrize("home_bundle", [False, True])
def test_saved_code_maps_do_not_overwrite_other_projects_or_files(tmp_path, home_bundle):
    brain = tmp_path / "brain"
    if home_bundle:
        home.init_home(str(brain))
    alpha = tmp_path / "alpha" / "src"
    beta = tmp_path / "beta" / "src"
    for folder in (alpha, beta):
        folder.mkdir(parents=True)
    (alpha / "shipping.py").write_text('"""Alpha shipping policy."""\ndef quote():\n    return 10\n')
    (beta / "shipping.py").write_text('"""Beta shipping policy."""\ndef quote():\n    return 20\n')
    cli.main(["extract", str(alpha), "--into", str(brain)])
    before = {path: path.read_text() for path in brain.rglob("*.md") if "extracted" in path.parts}
    assert before
    cli.main(["extract", str(beta), "--into", str(brain)])
    assert all(path.read_text() == body for path, body in before.items())
    (alpha / "returns.py").write_text('"""Alpha returns policy."""\ndef quote():\n    return 30\n')
    cli.main(["extract", str(alpha), "--into", str(brain), "--only", "py"])
    snapshot = {path: path.read_text() for path in brain.rglob("*.md") if "extracted" in path.parts}
    cli.main(["extract", str(alpha), "--into", str(brain), "--exclude", "returns.py"])
    assert all(path.read_text() == body for path, body in snapshot.items())
    assert any("Beta shipping policy" in body for body in snapshot.values())
    assert not any(str(tmp_path) in body for body in snapshot.values())


@pytest.mark.skipif(not extract.available(), reason="Optional parsers are absent in the core profile")
@pytest.mark.parametrize("command", ["extract", "build"])
def test_git_root_subfolder_and_file_maps_have_stable_paths(tmp_path, monkeypatch, command):
    monkeypatch.setattr(cli, "_graphify_layer", lambda *args: None)
    project = tmp_path / "project"
    source = project / "src"
    source.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    app = source / "app.py"
    app.write_text('"""Project behavior."""\ndef process_order():\n    return 42\n')
    brain = tmp_path / "brain"

    def run(path):
        arguments = ["--root", str(brain), command, str(path)]
        if command == "extract":
            arguments += ["--into", str(brain)]
        cli.main(arguments)

    run(project)
    before = {path: path.read_bytes() for path in brain.rglob("*.md") if "extracted" in path.parts}
    assert len(before) == 2
    assert all(b"resource: src/app.py" in body for body in before.values())
    run(source)
    after = {path: path.read_bytes() for path in brain.rglob("*.md") if "extracted" in path.parts}
    assert before == after
    if command == "extract":
        run(app)
        assert before == {path: path.read_bytes() for path in brain.rglob("*.md") if "extracted" in path.parts}


@pytest.mark.skipif(not extract.available(), reason="Optional parsers are absent in the core profile")
@pytest.mark.parametrize("home_bundle", [False, True])
def test_build_preserves_other_project_maps(tmp_path, monkeypatch, home_bundle):
    monkeypatch.setattr(cli, "_graphify_layer", lambda *args: None)
    brain = tmp_path / "brain"
    if home_bundle:
        home.init_home(str(brain))
    for name in ("alpha", "beta"):
        source = tmp_path / name
        source.mkdir()
        (source / "app.py").write_text(f'"""{name} behavior."""\ndef handle():\n    return 42\n')
        cli.main(["--root", str(brain), "build", str(source)])
        if name == "alpha":
            before = {path: path.read_bytes() for path in brain.rglob("*.md") if "extracted" in path.parts}
    assert before
    assert all(path.read_bytes() == body for path, body in before.items())
    assert any("beta behavior" in path.read_text() for path in brain.rglob("*.md"))


@pytest.mark.skipif(not extract.available(), reason="Optional parsers are absent in the core profile")
def test_colocated_map_excludes_generated_notes(tmp_path):
    (tmp_path / "app.py").write_text("def work():\n    return 42\n")
    arguments = ["extract", str(tmp_path), "--into", str(tmp_path)]
    cli.main(arguments)
    before = {path: path.read_bytes() for path in (tmp_path / "extracted").rglob("*.md")}
    cli.main(arguments)
    assert before == {path: path.read_bytes() for path in (tmp_path / "extracted").rglob("*.md")}
