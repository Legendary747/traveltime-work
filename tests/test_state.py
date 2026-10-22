from contextlib import chdir
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import state


def result(stdout, returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


class VisibilityTests(unittest.TestCase):
    def test_verified_private_github_origin_is_allowed(self):
        for remote in ("https://github.com/example/private-data.git", "git@github.com:example/private-data"):
            with self.subTest(remote=remote), patch.object(state, "git", return_value=result(remote)) as git:
                with patch.object(state.subprocess, "run", return_value=result(json.dumps(
                        {"private": True, "full_name": "example/private-data"}))) as api:
                    state.require_private_origin()
                git.assert_called_once_with("remote", "get-url", "--push", "--all", "origin", cwd=None)
                self.assertEqual(api.call_args.args[0], ["gh", "api", "repos/example/private-data"])

    def test_public_unknown_malformed_or_wrong_repository_fails_closed(self):
        for metadata in ({"private": False, "full_name": "example/private-data"},
                         {"full_name": "example/private-data"},
                         {"private": "true", "full_name": "example/private-data"},
                         {"private": True, "full_name": "another/private-data"},
                         {"private": True, "full_name": None}, [], None):
            with self.subTest(metadata=metadata), patch.object(state, "git", return_value=result(
                    "https://github.com/example/private-data.git")):
                with patch.object(state.subprocess, "run", return_value=result(json.dumps(metadata))):
                    with self.assertRaises(ValueError):
                        state.require_private_origin()

    def test_missing_credentials_or_cli_and_bad_response_fail_closed(self):
        for failure in (FileNotFoundError(), subprocess.CalledProcessError(1, ["gh"])):
            with self.subTest(failure=failure), patch.object(state, "git", return_value=result(
                    "https://github.com/example/private-data")):
                with patch.object(state.subprocess, "run", side_effect=failure):
                    with self.assertRaises(ValueError):
                        state.require_private_origin()
        with patch.object(state, "git", return_value=result("https://github.com/example/private-data")):
            with patch.object(state.subprocess, "run", return_value=result("not json")):
                with self.assertRaises(ValueError):
                    state.require_private_origin()

    def test_unverifiable_or_multiple_push_destinations_are_rejected_before_api(self):
        for remote in ("/tmp/local-repo.git", "https://example.invalid/owner/repo", "",
                       "https://github.com/example/private-data\nhttps://github.com/example/public-data"):
            with self.subTest(remote=remote), patch.object(state, "git", return_value=result(remote)):
                with patch.object(state.subprocess, "run") as api:
                    with self.assertRaises(ValueError):
                        state.require_private_origin()
                    api.assert_not_called()

    def test_failed_privacy_check_prevents_restore_and_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / "state"
            with chdir(root), patch.object(state, "git", return_value=result(str(root))) as git:
                with patch.object(state, "require_private_origin", side_effect=ValueError("Not private")):
                    with self.assertRaises(ValueError):
                        state.prepare(target)
                git.assert_called_once_with("rev-parse", "--show-toplevel")
                self.assertFalse(target.exists())
            target.mkdir()
            (target / ".git").write_text("gitdir: fixture")
            with patch.object(state, "git", return_value=result("commute-data")) as git:
                with patch.object(state, "require_private_origin", side_effect=ValueError("Not private")):
                    with self.assertRaises(ValueError):
                        state.save(target)
                git.assert_called_once_with("branch", "--show-current", cwd=target)


class StatePersistenceTests(unittest.TestCase):
    def test_initial_state_and_fresh_runner_restore(self):
        def run(*args, cwd=None):
            return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            origin, first, second = root / "origin.git", root / "first", root / "second"
            run("git", "init", "--bare", str(origin))
            run("git", "init", "-b", "main", str(first))
            run("git", "config", "user.email", "test@example.invalid", cwd=first)
            run("git", "config", "user.name", "Test", cwd=first)
            (first / "README.md").write_text("Source code stays on main.")
            run("git", "add", ".", cwd=first)
            run("git", "commit", "-m", "Initial fixture", cwd=first)
            run("git", "remote", "add", "origin", str(origin), cwd=first)
            run("git", "push", "-u", "origin", "main", cwd=first)
            # Local bare repos are offline fixtures, never a production bypass.
            with chdir(first), patch.object(state, "require_private_origin") as verify:
                state.prepare("state")
                (first / "state" / "commute.sqlite3").write_bytes(b"checkpoint-fixture")
                state.save("state")
                self.assertEqual(verify.call_count, 2)
            self.assertEqual((first / "README.md").read_text(), "Source code stays on main.")
            run("git", "clone", "--branch", "main", str(origin), str(second))
            with chdir(second), patch.object(state, "require_private_origin") as verify:
                state.prepare("state")
                self.assertEqual((second / "state" / "commute.sqlite3").read_bytes(),
                                 b"checkpoint-fixture")
                state.save("state")
                self.assertEqual(verify.call_count, 2)


if __name__ == "__main__":
    unittest.main()
