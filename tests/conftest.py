"""Shared pytest fixtures.

Two things every test can lean on:

* ``git_repo`` — a throwaway git repository with helpers to write files and
  commit, so tests exercise the real git plumbing without touching your machine.
* ``fake_client`` — a factory for a stand-in LLM client that returns a canned
  response, so nothing hits the network.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Make the src-layout package importable even without installation.
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

_ENV_TO_CLEAR = (
    "NVIDIA_API_KEY",
    "NIM_MODEL",
    "NIM_BASE_URL",
    "OLLAMA_HOST",
    "OLLAMA_MODEL",
)


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch, tmp_path_factory):
    """Keep every test hermetic and offline.

    No real ``~/.aicommit.yaml``, no API keys or Ollama host from the
    developer's shell, and no ``AICOMMIT_*`` overrides leak into the suite.
    """
    from aicommit import config as config_mod

    fake_home_config = tmp_path_factory.mktemp("home") / ".aicommit.yaml"
    monkeypatch.setattr(config_mod, "user_config_path", lambda: fake_home_config)
    for name in _ENV_TO_CLEAR:
        monkeypatch.delenv(name, raising=False)
    for name in list(os.environ):
        if name.startswith("AICOMMIT_"):
            monkeypatch.delenv(name, raising=False)


class FakeLLMClient:
    """Minimal drop-in for :class:`aicommit.llm.LLMClient`.

    Returns ``response`` for every ``complete`` call and records the calls so
    tests can assert on the prompts if they want.
    """

    def __init__(self, response: str):
        self.response = response
        self.calls = []

    def complete(self, system, user, temperature=0.3, max_tokens=1024):
        self.calls.append({"system": system, "user": user, "temperature": temperature})
        return self.response


@pytest.fixture
def fake_client():
    def _make(response: str) -> FakeLLMClient:
        return FakeLLMClient(response)

    return _make


def _git_available() -> bool:
    return shutil.which("git") is not None


@pytest.fixture
def git_repo(tmp_path):
    if not _git_available():
        pytest.skip("git is not installed")

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return result.stdout

    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test User")
    git("config", "commit.gpgsign", "false")

    def write(rel_path: str, content: str = "") -> Path:
        path = repo / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def commit(message: str, rel_path: str = None, content: str = "x") -> None:
        if rel_path is not None:
            write(rel_path, content)
        git("add", "-A")
        git("commit", "-q", "-m", message)

    return SimpleNamespace(path=repo, git=git, write=write, commit=commit)
