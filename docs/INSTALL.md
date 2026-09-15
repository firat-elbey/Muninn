# Install and configure Muninn

An agent can install Muninn and configure its memory protocol from this guide.
Installing the executable alone does not create `AGENTS.md` or hooks.
The separate `muninn setup` command creates those files within its documented scope.

The standalone installer installs Muninn without pip. Python 3.10 or later
must already be available as `python3` on a POSIX system. Online installation
also requires curl and access to GitHub releases.

## Instructions for a coding agent

Follow this procedure only for a user-authorized installation or setup request.
Reading or contributing to this repository does not authorize changes to the user's home.
If the user requests only the executable, stop after installation.

1. Read the repository's [AGENTS.md](../AGENTS.md) and this guide.
   The repository's `CLAUDE.md` and `GEMINI.md` already link to `AGENTS.md`.
   If the client does not discover these files, read them explicitly.
2. Check `command -v muninn` and the selected executable's `--help` output.
   Reuse the intended installation if it supports the required commands.
   Do not upgrade or replace another installation without authorization.
3. Find the existing knowledge-base root in the user's adopted instructions or `MUNINN_HOME`.
   Reuse that root and its canonical instruction file.
   If existing roots conflict, ask the user which root to use.
4. If no executable exists, use [the reviewed checkout](#build-or-install-offline) or [a published release](#install-a-release).
   Do not claim that pending release assets are available.
5. For an authorized setup request, complete [the configuration procedure](#configure-an-agent).
   Report the executable, root, changed paths, checks, conflicts, and remaining user actions.

Do not import transcripts, adopt a style repository, migrate memory, or install optional dependencies without separate authorization.
Do not copy private instructions or knowledge into this repository.

## Install a release

Download the version 0.1.0 installer:

```bash
muninn_installer_dir=$(mktemp -d)
curl --fail --show-error --location --proto '=https' --proto-redir '=https' \
  https://github.com/firat-elbey/muninn/releases/download/v0.1.0/install.sh \
  --output "$muninn_installer_dir/install.sh"
```

Read the downloaded script. Then run the installer and demonstration:

```bash
sh "$muninn_installer_dir/install.sh"
"$HOME/.local/bin/muninn" demo
```

The demonstration shows changes to retrieval in a temporary knowledge base.
It does not change existing notes. The installer does not run `setup`, edit
shell profiles, or install a background service.

The installer downloads `muninn-0.1.0.pyz` and
`muninn-0.1.0.pyz.sha256`. It checks the checksum and archive version before
it installs the executable. Both assets come from the same HTTPS release.
The checksum detects corruption, not a compromised publisher. The archive
contains the core source and license notices, not a Python runtime.

## Configure an agent

Setup uses `~/muninn` by default, or `MUNINN_HOME` when set.
Use `--brain` to select an existing root explicitly.
Use `--from` to select an existing canonical instruction file instead of `~/AGENTS.md`.
An invalid or unreadable canonical path fails before setup creates configuration.
An existing home pointer must select the requested root. A conflicting pointer
stops setup before it creates configuration. Doctor also checks managed command
roots, not only the presence of protocol markers.
Keep these selections consistent for setup and diagnostics.

### Inspect existing configuration

Before setup, inspect the intended paths without exposing private content:

| Path | Setup behavior |
|---|---|
| Selected knowledge-base root | Setup creates missing home directories and preserves existing notes. |
| `~/AGENTS.md`, or `--from` | A new file receives the protocol. An existing file receives a pointer or a managed-block refresh. |
| `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, `~/.gemini/GEMINI.md`, `~/.grok/AGENTS.md` | Missing files link to the canonical file. Existing regular files and divergent links remain unchanged. |
| `~/.claude/settings.json`, `~/.codex/hooks.json`, `~/.grok/hooks/muninn.json` | Setup merges three lifecycle hooks and keeps an exact backup of existing configuration once. |
| `~/.claude/skills/muninn/SKILL.md` | Setup writes this file, including replacement of any existing content. |

The hook events are `SessionStart`, `PostToolUse`, and `SessionEnd`.
Setup targets all supported clients, not only the client that runs it.

If the skill contains user changes, obtain approval for a preservation plan before setup.
If instructions have damaged markers, stop before setup and report the conflict.
If existing agent files differ from the canonical file, preserve them and request a reviewed merge.
Do not delete user files to make diagnostics pass.

Setup targets the fixed home paths listed here.
It does not detect custom client directories or higher-priority instruction overrides.
For example, Codex can use `CODEX_HOME` or `AGENTS.override.md` instead of the default instruction file.
If such configuration changes discovery, report the limitation before changing unrelated files.
The [Codex instruction guide](https://learn.chatgpt.com/docs/agent-configuration/agents-md) defines that precedence.

### Select the executable and apply setup

Before agent setup, make the selected installation available as `muninn` in
the agent's `PATH`. For the default destination in the current terminal, use:

```bash
export PATH="$HOME/.local/bin:$PATH"
command -v muninn
```

The output must identify the selected executable. Restart each agent with
that environment and repeat the check there. Desktop agents do not necessarily
inherit the terminal environment. Agent instructions use the bare `muninn`
command, even though standalone setup binds hooks to the running archive.

For a new installation with default paths, select these values:

```bash
muninn_bin="$HOME/.local/bin/muninn"
muninn_brain="$HOME/muninn"
muninn_instructions="$HOME/AGENTS.md"
```

For an existing installation, replace these values with its selected paths.
Inspect the planned changes:

```bash
"$muninn_bin" setup --brain "$muninn_brain" --from "$muninn_instructions" --dry-run
```

The dry run reports paths without changing them. It does not detect every content conflict.
If the proposed changes exceed the authorized scope, stop and ask the user.
Within the authorized scope, apply setup:

```bash
"$muninn_bin" setup --brain "$muninn_brain" --from "$muninn_instructions"
"$muninn_bin" doctor --home --brain "$muninn_brain" --from "$muninn_instructions"
"$muninn_bin" --root "$muninn_brain" prime
```

Successful diagnostics report `home configuration: valid`.
Setup exits with status 1 when protocol markers, instruction paths, or hook configuration remain unresolved.
It preserves conflicting files and reports incomplete configuration.
Damaged or empty managed protocol blocks also cause home diagnostics to fail.
If diagnostics fail, inspect the reported paths and preserve unresolved conflicts.
The setup completion message alone does not establish success.

### Check actual agent use

Diagnostics check files, not the agent's hook trust or instruction discovery.
Check that the current agent reads the protocol and can run `muninn` through its own command environment.
Read the `prime` output in the current session.
An empty knowledge base cannot supply prior personal knowledge.

For Codex, ask the user to review new or changed hooks through `/hooks`.
Do not bypass hook trust. The [Codex hook guide](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks) defines this approval.
For Grok, run `prime` explicitly because its startup-hook output does not enter model context.
Gemini receives instructions, but setup does not install Gemini lifecycle hooks.
For any client without a supplied context pack, run `prime` explicitly.
Restart clients that load instructions only at session start.

Report disk configuration and actual agent use separately.
List any remaining PATH, discovery, restart, or hook-approval requirement.
The [adapter guide](ADAPTERS.md) describes the lifecycle integration.

## Remove agent integration

Run `muninn uninstall` with the same `--brain` and `--from` selections used for setup.
It removes Muninn hooks, owned client symlinks, and the reserved Muninn skill directory.
It preserves memory, backups, regular client files, and the canonical instruction file.
The executable also remains installed.

The canonical file can still contain Muninn protocol, pointer, or style blocks.
Review and remove those instructions separately if you want to stop their use.
Preserve unrelated prose and existing memory.

## Choose a destination or version

The default destination is `~/.local/bin/muninn`. To use a different
directory, set `MUNINN_INSTALL_DIR`:

```bash
MUNINN_INSTALL_DIR="$HOME/tools/muninn/bin" \
  sh "$muninn_installer_dir/install.sh"
"$HOME/tools/muninn/bin/muninn" demo
```

`MUNINN_VERSION` selects a published version without the leading `v`.
The installer defaults to `0.1.0`, not an unpinned latest release.

The installer can replace a recognized standalone Muninn archive. It refuses
symbolic links, directories, and unrelated executables. If an existing pipx,
editable, or custom installation occupies the destination, choose a separate
directory. Do not overwrite the existing installation to resolve a PATH
conflict. Use the full path for manual commands. Before agent setup, place
the selected directory first in that agent's `PATH`.

Use an installation directory that you control. Do not modify its executable
while the installer runs. Upgrades recheck the existing content before
replacement, but do not isolate the destination from another local writer.

## Build or install offline

From a reviewed checkout, build the versioned archive and checksum:

```bash
muninn_build_dir=$(mktemp -d)
python3 tools/build_zipapp.py --output-dir "$muninn_build_dir"
python3 "$muninn_build_dir/muninn-0.1.0.pyz" demo
```

For offline installation, place both release assets in one trusted directory.
Point `MUNINN_ASSET_DIR` at that directory:

```bash
MUNINN_ASSET_DIR="$muninn_build_dir" \
MUNINN_INSTALL_DIR="$HOME/.local/bin" \
  sh install.sh
"$HOME/.local/bin/muninn" demo
```

The executable remains in the installation directory after the build directory is removed.
Continue with [agent configuration](#configure-an-agent) only when the user authorizes setup.

Offline installation performs the same checksum and version checks. A failed
download or checksum leaves the existing executable unchanged. After an
error, read its diagnostic and obtain a fresh copy from the trusted release.

## Optional dependencies

The archive includes core memory, journals, recall, setup, and source search.
It does not bundle tree-sitter, Graphify, or model providers. Optional syntax
extraction requires additional dependencies in the selected Python environment.
The [contribution guide](../CONTRIBUTING.md) describes package-based development.
Core installation and use do not require PyPI publication.
