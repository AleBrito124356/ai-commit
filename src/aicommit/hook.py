"""Install and remove a ``prepare-commit-msg`` git hook.

When installed, running ``git commit`` (with no ``-m`` and no template) calls
``aicommit prepare``, which prints a generated Conventional Commit to stdout.
The hook prepends that to the commit message file, so the editor opens with the
suggestion already filled in. You edit or accept as usual.

Two details make the hook actually fire in real setups:

* It is written to the directory git really runs hooks from
  (``git rev-parse --git-path hooks``), which honours ``core.hooksPath`` —
  husky, lefthook and friends set it — and linked worktrees.
* It calls ``<python> -m aicommit prepare`` with the absolute interpreter
  aicommit was installed with, and only then falls back to ``aicommit`` on
  ``PATH``. Commits made from an IDE or GUI client, where a virtualenv or pipx
  directory is not on ``PATH``, still get a suggestion.

The hook is a no-op for merge/squash/amend/``-m`` commits, and never blocks a
commit if aicommit is missing or the backend is unreachable.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .gitutil import GitError, run_git

HOOK_NAME = "prepare-commit-msg"
MARKER = "# >>> aicommit managed hook >>>"
END_MARKER = MARKER.replace(">>>", "<<<")
BACKUP_SUFFIX = ".aicommit.bak"

_PYTHON_LINE_RE = re.compile(r"^AICOMMIT_PYTHON='(?P<path>(?:[^']|'\"'\"')*)'$", re.MULTILINE)


def _sh_quote(value: str) -> str:
    """Single-quote ``value`` for POSIX sh."""
    return "'" + value.replace("'", "'\"'\"'") + "'"


def render_hook_script(python: Optional[str] = None) -> str:
    """The hook body. ``python`` is the interpreter to try first."""
    interpreter = Path(python).as_posix() if python else ""
    return f"""\
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

# 1) The interpreter aicommit was installed with, so the hook also works from
#    IDEs and GUI clients whose PATH does not include your virtualenv.
# 2) Otherwise whatever `aicommit` is on PATH. If neither exists, do nothing.
AICOMMIT_PYTHON={_sh_quote(interpreter)}
generated=""
ran=""
if [ -n "$AICOMMIT_PYTHON" ] && [ -f "$AICOMMIT_PYTHON" ]; then
    if generated=$("$AICOMMIT_PYTHON" -m aicommit prepare 2>/dev/null); then
        ran=1
    fi
fi
if [ -z "$ran" ] && command -v aicommit >/dev/null 2>&1; then
    generated=$(aicommit prepare 2>/dev/null)
fi
if [ -z "$generated" ]; then
    exit 0
fi

existing=$(cat "$COMMIT_MSG_FILE")
printf '%s\\n\\n%s\\n' "$generated" "$existing" > "$COMMIT_MSG_FILE"
{END_MARKER}
"""


# The PATH-only variant, kept for callers that imported the old constant.
HOOK_SCRIPT = render_hook_script(None)


@dataclass
class HookStatus:
    installed: bool
    path: Path
    managed: bool  # installed AND ours (contains the marker)
    hooks_dir: Optional[Path] = None
    hooks_path_config: Optional[str] = None  # core.hooksPath, when set
    interpreter: Optional[str] = None  # recorded in our hook
    interpreter_exists: bool = False


def hooks_dir(cwd: Optional[Path] = None) -> Path:
    """The directory git runs hooks from (respects core.hooksPath/worktrees)."""
    out = run_git(["rev-parse", "--git-path", "hooks"], cwd=cwd).strip()
    path = Path(out)
    if not path.is_absolute():
        path = Path(cwd or os.getcwd()) / path
    return path.resolve()


def hooks_path_config(cwd: Optional[Path] = None) -> Optional[str]:
    """The ``core.hooksPath`` value, or None when git uses ``.git/hooks``."""
    try:
        value = run_git(["config", "--get", "core.hooksPath"], cwd=cwd, check=False)
    except GitError:  # pragma: no cover - git missing is caught earlier
        return None
    return value.strip() or None


def hook_path(cwd: Optional[Path] = None) -> Path:
    return hooks_dir(cwd=cwd) / HOOK_NAME


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _is_managed(path: Path) -> bool:
    return path.is_file() and MARKER in _read(path)


def recorded_interpreter(path: Path) -> Optional[str]:
    match = _PYTHON_LINE_RE.search(_read(path))
    if not match:
        return None
    value = match.group("path").replace("'\"'\"'", "'")
    return value or None


def status(cwd: Optional[Path] = None) -> HookStatus:
    directory = hooks_dir(cwd=cwd)
    path = directory / HOOK_NAME
    installed = path.is_file()
    managed = _is_managed(path)
    interpreter = recorded_interpreter(path) if managed else None
    return HookStatus(
        installed=installed,
        path=path,
        managed=managed,
        hooks_dir=directory,
        hooks_path_config=hooks_path_config(cwd=cwd),
        interpreter=interpreter,
        interpreter_exists=bool(interpreter) and Path(interpreter).is_file(),
    )


def _make_executable(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def install(
    cwd: Optional[Path] = None, force: bool = False, python: Optional[str] = None
) -> str:
    """Install the hook. Returns a short status message.

    An existing unmanaged hook is preserved as ``<name>.aicommit.bak`` unless it
    is already ours. Pass ``force`` to overwrite an old backup. ``python``
    defaults to the interpreter running this code.
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

    interpreter = python if python is not None else sys.executable
    path.write_text(render_hook_script(interpreter), encoding="utf-8", newline="\n")
    _make_executable(path)
    configured = hooks_path_config(cwd=cwd)
    where = f" (core.hooksPath = {configured})" if configured else ""
    return f"Installed {HOOK_NAME} hook at {path}{where}{note}"


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
