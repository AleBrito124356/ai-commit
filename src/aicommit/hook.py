"""Install and remove a ``prepare-commit-msg`` git hook.

When installed, running ``git commit`` (with no ``-m`` and no template) calls
``aicommit prepare``, which prints a generated Conventional Commit to stdout.
The hook prepends that to the commit message file, so the editor opens with the
suggestion already filled in. You edit or accept as usual.

The hook is a no-op for merge/squash/amend/``-m`` commits, and never blocks a
commit if aicommit is missing or the backend is unreachable.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .gitutil import git_dir

HOOK_NAME = "prepare-commit-msg"
MARKER = "# >>> aicommit managed hook >>>"
BACKUP_SUFFIX = ".aicommit.bak"

HOOK_SCRIPT = f"""\
#!/bin/sh
{MARKER}
# Managed by aicommit. Remove with: aicommit hook uninstall
COMMIT_MSG_FILE="$1"
COMMIT_SOURCE="$2"

# Only prefill a fresh, hand-written commit. Skip merges, squashes, templates,
# `-m` messages and amends (those all set a non-empty source).
if [ -n "$COMMIT_SOURCE" ]; then
    exit 0
fi

# If aicommit is not installed, do nothing rather than break the commit.
if ! command -v aicommit >/dev/null 2>&1; then
    exit 0
fi

generated=$(aicommit prepare 2>/dev/null)
if [ -z "$generated" ]; then
    exit 0
fi

existing=$(cat "$COMMIT_MSG_FILE")
printf '%s\\n\\n%s\\n' "$generated" "$existing" > "$COMMIT_MSG_FILE"
{MARKER.replace(">>>", "<<<")}
"""


@dataclass
class HookStatus:
    installed: bool
    path: Path
    managed: bool  # installed AND ours (contains the marker)


def hooks_dir(cwd: Optional[Path] = None) -> Path:
    return git_dir(cwd=cwd) / "hooks"


def hook_path(cwd: Optional[Path] = None) -> Path:
    return hooks_dir(cwd=cwd) / HOOK_NAME


def _is_managed(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        return MARKER in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def status(cwd: Optional[Path] = None) -> HookStatus:
    path = hook_path(cwd=cwd)
    installed = path.is_file()
    return HookStatus(installed=installed, path=path, managed=_is_managed(path))


def _make_executable(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def install(cwd: Optional[Path] = None, force: bool = False) -> str:
    """Install the hook. Returns a short status message.

    An existing unmanaged hook is preserved as ``<name>.aicommit.bak`` unless it
    is already ours. Pass ``force`` to overwrite without asking upstream.
    """
    directory = hooks_dir(cwd=cwd)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / HOOK_NAME

    if path.is_file() and not _is_managed(path):
        backup = path.with_name(path.name + BACKUP_SUFFIX)
        if backup.exists() and not force:
            raise FileExistsError(
                f"a backup already exists at {backup}; resolve it or pass --force"
            )
        os.replace(path, backup)
        note = f" (existing hook backed up to {backup.name})"
    else:
        note = ""

    path.write_text(HOOK_SCRIPT, encoding="utf-8", newline="\n")
    _make_executable(path)
    return f"Installed {HOOK_NAME} hook at {path}{note}"


def uninstall(cwd: Optional[Path] = None) -> str:
    """Remove our hook and restore any backup we made."""
    path = hook_path(cwd=cwd)
    backup = path.with_name(path.name + BACKUP_SUFFIX)

    if not path.is_file():
        if backup.is_file():
            os.replace(backup, path)
            return f"Restored previous hook from {backup.name}"
        return "No aicommit hook installed; nothing to do"

    if not _is_managed(path):
        return (
            f"{path} exists but was not installed by aicommit; leaving it alone"
        )

    path.unlink()
    if backup.is_file():
        os.replace(backup, path)
        return f"Removed aicommit hook and restored {backup.name}"
    return f"Removed aicommit hook at {path}"
