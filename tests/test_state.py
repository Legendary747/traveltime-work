from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class StatePersistenceTests(unittest.TestCase):
    def test_initial_state_and_fresh_runner_restore(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "state.py"
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
            run(sys.executable, str(script), "prepare", "state", cwd=first)
            (first / "state" / "commute.sqlite3").write_bytes(b"checkpoint-fixture")
            run(sys.executable, str(script), "save", "state", cwd=first)
            self.assertEqual((first / "README.md").read_text(), "Source code stays on main.")
            run("git", "clone", "--branch", "main", str(origin), str(second))
            run(sys.executable, str(script), "prepare", "state", cwd=second)
            self.assertEqual((second / "state" / "commute.sqlite3").read_bytes(),
                             b"checkpoint-fixture")
            run(sys.executable, str(script), "save", "state", cwd=second)


if __name__ == "__main__":
    unittest.main()
