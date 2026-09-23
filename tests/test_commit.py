"""Conventional Commit formatting, parsing, validation and generation."""

from __future__ import annotations

import json

import pytest

from aicommit.commit import (
    CommitMessage,
    generate_commit,
    normalize_commit,
    parse_commit,
    shorten_subject,
    strip_leading_emoji,
    validate_commit,
)
from aicommit.config import Config
from aicommit.llm import LLMError


class SequenceClient:
    """Fake client that returns a different canned reply per call."""

    def __init__(self, *replies: str, finish_reason=None):
        self.replies = list(replies)
        self.calls = []
        self.last_finish_reason = finish_reason

    def complete(self, system, user, temperature=0.3, max_tokens=1024):
        self.calls.append({"system": system, "user": user, "temperature": temperature})
        if len(self.replies) > 1:
            return self.replies.pop(0)
        return self.replies[0]


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


# --- the audit's failing replies (every one used to crash or mislead) ------- #


def test_empty_reply_is_a_clean_llm_error_not_index_error(fake_client):
    with pytest.raises(LLMError, match="empty reply"):
        generate_commit("<diff>", Config(), fake_client(""))
    with pytest.raises(LLMError, match="empty reply"):
        generate_commit("<diff>", Config(), fake_client("   \n  "))


def test_empty_reply_at_token_limit_says_so():
    client = SequenceClient("", finish_reason="length")
    with pytest.raises(LLMError, match="token limit"):
        generate_commit("<diff>", Config(), client)


def test_reasoning_model_output_is_parsed(fake_client):
    raw = (
        "<think>\nThe diff adds {a cache}...\n</think>\n"
        '{"primary": {"type": "feat", "scope": "cache", "subject": "add lru cache"}}'
    )
    result = generate_commit("<diff>", Config(), fake_client(raw))
    assert result.best.format() == "feat(cache): add lru cache\n"


def test_reply_that_is_only_reasoning_is_an_error(fake_client):
    with pytest.raises(LLMError, match="reasoning"):
        generate_commit("<diff>", Config(), fake_client("<think>thinking forever"))


def test_prose_with_braces_around_json(fake_client):
    raw = (
        'Here you go: {"primary": {"type": "fix", "subject": "guard nulls"}}\n'
        "Note: {alternatives omitted}"
    )
    result = generate_commit("<diff>", Config(), fake_client(raw))
    assert result.best.format() == "fix: guard nulls\n"


@pytest.mark.parametrize(
    "value, expected",
    [("false", False), ("False", False), ("no", False), (0, False), (None, False),
     ("true", True), (True, True), (1, True)],
)
def test_breaking_flag_is_coerced_properly(fake_client, value, expected):
    raw = json.dumps({"primary": {"type": "feat", "subject": "add x", "breaking": value}})
    result = generate_commit("<diff>", Config(), fake_client(raw))
    assert result.best.breaking is expected
    assert ("BREAKING CHANGE" in result.best.format()) is expected


def test_small_model_drift_is_repaired_without_a_second_call():
    raw = json.dumps(
        {
            "primary": {
                "type": "Feature",
                "scope": "",
                "subject": "Added a new caching layer to the repository lookups "
                "so that repeated queries are faster.",
            }
        }
    )
    client = SequenceClient(raw)
    result = generate_commit("<diff>", Config(), client)
    assert validate_commit(result.best, Config()) == []
    assert result.best.format() == "feat: add a new caching layer to the repository lookups\n"
    assert len(client.calls) == 1
    assert any("type" in note for note in result.notes)


def test_invalid_answer_triggers_exactly_one_corrective_call():
    bad = json.dumps({"primary": {"type": "banana", "subject": "do things"}})
    good = json.dumps({"primary": {"type": "fix", "subject": "handle empty input"}})
    client = SequenceClient(bad, good)
    result = generate_commit("<diff>", Config(), client)
    assert len(client.calls) == 2
    assert result.attempts == 2
    assert result.best.header() == "fix: handle empty input"
    # The model was told exactly what was wrong.
    assert "banana" in client.calls[1]["user"]
    assert "Your previous answer" in client.calls[1]["user"]
    assert client.calls[1]["temperature"] <= 0.2


def test_valid_answer_makes_no_corrective_call():
    good = json.dumps({"primary": {"type": "fix", "subject": "handle empty input"}})
    client = SequenceClient(good)
    generate_commit("<diff>", Config(), client)
    assert len(client.calls) == 1


def test_corrective_call_that_still_fails_keeps_best_effort():
    bad = json.dumps({"primary": {"type": "banana", "subject": "do things"}})
    client = SequenceClient(bad, bad)
    result = generate_commit("<diff>", Config(), client)
    assert len(client.calls) == 2
    assert result.best.subject == "do things"


def test_corrective_call_failure_is_not_fatal():
    class Flaky(SequenceClient):
        def complete(self, system, user, temperature=0.3, max_tokens=1024):
            if self.calls:
                raise LLMError("rate limited")
            return super().complete(system, user, temperature, max_tokens)

    bad = json.dumps({"primary": {"type": "banana", "subject": "do things"}})
    result = generate_commit("<diff>", Config(), Flaky(bad))
    assert result.best.subject == "do things"
    assert any("repair call failed" in note for note in result.notes)


def test_repair_can_be_disabled():
    bad = json.dumps({"primary": {"type": "banana", "subject": "do things"}})
    client = SequenceClient(bad)
    generate_commit("<diff>", Config(), client, repair=False)
    assert len(client.calls) == 1


def test_prose_reply_is_retried_and_then_falls_back():
    client = SequenceClient("I think this diff improves the cache.", "Sorry, no JSON today.")
    result = generate_commit("<diff>", Config(), client)
    assert len(client.calls) == 2
    assert result.best.type == "chore"
    assert result.best.subject


