"""GitHub-only persistence. State is isolated in the private commute-data branch."""
import argparse
from pathlib import Path
import subprocess
import sys


def git(*args, cwd=None, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check, text=True, capture_output=True)


def prepare(path):
    root = Path(git("rev-parse", "--show-toplevel").stdout.strip()).resolve()
    target = Path(path).resolve()
    if target.parent != root or target.name != "state" or target.exists():
        raise ValueError("Expected a new state/ directory immediately under the repository.")
    remote = git("ls-remote", "--exit-code", "--heads", "origin", "commute-data", check=False)
    if remote.returncode == 0:
        git("fetch", "origin", "commute-data")
        git("worktree", "add", "-b", "commute-data", str(target), "FETCH_HEAD")
    elif remote.returncode == 2:
        git("worktree", "add", "--detach", str(target), "HEAD")
        git("switch", "--orphan", "commute-data", cwd=target)
        (target / "README.md").write_text(
            "# Private commute observations\n\n"
            "SQLite checkpoints and generated reports. Location secrets are not stored here.\n"
            "Keep this branch and repository private. Do not edit the database while collection runs.\n",
            encoding="utf-8")
    else:
        raise ValueError("Cannot inspect remote state branch. No state was initialized.")


def save(path):
    target = Path(path).resolve()
    if target.name != "state" or not (target / ".git").is_file():
        raise ValueError("Expected the prepared state worktree.")
    if git("branch", "--show-current", cwd=target).stdout.strip() != "commute-data":
        raise ValueError("State save is allowed only on commute-data.")
    git("config", "user.name", "github-actions[bot]", cwd=target)
    git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com", cwd=target)
    for item in ("README.md", "commute.sqlite3", "reports"):
        if (target / item).exists():
            git("add", "--", item, cwd=target)
    changed = git("diff", "--cached", "--quiet", cwd=target, check=False)
    if changed.returncode not in (0, 1):
        raise ValueError("Cannot inspect state changes.")
    if changed.returncode == 1:
        git("commit", "-m", "Checkpoint commute observations [skip ci]", cwd=target)
        git("push", "origin", "HEAD:refs/heads/commute-data", cwd=target)
    else:
        head = git("rev-parse", "--verify", "HEAD", cwd=target, check=False)
        if head.returncode == 0:
            git("push", "origin", "HEAD:refs/heads/commute-data", cwd=target)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "save"))
    parser.add_argument("path", nargs="?", default="state")
    args = parser.parse_args()
    try:
        (prepare if args.action == "prepare" else save)(args.path)
    except (subprocess.CalledProcessError, ValueError):
        print("State operation failed; check repository write permission and concurrent edits.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
