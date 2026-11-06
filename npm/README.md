# Muninn for npm

The `muninn-kb` package exposes the `muninn` command on macOS and Linux. It requires Node.js 18 or later and CPython 3.10 through 3.14 with `venv` and `ensurepip`. The parser installation requires PyPI access or a local directory containing the pinned wheels.

## Install a local package

From a reviewed checkout, build the tarball, then install it with an npm prefix you can write to:

```sh
npm pack ./npm
npm install --global ./muninn-kb-0.2.1.tgz
muninn --version
muninn doctor --parsers
muninn demo
```

npm installs a private parser environment under the package directory. The package contains the Python executable archive, its checksum, and the reviewed installer. The installer verifies the bundled archive and the hashes of all downloaded parser wheels. Agent configuration requires a separate `muninn setup` command.

If your npm policy disables install scripts, the command runs the bundled standard-library core and prints parser setup instructions. To enable parsers later, run:

```sh
npm explore --global muninn-kb -- node provision.cjs
muninn doctor --parsers
```

Omit `--global` for a project-local install and run `npx --no-install muninn` in that project. For offline parser installation, set `MUNINN_ASSET_DIR` to the directory containing the five compatible, hash-pinned wheels before installing or provisioning.

## Build and check a tarball

From the repository root on macOS or Linux:

```sh
npm pack ./npm
python3 npm/check.py ./muninn-kb-0.2.1.tgz
```

The build requires the npm manifest version to match the Python project version. The check compares the packaged archive with an independent source build, then installs and exercises the tarball in temporary npm prefixes with install scripts enabled and disabled. The release owner must publish the verified tarball to npm before registry installation by package name is available.
