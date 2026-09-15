# Installation options

Use these procedures for a custom destination, an upgrade, or an offline installation.
First read [the installation guide](INSTALL.md) for prerequisites and the versioned installer download.
The variable `muninn_installer_dir` refers to that downloaded installer.

After each installation, run the demonstration with the selected executable.
It must finish without an error. For the default profile, parser diagnostics must report `parsers: ready`.

## Choose a destination or version

The default destination is `~/.local/bin/muninn`. To use a different
directory, set `MUNINN_INSTALL_DIR`:

```bash
MUNINN_INSTALL_DIR="$HOME/tools/muninn/bin" \
  sh "$muninn_installer_dir/install.sh"
"$HOME/tools/muninn/bin/muninn" demo
```

`MUNINN_VERSION` selects a published version without the leading `v`.
The installer defaults to `0.2.0`, not an unpinned latest release.

For memory without syntax parsing, install the explicit core profile:

```bash
sh "$muninn_installer_dir/install.sh" --core-only
```

The core archive still provides memory, journals, setup, and lexical source retrieval.
Syntax extraction requires the default profile.
The core option does not delete memory or previously installed parser environments.

The installer can replace a recognized standalone Muninn archive. It refuses
symbolic links, directories, and unrelated executables. If an existing pipx,
editable, or custom installation occupies the destination, choose a separate
directory. Do not overwrite the existing installation to resolve a PATH
conflict. Use the full path for manual commands. Before agent setup, place
the selected directory first in that agent's `PATH`.

The default executable uses an absolute path to its parser environment.
If you move the installation or replace its base Python, repeat the installer in the desired destination.
Successful upgrades preserve older `.muninn-runtime-*` directories for processes that still use them.
These directories contain disposable packages, not memory. Remove an old directory only after its processes stop and no executable refers to it.

Use an installation directory that you control. Do not modify its executable
while the installer runs. Upgrades recheck the existing content before
replacement, but do not isolate the destination from another local writer.

## Build or install offline

From a reviewed checkout, build the versioned archive and checksum:

```bash
muninn_build_dir=$(mktemp -d)
python3 tools/build_zipapp.py --output-dir "$muninn_build_dir"
python3 "$muninn_build_dir/muninn-0.2.0.pyz" demo
```

For offline installation, place both release assets and the five compatible pinned wheels in one trusted directory.
On a connected machine with the same Python version and platform, prepare the wheel cache:

```bash
python3 -m pip --isolated download --only-binary=:all: --require-hashes --no-deps \
  --index-url https://pypi.org/simple -r requirements-parsers.txt --dest "$muninn_build_dir"
```

This command prepares offline assets. The normal online installer does not require this step.
Point `MUNINN_ASSET_DIR` at that directory:

```bash
MUNINN_ASSET_DIR="$muninn_build_dir" \
MUNINN_INSTALL_DIR="$HOME/.local/bin" \
  sh install.sh
"$HOME/.local/bin/muninn" demo
```

The executable remains in the installation directory after the build directory is removed.
If the user authorizes setup, continue with [agent configuration](INSTALL.md#configure-an-agent).

Offline installation performs the same checksum and version checks. A failed
download or checksum leaves the existing executable unchanged. After an
error, read its diagnostic and obtain a fresh copy from the trusted release.

## Parser packages and optional integrations

The archive includes core memory, journals, recall, setup, source search, and the parser dependency lock.
The default installer adds tree-sitter 0.25.2 and tree-sitter-language-pack 0.13.0 in its isolated environment.
It also installs the pinned C#, embedded-template, and YAML grammar packages.
The Python archive alone is the core profile, not a parser-complete installation.
The installer does not include Graphify or model providers.
The [contribution guide](../CONTRIBUTING.md) describes package-based development.
Core installation and use do not require PyPI publication.
