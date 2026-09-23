"""LLM backend: one OpenAI-compatible client for NVIDIA NIM or local Ollama.

Both backends speak the same ``/chat/completions`` shape, so a single tiny
``httpx`` client covers them. Swap between them with one config field
(``backend: nim`` or ``backend: ollama``) — the diff never leaves the backend
you choose, and with Ollama it never leaves your machine.

This module also owns the defensive parsing of model replies: reasoning blocks
(``<think>...</think>``) are stripped and the first JSON object that actually
parses is extracted with a string-aware brace scanner, so prose or stray braces
around the answer do not derail it.
"""

from __future__ import annotations

import email.utils
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, List, Optional

import httpx

from .config import Config

NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"
OLLAMA_BASE_URL = "http://localhost:11434/v1"

DEFAULT_NIM_MODEL = "meta/llama-3.3-70b-instruct"
DEFAULT_OLLAMA_MODEL = "llama3.1"

# HTTP statuses worth retrying: rate limiting and transient server trouble.
RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
# Never sleep longer than this between attempts, whatever Retry-After says.
MAX_RETRY_WAIT = 30.0

_SIGNUP_HINT = (
    "No NVIDIA_API_KEY found.\n"
    "  1. Create a free account at https://build.nvidia.com (takes ~2 minutes).\n"
    "  2. Generate an API key — it starts with 'nvapi-'.\n"
    "  3. Export it:  export NVIDIA_API_KEY=nvapi-...\n"
    "Or run fully local instead:  aicommit --backend ollama"
)

# Indirection so tests can patch the sleep without slowing the suite down.
_sleep = time.sleep


class LLMError(RuntimeError):
    """Any failure talking to the backend, with a user-facing message."""


