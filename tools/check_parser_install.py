"""Verify a parser-complete install with synthetic source and isolated agent configuration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def verify(assets: Path, destination: Path) -> dict:
    """Run the documented installer and coding commands without using real memory."""
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("MUNINN_", "PYTHON", "PIP_", "CODEX_", "CLAUDE_"))}
    home = destination / "home"
    home.mkdir()
    personal_instructions = "# Personal instructions\n\nPreserve this synthetic user instruction.\n"
    canonical = home / "AGENTS.md"
    canonical.write_text(personal_instructions, encoding="utf-8")
    binary = destination / "bin with spaces" / "muninn"
    brain = home / "memory"
    source = destination / "source"
    source.mkdir()
    fixtures = {
        "greeting.py": "def welcome_customer(name):\n    return 'Hello ' + name\n",
        "greeting.ts": "export function welcomeCustomer(name: string): string { return name; }\n"
                       "export function acceptanceTypeScriptOnly(): number { return 42; }\n",
        "greeting.rs": "pub fn welcome_rust_customer() -> u32 { 42 }\n",
        "greeting.go": "package greeting\nfunc AcceptanceGoOnly() int { return 42 }\n",
        "greeting.cs": "public class Greeting { public int AcceptanceCSharpOnly() { return 42; } }\n",
    }
    for name, body in fixtures.items():
        (source / name).write_text(body, encoding="utf-8")
    source_before = {name: ((source / name).read_bytes(), (source / name).stat().st_mode,
                            (source / name).stat().st_mtime_ns) for name in fixtures}
    environment.update(HOME=str(home), MUNINN_ASSET_DIR=str(assets.absolute()),
                       MUNINN_INSTALL_DIR=str(binary.parent),
                       PATH=str(binary.parent) + os.pathsep + str(Path(sys.executable).parent)
                       + os.pathsep + environment.get("PATH", ""))
    commands = 0

    def run(*arguments, expected=0):
        nonlocal commands
        result = subprocess.run(arguments, cwd=destination, env=environment,
                                capture_output=True, text=True, timeout=360, check=False)
        commands += 1
        if result.returncode != expected:
            raise RuntimeError(f"Command {commands} failed: {result.stdout}\n{result.stderr}")
        return result.stdout

    run("sh", str(ROOT / "install.sh"))
    original = binary.read_bytes()
    assert "parsers: ready" in run("muninn", "doctor", "--parsers")
    run("sh", str(ROOT / "install.sh"))
    assert binary.read_bytes() == original
    assert len(list(binary.parent.glob(".muninn-runtime-*"))) == 1
    assert not (home / ".claude").exists()
    assert canonical.read_text() == personal_instructions
    run(str(binary), "setup", "--brain", str(brain), "--dry-run")
    assert not (home / ".claude").exists()
    assert canonical.read_text() == personal_instructions
    run(str(binary), "setup", "--brain", str(brain))
    assert "home configuration: valid" in run(str(binary), "doctor", "--home", "--brain", str(brain))
    protocol = (home / ".claude/skills/muninn/SKILL.md").read_text()
    assert "For ordinary conversation, writing, or non-coding research, skip code mapping and source indexing." in protocol
    pointer = canonical.read_text()
    assert pointer.startswith(personal_instructions)
    pointer_commands = [shlex.split(command) for _, command in
                        re.findall(r"(`+)(?!`)(muninn --root .*?)\1", pointer)]
    assert pointer_commands == [["muninn", "--root", str(brain), verb]
                                for verb in ("prime", "skill")]
    run(*pointer_commands[0])
    assert run(*pointer_commands[1]).strip() == protocol.strip()
    for slot in (".claude/CLAUDE.md", ".codex/AGENTS.md", ".gemini/GEMINI.md", ".grok/AGENTS.md"):
        assert (home / slot).read_text() == pointer
    # Execute the installed skill's mapping command, with its declared source placeholder resolved.
    mapping = next(line.strip() for line in protocol.splitlines() if " extract <code-path> " in line)
    arguments = shlex.split(mapping.replace("<code-path>", shlex.quote(str(source))))
    assert f"extracted {len(fixtures)} file(s)" in run(*arguments)
    graph = destination / "graph.json"
    run(str(binary), "extract", str(source), "--json", str(graph))
    nodes = json.loads(graph.read_text())["nodes"]
    assert any("welcome_customer" in json.dumps(node) for node in nodes)
    assert any("welcomeCustomer" in json.dumps(node) for node in nodes)
    assert any("welcome_rust_customer" in json.dumps(node) and node.get("lang") == "rust" for node in nodes)
    result = run(str(binary), "--root", str(brain), "source", "search", "welcome_customer",
                 "--path", str(source), "--budget", "300")
    assert "greeting.py" in result
    assert len(result) <= 1200
    default_arguments = ("muninn", "--root", str(brain), "source", "search", "welcome_customer",
                         "--path", str(source), "--budget", "300", "--json")
    default_before = json.loads(run(*default_arguments))
    assert default_before["mode"] == "hybrid"
    assert not default_before["index_rebuilt"]
    indexes = brain / ".muninn/source-indexes"
    default_databases = list(indexes.glob("*.sqlite"))
    assert len(default_databases) == 1
    default_database = default_databases[0]
    assert "-structural-" not in default_database.name
    default_bytes = default_database.read_bytes()
    default_mtime = default_database.stat().st_mtime_ns
    symbol_cases = {
        "acceptanceTypeScriptOnly": "greeting.ts",
        "AcceptanceGoOnly": "greeting.go",
        "AcceptanceCSharpOnly": "greeting.cs",
    }
    for number, (symbol, path) in enumerate(symbol_cases.items()):
        symbol_arguments = ("muninn", "--root", str(brain), "source", "search", symbol,
                            "--path", str(source), "--budget", "300", "--mode", "symbol",
                            "--no-refresh", "--json")
        symbols = json.loads(run(*symbol_arguments))
        assert symbols["mode"] == "symbol"
        assert symbols["index_rebuilt"] == (number == 0)
        assert [hit["path"] for hit in symbols["hits"]] == [path]
        hit = symbols["hits"][0]
        assert "symbol" in hit["arms"]
        assert symbol in hit["text"]
        assert hit["text"] == "\n".join(fixtures[path].splitlines()[hit["start_line"] - 1:hit["end_line"]])
        repeated = json.loads(run(*symbol_arguments))
        assert not repeated["index_rebuilt"]
        assert repeated["hits"] == symbols["hits"]
    structural_databases = list(indexes.glob("*-structural-*.sqlite"))
    assert len(structural_databases) == 1
    assert len(list(indexes.glob("*.sqlite"))) == 2
    assert structural_databases[0].read_bytes() != default_bytes
    default_after = json.loads(run(*default_arguments))
    assert default_after == default_before
    assert default_database.read_bytes() == default_bytes
    assert default_database.stat().st_mtime_ns == default_mtime
    run(str(binary), "--root", str(brain), "add", "notes/parser-check.md", "--title", "Parser acceptance",
        "--body", "The synthetic greeting code uses welcome_customer.")
    assert "welcome_customer" in run(str(binary), "--root", str(brain), "pack", "Parser acceptance", "--budget", "200")
    assert all((source / name).read_text() == body for name, body in fixtures.items())
    # Core-only is a deliberate downgrade of this executable, not deletion of memory or old runtimes.
    run("sh", str(ROOT / "install.sh"), "--core-only")
    archive = next(assets.glob("*.pyz"))
    assert binary.read_bytes() == archive.read_bytes()
    run(sys.executable, "-I", "-S", str(binary), "doctor", "--parsers", expected=1)
    assert (brain / "notes/parser-check.md").exists()
    broken = destination / "corrupt assets"
    shutil.copytree(assets, broken)
    wheel = next(broken.glob("tree_sitter-*.whl"))
    wheel.write_bytes(b"Corrupt wheel.\n")
    environment["MUNINN_ASSET_DIR"] = str(broken)
    run("sh", str(ROOT / "install.sh"), expected=1)
    assert binary.read_bytes() == archive.read_bytes()
    assert (brain / "notes/parser-check.md").exists()
    assert set(source.iterdir()) == {source / name for name in fixtures}
    for name, before in source_before.items():
        path = source / name
        assert not path.is_symlink()
        assert (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns) == before
    return {"commands": commands, "source_files": len(fixtures), "nodes": len(nodes),
            "symbol_cases": len(symbol_cases), "structural_profiles": len(structural_databases),
            "default_index_unchanged": True, "source_unchanged": True,
            "personal_protocol_reached": True,
            "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", required=True, type=Path,
                        help="Use the built archive, checksum, and pinned compatible wheels in this directory.")
    arguments = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="muninn-parser-check-") as temporary:
        print(json.dumps(verify(arguments.assets, Path(temporary)), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
