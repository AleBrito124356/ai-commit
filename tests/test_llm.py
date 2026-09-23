"""The HTTP client (via httpx.MockTransport) and defensive reply parsing."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from aicommit import llm
from aicommit.config import Config
from aicommit.llm import (
    LLMClient,
    LLMError,
    _ollama_base_url,
    extract_json,
    iter_json_objects,
    parse_retry_after,
    strip_reasoning,
)

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _ok(content, finish_reason="stop"):
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]},
    )


def _client(handler, backend="nim", **kwargs) -> LLMClient:
    return LLMClient(
        backend=backend,
        model="test-model",
        base_url="http://llm.test/v1",
        api_key="nvapi-test" if backend == "nim" else "ollama",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


@pytest.fixture
def sleeps(monkeypatch):
    recorded = []
    monkeypatch.setattr(llm, "_sleep", recorded.append)
    return recorded


# --------------------------------------------------------------------------- #
# request shape and happy path
# --------------------------------------------------------------------------- #


def test_complete_sends_openai_payload_and_auth_header(sleeps):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return _ok("  hello  ")

    client = _client(handler)
    assert client.complete("SYS", "USER", temperature=0.1, max_tokens=77) == "hello"
    assert seen["url"] == "http://llm.test/v1/chat/completions"
    assert seen["auth"] == "Bearer nvapi-test"
    body = seen["body"]
    assert body["model"] == "test-model"
    assert body["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "USER"},
    ]
    assert body["temperature"] == 0.1
    assert body["max_tokens"] == 77
    assert body["stream"] is False
    assert client.last_attempts == 1
    assert sleeps == []


def test_null_content_returns_empty_and_records_finish_reason():
    client = _client(lambda request: _ok(None, finish_reason="length"))
    assert client.complete("s", "u") == ""
    assert client.last_finish_reason == "length"


def test_content_parts_are_joined():
    parts = [{"type": "text", "text": "feat: "}, {"type": "text", "text": "add x"}]
    client = _client(lambda request: _ok(parts))
    assert client.complete("s", "u") == "feat: add x"


def test_unexpected_shape_is_an_llm_error():
    client = _client(lambda request: httpx.Response(200, text="<html>proxy</html>"))
    with pytest.raises(LLMError, match="unexpected response shape"):
        client.complete("s", "u")


# --------------------------------------------------------------------------- #
# retries and backoff
# --------------------------------------------------------------------------- #


def test_429_then_200_succeeds_after_retry_after(sleeps):
    responses = [
        httpx.Response(429, headers={"Retry-After": "2"}, json={"error": "slow down"}),
        _ok("done"),
    ]
    client = _client(lambda request: responses.pop(0))
    assert client.complete("s", "u") == "done"
    assert sleeps == [2.0]
    assert client.last_attempts == 2


def test_retry_after_is_capped(sleeps):
    responses = [httpx.Response(503, headers={"Retry-After": "999"}), _ok("ok")]
    client = _client(lambda request: responses.pop(0))
    assert client.complete("s", "u") == "ok"
    assert sleeps == [llm.MAX_RETRY_WAIT]


def test_persistent_500_gives_up_with_exponential_backoff(sleeps):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500, json={"error": {"message": "overloaded"}})

    client = _client(handler)
    with pytest.raises(LLMError) as info:
        client.complete("s", "u")
    assert len(calls) == 4  # 1 try + 3 retries
    assert sleeps == [1.0, 2.0, 4.0]
    message = str(info.value)
    assert "HTTP 500" in message
    assert "after 4 attempts" in message
    assert "overloaded" in message


def test_persistent_429_explains_rate_limit(sleeps):
    client = _client(lambda request: httpx.Response(429, text="rate limited"))
    with pytest.raises(LLMError, match="rate limiting") as info:
        client.complete("s", "u")
    assert "free NIM tier" in str(info.value)
    assert len(sleeps) == 3


def test_remote_protocol_error_is_retried_then_reported(sleeps):
    def handler(request):
        raise httpx.RemoteProtocolError("peer closed connection", request=request)

    client = _client(handler)
    with pytest.raises(LLMError, match="reset") as info:
        client.complete("s", "u")
    assert "gave up after 4 attempts" in str(info.value)
    assert sleeps == [1.0, 2.0, 4.0]


def test_read_error_once_then_success(sleeps):
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            raise httpx.ReadError("connection reset by peer", request=request)
        return _ok("recovered")

    assert _client(handler).complete("s", "u") == "recovered"
    assert sleeps == [1.0]


def test_connect_error_fails_fast_with_ollama_hint(sleeps):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("refused", request=request)

    client = _client(handler, backend="ollama")
    with pytest.raises(LLMError, match="Could not reach Ollama") as info:
        client.complete("s", "u")
    assert "ollama serve" in str(info.value)
    assert len(calls) == 1 and sleeps == []


def test_timeout_is_an_llm_error(sleeps):
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(LLMError, match="timed out"):
        _client(handler).complete("s", "u")


def test_other_httpx_errors_do_not_escape_as_tracebacks(sleeps):
    def handler(request):
        raise httpx.UnsupportedProtocol("bad scheme", request=request)

    with pytest.raises(LLMError, match="UnsupportedProtocol"):
        _client(handler).complete("s", "u")


# --------------------------------------------------------------------------- #
# backend-specific error messages
# --------------------------------------------------------------------------- #


def test_401_on_nim_mentions_the_key():
    client = _client(lambda request: httpx.Response(401, text="unauthorized"))
    with pytest.raises(LLMError, match="NVIDIA_API_KEY"):
        client.complete("s", "u")


def test_401_on_ollama_does_not_blame_the_nvidia_key():
    client = _client(lambda request: httpx.Response(401, text="nope"), backend="ollama")
    with pytest.raises(LLMError) as info:
        client.complete("s", "u")
    assert "NVIDIA_API_KEY" not in str(info.value)
    assert "OLLAMA_HOST" in str(info.value)


def test_404_on_ollama_suggests_pulling_the_model():
    body = {"error": {"message": 'model "test-model" not found, try pulling it first'}}
    client = _client(lambda request: httpx.Response(404, json=body), backend="ollama")
    with pytest.raises(LLMError, match="ollama pull test-model") as info:
        client.complete("s", "u")
    assert "not found" in str(info.value)


def test_400_includes_the_error_detail():
    client = _client(lambda request: httpx.Response(400, json={"detail": "context too long"}))
    with pytest.raises(LLMError, match="HTTP 400: context too long"):
        client.complete("s", "u")


# --------------------------------------------------------------------------- #
# from_config and URL normalization
# --------------------------------------------------------------------------- #


def test_from_config_nim_requires_key(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    with pytest.raises(LLMError, match="build.nvidia.com"):
        LLMClient.from_config(Config(backend="nim"))


def test_from_config_nim_reads_env(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", " nvapi-abc ")
    monkeypatch.setenv("NIM_MODEL", "meta/other")
    monkeypatch.setenv("NIM_BASE_URL", "https://example.test/v1/")
    client = LLMClient.from_config(Config(backend="nim"))
    assert client.api_key == "nvapi-abc"
    assert client.model == "meta/other"
    assert client.base_url == "https://example.test/v1"
    # An explicit model in config wins over the env default.
    assert LLMClient.from_config(Config(backend="nim", model="x/y")).model == "x/y"


def test_from_config_ollama(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:9999")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3:8b")
    client = LLMClient.from_config(Config(backend="ollama"))
    assert client.backend == "ollama"
    assert client.base_url == "http://127.0.0.1:9999/v1"
    assert client.model == "qwen3:8b"


def test_from_config_unknown_backend():
    with pytest.raises(LLMError, match="unknown backend"):
        LLMClient.from_config(Config(backend="banana"))


@pytest.mark.parametrize(
    "host, expected",
    [
        ("", "http://localhost:11434/v1"),
        ("localhost:11434", "http://localhost:11434/v1"),
        ("http://gpu-box:11434/", "http://gpu-box:11434/v1"),
        ("https://ollama.internal/v1", "https://ollama.internal/v1"),
    ],
)
def test_ollama_base_url(host, expected):
    assert _ollama_base_url(host) == expected


def test_parse_retry_after():
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after("-3") == 0.0
    assert parse_retry_after("soon") is None
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    later = format_datetime(now + timedelta(seconds=90), usegmt=True)
    assert parse_retry_after(later, now=now) == pytest.approx(90.0)


# --------------------------------------------------------------------------- #
# reply parsing
# --------------------------------------------------------------------------- #


def test_strip_reasoning_variants():
    assert strip_reasoning("<think>hmm {x}</think>\nanswer") == "answer"
    assert strip_reasoning("<THINKING>a</THINKING>b") == "b"
    # Opening tag lived in the chat template; only the close is in the reply.
    assert strip_reasoning("the model mused...</think>\n{\"a\": 1}") == '{"a": 1}'
    # Cut off while still reasoning: nothing after the tag is an answer.
    assert strip_reasoning("<think>still going and going") == ""
    assert strip_reasoning("plain") == "plain"
    assert strip_reasoning("") == ""


def test_extract_json_plain_and_fenced():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('```\n{"a": 2}\n```') == {"a": 2}


def test_extract_json_ignores_reasoning_with_braces():
    raw = '<think>\nThe diff adds {a cache}...\n</think>\n{"primary": {"type": "feat"}}'
    assert extract_json(raw) == {"primary": {"type": "feat"}}


def test_extract_json_skips_prose_braces_on_both_sides():
    raw = 'Sure {really}: {"primary": {"subject": "x"}}\nNote: {alternatives omitted}'
    assert extract_json(raw) == {"primary": {"subject": "x"}}


def test_extract_json_is_string_aware():
    raw = 'Result: {"subject": "use {name} and } in templates", "type": "feat"} done'
    assert extract_json(raw)["subject"] == "use {name} and } in templates"


def test_extract_json_prefers_expected_keys():
    raw = 'meta {"confidence": 0.9} then {"primary": {"subject": "y"}}'
    assert extract_json(raw, expected_keys=("primary",)) == {"primary": {"subject": "y"}}
    assert extract_json(raw) == {"confidence": 0.9}


def test_extract_json_forgives_trailing_commas():
    assert extract_json('{"a": [1, 2,], "b": 3,}') == {"a": [1, 2], "b": 3}


def test_extract_json_errors():
    with pytest.raises(LLMError, match="did not return JSON"):
        extract_json("no json here")
    with pytest.raises(LLMError, match="valid JSON"):
        extract_json("broken {not: json")
    with pytest.raises(LLMError):
        extract_json("")


def test_iter_json_objects_yields_top_level_objects_in_order():
    text = '{"a": {"b": 1}} junk {bad} {"c": 2}'
    assert list(iter_json_objects(text)) == [{"a": {"b": 1}}, {"c": 2}]
