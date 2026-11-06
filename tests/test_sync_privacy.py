"""Check ledger destinations before any network Git command."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from muninn import sync as ledger


class TestOpenSSHRoute(unittest.TestCase):
    def test_resolved_endpoint_and_proxy_controls(self):
        fixtures = (
            ("hostname github.com\nport 22", ("github.com", "22")),
            ("hostname ssh.github.com\nport 443", ("ssh.github.com", "443")),
            ("hostname forge.example\nport 22", ("forge.example", "22")),
            ("hostname github.com\nport 443", ("github.com", "443")),
            ("hostname github.com\nport 22\nproxycommand wrapper", None),
            ("hostname github.com\nport 22\nproxyjump forge.example", None),
        )
        for output, expected in fixtures:
            with self.subTest(output=output), mock.patch.object(
                    subprocess, "run", return_value=subprocess.CompletedProcess(
                        [], 0, stdout=output)) as run:
                self.assertEqual(ledger._ssh_route("github.com", "git", 22), expected)
                self.assertEqual(run.call_args.args[0], ["ssh", "-G", "-l", "git", "-p", "22",
                                                        "--", "github.com"])


class TestSyncDestination(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.repo = Path(scratch.name)
        env = mock.patch.dict(os.environ, {"GIT_CONFIG_NOSYSTEM": "1",
                                           "GIT_CONFIG_GLOBAL": str(self.repo / "gitconfig")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("GIT_SSH_COMMAND", None)
        os.environ.pop("GIT_SSH", None)
        route = mock.patch.object(ledger, "_ssh_route",
                                  side_effect=lambda host, user, port: (host, str(port or 22)))
        self.route = route.start()
        self.addCleanup(route.stop)
        self.git("init", "-q")

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                       capture_output=True)

    def test_public_pushurl_stops_before_fetch_or_push(self):
        self.git("remote", "add", "origin", str(self.repo / "local.git"))
        self.git("remote", "set-url", "--push", "origin",
                 "git@github.com:sample/project.git")
        transport = []
        real_git = ledger._git

        def run(repo, *args, **kwargs):
            if args[0] in ("fetch", "push"):
                transport.append(args[0])
                return 1, ""
            return real_git(repo, *args, **kwargs)

        with mock.patch.object(ledger, "_git", side_effect=run), \
                mock.patch.dict(os.environ, {"GH_HOST": "example.invalid"}), \
                mock.patch.object(ledger, "_github_visibility",
                                  return_value="public", create=True):
            with self.assertRaisesRegex(ledger.SyncError, "private"):
                ledger.sync(str(self.repo))
        self.assertEqual(transport, [])
        self.assertFalse((self.repo / ".muninn").exists())

    def test_git_rewrites_and_all_push_destinations_are_checked(self):
        self.git("config", "url.https://github.com/.pushInsteadOf", "work:")
        self.git("remote", "add", "origin", "work:sample/project.git")
        with mock.patch.object(ledger, "_github_visibility", return_value="public") as check:
            with self.assertRaises(ledger.SyncError):
                ledger._check_private_remote(str(self.repo), "origin")
            check.assert_called_once_with("sample/project")
        self.git("remote", "set-url", "--add", "--push", "origin", str(self.repo))
        self.git("remote", "set-url", "--add", "--push", "origin",
                 "https://github.com/sample/project")
        with self.assertRaisesRegex(ledger.SyncError, "one"):
            ledger._check_private_remote(str(self.repo), "origin")

    def test_visibility_must_be_private_and_unknown_hosts_require_local_trust(self):
        self.git("remote", "add", "origin", "ssh://git@ssh.github.com:443/sample/project.git")
        for visibility in ("public", "internal", ""):
            with self.subTest(visibility=visibility), \
                    mock.patch.object(ledger, "_github_visibility", return_value=visibility):
                with self.assertRaises(ledger.SyncError):
                    ledger._check_private_remote(str(self.repo), "origin")
        with mock.patch.object(ledger, "_github_visibility", return_value="private"):
            ledger._check_private_remote(str(self.repo), "origin")
        endpoint = "git@forge.example:group/project.git"
        self.git("remote", "set-url", "origin", endpoint)
        with self.assertRaises(ledger.SyncError):
            ledger._check_private_remote(str(self.repo), "origin")
        self.git("config", "--local", "--add", "muninn.privateSyncUrl", endpoint)
        ledger._check_private_remote(str(self.repo), "origin")
        self.git("remote", "set-url", "origin", "git@forge.example:group/changed.git")
        with self.assertRaises(ledger.SyncError):
            ledger._check_private_remote(str(self.repo), "origin")

    def test_visibility_probe_uses_fixed_host_and_no_shell(self):
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, stdout="private\n")) as run:
            self.assertEqual(ledger._github_visibility("sample/project"), "private")
        self.assertEqual(run.call_args.args[0],
                         ["gh", "api", "--hostname", "github.com", "repos/sample/project",
                          "--jq", ".visibility"])
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(ledger._github_visibility("sample/project"), "")

    def test_separate_fetch_and_push_and_custom_transports_are_refused(self):
        self.git("remote", "add", "origin", str(self.repo / "source.git"))
        self.git("remote", "set-url", "--push", "origin", str(self.repo / "other.git"))
        with self.assertRaisesRegex(ledger.SyncError, "matching"):
            ledger._check_private_remote(str(self.repo), "origin")
        self.git("config", "--unset", "remote.origin.pushurl")
        for key in ("vcs", "receivepack", "uploadpack", "mirror"):
            with self.subTest(key=key):
                self.git("config", "remote.origin." + key, "sample")
                with self.assertRaisesRegex(ledger.SyncError, "standard transport"):
                    ledger._check_private_remote(str(self.repo), "origin")
                self.git("config", "--unset", "remote.origin." + key)

    def test_custom_ref_cannot_point_into_a_working_branch(self):
        self.git("remote", "add", "origin", str(self.repo / "other.git"))
        for ref in ("refs/heads/main", "refs/tags/v1", "refs/muninn/incoming",
                    "refs/muninn/incoming/branch", "refs/muninn/incoming-collision",
                    "refs/muninn/invalid..name", "HEAD:refs/heads/main"):
            with self.subTest(ref=ref), self.assertRaisesRegex(ledger.SyncError, "ref"):
                ledger.sync(str(self.repo), ref=ref, push=False)

    def test_public_or_mismatched_pull_only_stops_before_transport(self):
        self.git("remote", "add", "memory", "https://github.com/example/fiction.git")
        with mock.patch.object(ledger, "_github_visibility", return_value="public"), \
                mock.patch.object(ledger, "_fetch_remote") as fetch:
            with self.assertRaisesRegex(ledger.SyncError, "private"):
                ledger.sync(str(self.repo), remote="memory", push=False)
        fetch.assert_not_called()

    def test_direct_local_path_and_named_chain_use_one_git_url_interpretation(self):
        from muninn.dynamics import Dynamics

        allowed = self.repo / "allowed.git"
        trap = self.repo / "trap.git"
        self.git("init", "--bare", str(allowed))
        self.git("init", "--bare", str(trap))
        Dynamics(str(self.repo)).touch("granite.md")
        self.assertTrue(ledger.sync(str(self.repo), remote=str(allowed))["pushed"])
        self.git("config", "url." + str(allowed) + ".insteadOf", "example-destination")
        self.git("config", "url." + str(trap) + ".insteadOf", str(allowed))
        self.git("remote", "add", "memory", "example-destination")
        self.assertEqual(ledger.sync(str(self.repo), remote="memory")["events"], 1)
        untouched = subprocess.run(
            ["git", "-C", str(trap), "show-ref", "--verify", "--quiet", ledger.SYNC_REF])
        self.assertEqual(untouched.returncode, 1)
        with self.assertRaises(ledger.SyncError):
            ledger.sync(str(self.repo), remote=str(allowed))

    def test_direct_local_path_rejects_a_push_only_rewrite(self):
        raw = str(self.repo / "local.git")
        self.git("init", "--bare", raw)
        self.git("config", "url.https://github.com/example/wrong.pushInsteadOf", raw)
        with mock.patch.object(ledger, "_fetch_remote") as fetch:
            with self.assertRaises(ledger.SyncError):
                ledger.sync(str(self.repo), remote=raw)
        fetch.assert_not_called()

    def test_specific_http_redirect_configuration_cannot_override_guard(self):
        url = "https://github.com/example/fiction.git"
        self.git("remote", "add", "memory", url)
        self.git("config", "http." + url + ".followRedirects", "true")
        with mock.patch.object(ledger, "_github_visibility", return_value="private"), \
                mock.patch.object(ledger, "_fetch_remote") as fetch:
            with self.assertRaisesRegex(ledger.SyncError, "redirect"):
                ledger.sync(str(self.repo), remote="memory", push=False)
        fetch.assert_not_called()

    def test_installed_skill_and_intent_hint_use_private_remote(self):
        import io
        from contextlib import redirect_stdout
        from muninn import cli, skill

        self.assertIn("sync --remote memory", skill.SKILL_MD)
        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(["--root", str(self.repo), "intent", "review quartz",
                      "--branch", "fiction/quartz"])
        self.assertIn("muninn sync --remote memory", out.getvalue())

    def test_transport_disables_generic_http_redirects(self):
        redirect_setting = "http.followRedirects=false"
        for operation in ("fetch", "push"):
            with self.subTest(operation=operation), mock.patch.object(
                    subprocess, "run", return_value=subprocess.CompletedProcess(
                        [], 1, stdout="")) as git:
                ledger._git(str(self.repo), operation, "--", "memory", ledger.SYNC_REF)
                command = git.call_args.args[0]
                self.assertIn(redirect_setting, command[:command.index(operation)])

    def test_ssh_cannot_substitute_a_transport_or_github_host(self):
        self.git("remote", "add", "memory", "git@github.com:example/fiction.git")
        with mock.patch.object(ledger, "_github_visibility", return_value="private"), \
                mock.patch.object(ledger, "_fetch_remote") as fetch:
            for key in ("GIT_SSH", "GIT_SSH_COMMAND"):
                with self.subTest(key=key), mock.patch.dict(os.environ, {key: "ssh-wrapper"}):
                    with self.assertRaisesRegex(ledger.SyncError, "standard SSH"):
                        ledger.sync(str(self.repo), "memory", push=False)
            self.git("config", "core.sshCommand", "ssh-wrapper")
            with self.assertRaisesRegex(ledger.SyncError, "standard SSH"):
                ledger.sync(str(self.repo), "memory", push=False)
            fetch.assert_not_called()
        self.git("config", "--unset", "core.sshCommand")
        with mock.patch.object(ledger, "_ssh_route", return_value=("forge.example", "22")), \
                mock.patch.object(ledger, "_github_visibility", return_value="private"):
            with self.assertRaisesRegex(ledger.SyncError, "GitHub SSH"):
                ledger._check_private_remote(str(self.repo), "memory")

    def test_manually_trusted_ssh_alias_to_github_requires_visibility(self):
        url = "git@fictional-forge:example/fiction.git"
        self.git("remote", "add", "memory", url)
        self.git("config", "--local", "--add", "muninn.privateSyncUrl", url)
        with mock.patch.object(ledger, "_ssh_route", return_value=("github.com", "22"),
                               create=True), mock.patch.object(
                 ledger, "_github_visibility", return_value="public") as check, \
                mock.patch.object(ledger, "_fetch_remote") as fetch:
            with self.assertRaisesRegex(ledger.SyncError, "private"):
                ledger.sync(str(self.repo), "memory", push=False)
            check.assert_called_once_with("example/fiction")
        fetch.assert_not_called()
