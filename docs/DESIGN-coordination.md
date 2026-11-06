# Multi-agent coordination

- **Status:** implemented.
- **Mechanisms:** expiring intent events, an `In flight` context section, and
  append-only ledger synchronization over a Git reference.

## Problem

Agents working in separate clones need to know which files other agents plan
to change. They also need a way to exchange usage memory without putting that
memory on a working branch. A shared filesystem cannot be assumed, but each
agent can usually access the same private Git remote.

Muninn separates awareness from exclusion. An intent reports planned work but
does not lock a file. Agents use the report to coordinate when their paths
overlap.

## Intent events

An intent is an append-only ledger event keyed by branch:

```json
{"kind": "intent", "branch": "agent-a/atomic-writes",
 "paths": ["src/store.py"], "goal": "make ledger writes atomic",
 "ttl": 86400, "ts": 1770000000.0}
```

Re-declaring an intent refreshes it. `done: true` retires it. An unrefreshed
intent expires `ttl` seconds after `ts`, with a default of 86,400 seconds.
Expiry is evaluated at read time, so ledger replay remains deterministic.

The branch identifies the unit of work and the optional paths identify likely
overlap. The event never blocks a command. This avoids stale distributed locks
while preserving enough information for an agent to narrow or coordinate its
change.

## Context presentation

`prime` and the session-start hook append at most eight active intents to the
context pack. Muninn compares announced paths with uncommitted files by direct
path match and by the existing file-to-note mapping. The section labels an
overlap explicitly and distinguishes the current branch from other work.

Intent rendering sits outside the knowledge-token budget because it describes
current coordination state rather than knowledge content. The list remains
bounded, and excess intents are reported as a count.

## Ledger synchronization

`muninn sync` stores the append-only ledger in `refs/muninn/ledger`. It does not
create a commit on a working branch. Synchronization performs four operations:

1. Fetch the remote ledger reference.
2. Merge identified events and retain repeated legacy records.
3. Order identified events by logical clock and ID, preserve legacy ordering, and replay.
4. Push a commit whose parent is the fetched ledger commit.

When a concurrent push wins, Muninn fetches the new remote state and repeats
the merge within a bounded retry loop. The protocol converges without a lock or application server.

One synchronization is one convergence step. An agent that synchronizes while
another push is in progress may need a later synchronization to observe that
event. The installed protocol therefore synchronizes at session boundaries.

## Privacy boundary

The ledger may contain note paths, session identifiers, feedback, outcomes,
and intent text. Anyone who can read the remote reference can read these
events. Give the ledger its own private remote and use `muninn sync --remote memory`
after configuring a Git remote named `memory`. Use the same single URL for
fetch and push. Muninn resolves Git URL overrides and rewrite rules for a
named remote. Direct filesystem paths must be free of URL
rewrites. Network URLs require a named remote. HTTPS redirects are disabled.

For github.com, sign in to the GitHub CLI with read access to the destination.
Muninn synchronizes only when GitHub reports that the repository is private.
GitHub SSH must resolve directly to a GitHub SSH endpoint. Use standard SSH
or HTTPS when a checkout sets a custom SSH command.
An SSH alias that resolves to GitHub requires the same visibility check.
For a self-hosted SSH or HTTPS server, the operator must verify the effective
endpoint and who can read the repository. Muninn relies on that verification.
Record the verified URL in the local checkout with
`git config --local --add muninn.privateSyncUrl <exact-push-url>`.
Changing the URL requires verifying and recording the new destination.
For a local filesystem remote, control access with filesystem permissions.
Use `--pull-only` to read a ledger without publishing local events.

Agents that need separate memory do not run `muninn sync`. They can still use
intent events when they share a sidecar through another private mechanism.

## Non-goals

The protocol does not provide file locking, sub-second presence, or knowledge
merge policy. Git continues to merge knowledge files, and `supersedes` records
knowledge corrections. Intent events only report concurrent plans, and ledger
synchronization only merges append-only consumer events.
