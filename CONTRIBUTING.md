# Contributing

[AGENTS.md](AGENTS.md) defines the module map, verification procedure,
93 percent combined statement-and-branch coverage gate, system invariants, and repository
conventions. The same requirements apply to people and coding agents.

The short version:

1. Write the failing test first; put it in the suite that owns the
   concern.
2. Run `python -m pytest -q` without optional dependencies and with the
   `extract` dependencies installed. Both configurations must pass.
3. Run `python -m pyflakes src tests tools eval` and the coverage commands in
   [AGENTS.md](AGENTS.md).
4. Run `python tools/check_repository.py`. This command validates every
   tracked file, rejects common credential and private-state files, resolves
   local documentation links, and regenerates the licensed evaluation reports
   from their committed raw results.
5. Preserve the standard-library core, fail-soft hooks, bounded adaptive
   signals, and the documented privacy boundary.
6. Open a pull request. Continuous integration enforces both dependency
   configurations and the coverage floor.

## Contributor certification

Contributions are licensed under the MIT License unless a file states a
compatible third-party license. Every commit must include the contributor's
certification under the [Developer Certificate of Origin 1.1](DCO). Add the
sign-off with:

```bash
git commit --signoff
```

The sign-off records the contributor's name and email in the permanent public
history. A GitHub no-reply address may be used when it identifies the same
account. Do not submit credentials, personal memory sidecars, private ledger
references, or material that you do not have the right to redistribute.

Report vulnerabilities through the process in [SECURITY.md](SECURITY.md).
