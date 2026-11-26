"""Require author certification for every commit in a complete proposed range."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "tools/check_signoffs.py"
AUTHOR = "Synthetic Author <author@example.invalid>"
SIGNED = "Add a synthetic change.\n\nSigned-off-by: " + AUTHOR + "\n"
ZERO = "0" * 40


class SignoffTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name)
        self.env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_AUTHOR_NAME="Synthetic Author",
                        GIT_AUTHOR_EMAIL="author@example.invalid",
                        GIT_COMMITTER_NAME="Synthetic Committer",
                        GIT_COMMITTER_EMAIL="committer@example.invalid")
        self.git("init", "--quiet")
        self.tree = self.git("mktree", input_text="").strip()
        spec = importlib.util.spec_from_file_location("check_signoffs", CHECKER)
        self.checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.checker)

    def git(self, *args, input_text=None):
        return subprocess.run(["git", *args], cwd=self.repo, env=self.env,
                              input=input_text, text=True, capture_output=True,
                              check=True).stdout

    def commit(self, message=SIGNED, parents=(), **identity):
        with patch.dict(self.env, identity):
            args = ["commit-tree", self.tree]
            for parent in parents:
                args.extend(["-p", parent])
            return self.git(*args, input_text=message).strip()

    def test_accepts_author_trailer_and_additional_certifiers(self):
        for message in [SIGNED, SIGNED + "Signed-off-by: Other <other@example.invalid>\n",
                        SIGNED.replace("Signed-off-by", "signed-off-by")]:
            with self.subTest(message=message):
                self.assertTrue(self.checker.has_author_signoff(self.repo, self.commit(message)))

    def test_rejects_missing_invalid_spoofed_or_body_only_signoffs(self):
        messages = ["No certification.\n", "Signed-off-by: " + AUTHOR + "\n",
                    "Subject.\n\nSigned-off-by: " + AUTHOR + "\n\nMore ordinary body text.\n",
                    "Subject.\n\n> Signed-off-by: " + AUTHOR + "\n",
                    "Subject.\n\nSigned-off-by: Other <author@example.invalid>\n",
                    "Subject.\n\nSigned-off-by: Synthetic Author <wrong@example.invalid>\n",
                    "Subject.\n\nSigned-off-by: " + AUTHOR + " extra text\n",
                    "Subject.\n\nSigned-off-by: Synthetic Author\n"]
        for message in messages:
            with self.subTest(message=message):
                self.assertFalse(self.checker.has_author_signoff(self.repo, self.commit(message)))

    def test_committer_signoff_does_not_certify_author(self):
        sha = self.commit("Change.\n\nSigned-off-by: Synthetic Committer <committer@example.invalid>\n")
        self.assertFalse(self.checker.has_author_signoff(self.repo, sha))

    def test_no_bot_or_merge_exemption(self):
        root = self.commit()
        branch = self.commit(parents=[root])
        merge = self.commit("Merge the branch.\n", parents=[root, branch])
        bot = self.commit("Automated change.\n", parents=[merge],
                          GIT_AUTHOR_NAME="synthetic[bot]",
                          GIT_AUTHOR_EMAIL="synthetic[bot]@users.noreply.github.com")
        commits = self.checker.proposed_commits(self.repo, root, bot)
        self.assertIn(merge, commits)
        self.assertEqual(self.checker.invalid_signoffs(self.repo, commits), [merge, bot])

    def test_complete_range_checks_invalid_middle_commit(self):
        root = self.commit()
        unsigned = self.commit("Missing certification.\n", parents=[root])
        head = self.commit(parents=[unsigned])
        commits = self.checker.proposed_commits(self.repo, root, head)
        self.assertEqual(commits, [unsigned, head])
        self.assertEqual(self.checker.invalid_signoffs(self.repo, commits), [unsigned])

    def test_root_range_includes_initial_commit(self):
        root = self.commit("Initial contribution without certification.\n")
        head = self.commit(parents=[root])
        commits = self.checker.proposed_commits(self.repo, "ROOT", head)
        self.assertEqual(commits, [root, head])
        self.assertEqual(self.checker.invalid_signoffs(self.repo, commits), [root])

    def test_diverged_pr_range_excludes_base_history(self):
        root = self.commit()
        base = self.commit("A base-branch change.\n", parents=[root])
        head = self.commit(parents=[root])
        self.assertEqual(self.checker.proposed_commits(self.repo, base, head), [head])

    def test_missing_shallow_unrelated_or_empty_range_fails_closed(self):
        root = self.commit()
        other = self.commit("Unrelated root.\n")
        for base, head in [(root, root), ("f" * 40, root), (root, other), (root, ZERO),
                           ("", root), ("--all", root), (root, "HEAD")]:
            with self.subTest(base=base, head=head), self.assertRaises(self.checker.SignoffError):
                self.checker.proposed_commits(self.repo, base, head)
        (self.repo / ".git/shallow").write_text(root + "\n", encoding="ascii")
        with self.assertRaisesRegex(self.checker.SignoffError, "shallow"):
            self.checker.proposed_commits(self.repo, "ROOT", root)

    def test_git_replacement_cannot_hide_unsigned_commit(self):
        unsigned = self.commit("Unsigned original.\n")
        replacement = self.commit()
        self.git("replace", unsigned, replacement)
        self.assertFalse(self.checker.has_author_signoff(self.repo, unsigned))

    def test_trailer_aliases_cannot_create_author_certification(self):
        alias = self.commit("Change.\n\nReview-Certification: " + AUTHOR + "\n")
        valid = self.commit()
        key = "trailer.Review-Certification.key"
        config = self.repo / "global-config"
        config.write_text('[trailer "Review-Certification"]\nkey = Signed-off-by\n', encoding="utf-8")
        environments = {
            "local": {},
            "global": {"GIT_CONFIG_GLOBAL": str(config)},
            "runtime": {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": key,
                        "GIT_CONFIG_VALUE_0": "Signed-off-by"},
        }
        for scope, environment in environments.items():
            with self.subTest(scope=scope):
                if scope == "local":
                    self.git("config", key, "Signed-off-by")
                try:
                    with patch.dict(os.environ, environment):
                        self.assertFalse(self.checker.has_author_signoff(self.repo, alias))
                        self.assertTrue(self.checker.has_author_signoff(self.repo, valid))
                finally:
                    if scope == "local":
                        self.git("config", "--unset-all", key)

    def test_inherited_graft_cannot_hide_unsigned_ancestry(self):
        root = self.commit()
        unsigned = self.commit("Unsigned middle.\n", parents=[root])
        head = self.commit(parents=[unsigned])
        graft = self.repo / "graft"
        graft.write_text(f"{head} {root}\n", encoding="ascii")
        with patch.dict(os.environ, {"GIT_GRAFT_FILE": str(graft)}):
            commits = self.checker.proposed_commits(self.repo, root, head)
            self.assertEqual(commits, [unsigned, head])
            self.assertEqual(self.checker.invalid_signoffs(self.repo, commits), [unsigned])

    def test_local_graft_cannot_hide_unsigned_ancestry(self):
        root = self.commit()
        unsigned = self.commit("Unsigned middle.\n", parents=[root])
        head = self.commit(parents=[unsigned])
        (self.repo / ".git/info/grafts").write_text(f"{head} {root}\n", encoding="ascii")
        commits = self.checker.proposed_commits(self.repo, root, head)
        self.assertEqual(commits, [unsigned, head])
        self.assertEqual(self.checker.invalid_signoffs(self.repo, commits), [unsigned])

    def test_inherited_repository_routing_cannot_replace_selected_repo(self):
        sha = self.commit()
        other_directory = tempfile.TemporaryDirectory()
        self.addCleanup(other_directory.cleanup)
        other = Path(other_directory.name)
        with (patch.dict(os.environ, {"GIT_DIR": str(self.repo / ".git"),
                                     "GIT_WORK_TREE": str(self.repo),
                                     "GIT_COMMON_DIR": str(self.repo / ".git")}),
              self.assertRaises(self.checker.SignoffError)):
            self.checker.proposed_commits(other, "ROOT", sha)

    def test_inherited_shallow_override_cannot_hide_incomplete_history(self):
        root = self.commit()
        (self.repo / ".git/shallow").write_text(root + "\n", encoding="ascii")
        with (patch.dict(os.environ, {"GIT_SHALLOW_FILE": os.devnull}),
              self.assertRaisesRegex(self.checker.SignoffError, "shallow")):
            self.checker.proposed_commits(self.repo, "ROOT", root)

    def payload(self, base, head, number=7):
        return {"number": number, "repository": {"full_name": "owner/repo"},
                "pull_request": {"number": number, "base": {"sha": base,
                    "repo": {"full_name": "owner/repo"}}, "head": {"sha": head,
                    "repo": {"clone_url": "https://untrusted.example.invalid/ignore.git"}}}}

    def test_event_ranges_and_fixed_pr_fetch(self):
        base, head = self.commit(), self.commit("Other signed change.\n\nSigned-off-by: " + AUTHOR)
        with patch.object(self.checker, "fetch_pull_request") as fetch:
            self.assertEqual(self.checker.event_range(self.repo, "pull_request_target",
                self.payload(base, head), "owner/repo", base), (base, head))
            fetch.assert_called_once_with(self.repo, "owner/repo", 7, head)
        push = {"repository": {"full_name": "owner/repo"}, "before": base, "after": head}
        self.assertEqual(self.checker.event_range(self.repo, "push", push, "owner/repo", head), (base, head))
        push.update(before=ZERO, created=True)
        self.assertEqual(self.checker.event_range(self.repo, "push", push, "owner/repo", head), ("ROOT", head))
        manual = {"repository": {"full_name": "owner/repo"}, "inputs": {"base": "ROOT"}}
        self.assertEqual(self.checker.event_range(self.repo, "workflow_dispatch", manual,
                                               "owner/repo", head), ("ROOT", head))

    def test_event_validation_precedes_fetch(self):
        sha = self.commit()
        for number in ["7; echo unsafe", -1, True, None]:
            with self.subTest(number=number), patch.object(self.checker, "fetch_pull_request") as fetch:
                with self.assertRaises(self.checker.SignoffError):
                    self.checker.event_range(self.repo, "pull_request_target",
                        self.payload(sha, sha, number), "owner/repo", sha)
                fetch.assert_not_called()
        for name, payload, repository in [
            ("pull_request", self.payload(sha, sha), "owner/repo"),
            ("pull_request_target", self.payload(sha, "--upload-pack=unsafe"), "owner/repo"),
            ("pull_request_target", self.payload(sha, sha), "../other/repository"),
            ("push", {"repository": {"full_name": "owner/repo"}, "after": sha}, "owner/repo"),
            ("workflow_dispatch", {"repository": {"full_name": "owner/repo"}}, "owner/repo")]:
            with self.subTest(name=name, payload=payload), self.assertRaises(self.checker.SignoffError):
                self.checker.event_range(self.repo, name, payload, repository, sha)

    def test_pr_fetch_uses_fixed_repository_ref_and_checks_observed_sha(self):
        sha = self.commit()
        with patch.object(self.checker, "git", side_effect=["", sha + "\n"]) as git:
            self.checker.fetch_pull_request(self.repo, "owner/repo", 7, sha)
        self.assertEqual(git.call_args_list[0].args[1:], (
            "fetch", "--no-tags", "--no-recurse-submodules", "--no-auto-maintenance",
            "https://github.com/owner/repo.git", "refs/pull/7/head"))
        with (patch.object(self.checker, "git", side_effect=["", "f" * 40 + "\n"]),
              self.assertRaisesRegex(self.checker.SignoffError, "changed")):
            self.checker.fetch_pull_request(self.repo, "owner/repo", 7, sha)

    def test_cli_reports_failure_without_echoing_untrusted_message(self):
        sha = self.commit("::error::Untrusted workflow annotation.\n")
        result = subprocess.run([os.sys.executable, "-I", str(CHECKER), "--repo", str(self.repo),
                                 "--base", "ROOT", "--head", sha], text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn(sha, result.stdout)
        self.assertNotIn("Untrusted workflow annotation", result.stdout + result.stderr)

    def test_event_cli_binds_manual_head_to_checked_out_revision(self):
        sha = self.commit()
        self.git("update-ref", "HEAD", sha)
        path = self.repo / "event.json"
        path.write_text(json.dumps({"repository": {"full_name": "owner/repo"},
                                   "inputs": {"base": "ROOT"}}), encoding="utf-8")
        env = dict(self.env, GITHUB_REPOSITORY="owner/repo", GITHUB_SHA=sha)
        args = [os.sys.executable, "-I", str(CHECKER), "--repo", str(self.repo),
                "--event", "workflow_dispatch", "--event-path", str(path)]
        valid = subprocess.run(args, env=env, text=True, capture_output=True, check=False)
        self.assertEqual(valid.returncode, 0, valid.stderr)
        env["GITHUB_SHA"] = "f" * 40
        invalid = subprocess.run(args, env=env, text=True, capture_output=True, check=False)
        self.assertEqual(invalid.returncode, 1)

    def test_malformed_event_fails_without_traceback(self):
        sha = self.commit()
        self.git("update-ref", "HEAD", sha)
        event_path = self.repo / "event.json"
        event_path.write_text("{", encoding="utf-8")
        env = dict(self.env, GITHUB_REPOSITORY="owner/repo", GITHUB_SHA=sha)
        result = subprocess.run([os.sys.executable, "-I", str(CHECKER), "--repo", str(self.repo),
                                 "--event", "workflow_dispatch", "--event-path", str(event_path)],
                                env=env, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)

    def _make_gpg_keypair(self, name: str, email: str) -> tuple[str, str, frozenset[str]]:
        homedir = tempfile.mkdtemp(prefix="test-gpg-")
        self.addCleanup(subprocess.run, ["gpgconf", "--homedir", homedir, "--kill", "gpg-agent"],
                        capture_output=True, check=False)
        self.addCleanup(os.chmod, homedir, 0o700)
        os.chmod(homedir, 0o700)
        spec = (
            "%no-protection\n"
            "Key-Type: EDDSA\n"
            "Key-Curve: ed25519\n"
            "Key-Usage: sign\n"
            f"Name-Real: {name}\n"
            f"Name-Email: {email}\n"
            "Expire-Date: 0\n"
            "%commit\n"
        )
        env = dict(self.env, GNUPGHOME=homedir)
        subprocess.run(
            ["gpg", "--batch", "--quiet", "--generate-key"],
            input=spec, text=True, env=env, check=True, capture_output=True,
        )
        colons = subprocess.run(
            ["gpg", "--batch", "--list-keys", "--with-colons"],
            text=True, env=env, check=True, capture_output=True,
        ).stdout
        fprs = frozenset(
            line.split(":")[9].upper()
            for line in colons.splitlines()
            if line.startswith("fpr:")
        )
        asc = subprocess.run(
            ["gpg", "--batch", "--armor", "--export"],
            text=True, env=env, check=True, capture_output=True,
        ).stdout
        return homedir, asc, fprs

    def write_signed_commit(
        self,
        gpg_homedir: str | None,
        tree: str,
        parents: list[str],
        author_name: str,
        author_email: str,
        committer_name: str,
        committer_email: str,
        message: str,
    ) -> str:
        env = dict(
            self.env,
            GIT_AUTHOR_NAME=author_name,
            GIT_AUTHOR_EMAIL=author_email,
            GIT_COMMITTER_NAME=committer_name,
            GIT_COMMITTER_EMAIL=committer_email,
        )
        args = ["commit-tree", tree]
        if gpg_homedir is not None:
            env["GNUPGHOME"] = gpg_homedir
            args.append("-S")
        for parent in parents:
            args.extend(["-p", parent])
        return subprocess.run(
            ["git", "-c", "gpg.program=gpg", *args],
            cwd=self.repo, env=env, input=message, text=True,
            capture_output=True, check=True,
        ).stdout.strip()

    def write_raw_commit(
        self,
        tree: str,
        parents: list[str],
        author: str,
        committer: str,
        message: str,
        forged_pgp: bool = True,
    ) -> str:
        lines = [f"tree {tree}"]
        for parent in parents:
            lines.append(f"parent {parent}")
        lines.append(f"author {author} 1794564929 +0000")
        lines.append(f"committer {committer} 1794564929 +0000")
        if forged_pgp:
            lines.extend([
                "gpgsig -----BEGIN PGP SIGNATURE-----",
                " ",
                " wsFcBAABCAAQBQJq9uNCCRC1aQ7uu5UhlAAA1wsQAChRZZzeth7iWJa61KtRtISH",
                " =OT1t",
                " -----END PGP SIGNATURE-----",
            ])
        raw = "\n".join(lines) + "\n\n" + message
        return self.git("hash-object", "-t", "commit", "-w", "--stdin", input_text=raw).strip()

    def write_blob_tree(self, name: str, content: str) -> str:
        blob = self.git("hash-object", "-w", "--stdin", input_text=content).strip()
        entry = f"100644 blob {blob}\t{name}\n"
        return self.git("mktree", input_text=entry).strip()

    def test_cryptographic_dependabot_and_merge_verification_blocks_forgeries(self):
        trusted_home, trusted_asc, trusted_fprs = self._make_gpg_keypair(
            "GitHub", "noreply@github.com"
        )
        attacker_home, _attacker_asc, _attacker_fprs = self._make_gpg_keypair(
            "GitHub", "noreply@github.com"
        )
        root = self.commit()
        dep_msg = (
            "Bump tree-sitter from 0.25.2 to 0.26.1\n\n"
            "Bumps tree-sitter from 0.25.2 to 0.26.1.\n\n"
            "---\n"
            "updated-dependencies:\n"
            "- dependency-name: tree-sitter\n"
            "  dependency-version: 0.26.1\n"
            "...\n\n"
            "Signed-off-by: dependabot[bot] <support@github.com>"
        )
        valid_dep = self.write_signed_commit(
            trusted_home, self.tree, [root],
            "dependabot[bot]", "49699333+dependabot[bot]@users.noreply.github.com",
            "GitHub", "noreply@github.com",
            dep_msg,
        )
        self.assertTrue(
            self.checker.has_author_signoff(
                self.repo, valid_dep,
                keyring_asc=trusted_asc, allowed_fingerprints=trusted_fprs,
            )
        )

        # Forged raw gpgsig text, attacker-signed PGP key, wrong committer, or wrong trailer MUST fail!
        forged_raw = self.write_raw_commit(
            self.tree, [root],
            "dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>",
            "GitHub <noreply@github.com>",
            dep_msg, forged_pgp=True,
        )
        attacker_signed = self.write_signed_commit(
            attacker_home, self.tree, [root],
            "dependabot[bot]", "49699333+dependabot[bot]@users.noreply.github.com",
            "GitHub", "noreply@github.com",
            dep_msg,
        )
        wrong_trailer = self.write_signed_commit(
            trusted_home, self.tree, [root],
            "dependabot[bot]", "49699333+dependabot[bot]@users.noreply.github.com",
            "GitHub", "noreply@github.com",
            "Bump pkg\n\nSigned-off-by: dependabot[bot] <wrong@example.invalid>\n",
        )
        for bad in (forged_raw, attacker_signed, wrong_trailer):
            with self.subTest(sha=bad):
                self.assertFalse(
                    self.checker.has_author_signoff(
                        self.repo, bad,
                        keyring_asc=trusted_asc, allowed_fingerprints=trusted_fprs,
                    )
                )
        # Also verify forged_raw fails against the default pinned GitHub keyring!
        self.assertFalse(self.checker.has_author_signoff(self.repo, forged_raw))

        # Clean two-parent GitHub PR merge passes; evil merge and unsigned branch merge fail.
        base_tree = self.write_blob_tree("a.txt", "base\n")
        pr_tree = self.write_blob_tree("a.txt", "updated\n")
        evil_tree = self.write_blob_tree("a.txt", "tampered\n")
        base_c = self.write_raw_commit(base_tree, [], AUTHOR, AUTHOR, SIGNED, forged_pgp=False)
        signed_branch = self.write_raw_commit(
            pr_tree, [base_c], AUTHOR, AUTHOR, SIGNED, forged_pgp=False
        )
        merge_msg = "Merge pull request #7 from owner/topic\n\nImprove behavior\n"
        clean_merge = self.write_signed_commit(
            trusted_home, pr_tree, [base_c, signed_branch],
            "Synthetic Author", "author@example.invalid",
            "GitHub", "noreply@github.com",
            merge_msg,
        )
        commits = self.checker.proposed_commits(self.repo, base_c, clean_merge)
        self.assertEqual(
            self.checker.invalid_signoffs(
                self.repo, commits,
                keyring_asc=trusted_asc, allowed_fingerprints=trusted_fprs,
            ),
            [],
        )

        evil_merge = self.write_signed_commit(
            trusted_home, evil_tree, [base_c, signed_branch],
            "Synthetic Author", "author@example.invalid",
            "GitHub", "noreply@github.com",
            merge_msg,
        )
        self.assertEqual(
            self.checker.invalid_signoffs(
                self.repo, self.checker.proposed_commits(self.repo, base_c, evil_merge),
                keyring_asc=trusted_asc, allowed_fingerprints=trusted_fprs,
            ),
            [evil_merge],
        )

        unsigned_branch = self.write_raw_commit(
            pr_tree, [base_c], AUTHOR, AUTHOR, "Unsigned work.\n", forged_pgp=False
        )
        merge_of_unsigned = self.write_signed_commit(
            trusted_home, pr_tree, [base_c, unsigned_branch],
            "Synthetic Author", "author@example.invalid",
            "GitHub", "noreply@github.com",
            merge_msg,
        )
        self.assertFalse(
            self.checker.has_author_signoff(
                self.repo, merge_of_unsigned,
                keyring_asc=trusted_asc, allowed_fingerprints=trusted_fprs,
            )
        )

    def test_github_branch_update_merge_requires_certified_sides(self):
        trusted_home, trusted_asc, trusted_fprs = self._make_gpg_keypair(
            "GitHub", "noreply@github.com"
        )
        attacker_home, _attacker_asc, _attacker_fprs = self._make_gpg_keypair(
            "GitHub", "noreply@github.com"
        )
        root_tree = self.write_blob_tree("a.txt", "root\n")
        root = self.write_raw_commit(root_tree, [], AUTHOR, AUTHOR, SIGNED, forged_pgp=False)

        a_blob = self.git("hash-object", "-w", "--stdin", input_text="root\n").strip()
        b_blob = self.git("hash-object", "-w", "--stdin", input_text="main\n").strip()
        main_tree = self.git(
            "mktree",
            input_text=f"100644 blob {a_blob}\ta.txt\n100644 blob {b_blob}\tb.txt\n",
        ).strip()
        main_tip = self.write_raw_commit(
            main_tree, [root], AUTHOR, AUTHOR, SIGNED, forged_pgp=False
        )

        pr_tree = self.write_blob_tree("a.txt", "feature\n")
        pr_tip = self.write_raw_commit(
            pr_tree, [root], AUTHOR, AUTHOR, SIGNED, forged_pgp=False
        )

        merged_tree = self.git(
            "merge-tree", "--write-tree", "--end-of-options", pr_tip, main_tip
        ).strip()
        update_msg = "Merge branch 'main' into onboarding-memory-bootstrap"
        clean_update = self.write_signed_commit(
            trusted_home, merged_tree, [pr_tip, main_tip],
            "Synthetic Author", "author@example.invalid",
            "GitHub", "noreply@github.com",
            update_msg,
        )
        commits = self.checker.proposed_commits(self.repo, main_tip, clean_update)
        self.assertEqual(
            self.checker.invalid_signoffs(
                self.repo, commits,
                keyring_asc=trusted_asc, allowed_fingerprints=trusted_fprs,
            ),
            [],
        )
        self.assertTrue(
            self.checker.has_author_signoff(
                self.repo, clean_update,
                keyring_asc=trusted_asc, allowed_fingerprints=trusted_fprs,
            )
        )

        unsigned_pr = self.write_raw_commit(
            pr_tree, [root], AUTHOR, AUTHOR, "Unsigned feature.\n", forged_pgp=False
        )
        unsigned_pr_tree = self.git(
            "merge-tree", "--write-tree", "--end-of-options", unsigned_pr, main_tip
        ).strip()
        bad_pr_update = self.write_signed_commit(
            trusted_home, unsigned_pr_tree, [unsigned_pr, main_tip],
            "Synthetic Author", "author@example.invalid",
            "GitHub", "noreply@github.com",
            update_msg,
        )
        self.assertFalse(
            self.checker.has_author_signoff(
                self.repo, bad_pr_update,
                keyring_asc=trusted_asc, allowed_fingerprints=trusted_fprs,
            )
        )

        unsigned_base = self.write_raw_commit(
            main_tree, [root], AUTHOR, AUTHOR, "Unsigned fork main.\n", forged_pgp=False
        )
        unsigned_base_tree = self.git(
            "merge-tree", "--write-tree", "--end-of-options", pr_tip, unsigned_base
        ).strip()
        bad_base_update = self.write_signed_commit(
            trusted_home, unsigned_base_tree, [pr_tip, unsigned_base],
            "Synthetic Author", "author@example.invalid",
            "GitHub", "noreply@github.com",
            update_msg,
        )
        self.assertFalse(
            self.checker.has_author_signoff(
                self.repo, bad_base_update,
                keyring_asc=trusted_asc, allowed_fingerprints=trusted_fprs,
            )
        )

        evil_tree = self.write_blob_tree("a.txt", "tampered\n")
        evil_update = self.write_signed_commit(
            trusted_home, evil_tree, [pr_tip, main_tip],
            "Synthetic Author", "author@example.invalid",
            "GitHub", "noreply@github.com",
            update_msg,
        )
        attacker_update = self.write_signed_commit(
            attacker_home, merged_tree, [pr_tip, main_tip],
            "Synthetic Author", "author@example.invalid",
            "GitHub", "noreply@github.com",
            update_msg,
        )
        unsigned_ancestor = self.write_raw_commit(
            root_tree, [], AUTHOR, AUTHOR, "Unsigned root.\n", forged_pgp=False
        )
        signed_descendant = self.write_raw_commit(
            pr_tree, [unsigned_ancestor], AUTHOR, AUTHOR, SIGNED, forged_pgp=False
        )
        ancestor_update = self.write_signed_commit(
            trusted_home, pr_tree, [unsigned_ancestor, signed_descendant],
            "Synthetic Author", "author@example.invalid",
            "GitHub", "noreply@github.com",
            update_msg,
        )
        for bad in (evil_update, attacker_update, ancestor_update):
            with self.subTest(sha=bad):
                self.assertFalse(
                    self.checker.has_author_signoff(
                        self.repo, bad,
                        keyring_asc=trusted_asc, allowed_fingerprints=trusted_fprs,
                    )
                )


if __name__ == "__main__":
    unittest.main()
