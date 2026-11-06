"""Prepare the npm tarball from the same source and lock as the Python release."""

import json
import shutil
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "tools"))
import build_zipapp


def main():
    version = build_zipapp.version(ROOT)
    manifest = json.loads((HERE / "package.json").read_text(encoding="utf-8"))
    if manifest["version"] != version:
        sys.exit("The npm version must match the Python project version.")
    destination = HERE / "dist"
    name = f"muninn-{version}.pyz"
    allowed = {name, name + ".sha256", "install.sh", "LICENSE"}
    if destination.is_symlink() or (destination.exists() and any(
        p.name not in allowed or not p.is_file() or p.is_symlink()
        for p in destination.iterdir()
    )):
        sys.exit("The npm dist directory contains unexpected entries. Use a clean build directory.")
    build_zipapp.build(destination)
    for asset in ("install.sh", "LICENSE"):
        shutil.copyfile(ROOT / asset, destination / asset)


if __name__ == "__main__":
    main()
