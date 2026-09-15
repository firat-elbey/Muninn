#!/bin/sh
# Install the standalone release without changing agent or shell configuration.
set -eu

case "${1-}" in
    --help|-h)
        printf '%s\n' 'Install Muninn 0.2.0 with parsers. Python 3.10 or later with venv is required.' \
            'Usage: sh install.sh [--core-only | --help | --version]' \
            'The default installs pinned binary parsers in an isolated environment.' \
            '--core-only installs the standard-library archive without parsers or venv.' \
            'MUNINN_VERSION selects the release (default: 0.2.0).' \
            'MUNINN_INSTALL_DIR selects the bin directory (default: $HOME/.local/bin).' \
            'MUNINN_ASSET_DIR supplies local release assets for offline installation.' \
            'The installer refuses existing custom executables and symlinks.'
        exit 0 ;;
    --version) printf '%s\n' 'Muninn installer 0.2.0'; exit 0 ;;
    ''|--core-only) ;;
    *) printf '%s\n' 'ERROR: Unsupported argument. Run sh install.sh --help.' >&2; exit 1 ;;
esac
if [ "$#" -gt 1 ]; then
    printf '%s\n' 'ERROR: Installation does not accept positional arguments.' >&2
    exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' 'ERROR: Install Python 3.10 or later, then run this installer again.' >&2
    exit 1
fi

exec python3 -I -S - "$@" <<'PY'
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
import shutil
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path

LIMIT = 16 * 1024 * 1024
MARKER = b"muninn-kb-standalone-v1"
RUNTIME_MARKER = b"#!/bin/sh\n# muninn-parser-runtime-v1\n"


def runtime_prefix(python):
    return RUNTIME_MARKER + ("exec " + shlex.quote(str(python)) + ' -I "$0" "$@"\n').encode()


def parser_runtime(data, directory, assets, previous):
    """Provision binary parsers before switching the executable; never modify system packages."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        try:
            lock = archive.read("requirements-parsers.txt")
        except KeyError as error:
            raise ValueError("This release has no parser lock. Use its original installer or --core-only.") from error
    if assets and not any(Path(assets).glob("*.whl")):
        raise ValueError("Offline parser installation requires the pinned wheels beside the archive. "
                         "Use --core-only only when parsers are not required.")
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("PYTHON", "PIP_"))}
    environment["PIP_CONFIG_FILE"] = os.devnull
    pins = dict(re.findall(r"^([a-z0-9-]+)==([0-9.]+)", lock.decode("ascii"), re.MULTILINE))

    def healthy(python, archive):
        try:
            versions = subprocess.run(
                [str(python), "-I", "-c", "from importlib.metadata import version; "
                 + "assert all(version(name) == wanted for name, wanted in " + repr(pins) + ".items())"],
                env=environment, capture_output=True, timeout=30, check=False)
            if versions.returncode != 0:
                return False
            result = subprocess.run([str(python), "-I", str(archive), "doctor", "--parsers"],
                                    env=environment, capture_output=True, timeout=30, check=False)
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    # Reuse only our canonical launcher in this destination, with matching release bytes.
    if previous and previous.startswith(RUNTIME_MARKER):
        lines = previous.split(b"\n", 3)
        try:
            command = shlex.split(lines[2].decode())
            python = Path(command[1])
            runtime = python.parent.parent
            owned = (runtime.parent == directory and not runtime.is_symlink()
                     and re.fullmatch(r"\.muninn-runtime-[a-z0-9_]+", runtime.name)
                     and python == runtime / "bin/python3")
            if (owned and previous == runtime_prefix(python) + data
                    and read_regular(runtime / "requirements-parsers.txt") == lock
                    and read_regular(runtime / "muninn.pyz") == data
                    and healthy(python, runtime / "muninn.pyz")):
                return previous, None
        except (OSError, ValueError, IndexError, UnicodeError):
            pass

    runtime = Path(tempfile.mkdtemp(prefix=".muninn-runtime-", dir=directory))
    try:
        (runtime / "requirements-parsers.txt").write_bytes(lock)
        (runtime / "muninn.pyz").write_bytes(data)
        print("Preparing the isolated parser environment.", flush=True)
        subprocess.run([sys.executable, "-I", "-m", "venv", str(runtime)],
                       env=environment, check=True, capture_output=True, timeout=120)
        python = runtime / "bin/python3"
        command = [str(python), "-I", "-m", "pip", "--isolated", "install",
                   "--disable-pip-version-check", "--no-input", "--no-cache-dir",
                   "--only-binary=:all:", "--require-hashes", "--no-deps",
                   "--retries", "1", "--timeout", "30",
                   "-r", str(runtime / "requirements-parsers.txt")]
        if assets:
            command += ["--no-index", "--find-links", str(Path(assets).absolute())]
        else:
            command += ["--index-url", "https://pypi.org/simple"]
        subprocess.run(command, env=environment, check=True, capture_output=True, timeout=300)
        if not healthy(python, runtime / "muninn.pyz"):
            raise ValueError("The installed parsers failed the syntax test. No executable was replaced.")
        return runtime_prefix(python) + data, runtime
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        shutil.rmtree(runtime)
        raise ValueError("Parser installation failed. No executable was replaced. "
                         "Check Python venv support, network access, and compatible pinned wheels. "
                         "Use --core-only only when parsers are not required.") from error


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
    release = os.environ.get("MUNINN_VERSION") or "0.2.0"
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+[a-z0-9.-]*", release):
        raise ValueError("MUNINN_VERSION must contain a bare release version, such as 0.2.0.")
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

    directory = Path(os.environ.get("MUNINN_INSTALL_DIR") or Path.home() / ".local/bin").expanduser().absolute()
    directory.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = ".muninn-" + secrets.token_hex(12) + ".tmp"
    runtime = None
    installed = False
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
        if "--core-only" not in sys.argv[1:]:
            data, runtime = parser_runtime(data, directory, local_assets, previous)
        if previous is not None:
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
        installed = True
        print("Installed Muninn " + release + " at " + str(directory / "muninn") + ".")
        print("Run " + shlex.quote(str(directory / "muninn")) + " --help.")
        print("Before agent setup, add " + shlex.quote(str(directory)) + " to the agent PATH.")
    finally:
        try:
            os.unlink(temporary, dir_fd=descriptor)
        except FileNotFoundError:
            pass
        os.close(descriptor)
        if runtime is not None and not installed:
            shutil.rmtree(runtime)


try:
    install()
except (OSError, ValueError, SyntaxError, zipfile.BadZipFile, KeyError, TypeError, AttributeError) as error:
    sys.exit("ERROR: " + str(error))
PY
