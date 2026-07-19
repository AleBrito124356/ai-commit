"""LLM backend: one OpenAI-compatible client for NVIDIA NIM or local Ollama.

Both backends speak the same ``/chat/completions`` shape, so a single tiny
``httpx`` client covers them. Swap between them with one config field
(``backend: nim`` or ``backend: ollama``) — the diff never leaves the backend
you choose, and with Ollama it never leaves your machine.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Optional

import httpx

from .config import Config

NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"
OLLAMA_BASE_URL = "http://localhost:11434/v1"

DEFAULT_NIM_MODEL = "meta/llama-3.3-70b-instruct"
DEFAULT_OLLAMA_MODEL = "llama3.1"

_SIGNUP_HINT = (
    "No NVIDIA_API_KEY found.\n"
    "  1. Create a free account at https://build.nvidia.com (takes ~2 minutes).\n"
    "  2. Generate an API key — it starts with 'nvapi-'.\n"
    "  3. Export it:  export NVIDIA_API_KEY=nvapi-...\n"
    "Or run fully local instead:  aicommit --backend ollama"
)


class LLMError(RuntimeError):
    """Any failure talking to the backend, with a user-facing message."""


@dataclass
class LLMClient:
    backend: str
    model: str
    base_url: str
    api_key: Optional[str]
    timeout: float = 60.0

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
        """Return the assistant's text for a single system+user exchange."""
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
        try:
            response = httpx.post(
                url, json=payload, headers=headers, timeout=self.timeout
            )
        except httpx.ConnectError as exc:
            raise LLMError(self._connection_hint()) from exc
        except httpx.TimeoutException as exc:
            raise LLMError(
                f"request to {self.backend} timed out after {self.timeout:.0f}s"
            ) from exc

        if response.status_code == 401:
            raise LLMError(
                "Backend rejected the API key (401). Check NVIDIA_API_KEY is valid."
            )
        if response.status_code >= 400:
            raise LLMError(
                f"{self.backend} returned HTTP {response.status_code}: "
                f"{response.text[:400]}"
            )

        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError) as exc:
            raise LLMError(f"unexpected response shape from {self.backend}") from exc
        return (content or "").strip()

    def _connection_hint(self) -> str:
        if self.backend == "ollama":
            return (
                f"Could not reach Ollama at {self.base_url}.\n"
                "  Start it with:  ollama serve\n"
                f"  Pull the model:  ollama pull {self.model}"
            )
        return f"Could not reach {self.backend} at {self.base_url}."


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


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def extract_json(text: str) -> dict:
    """Best-effort JSON extraction from a model reply.

    Models sometimes wrap JSON in ``` fences or add prose. We strip fences and,
    failing a clean parse, grab the outermost ``{...}`` span.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Drop the opening fence (optionally ```json) and the closing fence.
        cleaned = re.sub(r"^```[a-zA-Z0-9]*\n", "", cleaned)
        cleaned = re.sub(r"\n```$", "", cleaned.strip())

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    match = _JSON_OBJECT_RE.search(cleaned)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise LLMError("model did not return valid JSON") from exc
    raise LLMError("model did not return JSON")
