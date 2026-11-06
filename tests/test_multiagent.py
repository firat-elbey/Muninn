"""Verify one knowledge bundle used by several consumers.

Consumers that share a sidecar rebuild stale caches from the union of ledger
events after interleaved writes. Consumers with separate sidecars can apply
different usage state to the same bundle. File-level supersession applies to
every consumer, while ledger supersession remains local to its sidecar.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from muninn.dynamics import Dynamics, sidecar_lock  # noqa: E402
from muninn.recall import recall  # noqa: E402
from muninn.store import RESERVED, SIDECAR_DIR, Bundle  # noqa: E402


class TestMultiAgent(unittest.TestCase):
    def setUp(self):
        self.top = tempfile.mkdtemp()
        self.shared = os.path.join(self.top, "shared")
        os.makedirs(self.shared)
        b = Bundle(self.shared)
        b.write_note("postgres-backup.md", {"title": "Postgres backup"},
                     "How to back up the postgres database.")
        b.write_note("mysql-backup.md", {"title": "Mysql backup"},
                     "How to back up the mysql database.")
        b.write_note("deploy.md", {"title": "Deploy service"},
                     "How to deploy the service.")

    def tearDown(self):
        shutil.rmtree(self.top)

    def _paths(self, bundle, dyn, cue, k=3, **kw):
        return [n.path for n, _s, _w in
                recall(bundle, dyn, cue, k=k, reactivate=False, **kw)]

    # -- team brain: same root, one shared sidecar -------------------------

    def test_same_root_is_one_shared_sidecar(self):
        a = Dynamics(self.shared)
        b = Dynamics(self.shared)
        self.assertEqual(a.dir, b.dir)  # root only: no per-consumer arg
        a.touch("postgres-backup.md")
        a.touch("postgres-backup.md")
        # agent B's next session loads the same sidecar: A's usage is
        # B's usage: one team memory
        b2 = Dynamics(self.shared)
        self.assertEqual(b2.entries["postgres-backup.md"]["recurrence"], 2)
        hits = recall(Bundle(self.shared), b2, "backup", k=2,
                      reactivate=False)
        self.assertEqual(hits[0][0].path, "postgres-backup.md")
        self.assertGreater(hits[0][1], hits[1][1])  # A's touches won the tie

    def test_interleaved_writers_cohere_on_next_load(self):
        a = Dynamics(self.shared)  # two LIVE agents over one sidecar
        b = Dynamics(self.shared)
        a.touch("postgres-backup.md")
        b.touch("deploy.md")  # b appends without having seen a's event
        a.touch("postgres-backup.md")
        # live instances do not tail the ledger: coherence is at load time
        self.assertNotIn("deploy.md", a.entries)
        self.assertNotIn("postgres-backup.md", b.entries)
        # each writer's state.json applied only its OWN bytes -> stale
        with open(a.state_path, encoding="utf-8") as fh:
            cached = json.load(fh)
        self.assertNotEqual(cached["applied_bytes"],
                            os.path.getsize(a.ledger_path))
        # a fresh load must detect that and rebuild the UNION from the ledger
        fresh = Dynamics(self.shared)
        self.assertEqual(fresh.entries["postgres-backup.md"]["recurrence"], 2)
        self.assertEqual(fresh.entries["deploy.md"]["recurrence"], 1)

    def test_concurrent_cache_writes_preserve_both_ledger_events(self):
        writers = [Dynamics(self.shared), Dynamics(self.shared)]
        barrier = threading.Barrier(2)
        original_dump = json.dump

        def overlapping_save(*args, **kwargs):
            original_dump(*args, **kwargs)
            barrier.wait(timeout=5)

        with (mock.patch("muninn.dynamics.json.dump", side_effect=overlapping_save),
              ThreadPoolExecutor(max_workers=2) as pool):
            futures = [pool.submit(writer.touch, f"writer-{index}.md")
                       for index, writer in enumerate(writers)]
            for future in futures:
                future.result(timeout=5)
        self.assertEqual(set(Dynamics(self.shared).entries),
                         {"writer-0.md", "writer-1.md"})

    def test_busy_write_lock_times_out_and_releases_after_error(self):
        with (self.assertRaisesRegex(RuntimeError, "interrupted"),
              sidecar_lock(self.shared)):
            with (self.assertRaisesRegex(TimeoutError, "write lock is busy"),
                  sidecar_lock(self.shared, timeout=0.01)):
                self.fail("A second writer acquired the active lock.")
            raise RuntimeError("interrupted")
        Dynamics(self.shared).touch("after-error.md")
        self.assertIn("after-error.md", Dynamics(self.shared).entries)

    def test_write_lock_refuses_a_symbolic_link_without_creating_its_target(self):
        directory = os.path.join(self.shared, ".muninn")
        os.makedirs(directory)
        external = os.path.join(self.top, "outside-lock")
        os.symlink(external, os.path.join(directory, "ledger.lock"))
        with self.assertRaises(OSError), sidecar_lock(self.shared):
            self.fail("A symbolic link was accepted as a write lock.")
        self.assertFalse(os.path.exists(external))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "named pipes are unavailable")
    def test_write_lock_refuses_a_named_pipe(self):
        directory = os.path.join(self.shared, ".muninn")
        os.makedirs(directory)
        lock = os.path.join(directory, "ledger.lock")
        os.mkfifo(lock)
        reader = os.open(lock, os.O_RDONLY | os.O_NONBLOCK)
        try:
            with (self.assertRaisesRegex(OSError, "not a regular file"),
                  sidecar_lock(self.shared)):
                self.fail("A named pipe was accepted as a write lock.")
        finally:
            os.close(reader)

    # -- per-agent memory: shared Bundle, own Dynamics root ----------------

    def test_split_roots_same_knowledge_divergent_recall(self):
        kb = Bundle(self.shared)  # ONE knowledge base
        dyn_a = Dynamics(os.path.join(self.top, "agent-a"))
        dyn_b = Dynamics(os.path.join(self.top, "agent-b"))
        self.assertNotEqual(dyn_a.dir, dyn_b.dir)
        for _ in range(3):  # divergent histories over the same notes
            dyn_a.touch("postgres-backup.md")
            dyn_b.touch("mysql-backup.md")
        top_a = self._paths(kb, dyn_a, "backup", k=1)[0]
        top_b = self._paths(kb, dyn_b, "backup", k=1)[0]
        # same cue, same knowledge, DIFFERENT top hit per agent
        self.assertEqual(top_a, "postgres-backup.md")
        self.assertEqual(top_b, "mysql-backup.md")
        self.assertNotEqual(top_a, top_b)
        # per-agent usage never grew a sidecar under the shared knowledge
        self.assertFalse(os.path.exists(
            os.path.join(self.shared, SIDECAR_DIR)))

    def test_one_root_consumers_use_per_file_symlink_roots(self):
        # the CLI wires Bundle and Dynamics to one --root; a per-agent root
        # of per-file symlinks gives it shared notes + its own .muninn/
        def link_root(name):
            root = os.path.join(self.top, name)
            os.makedirs(root)
            for fn in os.listdir(self.shared):
                if fn.endswith(".md") and fn not in RESERVED:
                    os.symlink(os.path.join(self.shared, fn),
                               os.path.join(root, fn))
            return root

        root_a, root_b = link_root("agent-a"), link_root("agent-b")
        # Bundle loads THROUGH file symlinks: full knowledge, real content
        self.assertEqual(sorted(Bundle(root_a).notes),
                         sorted(Bundle(self.shared).notes))
        self.assertIn("postgres",
                      Bundle(root_a).notes["postgres-backup.md"].body)
        # a symlinked DIRECTORY is a MOUNT: walked like any other dir
        # (how a home brain mounts a style repo as style/); a per-agent
        # root can therefore share the whole knowledge dir with one link
        nested = os.path.join(self.top, "nested")
        os.makedirs(nested)
        os.symlink(self.shared, os.path.join(nested, "notes"))
        self.assertIn("notes/postgres-backup.md", Bundle(nested).notes)
        # divergent usage through the two roots -> divergent recall
        dyn_a, dyn_b = Dynamics(root_a), Dynamics(root_b)
        for _ in range(3):
            dyn_a.touch("postgres-backup.md")
            dyn_b.touch("mysql-backup.md")
        self.assertEqual(self._paths(Bundle(root_a), dyn_a, "backup", k=1),
                         ["postgres-backup.md"])
        self.assertEqual(self._paths(Bundle(root_b), dyn_b, "backup", k=1),
                         ["mysql-backup.md"])
        self.assertNotEqual(dyn_a.dir, dyn_b.dir)

    # -- supersession across agents ----------------------------------------

    def test_file_level_supersede_travels_with_the_knowledge(self):
        dyn_b = Dynamics(os.path.join(self.top, "agent-b"))
        dyn_b.touch("postgres-backup.md")  # B relies on the stale note
        self.assertEqual(
            self._paths(Bundle(self.shared), dyn_b, "postgres backup")[0],
            "postgres-backup.md")
        # agent A authors a correction INTO the shared knowledge
        Bundle(self.shared).write_note(
            "postgres-backup-v2.md",
            {"title": "Postgres backup v2",
             "supersedes": ["postgres-backup.md"]},
            "Corrected: use pg_basebackup for the postgres database.")
        # B's ledger never saw any supersede event, yet B's next recall
        # honors the correction: it travels with the shared files
        paths = self._paths(Bundle(self.shared), dyn_b, "postgres backup")
        self.assertIn("postgres-backup-v2.md", paths)
        self.assertNotIn("postgres-backup.md", paths)

    def test_ledger_supersede_is_shared_or_personal_by_mode(self):
        # team brain (one sidecar): A's supersede event binds the team
        a = Dynamics(self.shared)
        a.supersede("deploy.md", "postgres-backup.md")
        fresh = Dynamics(self.shared)  # agent B's next session
        kb = Bundle(self.shared)
        self.assertIn("deploy.md",  # the note exists and matches ...
                      self._paths(kb, fresh, "deploy service",
                                  include_stale=True))
        self.assertNotIn("deploy.md",  # ... but A's correction demotes it
                         self._paths(kb, fresh, "deploy service"))
        # per-agent mode (own sidecars): the same event stays personal
        dyn_a = Dynamics(os.path.join(self.top, "agent-a"))
        dyn_b = Dynamics(os.path.join(self.top, "agent-b"))
        dyn_a.supersede("mysql-backup.md", "postgres-backup.md")
        self.assertNotIn("mysql-backup.md",
                         self._paths(kb, dyn_a, "mysql backup"))
        self.assertIn("mysql-backup.md",
                      self._paths(kb, dyn_b, "mysql backup"))


if __name__ == "__main__":
    unittest.main()
