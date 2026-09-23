"""Thin wrappers around the ``git`` command line.

Every git interaction in the project funnels through here so the rest of the
code never shells out directly and stays easy to reason about (and to mock).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Optional


class GitError(RuntimeError):
    """Raised when a git command fails or git is not a usable repository."""


def run_git(
    args: List[str],
    cwd: Optional[Path] = None,
    check: bool = True,
) -> str:
    """Run ``git <args>`` and return stdout as text.

    Raises :class:`GitError` on a non-zero exit when ``check`` is true.
    """
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:  # git not installed / not on PATH
        raise GitError("git is not installed or not on your PATH") from exc

    if check and completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        raise GitError(f"git {' '.join(args)} failed: {message}")
    return completed.stdout


def is_git_repo(cwd: Optional[Path] = None) -> bool:
    try:
        out = run_git(["rev-parse", "--is-inside-work-tree"], cwd=cwd)
    except GitError:
        return False
    return out.strip() == "true"


def repo_root(cwd: Optional[Path] = None) -> Path:
    out = run_git(["rev-parse", "--show-toplevel"], cwd=cwd)
    return Path(out.strip())


def git_dir(cwd: Optional[Path] = None) -> Path:
    """Absolute path to the .git directory (handles worktrees and submodules)."""
    out = run_git(["rev-parse", "--absolute-git-dir"], cwd=cwd)
    return Path(out.strip())


def current_branch(cwd: Optional[Path] = None) -> str:
    out = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    return out.strip()


def has_staged_changes(cwd: Optional[Path] = None) -> bool:
    """True when ``git diff --cached`` would produce output."""
    completed = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
    )
    # exit code 1 means "there are differences", 0 means "clean".
    return completed.returncode == 1


def stage_all(cwd: Optional[Path] = None) -> None:
    run_git(["add", "-A"], cwd=cwd)


def list_tags(
    cwd: Optional[Path] = None, merged_into: Optional[str] = None
) -> List[str]:
    """Tags ordered oldest-to-newest by the version they encode.

    ``v1.9.1`` sorts before ``v2.0.0`` even if it was created later (a
    backport). With ``merged_into``, only tags reachable from that ref are
    returned, so a changelog for HEAD ignores tags on other branches.
    """
    args = ["tag", "--sort=v:refname"]
    if merged_into:
        args += ["--merged", merged_into]
    out = run_git(args, cwd=cwd)
    return [line.strip() for line in out.splitlines() if line.strip()]


def nearest_tag(ref: str = "HEAD", cwd: Optional[Path] = None) -> Optional[str]:
    """The closest tag reachable from ``ref`` (``git describe``), or None."""
    try:
        out = run_git(["describe", "--tags", "--abbrev=0", ref], cwd=cwd)
    except GitError:
        return None
    return out.strip() or None


def rev_exists(ref: str, cwd: Optional[Path] = None) -> bool:
    try:
        run_git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=cwd)
    except GitError:
        return False
    return True


def commit_with_message(message: str, cwd: Optional[Path] = None) -> str:
    """Create a commit from an already-staged index using ``message``."""
    return run_git(["commit", "-m", message], cwd=cwd)