def test_header_is_found_on_any_line_of_prose(fake_client):
    raw = "Sure! Here's a commit:\n- **fix(api): handle timeouts**\n\nRetry once on 504."
    client = fake_client(raw)
    result = generate_commit("<diff>", Config(), client)
    assert result.best.header() == "fix(api): handle timeouts"
    assert len(client.calls) == 1


def test_alternatives_are_normalized_and_deduplicated(fake_client):
    raw = json.dumps(
        {
            "primary": {"type": "feat", "subject": "add search"},
            "alternatives": [
                {"type": "Feature", "subject": "Add search."},  # duplicate after cleanup
                {"type": "perf", "scope": "Search Index", "subject": "speed up lookups"},
                {"type": "fix", "subject": ""},  # dropped: no subject
            ],
        }
    )
    result = generate_commit("<diff>", Config(), fake_client(raw))
    headers = [m.header() for m in result.all]
    assert headers == ["feat: add search", "perf(search-index): speed up lookups"]


def test_string_primary_and_message_key_are_understood(fake_client):
    raw = json.dumps({"primary": "docs(readme): explain setup", "alternatives": ["chore: tidy"]})
    result = generate_commit("<diff>", Config(), fake_client(raw))
    assert result.best.header() == "docs(readme): explain setup"
    assert result.alternatives[0].header() == "chore: tidy"
    raw2 = json.dumps({"message": "fix: guard nulls", "body": ["line one", "line two"]})
    result2 = generate_commit("<diff>", Config(), fake_client(raw2))
    assert result2.best.header() == "fix: guard nulls"
    assert result2.best.body == "line one\nline two"


# --- normalization ------------------------------------------------------------ #


def _norm(**fields) -> CommitMessage:
    base = {"type": "feat", "subject": "add x"}
    base.update(fields)
    return normalize_commit(CommitMessage(**base), Config())


@pytest.mark.parametrize(
    "raw, expected",
    [("Feature", "feat"), ("bugfix", "fix"), ("doc", "docs"), ("tests", "test"),
     ("performance", "perf"), ("FIX", "fix"), ("Refactoring", "refactor"),
     ("deps", "build"), ("ci/cd", "ci")],
)
def test_normalize_maps_type_synonyms(raw, expected):
    assert _norm(type=raw).type == expected


def test_normalize_splits_scope_and_bang_out_of_type():
    msg = _norm(type="feat(api)!")
    assert (msg.type, msg.scope, msg.breaking) == ("feat", "api", True)


def test_normalize_does_not_map_to_disallowed_type():
    cfg = Config(allowed_types=["fix", "chore"])
    msg = normalize_commit(CommitMessage(type="feature", subject="add x"), cfg)
    assert msg.type == "feature"  # left for validation / the corrective call


@pytest.mark.parametrize(
    "scope, expected",
    [("", None), ("null", None), ("None", None), ("  ", None), ("API", "api"),
     ("Search Index", "search-index"), ("`cli`", "cli")],
)
def test_normalize_cleans_scope(scope, expected):
    assert _norm(scope=scope).scope == expected


@pytest.mark.parametrize(
    "subject, expected",
    [
        ("Added caching.", "add caching"),
        ("Fixes the null check", "fix the null check"),
        ("Raised AccountLocked for locked users.", "raise AccountLocked for locked users"),
        ("Using httpx transports in tests", "use httpx transports in tests"),
        ("feat: add search", "add search"),
        ("✨ add search", "add search"),
        ('"add search"', "add search"),
        ("API: add retry", "API: add retry"),
        ("Update README links", "update README links"),
        ("GitHub actions cache", "GitHub actions cache"),
    ],
)
def test_normalize_subject(subject, expected):
    assert _norm(subject=subject).subject == expected


def test_normalize_keeps_spanish_verbs():
    cfg = Config(language="es")
    msg = normalize_commit(CommitMessage(type="feat", subject="Añade caché."), cfg)
    assert msg.subject == "añade caché"


def test_shorten_subject_prefers_clause_boundaries():
    long = "add a caching layer to repository lookups, so that repeated queries are fast"
    assert shorten_subject(long, 50) == "add a caching layer to repository lookups"


def test_shorten_subject_drops_dangling_words():
    long = "add support for exporting reports to the new storage backend for teams"
    out = shorten_subject(long, 40)
    assert len(out) <= 40
    assert not out.endswith((" the", " to", " for"))
    assert out.startswith("add support for exporting")


def test_shorten_subject_hard_cuts_one_long_word():
    assert shorten_subject("x" * 100, 30) == "x" * 30


# --- emoji-prefixed headers ------------------------------------------------------ #


def test_strip_leading_emoji():
    assert strip_leading_emoji("✨ feat(api): add x") == "feat(api): add x"
    assert strip_leading_emoji("♻️ refactor: tidy") == "refactor: tidy"
    assert strip_leading_emoji(":sparkles: feat: add x") == "feat: add x"
    assert strip_leading_emoji("feat: add x") == "feat: add x"


def test_parse_commit_accepts_gitmoji_prefix():
    msg = parse_commit("\U0001f41b fix(core): guard nulls\n\nBody.")
    assert msg is not None
    assert (msg.type, msg.scope, msg.subject, msg.body) == ("fix", "core", "guard nulls", "Body.")
    assert parse_commit(":bug: fix: guard nulls").type == "fix"


def test_emoji_format_round_trips_through_parse():
    for type_ in ["feat", "fix", "docs", "refactor", "perf", "test", "revert"]:
        text = CommitMessage(type=type_, subject="do the thing").format(emoji=True)
        parsed = parse_commit(text)
        assert parsed is not None and parsed.type == type_
