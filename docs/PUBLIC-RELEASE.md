# Public release procedure

This procedure publishes Muninn without exposing private repository history,
pull requests, memory references, or author addresses. The public repository
starts with one reviewed commit.

This procedure applies to the initial public release only. Do not rename or
replace an existing public repository without a separate migration plan.
Check the remote identity, visibility, and branch names before any mutation.
Never push the private working history to the public repository.

Security scans and the clean-history export are pre-publication requirements.
Local integration and successful tests do not authorize publication.

## Release gates

Public source and standalone assets have separate gates:

1. Before a public source push, complete every local check in steps 1 and 2.
   Obtain explicit approval for public source publication.
2. Before a release tag or standalone assets, require successful public CI on
   the exact release commit. Every `core`, `full`, `package`, and `DCO sign-off` job must pass.
   Ask for explicit approval before the release.

If private CI is unavailable, use the local checks before the approved public
source push. Record private CI as unavailable, not as a test failure or
success. Local results do not establish that the hosted CI matrix passes.
The public source can remain available while public CI is pending.
Do not create a release tag or publish assets until public CI passes.

## Existing empty public destination

If the public replacement already exists, do not repeat the repository rename
or creation steps. First check that the destination contains no branches or
tags. Complete steps 1 and 2 before sending any source to it. Check that every
private checkout points to the renamed private repository.

After explicit publication approval, push only the reviewed one-commit export.
Do not push from the private working checkout. Then repeat the remote clone
checks in step 5. Complete public CI and safeguards in step 6 before the
release in step 7. An empty public repository does not remove the privacy
checks required for its first commit.

## 1. Check private `main`

Run every check from a clean `main` checkout.
Use current Gitleaks and Actionlint releases. Stop after any failure.

Run the core suite in a separate environment without optional extraction
dependencies:

```bash
muninn_core_dir=$(mktemp -d)
python -m venv "$muninn_core_dir/venv"
"$muninn_core_dir/venv/bin/python" -m pip install -r requirements-test.txt -e .
"$muninn_core_dir/venv/bin/python" -m pytest -q
```

Install the full test and package tools, then run the remaining checks:

```bash
python -m pip install -r requirements-test.txt -e ".[extract]" build twine
python -c 'from importlib.metadata import version; assert version("tree-sitter-language-pack").split(".", 1)[0] == "0"'
python tools/check_repository.py
python -m pyflakes src tests tools eval
python -m coverage run --branch --source=src/muninn -m pytest -q
python -m coverage report --fail-under=93
python -m build
python -m twine check dist/*
python tools/check_distribution.py dist/*
muninn_release_dir=$(mktemp -d)
python tools/build_zipapp.py --output-dir "$muninn_release_dir"
MUNINN_ASSET_DIR="$muninn_release_dir" \
MUNINN_INSTALL_DIR="$muninn_release_dir/bin" sh install.sh
"$muninn_release_dir/bin/muninn" --help
"$muninn_release_dir/bin/muninn" demo
actionlint
gitleaks git . --redact
git status --short
```

The final command must print no output. Check that `main` contains every
approved change and that the private remote contains the same revision.
Record the commit, Python versions, test counts, expected optional skips,
coverage, and tool versions. The full suite must pass the 93 percent combined
statement-and-branch coverage gate. Keep the results outside the public candidate.

## 2. Create and check the public candidate

The export tool reads only committed files from `HEAD`. It rejects a dirty
source, unsafe archive links, a non-`main` branch, and a nonprivate author
address. It creates one signed-off commit without remotes or custom
references.
The tool isolates Git routing and configuration environment variables for
every command. It sets both public author and committer identities explicitly.

```bash
python tools/prepare_public_release.py /tmp/muninn-public \
  --author-name "Firat Elbey" \
  --author-email "56897870+firat-elbey@users.noreply.github.com"

cd /tmp/muninn-public
python tools/check_repository.py
gitleaks git . --redact
git log --oneline --decorate
git for-each-ref --format='%(refname)'
git remote
```

The candidate must contain one commit on `refs/heads/main`, no other
reference, no remote, and no secret finding. Repeat the core, full, package,
and isolated-install checks from step 1 against this directory.
Check that the candidate tree matches the reviewed source tree.
If either tree changes, repeat the affected checks before publication approval.

## 3. Rename the private repository and update every private clone

Rename the existing repository before creating the public replacement. Keep
the renamed repository private because its history and
`refs/muninn/ledger` contain work records.

```bash
gh repo rename muninn-private --repo firat-elbey/muninn
git remote set-url origin https://github.com/firat-elbey/muninn-private.git
gh repo view firat-elbey/muninn-private --json nameWithOwner,visibility
```

GitHub redirects the old repository URL after a rename, but that redirect
ends when another repository takes the old name. Update every private clone,
automation, and saved URL before continuing. The reported visibility must be
`PRIVATE`.

