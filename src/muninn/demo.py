"""Demonstrate adaptive recall in an isolated temporary knowledge base."""

from __future__ import annotations

import tempfile

from .dynamics import Dynamics
from .recall import context_pack, recall
from .store import Bundle

QUERY = "restore the database"

SAMPLE_NOTES = [
    ("infra/db-port.md",
     {"type": "fact", "title": "Database port",
      "description": "the port used by PostgreSQL"},
     "PostgreSQL listens on port 5432. Every service connects to the "
     "database this way."),
    ("infra/db-port-2024.md",
     {"type": "fact", "title": "Database port (2024 migration)",
      "description": "the current PostgreSQL port",
      "supersedes": ["infra/db-port.md"]},
     "PostgreSQL now listens on port 7433 after the June migration. "
     "Port 5432 is no longer active."),
    ("policies/prod-access.md",
     {"type": "policy", "title": "Production access policy",
      "pinned": True},
     "All production access uses the SSO proxy. Direct SSH access to "
     "production hosts is prohibited."),
    ("runbooks/restore-db.md",
     {"type": "runbook", "title": "Restore database runbook",
      "description": "steps to restore from backup"},
     "1. Stop writers. 2. Fetch the newest basebackup. 3. Replay WAL "
     "up to the target time. 4. Reopen traffic."),
    ("infra/wal-g.md",
     {"type": "fact", "title": "wal-g backup options",
      "description": "required backup-fetch options"},
     "backup-fetch requires S3_ENDPOINT. Use --turbo on the high-capacity "
     "host."),
    ("notes/db-trivia.md",
     {"type": "note", "title": "Database trivia",
      "description": "assorted database facts"},
     "Postgres was called Postgres95 for two years."),
]


def _show(hits) -> None:
    for note, score, why in hits:
        print(f"    {score:5.2f}  {note.path}  ({why})")


def run_demo(root: str | None = None) -> str:
    """Run the narrated demo end to end. Returns the bundle root used."""
    root = root or tempfile.mkdtemp(prefix="muninn-demo-")
    print("Muninn demonstration: adaptive recall in an isolated bundle")
    print(f"Temporary bundle: {root}")
    print()

    print("[1/5] Create six Markdown notes with frontmatter.")
    b = Bundle(root)
    for rel, meta, body in SAMPLE_NOTES:
        b.write_note(rel, dict(meta), body)
    b.generate_index()
    b = Bundle(root)  # reload so links and supersedes resolve
    d = Dynamics(root)
    print("""\
    infra/db-port.md         records the obsolete port 5432.
    infra/db-port-2024.md    records port 7433 and supersedes the prior note.
    policies/prod-access.md  is pinned and retains a minimum recall strength.
    runbooks/restore-db.md   contains the restoration procedure.
    infra/wal-g.md           shares no query terms with the prompt below.
    notes/db-trivia.md       contains an incidental match for "database."
""")

    print(f'[2/5] Retrieve before observed use. Query: "{QUERY}"')
    print("    This condition uses lexical relevance without usage signals:")
    _show(recall(b, d, QUERY, k=5, use_dynamics=False, reactivate=False,
                 include_stale=True))
    print("""\
    The obsolete port note precedes its correction. The incidental note ranks
    on wording alone. The wal-g note is absent because it shares no query term.
""")

    print("[3/5] Record independent use, an outcome, and an active goal.")
    print("    $ export MUNINN_SESSION=drill-1")
    print("    $ muninn touch runbooks/restore-db.md")
    print("    $ muninn touch infra/wal-g.md")
    d.touch("runbooks/restore-db.md", session="drill-1")
    d.touch("infra/wal-g.md", session="drill-1")
    d.touch("runbooks/restore-db.md", session="drill-1")
    d.touch("infra/wal-g.md", session="drill-1")
    print("    $ muninn touch infra/db-port-2024.md")
    for _ in range(3):
        d.touch("infra/db-port-2024.md", session="fix-1")
    print('    $ muninn outcome -0.9 --why "restore drill failed: hit the'
          ' old port"')
    n = d.outcome(-0.9, why="restore drill failed: hit the old port")
    print(f"      The outcome increased the salience of {n} recently used notes.")
    print('    $ muninn goal "finish the port migration" --weight 0.8')
    d.goal("finish the port migration", 0.8)
    print("      The active goal provides a bounded ranking signal.")
    print()

    print(f'[4/5] Retrieve again with the same files and query: "{QUERY}"')
    _show(recall(b, d, QUERY, k=5, reactivate=False))
    print("""\
    The result changes for four documented reasons:
    - The correction supersedes the obsolete port note.
    - Independent use increases the rank of the runbook and correction.
    - Co-use links infra/wal-g.md to the runbook despite zero query overlap.
    - The active migration goal increases the correction's rank within the
      relevant set.
""")

    print("[5/5] Render the budgeted context supplied to an agent.")
    print(f'    $ muninn pack "{QUERY}" --budget 520 -k 2')
    pack = context_pack(b, d, QUERY, budget=520, k=2, reactivate=False)
    for line in pack.splitlines():
        print(f"    | {line}")
    print()

    print("Available next commands:")
    print("  muninn --root ~/knowledge init")
    print("  muninn --root ~/knowledge add ...")
    print("  muninn --root ~/knowledge build <folder>")
    print("  muninn --root ~/knowledge enrich <folder> --request")
    print("  muninn --root ~/knowledge source search <query> --path <folder>")
    print("  muninn --root ~/knowledge viz")
    print("  muninn skill")
    print(f"The temporary demonstration bundle remains at {root}.")
    return root
