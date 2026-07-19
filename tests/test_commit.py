"""Conventional Commit formatting, parsing, validation and generation."""

from __future__ import annotations

import json

from aicommit.commit import (
    CommitMessage,
    generate_commit,
    parse_commit,
    validate_commit,
)
from aicommit.config import Config


# --- formatting ------------------------------------------------------------ #


def test_format_header_only():
    msg = CommitMessage(type="feat", subject="add pr command", scope="cli")
    assert msg.format() == "feat(cli): add pr command\n"


def test_format_with_body_and_footer():
    msg = CommitMessage(
        type="fix",
        subject="handle empty diff",
        body="Return early when nothing is staged.",
        footer="Closes #42",
    )
    out = msg.format()
    assert out.splitlines()[0] == "fix: handle empty diff"
    assert "Return early when nothing is staged." in out
    assert "Closes #42" in out


def test_format_emoji_prefix():
    msg = CommitMessage(type="feat", subject="ship it")
    assert msg.format(emoji=True).startswith("✨ feat: ship it")


def test_format_breaking_adds_footer_note():
    msg = CommitMessage(type="feat", subject="drop py38", breaking=True)
    out = msg.format()
    assert "feat!: drop py38" in out
    assert "BREAKING CHANGE" in out


# --- parsing --------------------------------------------------------------- #


def test_parse_with_scope():
    msg = parse_commit("feat(api): add users endpoint")
    assert msg is not None
    assert msg.type == "feat"
    assert msg.scope == "api"
    assert msg.subject == "add users endpoint"


def test_parse_without_scope():
    msg = parse_commit("fix: correct off-by-one")
    assert msg.scope is None
    assert msg.type == "fix"


def test_parse_breaking_bang():
    msg = parse_commit("refactor!: rename public API")
    assert msg.breaking is True


def test_parse_body_and_footer():
    text = (
        "feat(db): add migration runner\n"
        "\n"
        "Runs pending migrations on startup.\n"
        "\n"
        "BREAKING CHANGE: requires a schema reset\n"
    )
    msg = parse_commit(text)
    assert msg.body == "Runs pending migrations on startup."
    assert "BREAKING CHANGE" in msg.footer
    assert msg.breaking is True


def test_parse_invalid_returns_none():
    assert parse_commit("just some words") is None
    assert parse_commit("") is None


# --- validation ------------------------------------------------------------ #


def test_validate_clean_message():
    cfg = Config(allowed_types=["feat", "fix"], subject_max_length=72)
    msg = parse_commit("feat: add caching layer")
    assert validate_commit(msg, cfg) == []


def test_validate_disallowed_type():
    cfg = Config(allowed_types=["feat", "fix"])
    msg = parse_commit("docs: update readme")
    issues = validate_commit(msg, cfg)
    assert any("not one of the allowed types" in i for i in issues)


def test_validate_subject_too_long():
    cfg = Config(subject_max_length=20)
    msg = parse_commit("feat: " + "x" * 40)
    issues = validate_commit(msg, cfg)
    assert any("over the" in i for i in issues)


def test_validate_trailing_period_and_mood():
    cfg = Config()
    msg = parse_commit("fix: Fixed the parser.")
    issues = validate_commit(msg, cfg)
    assert any("period" in i for i in issues)
    assert any("imperative" in i for i in issues)


# --- generation (LLM mocked) ----------------------------------------------- #


def test_generate_commit_parses_primary_and_alternatives(fake_client):
    payload = json.dumps(
        {
            "primary": {
                "type": "feat",
                "scope": "cli",
                "subject": "add pr subcommand",
                "body": "Generate PR descriptions from the branch diff.",
                "footer": "",
            },
            "alternatives": [
                {
                    "type": "feat",
                    "scope": None,
                    "subject": "support pull request generation",
                    "body": "",
                    "footer": "",
                }
            ],
        }
    )
    client = fake_client(payload)
    result = generate_commit("<diff>", Config(), client)

    assert result.best.type == "feat"
    assert result.best.scope == "cli"
    assert result.best.subject == "add pr subcommand"
    assert len(result.alternatives) == 1
    assert result.alternatives[0].subject == "support pull request generation"
    assert len(result.all) == 2


def test_generate_commit_handles_fenced_json(fake_client):
    payload = (
        "```json\n"
        + json.dumps({"primary": {"type": "fix", "subject": "guard nulls"}})
        + "\n```"
    )
    result = generate_commit("<diff>", Config(), fake_client(payload))
    assert result.best.type == "fix"
    assert result.best.subject == "guard nulls"


def test_generate_commit_falls_back_on_plain_text(fake_client):
    # Model ignored the JSON instruction and returned a bare commit line.
    result = generate_commit("<diff>", Config(), fake_client("chore: bump deps"))
    assert result.best.type == "chore"
    assert result.best.subject == "bump deps"
