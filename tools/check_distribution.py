#!/usr/bin/env python3
"""Verify the contents and metadata of built Muninn distributions."""

from __future__ import annotations

import argparse
import email.policy
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
PROJECT = "muninn-kb"
VERSION = "0.2.0"
LEGAL_FILES = {
    "LICENSE",
    "NOTICE",
    "THIRD_PARTY_NOTICES.md",
    "LICENSES/LongMemEval-MIT.txt",
}
PRIVATE_NAMES = {".coverage", ".env", "ledger.jsonl", "state.json"}
PRIVATE_SUFFIXES = {".key", ".p12", ".pem", ".pfx"}


class DistributionError(RuntimeError):
    """A built distribution violates the public package contract."""


def validate_names(names: list[str]) -> None:
    """Reject duplicate, unsafe, private, and unlicensed archive paths."""
    if len(names) != len(set(names)):
        raise DistributionError("the archive contains a duplicate path")
    for raw in names:
        path = PurePosixPath(raw)
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise DistributionError(f"the archive contains an unsafe path: {raw}")
        if "\\" in raw:
            raise DistributionError(f"the archive contains a backslash path: {raw}")
        if ".muninn" in path.parts or path.name in PRIVATE_NAMES:
            raise DistributionError(f"the archive contains private state: {raw}")
        if path.suffix.lower() in PRIVATE_SUFFIXES:
            raise DistributionError(f"the archive contains a credential file: {raw}")
        if "RESULTS-dependeval-series5-confirmation-" in path.name:
            raise DistributionError(f"the archive contains unlicensed results: {raw}")


def source_modules() -> set[str]:
    return {
        f"muninn/{path.name}"
        for path in (ROOT / "src" / "muninn").glob("*.py")
    }


def parse_metadata(data: bytes) -> None:
    metadata = BytesParser(policy=email.policy.default).parsebytes(data)
    expected = {
        "Name": PROJECT,
        "Version": VERSION,
        "License-Expression": "MIT",
        "Requires-Python": ">=3.10",
    }
    for field, value in expected.items():
        if metadata.get(field) != value:
            raise DistributionError(
                f"metadata field {field} is {metadata.get(field)!r}, not {value!r}"
            )
    packaged_licenses = set(metadata.get_all("License-File", []))
    if packaged_licenses != LEGAL_FILES:
        raise DistributionError(
            f"metadata license files differ from the required set: {packaged_licenses}"
        )


def check_wheel(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        validate_names(names)
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise DistributionError("the wheel must contain one METADATA file")
        parse_metadata(archive.read(metadata_names[0]))
        prefix = metadata_names[0].removesuffix("METADATA")
        required = {prefix + "licenses/" + name for name in LEGAL_FILES}
        missing = required - set(names)
        if missing:
            raise DistributionError(f"the wheel omits legal files: {sorted(missing)}")
        for name in sorted(LEGAL_FILES):
            if archive.read(prefix + "licenses/" + name) != (ROOT / name).read_bytes():
                raise DistributionError(f"the wheel legal file differs from the repository: {name}")
        missing_modules = source_modules() - set(names)
        if missing_modules:
            raise DistributionError(f"the wheel omits modules: {sorted(missing_modules)}")
        wheel = archive.read(prefix + "WHEEL").decode("utf-8")
        if "Root-Is-Purelib: true" not in wheel or "Tag: py3-none-any" not in wheel:
            raise DistributionError("the wheel is not a universal pure-Python wheel")
        entry_points = archive.read(prefix + "entry_points.txt").decode("utf-8")
        if "muninn = muninn.cli:main" not in entry_points:
            raise DistributionError("the wheel does not expose the muninn command")


def check_sdist(path: Path) -> None:
    with tarfile.open(path, mode="r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        validate_names(names)
        if any(member.issym() or member.islnk() or member.isdev() for member in members):
            raise DistributionError("the source archive contains a link or device")
        roots = {PurePosixPath(name).parts[0] for name in names}
        if len(roots) != 1:
            raise DistributionError("the source archive must have one top-level directory")
        root = next(iter(roots))
        if any(
            len(PurePosixPath(name).parts) > 1
            and PurePosixPath(name).parts[1] == "tests"
            for name in names
        ):
            raise DistributionError("the source archive contains repository-only tests")
        required = {
            f"{root}/README.md",
            f"{root}/pyproject.toml",
            f"{root}/PKG-INFO",
            *(f"{root}/{name}" for name in LEGAL_FILES),
            *(f"{root}/src/{name}" for name in source_modules()),
        }
        missing = required - set(names)
        if missing:
            raise DistributionError(f"the source archive omits files: {sorted(missing)}")
        for name in sorted(LEGAL_FILES):
            legal_file = archive.extractfile(f"{root}/{name}")
            if legal_file is None or legal_file.read() != (ROOT / name).read_bytes():
                raise DistributionError(
                    f"the source archive legal file differs from the repository: {name}"
                )
        metadata = archive.extractfile(f"{root}/PKG-INFO")
        if metadata is None:
            raise DistributionError("the source archive has no readable PKG-INFO")
        parse_metadata(metadata.read())


def check(paths: list[Path]) -> None:
    wheels = [path for path in paths if path.suffix == ".whl"]
    sdists = [path for path in paths if path.name.endswith(".tar.gz")]
    if len(wheels) != 1 or len(sdists) != 1 or len(paths) != 2:
        raise DistributionError("provide exactly one wheel and one .tar.gz source archive")
    check_wheel(wheels[0])
    check_sdist(sdists[0])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("distributions", nargs="+", type=Path)
    arguments = parser.parse_args()
    try:
        check(arguments.distributions)
    except (DistributionError, OSError, tarfile.TarError, zipfile.BadZipFile) as error:
        print(f"ERROR: {error}")
        return 1
    print("Distribution audit passed: metadata, legal files, modules, and archive paths are valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
