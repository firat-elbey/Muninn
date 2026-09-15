# Integrating Muninn with an agent

Muninn supplies prior knowledge at session start and records relevant file use
through agent hooks. Explicit commands support agents without hooks and record
judgments, such as outcomes and goals, that file activity cannot establish.

## Install the agent protocol

The generated protocol tells an agent when to retrieve knowledge, search source
code, record judgments, preserve continuity, and enrich the knowledge graph.
The protocol can use a placeholder or a fixed bundle path:

```bash
muninn skill
muninn --root ~/knowledge skill

mkdir -p .claude/skills/muninn
muninn --root ~/knowledge skill > .claude/skills/muninn/SKILL.md
muninn --root ~/knowledge skill >> AGENTS.md
```

An agent should run `prime` when a session-start hook did not supply context.
It should use `pack "<question>"` for stored knowledge and `source search
"<question>"` for repository evidence before broad file searches.
The protocol also defines `outcome`, `goal`, `add --supersedes`, `journal`, `lesson`, `build`, `enrich`, and `viz`.

## Installed lifecycle hooks

The following command prints the required configuration:

```bash
muninn --root ~/knowledge hook print-config
muninn --root ~/knowledge hook print-config --adapter codex
muninn --root ~/knowledge hook print-config --adapter grok
```

The resulting configuration has this form:

```json
{
  "hooks": {
    "SessionStart": [{"hooks": [{"type": "command",
      "command": "muninn --root /path/to/knowledge hook session-start 2>/dev/null || true"}]}],
    "PostToolUse": [{"matcher": "Read|Edit|Write|read_file|search_replace|write", "hooks": [{"type": "command",
      "command": "muninn --root /path/to/knowledge hook post-tool 2>/dev/null || true"}]}],
    "SessionEnd": [{"hooks": [{"type": "command",
      "command": "muninn --root /path/to/knowledge hook session-end 2>/dev/null || true"}]}]
  }
}
```

`muninn setup` merges these hooks into the supported global locations:

- Claude Code uses `~/.claude/settings.json`.
- Codex uses `~/.codex/hooks.json`. Review new hooks through `/hooks` before relying on them.
- Grok uses `~/.grok/hooks/muninn.json`.

Each hook has one defined function:

- `session-start` writes a context pack to standard output and records the
  session identifier. Recall events receive the same identifier, which permits
  bounded co-use learning.
- `post-tool` records a `Read` as `touch` and an `Edit` or `Write` as `encode`.
  Muninn maps the path through the bundle tree and then through `resource:`
  frontmatter. It discards unmapped paths and combines repeated events for the
  same note and event type within 60 seconds.
- The `session-end` hook applies decay when due and compares served notes with notes used independently during the session.

Codex and Claude Code add successful SessionStart output to agent context.
Grok executes the hook but does not add its standard output to model context.
Muninn recognizes Grok's camel-case event envelope and treats startup as
record-only. This mode records the lifecycle but emits no pack and records no
recall. The native Grok hooks equal the Claude-compatible hooks, so Grok can
deduplicate the two sources. Grok receives the protocol through
`~/.grok/AGENTS.md`, which requires an explicit `prime` call when no context
pack was supplied.

Adapter errors do not interrupt the agent process. Hook commands always return
exit status 0 and remain silent when the bundle cannot be read.

### Privacy boundary

The adapter retains only the session identifier and file path. It accepts the
Claude and Codex snake-case field names and the Grok camel-case field names.
For Grok, `read_file` maps to a touch. `search_replace` and `write` map to an
encode. The adapter reads the tool name only to select the event type and then
discards it. It does not retain prompt text, transcript paths, working
directories, assistant messages, tool output, patches, or credentials.
Adversarial tests verify this boundary with payloads that contain decoy
sensitive fields.

## Integrate another agent or editor

`muninn observe` provides the common intake for other hooks, editors, and file
watchers. It applies the same path mapping, session handling, duplicate-event
control, and decay checks:

```bash
muninn --root ~/knowledge observe --kind touch --note src/api/server.py
echo '{"kind":"encode","note":"notes/db.md","session":"s1"}' | \
    muninn --root ~/knowledge observe --json
```

An invalid observation, unmapped path, or unreadable bundle produces no event
and returns exit status 0. A malformed command-line option still produces the
standard argument error. When an event omits a session identifier, events
within the same 30-minute window share one inferred identifier.

An integration that supports only session-start context can call `pack` or
`prime` directly:

```bash
muninn --root ~/knowledge pack "$TASK_DESCRIPTION" --budget 900
muninn --root ~/knowledge prime --session "$SESSION_ID"
```

The `--session` option permits later comparison between context served and
notes used independently.

## Apply decay

Every intake checks the time of the latest `consolidate` event. After 24 hours,
Muninn records one decay step per elapsed day, subject to a limit of three steps
per intake. A scheduled command is optional, but it permits decay on days when
no agent accesses the bundle:

```bash
17 5 * * * muninn --root "$HOME/knowledge" consolidate
```

## Select a sharing model

Agents that use the same root share both Markdown knowledge and the `.muninn/`
usage sidecar. A use recorded by one agent can therefore affect later recall
for another agent:

```bash
muninn --root /srv/team-kb hook print-config
muninn --root /srv/team-kb recall "postgres backup"
```

Agents can instead share the Markdown bundle while retaining separate usage
memory. The Python interface accepts the knowledge bundle and dynamics sidecar
separately:

```python
kb = Bundle("/srv/team-kb")
personal = Dynamics(os.path.expanduser("~/agent-a"))
recall(kb, personal, "postgres backup")
```

The command-line interface can read a shared directory through a mounted
directory inside a private root. Mounted directories are read-only through
Muninn, and the private root retains its own `.muninn/` sidecar:

```bash
mkdir -p ~/agent-a
ln -s /srv/team-kb ~/agent-a/shared
muninn --root ~/agent-a recall "postgres backup"
```

File-level `supersedes:` metadata travels with shared knowledge. A correction
recorded only with `muninn supersede` remains in the sidecar where the command
ran.

Agents on separate machines can synchronize their append-only ledgers through
the Git remote. Muninn stores the ledger at `refs/muninn/ledger`, outside normal
branches. Intent records communicate concurrent work but do not lock files:

```bash
muninn --root kb sync
muninn --root kb intent "harden restore" --paths src/restore.py
muninn --root kb sync

# Perform the work and record the result.
muninn --root kb outcome 0.9 --why "the restore checks passed"
muninn --root kb intent --done
muninn --root kb sync
```

The [coordination design](DESIGN-coordination.md) specifies this exchange.

## Record judgments explicitly

Outcomes and goals require an agent or user to state the judgment:

```bash
export MUNINN_SESSION="$(date +%s)-$$"
muninn --root ~/knowledge touch infra/db-port.md
muninn --root ~/knowledge outcome -0.9 --why "the outage used a stale port"
muninn --root ~/knowledge goal "complete the database migration" --weight 0.8
```

An outcome changes the salience of recently used notes. A goal provides a
bounded ranking signal but never excludes a retrieval candidate.

## Treat retrieved content as data

Context packs include note bodies. Text imported from websites, third-party
documents, or repositories therefore enters the agent context. `prime` also
includes selected Git metadata, such as branch names and changed paths. Treat
all untrusted text as data rather than instructions. The same rule applies to
notes marked `provenance: extracted` or `provenance: inferred`. Reserve
`provenance: curated` for content reviewed and adopted by the user.
