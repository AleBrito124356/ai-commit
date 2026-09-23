"""Configuration loading with layered precedence.

Precedence, lowest to highest:

    built-in defaults
      < user config      (~/.aicommit.yaml)
      < project config   (nearest .aicommit.yaml walking up from cwd)
      < environment       (AICOMMIT_* variables)
      < CLI overrides      (flags passed on the command line)

The resulting :class:`Config` is the single source of truth handed to every
other module. Nothing in here touches the network.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# Conventional Commits <https://www.conventionalcommits.org> — the standard set.
DEFAULT_TYPES: List[str] = [
    "feat",
    "fix",
    "docs",
    "style",
    "refactor",
    "perf",
    "test",
    "build",
    "ci",
    "chore",
    "revert",
]

CONFIG_FILENAMES = (".aicommit.yaml", ".aicommit.yml")

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n"}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(f"cannot interpret {value!r} as a boolean")


def _as_int(value: Any) -> int:
    return int(str(value).strip())


def _as_list(value: Any) -> List[str]:
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    # Allow a comma-separated string (handy for env vars).
    return [part.strip() for part in str(value).split(",") if part.strip()]


@dataclass
class Config:
    """Fully-resolved runtime configuration."""

    backend: str = "nim"  # "nim" or "ollama"
    model: Optional[str] = None  # None -> backend default (see llm.py)
    language: str = "en"  # "en" or "es"
    allowed_types: List[str] = field(default_factory=lambda: list(DEFAULT_TYPES))
    subject_max_length: int = 72
    emoji: bool = False
    pr_base: str = "main"
    max_diff_chars: int = 12000
    include_body: bool = True
    n_alternatives: int = 2
    temperature: float = 0.3
    # Globs for project-specific generated files that the model should never
    # see (they are still named in the prompt, just without content).
    ignore_paths: List[str] = field(default_factory=list)

    def validate(self) -> "Config":
        if self.backend not in {"nim", "ollama"}:
            raise ValueError(
                f"backend must be 'nim' or 'ollama', got {self.backend!r}"
            )
        if self.language not in {"en", "es"}:
            raise ValueError(f"language must be 'en' or 'es', got {self.language!r}")
        if self.subject_max_length < 20:
            raise ValueError("subject_max_length must be at least 20")
        if not self.allowed_types:
            raise ValueError("allowed_types must not be empty")
        if self.max_diff_chars < 200:
            raise ValueError("max_diff_chars must be at least 200")
        return self

    def with_overrides(self, **overrides: Any) -> "Config":
        """Return a copy with the given non-None fields replaced."""
        clean = {k: v for k, v in overrides.items() if v is not None}
        return replace(self, **clean) if clean else self


# Field -> coercion function. Anything not listed is copied verbatim.
_COERCERS = {
    "subject_max_length": _as_int,
    "max_diff_chars": _as_int,
    "n_alternatives": _as_int,
    "temperature": float,
    "emoji": _as_bool,
    "include_body": _as_bool,
    "allowed_types": _as_list,
    "ignore_paths": _as_list,
}

_VALID_FIELDS = set(Config().__dict__.keys())


def _coerce(data: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in data.items():
        if key not in _VALID_FIELDS:
            # Silently ignore unknown keys so future config files stay loadable.
            continue
        if value is None:
            continue
        coercer = _COERCERS.get(key)
        out[key] = coercer(value) if coercer else value
    return out


def _read_yaml(path: Path) -> Dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        loaded = yaml.safe_load(raw)
    except yaml.YAMLError as exc:  # pragma: no cover - depends on bad user input
        raise ValueError(f"invalid YAML in {path}: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} must contain a mapping at the top level")
    return _coerce(loaded)


def find_project_config(start: Optional[Path] = None) -> Optional[Path]:
    """Walk upward from ``start`` (default: cwd) looking for a project config."""
    current = (start or Path.cwd()).resolve()
    for directory in [current, *current.parents]:
        for name in CONFIG_FILENAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def user_config_path() -> Path:
    return Path.home() / ".aicommit.yaml"


def _env_overrides(env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    env = env if env is not None else dict(os.environ)
    mapping = {
        "AICOMMIT_BACKEND": "backend",
        "AICOMMIT_MODEL": "model",
        "AICOMMIT_LANGUAGE": "language",
        "AICOMMIT_ALLOWED_TYPES": "allowed_types",
        "AICOMMIT_SUBJECT_MAX_LENGTH": "subject_max_length",
        "AICOMMIT_EMOJI": "emoji",
        "AICOMMIT_PR_BASE": "pr_base",
        "AICOMMIT_MAX_DIFF_CHARS": "max_diff_chars",
        "AICOMMIT_TEMPERATURE": "temperature",
        "AICOMMIT_IGNORE_PATHS": "ignore_paths",
    }
    raw = {field_name: env[var] for var, field_name in mapping.items() if env.get(var)}
    return _coerce(raw)


def load_config(
    start: Optional[Path] = None,
    cli_overrides: Optional[Dict[str, Any]] = None,
    env: Optional[Dict[str, str]] = None,
) -> Config:
    """Resolve the effective configuration following the documented precedence."""
    merged: Dict[str, Any] = {}

    user_path = user_config_path()
    if user_path.is_file():
        merged.update(_read_yaml(user_path))

    project_path = find_project_config(start)
    if project_path is not None:
        merged.update(_read_yaml(project_path))

    merged.update(_env_overrides(env))

    if cli_overrides:
        merged.update(_coerce({k: v for k, v in cli_overrides.items() if v is not None}))

    config = Config(**merged)
    return config.validate()


SAMPLE_CONFIG = """\
# .aicommit.yaml — project configuration for aicommit
# Commit this file so the whole team shares the same conventions.

# Which backend generates the text: "nim" (free NVIDIA cloud) or "ollama" (local).
backend: nim

# Optional model override. Leave unset to use the backend default:
#   nim    -> meta/llama-3.3-70b-instruct
#   ollama -> llama3.1
# model: qwen2.5-coder:7b

# Output language for subjects and bodies: "en" or "es".
language: en

# Conventional Commit types you allow. Anything else is flagged in validation.
allowed_types:
  - feat
  - fix
  - docs
  - style
  - refactor
  - perf
  - test
  - build
  - ci
  - chore
  - revert

# Hard cap on the subject line length.
subject_max_length: 72

# Prefix each message with a gitmoji for its type.
emoji: false

# Default base branch for `aicommit pr`.
pr_base: main

# Prompt budget in characters. Bigger diffs are condensed file by file:
# source code keeps its full patch first, data and fixtures are cut first.
# max_diff_chars: 12000

# Generated files the model should never read (still listed by name).
# Lockfiles, binaries, minified bundles, dist/, vendor/ and files marked
# linguist-generated in .gitattributes are already skipped automatically.
# ignore_paths:
#   - "src/generated/"
#   - "*.pb.go"
"""
