#!/usr/bin/env python3
"""Build a deterministic, standard-library Muninn executable and checksum."""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGAL_FILES = ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md", "LICENSES/LongMemEval-MIT.txt")
MARKER = b"muninn-kb-standalone-v1"
ENTRY_POINT = b'''"""Run the standalone Muninn command."""
import sys

if sys.version_info < (3, 10):
    sys.exit("Muninn requires Python 3.10 or later.")
if sys.argv[1:] == ["--version"]:
    from muninn import __version__
    print("muninn " + __version__)
else:
    from muninn.cli import main
    main()
'''


def version(root: Path) -> str:
    """Require matching static project and package versions without TOML dependencies."""
    project = re.search(r"(?ms)^\[project\]\s*\n(.*?)(?=^\[|\Z)",
                        (root / "pyproject.toml").read_text(encoding="utf-8"))
    match = re.search(r'^version = "([0-9]+\.[0-9]+\.[0-9]+[a-z0-9.-]*)"$',
                      project.group(1) if project else "", re.MULTILINE)
    if not match:
        raise ValueError("The project must declare a static release version.")
    namespace = ast.parse((root / "src/muninn/__init__.py").read_text(encoding="utf-8"))
    versions = [ast.literal_eval(node.value) for node in namespace.body
                if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "__version__"
                        for target in node.targets)]
    if versions != [match.group(1)]:
        raise ValueError("The project and package versions differ.")
    return match.group(1)


def write_asset(path: Path, data: bytes, mode: int) -> None:
    """Replace a build artifact atomically without following an existing file link."""
    descriptor, temporary = tempfile.mkstemp(prefix=".muninn-build-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            os.fchmod(output.fileno(), mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def build(output_dir: Path, *, root: Path = ROOT) -> Path:
    """Include only source modules, the entry point, release identity, and legal files."""
    release = version(root)
    members = {name: (root / name).read_bytes() for name in LEGAL_FILES}
    members["requirements-parsers.txt"] = (root / "requirements-parsers.txt").read_bytes()
    for path in sorted((root / "src/muninn").glob("*.py")):
        if path.is_symlink() or not path.is_file():
            raise ValueError("A source module is not a regular file.")
        members[f"muninn/{path.name}"] = path.read_bytes()
    members["__main__.py"] = ENTRY_POINT
    members["muninn-standalone.json"] = (json.dumps(
        {"format": 1, "project": "muninn-kb", "version": release},
        sort_keys=True, separators=(",", ":")) + "\n").encode()
    stream = io.BytesIO(b"#!/usr/bin/env python3\n")
    stream.seek(0, io.SEEK_END)
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.comment = MARKER
        for name, contents in sorted(members.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, contents)
    data = stream.getvalue()
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact = output_dir / f"muninn-{release}.pyz"
    write_asset(artifact, data, 0o755)
    checksum = f"{hashlib.sha256(data).hexdigest()}  {artifact.name}\n"
    write_asset(artifact.with_suffix(".pyz.sha256"), checksum.encode("ascii"), 0o644)
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="Write the versioned executable and checksum to this directory.")
    arguments = parser.parse_args()
    try:
        artifact = build(arguments.output_dir)
    except (OSError, ValueError) as error:
        parser.exit(1, f"ERROR: {error}\n")
    print(f"Built {artifact} and its SHA-256 checksum.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
