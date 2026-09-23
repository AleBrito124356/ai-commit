"""End to end through the CLI with the rule-based local backend (no network)."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from aicommit.cli import app
from aicommit.commit import is_valid_commit, parse_commit
from aicommit.config import Config

runner = CliRunner()


@pytest.fixture
def staged_repo(git_repo, monkeypatch):
    r = git_repo
    r.commit("feat: init", "src/auth.py", "def login(user):\n    return token(user)\n")
    r.write(
        "src/auth.py",
        "def login(user):\n    if user.locked:\n        raise AccountLocked(user)\n    return token(user)\n",
    )
    r.git("add", "-A")
    monkeypatch.chdir(r.path)
    return r


def test_local_backend_creates_a_valid_commit(staged_repo):
    result = runner.invoke(app, ["--backend", "local", "-y"])
    assert result.exit_code == 0, result.output
    assert "rule-based draft, no AI" in result.output
    message = staged_repo.git("log", "-1", "--format=%B")
    assert message.splitlines()[0] == "fix(auth): handle AccountLocked in login"
    assert is_valid_commit(message, Config())


def test_local_backend_dry_run_does_not_commit(staged_repo):
    before = staged_repo.git("rev-parse", "HEAD")
    result = runner.invoke(app, ["--backend", "local", "-y", "--dry-run", "--hint", "fixes #9"])
    assert result.exit_code == 0, result.output
    assert "Fixes #9" in result.output
    assert staged_repo.git("rev-parse", "HEAD") == before


def test_local_backend_emoji_commit_is_still_parseable(staged_repo):
    result = runner.invoke(app, ["--backend", "local", "--emoji", "-y"])
    assert result.exit_code == 0, result.output
    header = staged_repo.git("log", "-1", "--format=%s").strip()
    assert header.startswith("\U0001f41b fix(auth):")
    assert parse_commit(header).type == "fix"


def test_prepare_falls_back_to_local_draft_when_backend_fails(staged_repo):
    # Default backend is nim and the isolated environment has no NVIDIA_API_KEY.
    result = runner.invoke(app, ["prepare"])
    assert result.exit_code == 0
    assert result.stdout.splitlines()[0] == "fix(auth): handle AccountLocked in login"


def test_prepare_stays_silent_without_fallback(staged_repo, monkeypatch):
    monkeypatch.setenv("AICOMMIT_HOOK_FALLBACK", "false")
    result = runner.invoke(app, ["prepare"])
    assert result.exit_code == 0
    assert result.stdout == ""


def test_prepare_with_local_backend(staged_repo):
    result = runner.invoke(app, ["--backend", "local", "prepare"])
    assert result.exit_code == 0
    assert result.stdout.startswith("fix(auth): handle AccountLocked in login")


def test_polish_with_local_backend_is_a_clear_error(staged_repo):
    result = runner.invoke(app, ["--backend", "local", "changelog", "--polish"])
    assert result.exit_code == 1
    assert "--polish" in result.output and "rule-based" in result.output


def test_pr_with_local_backend(staged_repo):
    r = staged_repo
    r.git("checkout", "-q", "-b", "feature/lock")
    r.git("commit", "-q", "-m", "fix(auth): refuse locked accounts")
    result = runner.invoke(app, ["--backend", "local", "pr", "--base", "main"])
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("# Refuse locked accounts\n")
    assert "## Summary" in result.stdout
    assert "fix(auth): refuse locked accounts" in result.stdout


def test_config_file_can_select_local_backend(staged_repo):
    (staged_repo.path / ".aicommit.yaml").write_text("backend: local\n", encoding="utf-8")
    result = runner.invoke(app, ["-y", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "fix(auth): handle AccountLocked in login" in result.output
