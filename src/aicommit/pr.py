"""Generate a pull request title and description from a branch.

Collects the commits and combined diff between a base branch and HEAD, asks the
model for structured fields, then renders deterministic markdown so the output
shape is stable and testable.

The git side (:func:`collect_pr_context`) runs first and fails fast — an
unknown base or a branch with no commits of its own is reported before any
backend is contacted, so the model is never asked to describe nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

from .commit import parse_commit
from .config import Config
from .diff import DiffBundle, RenderedDiff, annotate_bundle, parse_diff, plan_render
from .gitutil import GitError, current_branch, rev_exists, run_git
from .llm import extract_json
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


@dataclass
class PRContext:
    """Everything a backend needs to describe a branch."""

    base: str
    head: str
    head_name: str
    commits: List[str]
    bundle: DiffBundle
    rendered: RenderedDiff

    @property
    def diff_view(self) -> str:
        return self.rendered.text


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


def resolve_base(base: str, cwd: Optional[Path] = None) -> str:
    """Return ``base`` if it exists locally, else ``origin/<base>``.

    CI checkouts and fresh clones often have only the remote-tracking branch.
    """
    if rev_exists(base, cwd=cwd):
        return base
    remote = f"origin/{base}"
    if not base.startswith("origin/") and rev_exists(remote, cwd=cwd):
        return remote
    raise GitError(
        f"base branch '{base}' does not exist (locally or as origin/{base}). "
        "Pass one with --base or set pr_base in .aicommit.yaml."
    )


def render_pr(result: PRResult) -> str:
    """Render a :class:`PRResult` into review-ready markdown."""

    def bullets(items: List[str]) -> str:
        cleaned = [item.strip() for item in items if item.strip()]
        if not cleaned:
            return "_None._"
        return "\n".join(f"- {item}" for item in cleaned)

    return (
        f"## Summary\n\n{result.summary.strip()}\n\n"
        f"## Changes\n\n{bullets(result.changes)}\n\n"
        f"## Testing\n\n{bullets(result.testing)}\n\n"
        f"## Risks\n\n{bullets(result.risks)}\n"
    )


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [str(v).strip() for v in value if str(v).strip()]


def collect_pr_context(
    base: Optional[str] = None,
    head: str = "HEAD",
    config: Optional[Config] = None,
    cwd: Optional[Path] = None,
) -> PRContext:
    """Gather commits and the condensed diff for ``head`` versus ``base``.

    Raises :class:`GitError` when the base is unknown or when ``head`` has no
    commits that are not already on ``base``.
    """
    config = config or Config()
    base = resolve_base(base or config.pr_base, cwd=cwd)
    head_name = current_branch(cwd=cwd) if head == "HEAD" else head
    commits = collect_commits(base, head, cwd=cwd)
    if not commits:
        raise GitError(
            f"no commits on '{head_name}' that are not already on '{base}'; "
            "nothing to describe. Commit your work or pick another --base."
        )
    raw_diff = diff_vs_base(base, head, cwd=cwd)
    bundle = annotate_bundle(parse_diff(raw_diff), config.ignore_paths, cwd=cwd)
    rendered = plan_render(bundle, config.max_diff_chars)
    return PRContext(
        base=base,
        head=head,
        head_name=head_name,
        commits=commits,
        bundle=bundle,
        rendered=rendered,
    )


def _title_from_commit(subject: str) -> str:
    parsed = parse_commit(subject)
    text = parsed.subject if parsed is not None else subject
    text = text.strip().rstrip(".")
    return text[:1].upper() + text[1:] if text else text


def generate_pr(
    base: Optional[str] = None,
    head: str = "HEAD",
    config: Optional[Config] = None,
    client: Any = None,
    cwd: Optional[Path] = None,
    context: Optional[PRContext] = None,
) -> PRResult:
    """End-to-end PR generation for the current branch versus ``base``."""
    if config is None or client is None:
        raise ValueError("config and client are required")
    if context is None:
        context = collect_pr_context(base=base, head=head, config=config, cwd=cwd)

    draft_pr = getattr(client, "draft_pr", None)
    if callable(draft_pr):  # the rule-based local backend
        return draft_pr(context, config)

    system = pr_system(config)
    user = pr_user(context.commits, context.diff_view, context.base, context.head_name)
    raw = client.complete(system, user, temperature=config.temperature, max_tokens=2000)

    data = extract_json(raw, expected_keys=("title", "summary", "changes"))
    title = str(data.get("title", "") or "").strip().rstrip(".")
    if not title:
        title = _title_from_commit(context.commits[0])
    changes = _as_list(data.get("changes"))
    if not changes:
        changes = list(context.commits)
    return PRResult(
        title=title,
        summary=str(data.get("summary", "") or "").strip(),
        changes=changes,
        testing=_as_list(data.get("testing")),
        risks=_as_list(data.get("risks")),
    )
