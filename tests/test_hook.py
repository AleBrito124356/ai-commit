"""prepare-commit-msg hook install / status / uninstall lifecycle."""

from __future__ import annotations

from aicommit.hook import HOOK_NAME, MARKER, hook_path, install, status, uninstall


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
