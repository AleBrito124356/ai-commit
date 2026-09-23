"""prepare-commit-msg hook install / status / uninstall lifecycle."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from aicommit.config import Config
from aicommit.diff import collect_staged
from aicommit.heuristic import draft_commit
from aicommit.hook import (
    HOOK_NAME,
    MARKER,
    hook_path,
    hooks_dir,
    install,
    recorded_interpreter,
    render_hook_script,
    status,
    uninstall,
)

SRC = Path(__file__).resolve().parents[1] / "src"


def test_install_creates_managed_hook(git_repo):
    r = git_repo
    install(cwd=r.path)

    st = status(cwd=r.path)
    assert st.installed is True
    assert st.managed is True

    path = hook_path(cwd=r.path)
    assert path.name == HOOK_NAME
    content = path.read_text(encoding="utf-8")
    assert MARKER in content
    assert "aicommit prepare" in content


def test_install_backs_up_existing_unmanaged_hook(git_repo):
    r = git_repo
    hooks = hook_path(cwd=r.path)
    hooks.parent.mkdir(parents=True, exist_ok=True)
    hooks.write_text("#!/bin/sh\necho existing\n", encoding="utf-8")

    install(cwd=r.path)

    backup = hooks.with_name(hooks.name + ".aicommit.bak")
    assert backup.is_file()
    assert "echo existing" in backup.read_text(encoding="utf-8")
    assert MARKER in hooks.read_text(encoding="utf-8")


def test_uninstall_restores_backup(git_repo):
    r = git_repo
    hooks = hook_path(cwd=r.path)
    hooks.parent.mkdir(parents=True, exist_ok=True)
    hooks.write_text("#!/bin/sh\necho existing\n", encoding="utf-8")

    install(cwd=r.path)
    uninstall(cwd=r.path)

    restored = hooks.read_text(encoding="utf-8")
    assert "echo existing" in restored
    assert MARKER not in restored


def test_uninstall_without_install_is_noop(git_repo):
    r = git_repo
    message = uninstall(cwd=r.path)
    assert "nothing to do" in message.lower()


def test_uninstall_leaves_foreign_hook_alone(git_repo):
    r = git_repo
    hooks = hook_path(cwd=r.path)
    hooks.parent.mkdir(parents=True, exist_ok=True)
    hooks.write_text("#!/bin/sh\necho theirs\n", encoding="utf-8")

    message = uninstall(cwd=r.path)
    assert "not installed by aicommit" in message
    assert hooks.is_file()


# --- core.hooksPath and the recorded interpreter ------------------------------ #


def test_hooks_dir_follows_core_hooks_path(git_repo):
    r = git_repo
    assert hooks_dir(cwd=r.path) == (r.path / ".git" / "hooks").resolve()
    r.git("config", "core.hooksPath", ".husky")
    assert hooks_dir(cwd=r.path) == (r.path / ".husky").resolve()
    # Also from a subdirectory: the path is resolved against the worktree.
    sub = r.path / "pkg"
    sub.mkdir()
    assert hooks_dir(cwd=sub) == (r.path / ".husky").resolve()


def test_install_under_hooks_path_reports_it(git_repo):
    r = git_repo
    r.git("config", "core.hooksPath", ".husky")
    message = install(cwd=r.path)
    assert "core.hooksPath = .husky" in message
    assert (r.path / ".husky" / HOOK_NAME).is_file()
    assert not (r.path / ".git" / "hooks" / HOOK_NAME).exists()
    st = status(cwd=r.path)
    assert st.managed
    assert st.hooks_path_config == ".husky"
    assert st.hooks_dir == (r.path / ".husky").resolve()


def test_hook_records_the_installing_interpreter(git_repo):
    r = git_repo
    install(cwd=r.path)
    st = status(cwd=r.path)
    assert st.interpreter == Path(sys.executable).as_posix()
    assert st.interpreter_exists
    content = hook_path(cwd=r.path).read_text(encoding="utf-8")
    assert '"$AICOMMIT_PYTHON" -m aicommit prepare' in content
    assert "command -v aicommit" in content


def test_interpreter_paths_with_quotes_round_trip(tmp_path):
    script_path = tmp_path / HOOK_NAME
    script_path.write_text(render_hook_script("/opt/it's here/python"), encoding="utf-8")
    assert recorded_interpreter(script_path) == "/opt/it's here/python"
    script_path.write_text(render_hook_script(None), encoding="utf-8")
    assert recorded_interpreter(script_path) is None


def _path_without_aicommit() -> str:
    keep = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        folder = Path(entry)
        if any((folder / name).exists() for name in ("aicommit", "aicommit.exe")):
            continue
        keep.append(entry)
    return os.pathsep.join(keep)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_real_git_commit_through_hook_under_hooks_path(git_repo, tmp_path):
    """The whole chain: husky-style hooksPath, aicommit not on PATH, real commit."""
    r = git_repo
    r.write(".aicommit.yaml", "backend: local\n")
    r.commit("chore: init", "src/auth.py", "def login(user):\n    return token(user)\n")
    r.git("config", "core.hooksPath", ".husky")
    install(cwd=r.path)

    r.write(
        "src/auth.py",
        "def login(user):\n    if user.locked:\n        raise AccountLocked(user)\n    return token(user)\n",
    )
    r.git("add", "-A")
    expected = draft_commit(collect_staged(cwd=r.path), Config(backend="local")).best
    expected_text = expected.format().strip()
    assert expected.header() == "fix(auth): handle AccountLocked in login"

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = {
        **os.environ,
        "PATH": _path_without_aicommit(),
        "GIT_EDITOR": "true",
        "HOME": str(fake_home),
        "USERPROFILE": str(fake_home),
        "PYTHONPATH": str(SRC),
        "PYTHONIOENCODING": "utf-8",
    }
    env.pop("NVIDIA_API_KEY", None)
    assert shutil.which("aicommit", path=env["PATH"]) is None
    subprocess.run(
        ["git", "commit", "-q"], cwd=r.path, env=env, check=True, capture_output=True
    )
    message = r.git("log", "-1", "--format=%B").strip()
    assert message == expected_text

    # Uninstall removes it from the hooksPath directory, not .git/hooks.
    assert "Removed aicommit hook" in uninstall(cwd=r.path)
    assert not (r.path / ".husky" / HOOK_NAME).exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_hook_skips_messages_given_with_dash_m(git_repo, tmp_path):
    r = git_repo
    r.write(".aicommit.yaml", "backend: local\n")
    r.commit("chore: init", "a.py", "x = 1\n")
    install(cwd=r.path)
    r.write("a.py", "x = 2\n")
    r.git("add", "-A")
    env = {**os.environ, "PYTHONPATH": str(SRC), "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    subprocess.run(
        ["git", "commit", "-q", "-m", "my own message"],
        cwd=r.path,
        env=env,
        check=True,
        capture_output=True,
    )
    assert r.git("log", "-1", "--format=%B").strip() == "my own message"
