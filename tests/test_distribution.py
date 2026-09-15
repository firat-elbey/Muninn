"""Keep package metadata and included legal text consistent with the repository."""

from __future__ import annotations

import importlib.util
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_distribution_module():
    spec = importlib.util.spec_from_file_location(
        "check_distribution", ROOT / "tools/check_distribution.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def metadata(audit, license_expression="MIT"):
    return (
        "Metadata-Version: 2.4\nName: muninn-kb\nVersion: 0.1.0\n"
        f"License-Expression: {license_expression}\nRequires-Python: >=3.10\n"
        + "".join(f"License-File: {name}\n" for name in sorted(audit.LEGAL_FILES))
        + "\n"
    ).encode()


def distribution(tmp_path, audit, kind, changed_license=None):
    legal = {name: (ROOT / name).read_bytes() for name in audit.LEGAL_FILES}
    if changed_license:
        legal[changed_license] = b"Changed legal text.\n"
    if kind == "wheel":
        path = tmp_path / "muninn_kb-0.1.0-py3-none-any.whl"
        prefix = "muninn_kb-0.1.0.dist-info/"
        members = {name: b"" for name in audit.source_modules()}
        members.update({prefix + "licenses/" + name: data for name, data in legal.items()})
        members[prefix + "METADATA"] = metadata(audit)
        members[prefix + "WHEEL"] = b"Root-Is-Purelib: true\nTag: py3-none-any\n"
        members[prefix + "entry_points.txt"] = b"[console_scripts]\nmuninn = muninn.cli:main\n"
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in members.items():
                archive.writestr(name, data)
    else:
        path = tmp_path / "muninn_kb-0.1.0.tar.gz"
        members = {"src/" + name: b"" for name in audit.source_modules()}
        members.update(legal)
        members.update({"README.md": b"# Muninn\n", "pyproject.toml": b"",
                        "PKG-INFO": metadata(audit)})
        with tarfile.open(path, "w:gz") as archive:
            for name, data in members.items():
                info = tarfile.TarInfo("muninn_kb-0.1.0/" + name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    return path


def test_metadata_accepts_mit_and_rejects_stale_apache():
    audit = load_distribution_module()
    audit.parse_metadata(metadata(audit))
    with pytest.raises(audit.DistributionError, match="License-Expression"):
        audit.parse_metadata(metadata(audit, "Apache-2.0"))


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
def test_distribution_preserves_all_legal_files(tmp_path, kind):
    audit = load_distribution_module()
    path = distribution(tmp_path, audit, kind)

    getattr(audit, "check_" + kind)(path)


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
@pytest.mark.parametrize("name", ["LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md",
                                 "LICENSES/LongMemEval-MIT.txt"])
def test_distribution_rejects_changed_legal_file(tmp_path, kind, name):
    audit = load_distribution_module()
    path = distribution(tmp_path, audit, kind, changed_license=name)

    with pytest.raises(audit.DistributionError, match="legal file differs"):
        getattr(audit, "check_" + kind)(path)
