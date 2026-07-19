"""Conventional Commit modelling: parse, format, validate and generate.

The formatting and validation logic is deterministic and unit-tested. Only
:func:`generate_commit` touches an LLM, and it takes the client as an argument
so tests inject a fake.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from .config import Config
from .llm import LLMClient, extract_json
from .prompts import commit_system, commit_user

# Gitmoji-style prefix per Conventional Commit type.
EMOJI_BY_TYPE = {
    "feat": "✨",  # sparkles
    "fix": "\U0001f41b",  # bug
    "docs": "\U0001f4dd",  # memo
    "style": "\U0001f484",  # lipstick
    "refactor": "♻️",  # recycle
    "perf": "⚡",  # zap
    "test": "✅",  # check mark
    "build": "\U0001f4e6",  # package
    "ci": "\U0001f477",  # construction worker
    "chore": "\U0001f527",  # wrench
    "revert": "⏪",  # rewind
}

# type: subject  /  type(scope): subject  /  type(scope)!: subject
_HEADER_RE = re.compile(
    r"^(?P<type>[a-zA-Z]+)"
    r"(?:\((?P<scope>[^)]+)\))?"
    r"(?P<breaking>!)?"
    r":\s(?P<subject>.+)$"
)

# Common non-imperative openings that read as changelog prose, not commands.
_NON_IMPERATIVE = {
    "added",
    "adds",
    "adding",
    "fixed",
    "fixes",
    "fixing",
    "updated",
    "updates",
    "updating",
    "changed",
    "changes",
    "changing",
    "removed",
    "removes",
    "removing",
    "created",
    "creates",
    "creating",
    "implemented",
    "implements",
    "refactored",
    "improved",
    "improves",
}


@dataclass
class CommitMessage:
    type: str
    subject: str
    scope: Optional[str] = None
    body: str = ""
    footer: str = ""
    breaking: bool = False

    def header(self, emoji: bool = False) -> str:
        head = self.type
        if self.scope:
            head += f"({self.scope})"
        if self.breaking:
            head += "!"
        head += f": {self.subject}"
        if emoji:
            icon = EMOJI_BY_TYPE.get(self.type)
            if icon:
                head = f"{icon} {head}"
        return head

    def format(self, emoji: bool = False, include_body: bool = True) -> str:
        parts = [self.header(emoji=emoji)]
        if include_body and self.body.strip():
            parts.append("")
            parts.append(self.body.strip())

        footer = self.footer.strip()
        if self.breaking and "BREAKING CHANGE" not in footer:
            note = "BREAKING CHANGE: see body for details."
            footer = f"{footer}\n{note}".strip() if footer else note
        if footer:
            parts.append("")
            parts.append(footer)
        return "\n".join(parts).strip() + "\n"

    @classmethod
    def from_dict(cls, data: dict) -> "CommitMessage":
        subject = str(data.get("subject", "")).strip()
        scope = data.get("scope")
        scope = str(scope).strip() if scope else None
        return cls(
            type=str(data.get("type", "")).strip().lower(),
            subject=subject,
            scope=scope or None,
            body=str(data.get("body", "") or "").strip(),
            footer=str(data.get("footer", "") or "").strip(),
            breaking=bool(data.get("breaking", False))
            or "BREAKING CHANGE" in str(data.get("footer", "")),
        )


@dataclass
class CommitResult:
    best: CommitMessage
    alternatives: List[CommitMessage] = field(default_factory=list)
    raw: str = ""

    @property
    def all(self) -> List[CommitMessage]:
        return [self.best, *self.alternatives]


def parse_commit(text: str) -> Optional[CommitMessage]:
    """Parse a Conventional Commit string into a :class:`CommitMessage`.

    Returns ``None`` when the header line does not conform.
    """
    if not text or not text.strip():
        return None
    lines = text.strip().splitlines()
    header = lines[0].strip()
    match = _HEADER_RE.match(header)
    if not match:
        return None

    rest = [ln for ln in lines[1:]]
    # Drop a single blank separator line after the header.
    while rest and not rest[0].strip():
        rest.pop(0)

    body_lines: List[str] = []
    footer_lines: List[str] = []
    footer_re = re.compile(r"^(BREAKING CHANGE|BREAKING-CHANGE|[A-Za-z-]+): .+")
    in_footer = False
    for line in rest:
        if not in_footer and footer_re.match(line):
            in_footer = True
        (footer_lines if in_footer else body_lines).append(line)

    breaking = bool(match.group("breaking")) or any(
        "BREAKING CHANGE" in ln for ln in footer_lines
    )
    return CommitMessage(
        type=match.group("type").lower(),
        scope=match.group("scope"),
        subject=match.group("subject").strip(),
        body="\n".join(body_lines).strip(),
        footer="\n".join(footer_lines).strip(),
        breaking=breaking,
    )


def validate_commit(msg: CommitMessage, config: Config) -> List[str]:
    """Return a list of human-readable problems; empty means it is clean."""
    issues: List[str] = []

    if not msg.type:
        issues.append("missing commit type")
    elif msg.type not in config.allowed_types:
        allowed = ", ".join(config.allowed_types)
        issues.append(f"type '{msg.type}' is not one of the allowed types: {allowed}")

    if not msg.subject:
        issues.append("subject is empty")
    else:
        if len(msg.subject) > config.subject_max_length:
            issues.append(
                f"subject is {len(msg.subject)} chars, over the "
                f"{config.subject_max_length}-char limit"
            )
        if msg.subject.endswith("."):
            issues.append("subject should not end with a period")
        first_word = msg.subject.split(" ", 1)[0].lower().strip(":")
        if first_word in _NON_IMPERATIVE:
            issues.append(
                f"subject starts with '{first_word}'; use the imperative mood "
                "(e.g. 'add' not 'added')"
            )

    if msg.scope is not None and not msg.scope.strip():
        issues.append("scope is present but empty")

    return issues


def is_valid_commit(text: str, config: Config) -> bool:
    parsed = parse_commit(text)
    if parsed is None:
        return False
    return not validate_commit(parsed, config)


def generate_commit(
    diff_text: str,
    config: Config,
    client: LLMClient,
    hint: str = "",
    temperature: Optional[float] = None,
) -> CommitResult:
    """Ask the model for a primary commit plus alternatives."""
    system = commit_system(config)
    user = commit_user(diff_text, config.n_alternatives, hint=hint)
    raw = client.complete(
        system,
        user,
        temperature=config.temperature if temperature is None else temperature,
        max_tokens=800,
    )
    return _parse_generation(raw, config)


def _parse_generation(raw: str, config: Config) -> CommitResult:
    """Turn a raw model reply into a :class:`CommitResult`, defensively."""
    try:
        data = extract_json(raw)
    except Exception:
        # Fall back to treating the reply as a bare commit message.
        parsed = parse_commit(raw) or CommitMessage(
            type="chore", subject=raw.strip().splitlines()[0][: config.subject_max_length]
        )
        return CommitResult(best=parsed, alternatives=[], raw=raw)

    primary_data = data.get("primary") or data
    best = CommitMessage.from_dict(primary_data)

    alternatives: List[CommitMessage] = []
    for alt in data.get("alternatives", []) or []:
        if isinstance(alt, dict) and alt.get("subject"):
            alternatives.append(CommitMessage.from_dict(alt))

    return CommitResult(best=best, alternatives=alternatives, raw=raw)