## 4. Create the empty replacement as a private repository

Create the replacement without generated files because the reviewed
candidate already contains the README, license, and ignore rules. Keep the
replacement private until the remote copy passes the local checks in step 5.

```bash
gh repo create firat-elbey/muninn --private \
  --description "Plain-Markdown knowledge with use-adaptive recall for AI agents" \
  --disable-wiki
```

Do not initialize the repository through the GitHub interface.

## 5. Push only the candidate and check the boundary

Add the new remote from the public candidate. Do not use `git push --mirror` or `git push --all`.

```bash
cd /tmp/muninn-public
git remote add origin https://github.com/firat-elbey/muninn.git
git push --set-upstream origin main
git ls-remote origin
git ls-remote origin 'refs/muninn/*'
gh repo view firat-elbey/muninn \
  --json defaultBranchRef,isPrivate,licenseInfo
```

The remote must expose `HEAD` and `refs/heads/main`, and the custom-reference
query must print no output. For the private-first route, `isPrivate` must
remain `true`. For an existing empty public destination, `isPrivate` must be
`false` after the approved source push. Check that the remote commit matches
the reviewed candidate.

Clone the remote repository into a new directory. Repeat the repository audit,
secret scan, core and full suites, build, distribution inspection, and isolated
installation. These checks do not replace the privacy gates before publication.

## 6. Run public CI and configure safeguards

For the private-first route, obtain explicit approval before the visibility
change. After the clean clone passes every local check, change visibility:

```bash
gh repo edit firat-elbey/muninn --visibility public \
  --accept-visibility-change-consequences
```

If the destination is already public, skip the visibility command.
Run public CI against the reviewed `main` commit:

```bash
muninn_release_sha=$(git rev-parse HEAD)
gh workflow run ci.yml --repo firat-elbey/muninn --ref main
gh run list --repo firat-elbey/muninn --workflow ci.yml \
  --event workflow_dispatch --limit 5 \
  --json databaseId,headSha,status,conclusion
gh run view <run-id> --repo firat-elbey/muninn \
  --json headSha,status,conclusion,jobs
gh run watch <run-id> --repo firat-elbey/muninn --exit-status
```

Check that the selected run has `headSha` equal to `$muninn_release_sha`.
Every `core` matrix job, `full`, and `package` must complete successfully.
A pending, cancelled, skipped, or failed job does not satisfy this gate.
If CI fails, inspect its logs and correct the cause before release.
If the release commit changes, repeat the local checks and public CI.

Check that GitHub reports the repository as public and recognizes the
MIT License. Check again that only `refs/heads/main` is present.

Require pull requests and these checks on `main`:

- `core (3.10)`
- `core (3.12)`
- `core (3.14)`
- `full`
- `package`
- `DCO sign-off`

Select GitHub Actions as the source of each required check.
Require branches to be up to date before merging.
Apply the rules to administrators without a bypass.
Block force pushes and branch deletion.
Until another maintainer is available, leave the required approval count at zero.
Enable private vulnerability reporting and Dependabot.

The DCO workflow checks each proposed commit against its author's sign-off.
It uses trusted base-branch code and read-only permissions for pull requests.
It reads proposed commit objects without checking out or running proposed files.
Bot and merge commits have no exemption.

Before release, check DCO enforcement on a temporary pull request.
A commit without the author's DCO trailer must fail the required check and block the merge.
A replacement with the matching trailer must pass the same check.
Close the test pull request without merging it.
A successful manual workflow run alone does not establish pull-request enforcement.

The issue forms route suspected vulnerabilities to private reporting. Check
that the private advisory link works before accepting issues. Create labels
named `bug` and `enhancement` if GitHub did not create them automatically.

## 7. Publish the standalone release

PyPI publication is not required. The release workflow publishes the versioned
Python archive, its SHA-256 checksum, and `install.sh` as GitHub release assets.
It retains wheel and source-distribution checks for package compatibility.

After every release gate passes, obtain explicit release approval.
Create the release from the exact commit that passed public CI:

```bash
gh release create v0.1.0 --repo firat-elbey/muninn --target "$muninn_release_sha" \
  --title "Muninn 0.1.0" --generate-notes
```

The release tag must match the version in `pyproject.toml` and the installer.
The workflow checks the revision and standalone archive before upload. It
does not overwrite existing release assets.

Create the release through an authenticated user or application.
Do not create it with a workflow's `GITHUB_TOKEN`.
That token does not trigger the required `release: published` workflow.

After the workflow succeeds, use the curl procedure in the
[installation guide](INSTALL.md) with a fresh destination. Check that the
download, checksum, `muninn demo`, and `muninn setup --dry-run` succeed.
Only then remove the pending-release notices from the README and installation
guide. The repository can remain public if asset publication fails, but its
installation instructions must not claim that the release is available.
