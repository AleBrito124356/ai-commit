"""Produce Keep a Changelog release notes from git history.

Grouping is pure and deterministic — no network needed — so ``aicommit
changelog`` works offline. An optional ``--polish`` pass rewrites the raw commit
subjects into friendlier prose via the LLM.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

from .commit import parse_commit
from .config import Config
from .gitutil import list_tags, run_git
from .llm import LLMClient, extract_json
from .prompts import changelog_polish_system, changelog_polish_user

# Conventional Commit type -> Keep a Changelog section.
TYPE_TO_SECTION: Dict[str, str] = {
    "feat": "Added",
    "fix": "Fixed",
    "perf": "Changed",
    "refactor": "Changed",
    "revert": "Changed",
    "deprecate": "Deprecated",
    "remove": "Removed",
    "security": "Security",
}

# Canonical Keep a Changelog ordering.
SECTION_ORDER = ["Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"]

# Types that are internal-only and excluded from release notes by default.
INTERNAL_TYPES = {"docs", "style", "test", "build", "ci", "chore"}

_RECORD_SEP = "\x1e"  # ASCII record separator, unlikely to appear in messages.
_FIELD_SEP = "\x1f"  # ASCII unit separator.


@dataclass
class ChangelogEntry:
    section: str
    text: str
    scope: Optional[str] = None
    short_hash: str = ""
    breaking: bool = False

    def render(self) -> str:
        prefix = "**BREAKING** " if self.breaking else ""
        scope = f"**{self.scope}:** " if self.scope else ""
        suffix = f" (`{self.short_hash}`)" if self.short_hash else ""
        return f"- {prefix}{scope}{self.text}{suffix}"


@dataclass
class Release:
    version: str
    date: str
    sections: "OrderedDict[str, List[ChangelogEntry]]" = field(
        default_factory=OrderedDict
    )

    @property
    def is_empty(self) -> bool:
        return not any(self.sections.values())

    def render(self) -> str:
        lines = [f"## [{self.version}] - {self.date}"]
        for section in SECTION_ORDER:
            entries = self.sections.get(section)
            if not entries:
                continue
            lines.append("")
            lines.append(f"### {section}")
            lines.extend(entry.render() for entry in entries)
        return "\n".join(lines)


def _log_between(
    since: Optional[str], until: str = "HEAD", cwd: Optional[Path] = None
) -> List[dict]:
    """Return commits in ``since..until`` as dicts with hash and message."""
    fmt = _FIELD_SEP.join(["%h", "%B"]) + _RECORD_SEP
    ref_range = f"{since}..{until}" if since else until
    out = run_git(["log", f"--pretty=format:{fmt}", ref_range], cwd=cwd)
    commits: List[dict] = []
    for record in out.split(_RECORD_SEP):
        record = record.strip("\n")
        if not record.strip():
            continue
        short_hash, _, message = record.partition(_FIELD_SEP)
        commits.append({"hash": short_hash.strip(), "message": message.strip()})
    return commits


def group_commits(
    commits: List[dict], include_internal: bool = False
) -> "OrderedDict[str, List[ChangelogEntry]]":
    """Bucket parsed commits into Keep a Changelog sections.

    ``commits`` is a list of ``{"hash": ..., "message": ...}`` dicts. Merge
    commits and messages that are not Conventional Commits are skipped.
    """
    grouped: "OrderedDict[str, List[ChangelogEntry]]" = OrderedDict(
        (section, []) for section in SECTION_ORDER
    )

    for commit in commits:
        message = commit.get("message", "")
        if message.startswith("Merge "):
            continue
        parsed = parse_commit(message)
        if parsed is None:
            continue
        if parsed.type in INTERNAL_TYPES and not include_internal:
            continue

        if parsed.breaking:
            section = "Changed"
        else:
            section = TYPE_TO_SECTION.get(parsed.type)
            if section is None:
                section = "Changed" if include_internal else None
        if section is None:
            continue

        grouped.setdefault(section, [])
        grouped[section].append(
            ChangelogEntry(
                section=section,
                text=parsed.subject,
                scope=parsed.scope,
                short_hash=commit.get("hash", ""),
                breaking=parsed.breaking,
            )
        )

    # Drop empty sections to keep the OrderedDict tidy.
    return OrderedDict((k, v) for k, v in grouped.items() if v)


def build_release(
    version: str,
    commits: List[dict],
    release_date: Optional[str] = None,
    include_internal: bool = False,
) -> Release:
    return Release(
        version=version,
        date=release_date or date.today().isoformat(),
        sections=group_commits(commits, include_internal=include_internal),
    )


def ref_date(ref: str, cwd: Optional[Path] = None) -> str:
    """Committer date (YYYY-MM-DD) of ``ref``; today's date on any failure."""
    try:
        out = run_git(
            ["log", "-1", "--format=%cd", "--date=short", ref], cwd=cwd
        )
    except Exception:  # noqa: BLE001 - never let dating a tag crash the run
        return date.today().isoformat()
    out = out.strip()
    return out or date.today().isoformat()


def _polish(release: Release, config: Config, client: LLMClient) -> None:
    """Rewrite entry text in place using the LLM, preserving order and metadata."""
    flat: List[ChangelogEntry] = [
        entry for entries in release.sections.values() for entry in entries
    ]
    if not flat:
        return
    system = changelog_polish_system(config)
    user = changelog_polish_user([e.text for e in flat])
    raw = client.complete(system, user, temperature=0.2, max_tokens=1200)
    data = extract_json(raw, expected_keys=("entries",))
    rewritten = data.get("entries", [])
    for entry, new_text in zip(flat, rewritten):
        if isinstance(new_text, str) and new_text.strip():
            entry.text = new_text.strip()


def generate_release_notes(
    from_tag: Optional[str] = None,
    to_ref: str = "HEAD",
    version: Optional[str] = None,
    config: Optional[Config] = None,
    client: Optional[LLMClient] = None,
    polish: bool = False,
    include_internal: bool = False,
    release_date: Optional[str] = None,
    cwd: Optional[Path] = None,
) -> str:
    """Generate a single release section of a Keep a Changelog document."""
    commits = _log_between(from_tag, to_ref, cwd=cwd)
    resolved_version = version or (to_ref if to_ref != "HEAD" else "Unreleased")
    release = build_release(
        resolved_version,
        commits,
        release_date=release_date,
        include_internal=include_internal,
    )
    if resolved_version == "Unreleased" and release_date is None:
        release.date = date.today().isoformat()

    if polish and client is not None and config is not None:
        _polish(release, config, client)

    if release.is_empty:
        header = f"## [{release.version}] - {release.date}"
        return f"{header}\n\n_No user-facing changes._\n"
    return release.render() + "\n"


CHANGELOG_HEADER = (
    "# Changelog\n\n"
    "All notable changes to this project are documented here.\n\n"
    "The format is based on "
    "[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), "
    "and this project adheres to "
    "[Semantic Versioning](https://semver.org/spec/v2.0.0.html).\n"
)


def render_full_changelog(releases: List[str]) -> str:
    """Assemble a complete CHANGELOG.md from rendered release sections."""
    body = "\n\n".join(section.rstrip() for section in releases)
    return f"{CHANGELOG_HEADER}\n{body}\n"


def latest_tag(cwd: Optional[Path] = None) -> Optional[str]:
    tags = list_tags(cwd=cwd)
    return tags[-1] if tags else None
