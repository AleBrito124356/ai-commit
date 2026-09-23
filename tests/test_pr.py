"""PR description: git context collection, generation and rendering."""

from __future__ import annotations

import json

import pytest

from aicommit.config import Config
from aicommit.gitutil import GitError
from aicommit.pr import (
    PRResult,
    collect_commits,
    collect_pr_context,
    diff_vs_base,
    generate_pr,
    render_pr,
    resolve_base,
)

PR_JSON = json.dumps(
    {
        "title": "Add payment retries",
        "summary": "Retries failed payments with backoff.",
        "changes": ["Retry failed payments", "Log each attempt"],
        "testing": ["Unit tests for the retry policy"],
        "risks": [],
    }
)


@pytest.fixture
def feature_repo(git_repo):
    r = git_repo
    r.commit("chore: initial", "README.md", "# shop\n")
    r.git("checkout", "-q", "-b", "feature/retries")
    r.commit("feat(payments): retry failed payments", "src/payments.py", "def pay():\n    retry()\n")
    r.commit("test(payments): cover retries", "tests/test_payments.py", "def test_pay():\n    pass\n")
    return r


def test_collect_commits_and_diff(feature_repo):
    r = feature_repo
    assert collect_commits("main", cwd=r.path) == [
        "test(payments): cover retries",
        "feat(payments): retry failed payments",
    ]
    diff = diff_vs_base("main", cwd=r.path)
    assert "src/payments.py" in diff and "tests/test_payments.py" in diff


def test_generate_pr_with_fake_model(feature_repo, fake_client):
    r = feature_repo
    client = fake_client(PR_JSON)
    result = generate_pr(base="main", config=Config(), client=client, cwd=r.path)
    assert result.title == "Add payment retries"
    assert result.changes == ["Retry failed payments", "Log each attempt"]
    prompt = client.calls[0]["user"]
    assert "Head: feature/retries" in prompt
    assert "feat(payments): retry failed payments" in prompt
    assert "retry()" in prompt


def test_generate_pr_parses_reasoning_and_prose(feature_repo, fake_client):
    raw = "<think>{hmm}</think>Here it is: " + PR_JSON + " {end}"
    result = generate_pr(base="main", config=Config(), client=fake_client(raw), cwd=feature_repo.path)
    assert result.title == "Add payment retries"


def test_no_commits_between_base_and_head_fails_before_calling_the_model(git_repo, fake_client):
    r = git_repo
    r.commit("feat: initial", "a.py", "1")
    client = fake_client(PR_JSON)
    with pytest.raises(GitError, match="no commits"):
        generate_pr(base="main", config=Config(), client=client, cwd=r.path)
    assert client.calls == []


def test_unknown_base_is_reported(feature_repo):
    with pytest.raises(GitError, match="does not exist"):
        collect_pr_context(base="develop", config=Config(), cwd=feature_repo.path)


def test_falls_back_to_origin_base(feature_repo):
    r = feature_repo
    main_sha = r.git("rev-parse", "main").strip()
    r.git("update-ref", "refs/remotes/origin/trunk", main_sha)
    assert resolve_base("trunk", cwd=r.path) == "origin/trunk"
    context = collect_pr_context(base="trunk", config=Config(), cwd=r.path)
    assert context.base == "origin/trunk"
    assert len(context.commits) == 2


def test_empty_title_falls_back_to_first_commit(feature_repo, fake_client):
    raw = json.dumps({"title": "", "summary": "s", "changes": [], "testing": [], "risks": []})
    result = generate_pr(base="main", config=Config(), client=fake_client(raw), cwd=feature_repo.path)
    # Newest commit first, conventional prefix dropped, sentence case.
    assert result.title == "Cover retries"
    # No changes from the model: list the branch's commit subjects instead.
    assert result.changes == [
        "test(payments): cover retries",
        "feat(payments): retry failed payments",
    ]


def test_context_uses_config_base_and_budget(feature_repo):
    cfg = Config(pr_base="main", max_diff_chars=300)
    context = collect_pr_context(config=cfg, cwd=feature_repo.path)
    assert context.base == "main"
    assert len(context.diff_view) <= 300
    assert context.head_name == "feature/retries"


def test_generate_pr_requires_config_and_client():
    with pytest.raises(ValueError):
        generate_pr(base="main")


def test_render_pr_sections_and_empty_lists():
    md = render_pr(PRResult(title="t", summary="Why.", changes=["a", " "], testing=[], risks=[]))
    assert md.startswith("## Summary\n\nWhy.")
    assert "## Changes\n\n- a\n" in md
    assert "## Testing\n\n_None._" in md
    assert "## Risks\n\n_None._" in md
    assert PRResult(title="t", summary="s").to_markdown() == render_pr(PRResult(title="t", summary="s"))