@dataclass
class LLMClient:
    backend: str
    model: str
    base_url: str
    api_key: Optional[str]
    timeout: float = 60.0
    max_retries: int = 3
    backoff_base: float = 1.0
    # Optional httpx transport (e.g. ``httpx.MockTransport``) for offline tests.
    transport: Optional[httpx.BaseTransport] = field(default=None, repr=False)
    # ``finish_reason`` of the last reply, used to explain empty answers.
    last_finish_reason: Optional[str] = field(default=None, repr=False)
    last_attempts: int = field(default=0, repr=False)

    @classmethod
    def from_config(cls, config: Config, timeout: float = 60.0) -> "LLMClient":
        if config.backend == "nim":
            api_key = os.environ.get("NVIDIA_API_KEY", "").strip()
            if not api_key:
                raise LLMError(_SIGNUP_HINT)
            model = config.model or os.environ.get("NIM_MODEL") or DEFAULT_NIM_MODEL
            base_url = os.environ.get("NIM_BASE_URL", NIM_BASE_URL)
            return cls(
                backend="nim",
                model=model,
                base_url=base_url.rstrip("/"),
                api_key=api_key,
                timeout=timeout,
            )

        if config.backend == "ollama":
            host = os.environ.get("OLLAMA_HOST", "").strip()
            base_url = _ollama_base_url(host)
            model = (
                config.model
                or os.environ.get("OLLAMA_MODEL")
                or DEFAULT_OLLAMA_MODEL
            )
            return cls(
                backend="ollama",
                model=model,
                base_url=base_url,
                api_key="ollama",  # Ollama ignores the key but the API wants one.
                timeout=timeout,
            )

        raise LLMError(f"unknown backend {config.backend!r}")

    # ------------------------------------------------------------------ #

    def complete(
        self,
        system: str,
        user: str,
        temperature: float = 0.3,
        max_tokens: int = 1024,
    ) -> str:
        """Return the assistant's text for a single system+user exchange.

        Rate limits (429) and transient server errors (5xx) are retried with
        bounded exponential backoff that honours ``Retry-After``; a dropped
        connection is retried too. Every other transport problem becomes an
        :class:`LLMError` with a readable message instead of a traceback.
        """
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        url = f"{self.base_url}/chat/completions"
        response = self._post_with_retries(url, payload, headers)
        return self._read_content(response)

    # ------------------------------------------------------------------ #

    def _post_with_retries(
        self, url: str, payload: dict, headers: dict
    ) -> httpx.Response:
        attempts = max(1, self.max_retries + 1)
        last_problem = ""
        self.last_attempts = 0
        with httpx.Client(transport=self.transport, timeout=self.timeout) as http:
            for attempt in range(attempts):
                self.last_attempts = attempt + 1
                is_last = attempt == attempts - 1
                try:
                    response = http.post(url, json=payload, headers=headers)
                except httpx.ConnectError as exc:
                    # Nothing is listening: retrying would only delay the hint.
                    raise LLMError(self._connection_hint()) from exc
                except httpx.TimeoutException as exc:
                    raise LLMError(
                        f"request to {self.backend} timed out after "
                        f"{self.timeout:.0f}s"
                    ) from exc
                except (
                    httpx.RemoteProtocolError,
                    httpx.ReadError,
                    httpx.WriteError,
                ) as exc:
                    # The connection dropped mid-flight: usually transient.
                    last_problem = f"connection to {self.backend} was reset ({exc})"
                    if is_last:
                        break
                    _sleep(self._backoff(attempt))
                    continue
                except httpx.HTTPError as exc:
                    raise LLMError(
                        f"request to {self.backend} failed: "
                        f"{exc.__class__.__name__}: {exc}"
                    ) from exc

                if response.status_code in RETRY_STATUSES and not is_last:
                    _sleep(self._retry_delay(response, attempt))
                    continue
                if response.status_code >= 400:
                    raise LLMError(self._http_error_message(response))
                return response

        raise LLMError(f"{last_problem}; gave up after {attempts} attempts")

    def _backoff(self, attempt: int) -> float:
        return min(MAX_RETRY_WAIT, self.backoff_base * (2**attempt))

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = parse_retry_after(response.headers.get("Retry-After"))
        if retry_after is not None:
            return min(MAX_RETRY_WAIT, retry_after)
        return self._backoff(attempt)

    def _http_error_message(self, response: httpx.Response) -> str:
        status = response.status_code
        detail = _error_detail(response)
        if status in (401, 403):
            if self.backend == "nim":
                return (
                    f"NVIDIA NIM rejected the API key ({status}). Check that "
                    "NVIDIA_API_KEY is a valid key (it starts with 'nvapi-')."
                )
            return (
                f"{self.backend} at {self.base_url} refused the request ({status}). "
                "Ollama itself needs no key; if it sits behind an authenticating "
                "proxy, check OLLAMA_HOST."
            )
        if status == 404 and self.backend == "ollama":
            return (
                f"Ollama has no model named '{self.model}' "
                f"({detail or 'HTTP 404'}).\n"
                f"  Pull it with:  ollama pull {self.model}"
            )
        if status == 429:
            hint = (
                " The free NIM tier is rate limited; wait a minute or use "
                "--backend ollama."
                if self.backend == "nim"
                else ""
            )
            return (
                f"{self.backend} is rate limiting requests (HTTP 429), gave up "
                f"after {self.last_attempts} attempts.{hint}"
            )
        suffix = f": {detail}" if detail else ""
        attempts = (
            f" after {self.last_attempts} attempts" if self.last_attempts > 1 else ""
        )
        return f"{self.backend} returned HTTP {status}{attempts}{suffix}"

    def _read_content(self, response: httpx.Response) -> str:
        try:
            data = response.json()
            choice = data["choices"][0]
            content = choice["message"].get("content")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            raise LLMError(f"unexpected response shape from {self.backend}") from exc
        self.last_finish_reason = choice.get("finish_reason")
        if isinstance(content, list):  # content parts: [{"type": "text", ...}]
            content = "".join(
                str(part.get("text", "")) for part in content if isinstance(part, dict)
            )
        return (content or "").strip()

    def _connection_hint(self) -> str:
        if self.backend == "ollama":
            return (
                f"Could not reach Ollama at {self.base_url}.\n"
                "  Start it with:  ollama serve\n"
                f"  Pull the model:  ollama pull {self.model}"
            )
        return f"Could not reach {self.backend} at {self.base_url}."


def _error_detail(response: httpx.Response) -> str:
    """Pull a short human message out of an error body (JSON or text)."""
    try:
        data = response.json()
    except ValueError:
        return response.text.strip()[:400]
    if isinstance(data, dict):
        err = data.get("error", data.get("detail", data.get("message")))
        if isinstance(err, dict):
            err = err.get("message") or err.get("detail")
        if err:
            return str(err)[:400]
    return response.text.strip()[:400]


