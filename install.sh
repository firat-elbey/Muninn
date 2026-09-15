#!/bin/sh
# Install the standalone release without changing agent or shell configuration.
set -eu

case "${1-}" in
    --help|-h)
        printf '%s\n' 'Install Muninn 0.1.0 without pip. Python 3.10 or later is required.' \
            'Usage: sh install.sh [--help | --version]' \
            'MUNINN_VERSION selects the release (default: 0.1.0).' \
            'MUNINN_INSTALL_DIR selects the bin directory (default: $HOME/.local/bin).' \
            'MUNINN_ASSET_DIR supplies local release assets for offline installation.' \
            'The installer refuses existing custom executables and symlinks.'
        exit 0 ;;
    --version) printf '%s\n' 'Muninn installer 0.1.0'; exit 0 ;;
    '') ;;
    *) printf '%s\n' 'ERROR: Unsupported argument. Run sh install.sh --help.' >&2; exit 1 ;;
esac
if [ "$#" -ne 0 ]; then
    printf '%s\n' 'ERROR: Installation does not accept positional arguments.' >&2
    exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' 'ERROR: Install Python 3.10 or later, then run this installer again.' >&2
    exit 1
fi

exec python3 -I -S - <<'PY'
import sys

if sys.version_info < (3, 10):
    sys.exit("ERROR: Install Python 3.10 or later, then run this installer again.")

import ast
import hashlib
import io
import json
import os
import re
import secrets
import shlex
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path

LIMIT = 16 * 1024 * 1024
MARKER = b"muninn-kb-standalone-v1"


def read_regular(path, *, directory=None, limit=LIMIT):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("The asset or existing executable is not a regular file.")
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ValueError("The asset or existing executable exceeds the size limit.")
    return data


def inspect_archive(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        required = {"__main__.py", "muninn/__init__.py", "muninn/cli.py", "LICENSE",
                    "NOTICE", "THIRD_PARTY_NOTICES.md", "LICENSES/LongMemEval-MIT.txt",
                    "muninn-standalone.json"}
        names = archive.namelist()
        if (archive.comment != MARKER or not required.issubset(names)
                or len(names) != len(set(names))
                or sum(item.file_size for item in archive.infolist()) > LIMIT):
            raise ValueError("The asset is not a recognized Muninn standalone release.")
        metadata = json.loads(archive.read("muninn-standalone.json"))
        release = metadata.get("version")
        if metadata != {"format": 1, "project": "muninn-kb", "version": release}:
            raise ValueError("The standalone release identity is invalid.")
        namespace = ast.parse(archive.read("muninn/__init__.py"))
        versions = [ast.literal_eval(node.value) for node in namespace.body
                    if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == "__version__"
                            for target in node.targets)]
        if versions != [release] or not isinstance(release, str):
            raise ValueError("The standalone package version is invalid.")
        if archive.testzip() is not None:
            raise ValueError("The standalone archive is corrupt.")
        return release


def install():
    release = os.environ.get("MUNINN_VERSION") or "0.1.0"
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+[a-z0-9.-]*", release):
        raise ValueError("MUNINN_VERSION must contain a bare release version, such as 0.1.0.")
    name = "muninn-" + release + ".pyz"
    local_assets = os.environ.get("MUNINN_ASSET_DIR")
    with tempfile.TemporaryDirectory(prefix="muninn-download-") as temporary:
        assets = Path(local_assets) if local_assets else Path(temporary)
        if not local_assets:
            base = "https://github.com/firat-elbey/muninn/releases/download/v" + release + "/"
            for asset in (name, name + ".sha256"):
                try:
                    subprocess.run(
                        ["curl", "--fail", "--silent", "--show-error", "--location",
                         "--proto", "=https", "--proto-redir", "=https", "--tlsv1.2",
                         "--connect-timeout", "15", "--max-time", "120",
                         "--max-filesize", str(1024 if asset.endswith(".sha256") else LIMIT),
                         "--output", str(assets / asset), base + asset], check=True,
                    )
                except (OSError, subprocess.CalledProcessError) as error:
                    raise ValueError("The release download failed. No executable was replaced.") from error
        data = read_regular(assets / name)
        checksum = read_regular(assets / (name + ".sha256"), limit=1024).decode("ascii")
        expected = hashlib.sha256(data).hexdigest() + "  " + name + "\n"
        if checksum != expected:
            raise ValueError("The release checksum does not match the requested asset.")
        if inspect_archive(data) != release:
            raise ValueError("The asset version does not match MUNINN_VERSION.")

    directory = Path(os.environ.get("MUNINN_INSTALL_DIR") or Path.home() / ".local/bin").expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = ".muninn-" + secrets.token_hex(12) + ".tmp"
    try:
        try:
            previous = read_regular("muninn", directory=descriptor)
        except FileNotFoundError:
            previous = None
        except OSError as error:
            raise ValueError("The existing executable is a symlink or cannot be read. It was not replaced.") from error
        if previous is not None:
            try:
                inspect_archive(previous)
            except (ValueError, SyntaxError, zipfile.BadZipFile, KeyError, TypeError, AttributeError) as error:
                raise ValueError("The existing executable is unmanaged. It was not replaced.") from error
            executable_mode = os.stat("muninn", dir_fd=descriptor, follow_symlinks=False).st_mode
            if previous == data and executable_mode & 0o777 == 0o755:
                print("Muninn " + release + " is already installed at " + str(directory / "muninn") + ".")
                print("Before agent setup, add " + shlex.quote(str(directory)) + " to the agent PATH.")
                return
        output_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o755, dir_fd=descriptor)
        with os.fdopen(output_fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fchmod(output.fileno(), 0o755)
            os.fsync(output.fileno())
        if previous is None:
            os.link(temporary, "muninn", src_dir_fd=descriptor, dst_dir_fd=descriptor)
        else:
            if read_regular("muninn", directory=descriptor) != previous:
                raise ValueError("The existing executable changed during installation. It was not replaced.")
            os.replace(temporary, "muninn", src_dir_fd=descriptor, dst_dir_fd=descriptor)
        print("Installed Muninn " + release + " at " + str(directory / "muninn") + ".")
        print("Run " + shlex.quote(str(directory / "muninn")) + " --help.")
        print("Before agent setup, add " + shlex.quote(str(directory)) + " to the agent PATH.")
    finally:
        try:
            os.unlink(temporary, dir_fd=descriptor)
        except FileNotFoundError:
            pass
        os.close(descriptor)


try:
    install()
except (OSError, ValueError, SyntaxError, zipfile.BadZipFile, KeyError, TypeError, AttributeError) as error:
    sys.exit("ERROR: " + str(error))
PY
