"""The CLI end to end: interactive loop, prepare, pr, changelog, hook, init.

Every test runs inside a throwaway git repository with the backend replaced by
a fake, so nothing touches the network.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from aicommit import __version__
from aicommit import cli
from aicommit.llm import LLMClient, LLMError

runner = CliRunner()

COMMIT_JSON = json.dumps(
    {
        "primary": {"type": "feat", "scope": "auth", "subject": "lock accounts after failures",
                    "body": "Stop brute force attempts.", "footer": ""},
        "alternatives": [
            {"type": "fix", "scope": "auth", "subject": "refuse locked accounts"},
            {"type": "refactor", "scope": None, "subject": "tidy login flow"},
        ],
    }
)
PR_JSON = json.dumps(
    {
        "title": "Lock accounts after failures",
        "summary": "Adds lockout.",
        "changes": ["Lock accounts"],
        "testing": ["Unit tests"],
        "risks": ["Users may get locked out"],
    }
)


class ScriptedClient:
    """Returns the queued replies in order (the last one repeats)."""

    backend = "fake"
    model = "fake-model"

    def __init__(self, *replies, error=None):
        self.replies = list(replies)
        self.error = error
        self.calls = []
        self.last_finish_reason = "stop"
        self.last_attempts = 1

    def complete(self, system, user, temperature=0.3, max_tokens=1024):
        self.calls.append({"system": system, "user": user, "temperature": temperature})
        if self.error is not None:
            raise self.error
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


@pytest.fixture
def use_client(monkeypatch):
    """Make every backend lookup return the given fake client."""

    def install(client):
        monkeypatch.setattr(LLMClient, "from_config", lambda config, timeout=60.0: client)
        return client

    return install


@pytest.fixture
def repo(git_repo, monkeypatch):
    r = git_repo
    r.commit("chore: init", "src/auth.py", "def login(user):\n    return token(user)\n")
    monkeypatch.chdir(r.path)
    return r


@pytest.fixture
def staged(repo):
    repo.write("src/auth.py", "def login(user):\n    check(user)\n    return token(user)\n")
    repo.git("add", "-A")
    return repo


def last_message(r) -> str:
    return r.git("log", "-1", "--format=%B").strip()


def commit_count(r) -> int:
    return int(r.git("rev-list", "--count", "HEAD").strip())


# --------------------------------------------------------------------------- #
# global options
# --------------------------------------------------------------------------- #


def test_version():
    result = runner.invoke(cli.app, ["--version"])
    assert result.exit_code == 0
    assert f"aicommit {__version__}" in result.output


def test_help_mentions_every_command():
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for word in ("pr", "changelog", "hook", "prepare", "init", "--verbose"):
        assert word in result.output


def test_outside_a_repository(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, ["-y"])
    assert result.exit_code == 1
    assert "not inside a git repository" in result.output


def test_invalid_config_is_reported(staged):
    (staged.path / ".aicommit.yaml").write_text("backend: banana\n", encoding="utf-8")
    result = runner.invoke(cli.app, ["-y"])
    assert result.exit_code == 1
    assert "backend must be" in result.output


# --------------------------------------------------------------------------- #
# the interactive commit loop
# --------------------------------------------------------------------------- #


def test_nothing_staged(repo, use_client):
    use_client(ScriptedClient(COMMIT_JSON))
    result = runner.invoke(cli.app, [])
    assert result.exit_code == 0
    assert "Nothing staged" in result.output


def test_missing_key_explains_how_to_get_one(staged):
    result = runner.invoke(cli.app, ["-y"])  # default backend nim, no key set
    assert result.exit_code == 1
    assert "build.nvidia.com" in result.output
    assert "--backend local" in result.output
    assert commit_count(staged) == 1


def test_accept_commits_the_proposal(staged, use_client):
    client = use_client(ScriptedClient(COMMIT_JSON))
    result = runner.invoke(cli.app, [], input="a\n")
    assert result.exit_code == 0, result.output
    assert "valid Conventional Commit" in result.output
    assert "committed" in result.output
    assert last_message(staged) == (
        "feat(auth): lock accounts after failures\n\nStop brute force attempts."
    )
    # The staged diff reached the prompt.
    assert "check(user)" in client.calls[0]["user"]


def test_yes_skips_the_prompt(staged, use_client):
    use_client(ScriptedClient(COMMIT_JSON))
    result = runner.invoke(cli.app, ["-y"])
    assert result.exit_code == 0, result.output
    assert last_message(staged).startswith("feat(auth): lock accounts after failures")


def test_pick_an_alternative(staged, use_client):
    use_client(ScriptedClient(COMMIT_JSON))
    result = runner.invoke(cli.app, [], input="2\na\n")
    assert result.exit_code == 0, result.output
    assert last_message(staged) == "fix(auth): refuse locked accounts"


def test_bad_alternative_number_and_unknown_action(staged, use_client):
    use_client(ScriptedClient(COMMIT_JSON))
    result = runner.invoke(cli.app, [], input="9\nzzz\na\n")
    assert result.exit_code == 0, result.output
    assert "no alternative with that number" in result.output
    assert "unrecognized action" in result.output


def test_edit_uses_the_editor_text(staged, use_client, monkeypatch):
    use_client(ScriptedClient(COMMIT_JSON))
    monkeypatch.setattr(cli.click, "edit", lambda text: "fix(auth): handle lockout edge case\n")
    result = runner.invoke(cli.app, [], input="e\na\n")
    assert result.exit_code == 0, result.output
    assert last_message(staged) == "fix(auth): handle lockout edge case"


def test_edit_cancelled_keeps_the_proposal(staged, use_client, monkeypatch):
    use_client(ScriptedClient(COMMIT_JSON))
    monkeypatch.setattr(cli.click, "edit", lambda text: None)
    result = runner.invoke(cli.app, [], input="e\na\n")
    assert result.exit_code == 0, result.output
    assert last_message(staged).startswith("feat(auth): lock accounts")


def test_edited_invalid_message_is_flagged(staged, use_client, monkeypatch):
    use_client(ScriptedClient(COMMIT_JSON))
    monkeypatch.setattr(cli.click, "edit", lambda text: "just some words\n")
    result = runner.invoke(cli.app, ["--dry-run"], input="e\na\n")
    assert result.exit_code == 0, result.output
    assert "does not follow the Conventional Commits format" in result.output


def test_regenerate_raises_temperature_and_uses_new_reply(staged, use_client):
    second = json.dumps({"primary": {"type": "fix", "subject": "stop brute force logins"}})
    client = use_client(ScriptedClient(COMMIT_JSON, second))
    result = runner.invoke(cli.app, [], input="r\na\n")
    assert result.exit_code == 0, result.output
    assert len(client.calls) == 2
    assert client.calls[1]["temperature"] > client.calls[0]["temperature"]
    assert last_message(staged) == "fix: stop brute force logins"


def test_regenerate_failure_keeps_the_loop_alive(staged, use_client):
    class FailsSecond(ScriptedClient):
        def complete(self, system, user, temperature=0.3, max_tokens=1024):
            if self.calls:
                self.calls.append({})
                raise LLMError("rate limited [429]")
            return super().complete(system, user, temperature, max_tokens)

    use_client(FailsSecond(COMMIT_JSON))
    result = runner.invoke(cli.app, [], input="r\na\n")
    assert result.exit_code == 0, result.output
    assert "rate limited [429]" in result.output  # printed literally, not as markup
    assert last_message(staged).startswith("feat(auth): lock accounts")


def test_quit_commits_nothing(staged, use_client):
    use_client(ScriptedClient(COMMIT_JSON))
    result = runner.invoke(cli.app, [], input="q\n")
    assert result.exit_code == 1
    assert "Nothing was committed" in result.output
    assert commit_count(staged) == 1


def test_dry_run_prints_but_does_not_commit(staged, use_client):
    use_client(ScriptedClient(COMMIT_JSON))
    result = runner.invoke(cli.app, ["-y", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "--dry-run: not committing" in result.output
    assert commit_count(staged) == 1


def test_all_flag_stages_first(repo, use_client):
    use_client(ScriptedClient(COMMIT_JSON))
    repo.write("src/auth.py", "def login(user):\n    return None\n")  # not staged
    result = runner.invoke(cli.app, ["-a", "-y"])
    assert result.exit_code == 0, result.output
    assert commit_count(repo) == 2


def test_generation_error_is_clean(staged, use_client):
    use_client(ScriptedClient("", error=LLMError("Could not reach Ollama at http://x/v1.")))
    result = runner.invoke(cli.app, ["-y"])
    assert result.exit_code == 1
    assert "error: Could not reach Ollama" in result.output
    assert "Traceback" not in result.output


def test_empty_reply_is_a_clean_error(staged, use_client):
    use_client(ScriptedClient(""))
    result = runner.invoke(cli.app, ["-y"])
    assert result.exit_code == 1
    assert "model returned an empty reply" in result.output
    assert "Traceback" not in result.output


def test_emoji_flag_prefixes_and_still_validates(staged, use_client):
    use_client(ScriptedClient(COMMIT_JSON))
    result = runner.invoke(cli.app, ["--emoji", "-y"])
    assert result.exit_code == 0, result.output
    assert "valid Conventional Commit" in result.output
    assert last_message(staged).startswith("✨ feat(auth):")


def test_hint_and_language_reach_the_prompt(staged, use_client):
    client = use_client(ScriptedClient(COMMIT_JSON))
    result = runner.invoke(cli.app, ["-y", "--dry-run", "--hint", "closes #212", "-l", "es"])
    assert result.exit_code == 0, result.output
    assert "closes #212" in client.calls[0]["user"]
    assert "Spanish" in client.calls[0]["system"]


def test_commit_failure_is_reported(staged, use_client):
    use_client(ScriptedClient(COMMIT_JSON))
    hook = staged.path / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho blocked >&2\nexit 1\n", encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    result = runner.invoke(cli.app, ["-y"])
    assert result.exit_code == 1
    assert "git commit" in result.output and "failed" in result.output


def test_verbose_traces_to_stderr(staged, use_client):
    raw = '<think>{x}</think>{"primary": {"type": "Feature", "subject": "Added lockout."}}'
    use_client(ScriptedClient(raw))
    result = runner.invoke(cli.app, ["-v", "-y", "--dry-run"])
    assert result.exit_code == 0, result.output
    err = result.stderr
    assert "diff stage 'full'" in err
    assert "prompt -> fake/fake-model" in err
    assert "raw reply:" in err and "<think>" in err
    assert "normalized: type 'feature' -> 'feat'" in err
    # stdout stays clean of the trace
    assert "raw reply" not in result.stdout
    assert "feat: add lockout" in result.stdout


def test_verbose_reports_the_corrective_retry(staged, use_client):
    bad = json.dumps({"primary": {"type": "banana", "subject": "do it"}})
    good = json.dumps({"primary": {"type": "fix", "subject": "do it"}})
    use_client(ScriptedClient(bad, good))
    result = runner.invoke(cli.app, ["-v", "-y", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "model calls: 2" in result.stderr
    assert "fix: do it" in result.stdout


# --------------------------------------------------------------------------- #
# prepare (the hook entry point)
# --------------------------------------------------------------------------- #


def test_prepare_prints_one_message(staged, use_client):
    use_client(ScriptedClient(COMMIT_JSON))
    result = runner.invoke(cli.app, ["prepare"])
    assert result.exit_code == 0
    assert result.stdout.startswith("feat(auth): lock accounts after failures\n")


def test_prepare_is_silent_on_failure_without_fallback(staged, use_client, monkeypatch):
    monkeypatch.setenv("AICOMMIT_HOOK_FALLBACK", "0")
    use_client(ScriptedClient("", error=LLMError("down")))
    result = runner.invoke(cli.app, ["prepare"])
    assert result.exit_code == 0
    assert result.stdout == ""


def test_prepare_falls_back_when_the_model_fails(staged, use_client):
    use_client(ScriptedClient("", error=LLMError("down")))
    result = runner.invoke(cli.app, ["prepare"])
    assert result.exit_code == 0
    assert result.stdout.startswith("feat(auth): update login")  # rule-based draft


def test_prepare_with_nothing_staged_prints_nothing(repo):
    result = runner.invoke(cli.app, ["prepare"])
    assert result.exit_code == 0
    assert result.stdout == ""


def test_prepare_with_broken_config_prints_nothing(staged):
    (staged.path / ".aicommit.yaml").write_text("backend: banana\n", encoding="utf-8")
    result = runner.invoke(cli.app, ["prepare"])
    assert result.exit_code == 0
    assert result.stdout == ""


# --------------------------------------------------------------------------- #
# pr
# --------------------------------------------------------------------------- #


@pytest.fixture
def branch(repo):
    repo.git("checkout", "-q", "-b", "feature/lockout")
    repo.commit("feat(auth): lock accounts", "src/auth.py", "def login(user):\n    lock(user)\n")
    return repo


def test_pr_to_stdout(branch, use_client):
    client = use_client(ScriptedClient(PR_JSON))
    result = runner.invoke(cli.app, ["pr", "--base", "main"])
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("# Lock accounts after failures\n\n## Summary")
    assert "- Users may get locked out" in result.stdout
    assert "feat(auth): lock accounts" in client.calls[0]["user"]


def test_pr_to_file(branch, use_client):
    use_client(ScriptedClient(PR_JSON))
    result = runner.invoke(cli.app, ["pr", "--base", "main", "-o", "pr.md"])
    assert result.exit_code == 0, result.output
    text = (branch.path / "pr.md").read_text(encoding="utf-8")
    assert text.startswith("# Lock accounts after failures")
    assert "wrote PR description" in result.output


def test_pr_with_no_commits_never_calls_the_model(repo, use_client):
    client = use_client(ScriptedClient(PR_JSON))
    result = runner.invoke(cli.app, ["pr", "--base", "main"])
    assert result.exit_code == 1
    assert "no commits" in result.output
    assert client.calls == []


def test_pr_unknown_base(branch, use_client):
    use_client(ScriptedClient(PR_JSON))
    result = runner.invoke(cli.app, ["pr", "--base", "develop"])
    assert result.exit_code == 1
    assert "does not exist" in result.output


def test_pr_model_error(branch, use_client):
    use_client(ScriptedClient("", error=LLMError("nim returned HTTP 500")))
    result = runner.invoke(cli.app, ["pr"])
    assert result.exit_code == 1
    assert "HTTP 500" in result.output


def test_pr_verbose(branch, use_client):
    use_client(ScriptedClient(PR_JSON))
    result = runner.invoke(cli.app, ["-v", "pr"])
    assert result.exit_code == 0, result.output
    assert "pr: 1 commits on feature/lockout vs main" in result.stderr


# --------------------------------------------------------------------------- #
# changelog
# --------------------------------------------------------------------------- #


@pytest.fixture
def history(repo):
    repo.git("tag", "v1.0.0")
    repo.commit("feat(api): add users endpoint", "api.py", "1")
    repo.commit("fix: guard nulls", "api.py", "2")
    repo.commit("docs: explain setup", "README.md", "x")
    return repo


def test_changelog_since_latest_tag(history):
    result = runner.invoke(cli.app, ["changelog"])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert out.startswith("## [Unreleased]\n")
    assert "- **api:** add users endpoint" in out
    assert "- guard nulls" in out
    assert "explain setup" not in out


def test_changelog_version_and_internal(history):
    result = runner.invoke(cli.app, ["changelog", "--version", "1.1.0", "--include-internal"])
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("## [1.1.0] - ")
    assert "explain setup" in result.stdout


def test_changelog_full_to_file(history):
    result = runner.invoke(cli.app, ["changelog", "--full", "-o", "CHANGELOG.md"])
    assert result.exit_code == 0, result.output
    text = (history.path / "CHANGELOG.md").read_text(encoding="utf-8")
    assert text.startswith("# Changelog")
    assert "## [Unreleased]" in text and "## [v1.0.0] - " in text


def test_changelog_polish_uses_the_model(history, use_client):
    client = use_client(ScriptedClient(json.dumps({"entries": ["Users endpoint", "Null safety"]})))
    result = runner.invoke(cli.app, ["changelog", "--polish"])
    assert result.exit_code == 0, result.output
    assert "- **api:** Users endpoint" in result.stdout
    assert "- Null safety" in result.stdout
    assert len(client.calls) == 1


def test_changelog_bad_ref(history):
    result = runner.invoke(cli.app, ["changelog", "--from", "v0.0.0-missing"])
    assert result.exit_code == 1
    assert "error:" in result.output


# --------------------------------------------------------------------------- #
# hook and init
# --------------------------------------------------------------------------- #


def test_hook_lifecycle(repo):
    result = runner.invoke(cli.app, ["hook", "status"])
    assert "no prepare-commit-msg hook installed" in result.output

    result = runner.invoke(cli.app, ["hook", "install"])
    assert result.exit_code == 0, result.output
    assert "Installed prepare-commit-msg hook" in result.output

    result = runner.invoke(cli.app, ["hook", "status"])
    assert "installed" in result.output and "managed by aicommit" in result.output
    assert "-m aicommit prepare" in result.output

    result = runner.invoke(cli.app, ["hook", "uninstall"])
    assert result.exit_code == 0
    assert "Removed aicommit hook" in result.output


def test_hook_status_under_hooks_path_and_foreign_hook(repo):
    repo.git("config", "core.hooksPath", ".husky")
    hook = repo.path / ".husky" / "prepare-commit-msg"
    hook.parent.mkdir()
    hook.write_text("#!/bin/sh\necho theirs\n", encoding="utf-8")
    result = runner.invoke(cli.app, ["hook", "status"])
    assert "not managed by aicommit" in result.output
    assert "core.hooksPath = .husky" in result.output


def test_hook_status_warns_when_interpreter_is_gone(repo):
    from aicommit.hook import install

    install(python="/nowhere/python3")
    result = runner.invoke(cli.app, ["hook", "status"])
    assert "recorded interpreter is gone" in result.output


def test_hook_install_refuses_to_clobber_an_old_backup(repo):
    hooks = repo.path / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "prepare-commit-msg").write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    (hooks / "prepare-commit-msg.aicommit.bak").write_text("old", encoding="utf-8")
    result = runner.invoke(cli.app, ["hook", "install"])
    assert result.exit_code == 1
    assert "backup already exists" in result.output
    result = runner.invoke(cli.app, ["hook", "install", "--force"])
    assert result.exit_code == 0, result.output


def test_init_writes_config_and_respects_force(repo):
    result = runner.invoke(cli.app, ["init"])
    assert result.exit_code == 0, result.output
    text = (repo.path / ".aicommit.yaml").read_text(encoding="utf-8")
    assert "backend: nim" in text and "local" in text and "hook_fallback" in text
    result = runner.invoke(cli.app, ["init"])
    assert result.exit_code == 1
    assert "already exists" in result.output
    result = runner.invoke(cli.app, ["init", "--force"])
    assert result.exit_code == 0
