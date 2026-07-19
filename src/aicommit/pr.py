"""Generate a pull request title and description from a branch.

Collects the commits and combined diff between a base branch and HEAD, asks the
model for structured fields, then renders deterministic markdown so the output
shape is stable and testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .config import Config
from .diff import parse_diff, render_for_prompt
from .gitutil import GitError, current_branch, rev_exists, run_git
from .llm import LLMClient, extract_json
from .prompts import pr_system, pr_user


@dataclass
class PRResult:
    title: str
    summary: str
    changes: List[str] = field(default_factory=list)
    testing: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        return render_pr(self)


def collect_commits(
    base: str, head: str = "HEAD", cwd: Optional[Path] = None
) -> List[str]:
    """Subject lines of commits on ``head`` that are not on ``base``."""
    out = run_git(
        ["log", "--pretty=format:%s", f"{base}..{head}"],
        cwd=cwd,
    )
    return [line.strip() for line in out.splitlines() if line.strip()]


def diff_vs_base(base: str, head: str = "HEAD", cwd: Optional[Path] = None) -> str:
    """Combined diff of ``head`` versus the merge base with ``base``."""
    return run_git(
        ["diff", "--no-color", "--no-ext-diff", f"{base}...{head}"],
        cwd=cwd,
    )


def render_pr(result: PRResult) -> str:
    """Render a :class:`PRResult` into review-ready markdown."""

    def bullets(items: List[str]) -> str:
        if not items:
            return "_None._"
        return "\n".join(f"- {item.strip()}" for item in items if item.strip())

    return (
        f"## Summary\n\n{result.summary.strip()}\n\n"
        f"## Changes\n\n{bullets(result.changes)}\n\n"
        f"## Testing\n\n{bullets(result.testing)}\n\n"
        f"## Risks\n\n{bullets(result.risks)}\n"
    )


def _as_list(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [str(v).strip() for v in value if str(v).strip()]


def generate_pr(
    base: Optional[str] = None,
    head: str = "HEAD",
    config: Optional[Config] = None,
    client: Optional[LLMClient] = None,
    cwd: Optional[Path] = None,
) -> PRResult:
    """End-to-end PR generation for the current branch versus ``base``."""
    if config is None or client is None:
        raise ValueError("config and client are required")

    base = base or config.pr_base
    if not rev_exists(base, cwd=cwd):
        raise GitError(
            f"base branch '{base}' does not exist. "
            "Pass one with --base or set pr_base in .aicommit.yaml."
        )

    head_name = current_branch(cwd=cwd)
    commits = collect_commits(base, head, cwd=cwd)
    raw_diff = diff_vs_base(base, head, cwd=cwd)
    bundle = parse_diff(raw_diff)
    diff_view = render_for_prompt(bundle, config.max_diff_chars)

    system = pr_system(config)
    user = pr_user(commits, diff_view, base, head_name)
    raw = client.complete(system, user, temperature=config.temperature, max_tokens=1200)

    data = extract_json(raw)
    return PRResult(
        title=str(data.get("title", "")).strip(),
        summary=str(data.get("summary", "")).strip(),
        changes=_as_list(data.get("changes")),
        testing=_as_list(data.get("testing")),
        risks=_as_list(data.get("risks")),
    )
