"""Configuration precedence, discovery and coercion."""

from __future__ import annotations

import pytest

from aicommit import config as config_mod
from aicommit.config import Config, find_project_config, load_config


@pytest.fixture
def isolated_user_config(tmp_path, monkeypatch):
    """Point the 'user config' at a temp file so we never read the real home."""
    user_file = tmp_path / "user_aicommit.yaml"
    monkeypatch.setattr(config_mod, "user_config_path", lambda: user_file)
    return user_file


def test_defaults(isolated_user_config, tmp_path):
    cfg = load_config(start=tmp_path, env={})
    assert cfg.backend == "nim"
    assert cfg.language == "en"
    assert cfg.subject_max_length == 72
    assert "feat" in cfg.allowed_types


def test_project_overrides_user(isolated_user_config, tmp_path):
    isolated_user_config.write_text("language: es\nbackend: ollama\n", encoding="utf-8")
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".aicommit.yaml").write_text(
        "backend: nim\nsubject_max_length: 50\n", encoding="utf-8"
    )

    cfg = load_config(start=project, env={})
    # From the user file (not overridden by the project):
    assert cfg.language == "es"
    # Project wins over user:
    assert cfg.backend == "nim"
    assert cfg.subject_max_length == 50


def test_env_overrides_files(isolated_user_config, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".aicommit.yaml").write_text("backend: nim\n", encoding="utf-8")

    cfg = load_config(start=project, env={"AICOMMIT_BACKEND": "ollama"})
    assert cfg.backend == "ollama"


def test_cli_overrides_env(isolated_user_config, tmp_path):
    cfg = load_config(
        start=tmp_path,
        env={"AICOMMIT_BACKEND": "ollama"},
        cli_overrides={"backend": "nim"},
    )
    assert cfg.backend == "nim"


def test_env_coercion(isolated_user_config, tmp_path):
    cfg = load_config(
        start=tmp_path,
        env={
            "AICOMMIT_EMOJI": "true",
            "AICOMMIT_SUBJECT_MAX_LENGTH": "60",
            "AICOMMIT_ALLOWED_TYPES": "feat,fix,docs",
        },
    )
    assert cfg.emoji is True
    assert cfg.subject_max_length == 60
    assert cfg.allowed_types == ["feat", "fix", "docs"]


def test_find_project_config_walks_up(tmp_path):
    root = tmp_path / "repo"
    nested = root / "src" / "pkg"
    nested.mkdir(parents=True)
    (root / ".aicommit.yaml").write_text("backend: nim\n", encoding="utf-8")

    found = find_project_config(start=nested)
    assert found == root / ".aicommit.yaml"


def test_find_project_config_absent(tmp_path):
    assert find_project_config(start=tmp_path) is None


def test_invalid_backend_rejected():
    with pytest.raises(ValueError):
        Config(backend="banana").validate()


def test_none_cli_overrides_are_ignored(isolated_user_config, tmp_path):
    cfg = load_config(
        start=tmp_path,
        env={},
        cli_overrides={"backend": None, "model": None, "emoji": None},
    )
    assert cfg.backend == "nim"  # unchanged by the None overrides
