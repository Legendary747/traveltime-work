"""GitHub-only checkpoints, allowed only in a verified private repository.

Branches do not have separate privacy: refuse a public or unknown destination.
"""
import argparse
import json
import re
from pathlib import Path
import subprocess
import sys


def git(*args, cwd=None, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check, text=True, capture_output=True)


def require_private_origin(cwd=None):
    """Check live metadata for the actual push destination before reading/writing state."""
    destinations = git("remote", "get-url", "--push", "--all", "origin", cwd=cwd).stdout.splitlines()
    if len(destinations) != 1:
        raise ValueError("Private checkpoints require one verified GitHub push destination.")
    remote = destinations[0].strip()
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?", remote)
    if not match:
        raise ValueError("Private checkpoints require a verifiable GitHub origin.")
    repository = match.group(1)
    try:
        response = subprocess.run(["gh", "api", f"repos/{repository}"],
                                  check=True, text=True, capture_output=True)
        metadata = json.loads(response.stdout)
    except (OSError, subprocess.CalledProcessError, ValueError):
        raise ValueError("Cannot verify repository privacy; no state operation performed.") from None
    if (not isinstance(metadata, dict) or metadata.get("private") is not True
            or not isinstance(metadata.get("full_name"), str)
            or metadata["full_name"].lower() != repository.lower()):
        raise ValueError("State checkpoints require a private repository; public or unknown visibility is refused.")


def prepare(path):
    root = Path(git("rev-parse", "--show-toplevel").stdout.strip()).resolve()
    target = Path(path).resolve()
    if target.parent != root or target.name != "state" or target.exists():
        raise ValueError("Expected a new state/ directory immediately under the repository.")
    require_private_origin()
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
            "Branches share repository visibility. Keep this repository private and review this history before making it public.\n"
            "Do not edit the database while collection runs.\n",
            encoding="utf-8")
    else:
        raise ValueError("Cannot inspect remote state branch. No state was initialized.")


def save(path):
    target = Path(path).resolve()
    if target.name != "state" or not (target / ".git").is_file():
        raise ValueError("Expected the prepared state worktree.")
    if git("branch", "--show-current", cwd=target).stdout.strip() != "commute-data":
        raise ValueError("State save is allowed only on commute-data.")
    require_private_origin(cwd=target)
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
        print("State operation failed; verify a private GitHub origin, gh authentication, write permission, and concurrent edits.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
