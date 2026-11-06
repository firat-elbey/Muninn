"""Verify a local npm tarball and exercise both supported installation profiles."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "tools"))
import build_zipapp


def check(tarball: Path, work: Path) -> None:
    asset = build_zipapp.build(work / "expected")
    with tarfile.open(tarball) as package:
        expected = {f"package/{path}" for path in (
            "package.json", "README.md", "bin/muninn.js", "provision.cjs",
            f"dist/{asset.name}", f"dist/{asset.name}.sha256", "dist/LICENSE", "dist/install.sh",
        )}
        members = package.getmembers()
        if (len(members) != len(expected) or {item.name for item in members} != expected
                or any(not item.isfile() for item in members)):
            raise ValueError("The npm tarball contains missing, unexpected, or non-regular files.")
        for name, source in ((asset.name, asset), (asset.name + ".sha256", asset.with_suffix(".pyz.sha256")),
                             ("install.sh", ROOT / "install.sh"), ("LICENSE", ROOT / "LICENSE")):
            if package.extractfile("package/dist/" + name).read() != source.read_bytes():
                raise ValueError(f"The packaged {name} differs from the release source.")
        for name in ("bin/muninn.js", "provision.cjs", "README.md"):
            if package.extractfile("package/" + name).read() != (HERE / name).read_bytes():
                raise ValueError(f"The packaged {name} differs from the npm source.")
        manifest = json.loads(package.extractfile("package/package.json").read())
        if (manifest != json.loads((HERE / "package.json").read_text(encoding="utf-8"))
                or manifest["name"] != "muninn-kb" or manifest["version"] != build_zipapp.version(ROOT)
                or manifest["bin"] != {"muninn": "bin/muninn.js"}):
            raise ValueError("The npm package identity differs from the release.")

    environment = {**os.environ, "HOME": str(work / "home"), "npm_config_cache": str(work / "cache")}
    for name, allow in (("core", False), ("parsers", True)):
        prefix = work / name
        subprocess.run(
            ["npm", "install", "--global", "--prefix", str(prefix), "--no-audit", "--no-fund",
             "--foreground-scripts", "--ignore-scripts=" + str(not allow).lower(), str(tarball)],
            env=environment, check=True, timeout=480,
        )
        command = str(prefix / "bin/muninn")

        def invoke(*args):
            return subprocess.run([command, *args], cwd=work, env=environment, text=True,
                                  capture_output=True, timeout=60)

        identity = invoke("--version")
        if identity.returncode or identity.stdout.strip() != "muninn " + manifest["version"]:
            raise ValueError(f"The {name} CLI identity check failed: {identity.stderr}")
        diagnostic = invoke("doctor", "--parsers")
        if allow and (diagnostic.returncode or "parsers: ready" not in diagnostic.stdout):
            raise ValueError(f"The parser installation failed: {diagnostic.stdout} {diagnostic.stderr}")
        if not allow and (diagnostic.returncode != 1 or "parsers: unavailable" not in diagnostic.stdout
                          or "provision.cjs" not in diagnostic.stderr):
            raise ValueError("The core-only installation did not report its parser setup procedure.")
        demonstration = invoke("demo")
        if demonstration.returncode:
            raise ValueError(f"The {name} demonstration failed: {demonstration.stderr}")
    if any((work / "home" / name).exists() for name in ("muninn", "AGENTS.md", ".claude", ".codex")):
        raise ValueError("Package installation or demonstration created agent configuration.")
    print(f"Verified both npm installation profiles. SHA-256 {hashlib.sha256(tarball.read_bytes()).hexdigest()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tarball", type=Path)
    with tempfile.TemporaryDirectory(prefix="muninn-npm-check-") as directory:
        check(parser.parse_args().tarball.resolve(), Path(directory))