def parse_retry_after(
    value: Optional[str], now: Optional[datetime] = None
) -> Optional[float]:
    """Seconds to wait from a ``Retry-After`` header (delta-seconds or HTTP-date)."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:  # pragma: no cover - older Pythons return None on bad input
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return max(0.0, (when - now).total_seconds())


def _ollama_base_url(host: str) -> str:
    """Normalize an OLLAMA_HOST value into an OpenAI-compatible base URL."""
    if not host:
        return OLLAMA_BASE_URL
    if not host.startswith(("http://", "https://")):
        host = f"http://{host}"
    host = host.rstrip("/")
    if host.endswith("/v1"):
        return host
    return f"{host}/v1"


# --------------------------------------------------------------------------- #
# Reply parsing
# --------------------------------------------------------------------------- #

_REASONING_BLOCK_RE = re.compile(
    r"<(think|thinking|reasoning)>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_REASONING_OPEN_RE = re.compile(r"<(think|thinking|reasoning)>", re.IGNORECASE)
_REASONING_CLOSE_RE = re.compile(r"</(think|thinking|reasoning)>", re.IGNORECASE)
_OPEN_FENCE_RE = re.compile(r"^```[A-Za-z0-9_-]*[ \t]*\n?")
_CLOSE_FENCE_RE = re.compile(r"\n?```\s*$")
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def strip_reasoning(text: str) -> str:
    """Remove ``<think>``-style reasoning that models such as qwen3 or
    deepseek-r1 emit before their answer.

    Handles complete blocks, a dangling ``</think>`` whose opening tag was part
    of the chat template, and an unterminated ``<think>`` (the reply was cut
    off while still reasoning, so nothing after the tag is an answer).
    """
    if not text:
        return ""
    cleaned = _REASONING_BLOCK_RE.sub("", text)
    closes = list(_REASONING_CLOSE_RE.finditer(cleaned))
    if closes:
        cleaned = cleaned[closes[-1].end() :]
    opening = _REASONING_OPEN_RE.search(cleaned)
    if opening is not None:
        cleaned = cleaned[: opening.start()]
    return cleaned.strip()


def _strip_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = _OPEN_FENCE_RE.sub("", cleaned)
        cleaned = _CLOSE_FENCE_RE.sub("", cleaned)
    return cleaned.strip()


def iter_json_objects(text: str) -> Iterator[dict]:
    """Yield every JSON object embedded in ``text`` that parses, in order.

    A string-aware balanced-brace scanner: braces inside JSON strings do not
    count, and a ``{`` that does not open a valid object (prose such as
    ``{a cache}``) is skipped so a later, real object is still found. Objects
    nested inside an object that was already yielded are not yielded again.
    """
    i = 0
    n = len(text)
    while i < n:
        start = text.find("{", i)
        if start == -1:
            return
        end = _matching_brace(text, start)
        if end is not None:
            parsed = _loads_lenient(text[start : end + 1])
            if isinstance(parsed, dict):
                yield parsed
                i = end + 1
                continue
        i = start + 1


def _matching_brace(text: str, start: int) -> Optional[int]:
    depth = 0
    in_string = False
    escaped = False
    for pos in range(start, len(text)):
        ch = text[pos]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return pos
    return None


def _loads_lenient(candidate: str) -> Any:
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    # Small models love trailing commas; forgive exactly that and nothing else.
    repaired = _TRAILING_COMMA_RE.sub(r"\1", candidate)
    if repaired != candidate:
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            return None
    return None


def extract_json(text: str, expected_keys: Iterable[str] = ()) -> dict:
    """Best-effort JSON extraction from a model reply.

    Strips reasoning blocks and ``` fences, then tries a clean parse. Failing
    that, it scans for embedded objects and returns the first one that has any
    of ``expected_keys`` (or simply the first object when none are given or
    none match). Raises :class:`LLMError` when no JSON object parses.
    """
    cleaned = _strip_fences(strip_reasoning(text or ""))
    if not cleaned:
        raise LLMError("model did not return JSON")

    whole = _loads_lenient(cleaned)
    if isinstance(whole, dict):
        return whole

    wanted = set(expected_keys)
    objects: List[dict] = []
    for obj in iter_json_objects(cleaned):
        if wanted and wanted.intersection(obj):
            return obj
        objects.append(obj)
    if objects:
        return objects[0]
    if "{" in cleaned:
        raise LLMError("model did not return valid JSON")
    raise LLMError("model did not return JSON")
