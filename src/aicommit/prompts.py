"""System and user prompt builders.

Prompts are plain functions so they can be inspected, diffed and tested. Every
one instructs the model to return strict JSON; parsing lives in the calling
module. Language switches between English and Spanish output.
"""

from __future__ import annotations

from typing import List

from .config import Config

_LANG_NAME = {"en": "English", "es": "Spanish"}


def _language(config: Config) -> str:
    return _LANG_NAME.get(config.language, "English")


def commit_system(config: Config) -> str:
    types = ", ".join(config.allowed_types)
    return (
        "You are a senior engineer who writes precise Conventional Commits "
        "(https://www.conventionalcommits.org). "
        "You read a staged git diff and produce commit messages that describe "
        "WHY the change was made and WHAT changed, in imperative mood.\n\n"
        "Hard rules:\n"
        f"- The `type` MUST be one of: {types}.\n"
        "- Pick a short lowercase `scope` when one clear area is touched, "
        "otherwise use null.\n"
        f"- The `subject` MUST be imperative mood, no trailing period, and at "
        f"most {config.subject_max_length} characters.\n"
        "- The `body` explains the reasoning and notable details as 1-4 short "
        "lines; omit it (empty string) for trivial changes.\n"
        "- Put breaking-change notes and issue references in `footer` "
        "(e.g. 'BREAKING CHANGE: ...'), otherwise an empty string.\n"
        f"- Write all prose in {_language(config)}.\n\n"
        "Return ONLY a JSON object with this exact shape and nothing else:\n"
        "{\n"
        '  "primary": {"type": "", "scope": null, "subject": "", '
        '"body": "", "footer": ""},\n'
        '  "alternatives": [{"type": "", "scope": null, "subject": "", '
        '"body": "", "footer": ""}]\n'
        "}"
    )


def commit_user(diff_text: str, n_alternatives: int, hint: str = "") -> str:
    hint_block = f"\nExtra guidance from the developer: {hint}\n" if hint else ""
    return (
        f"Provide the best commit as `primary` and {n_alternatives} distinct "
        "`alternatives` that differ in scope, type or emphasis."
        f"{hint_block}\n"
        "Staged diff:\n"
        "```diff\n"
        f"{diff_text}\n"
        "```"
    )


def pr_system(config: Config) -> str:
    return (
        "You are a senior engineer writing a pull request description for "
        "reviewers. Be concrete and skimmable. Base everything on the commits "
        "and diff provided; never invent changes.\n\n"
        f"Write all prose in {_language(config)}.\n"
        "Return ONLY a JSON object with this exact shape:\n"
        "{\n"
        '  "title": "",\n'
        '  "summary": "",\n'
        '  "changes": ["", ""],\n'
        '  "testing": ["", ""],\n'
        '  "risks": ["", ""]\n'
        "}\n"
        "- `title` is a single line, imperative, no trailing period.\n"
        "- `summary` is 2-4 sentences on the motivation and outcome.\n"
        "- `changes` are bullet-sized statements of what changed.\n"
        "- `testing` describes how it was or should be verified.\n"
        "- `risks` lists rollout risks, migrations or follow-ups; use an empty "
        "list if genuinely none."
    )


def pr_user(commits: List[str], diff_text: str, base: str, head: str) -> str:
    commit_block = "\n".join(f"- {c}" for c in commits) if commits else "- (none)"
    return (
        f"Base branch: {base}\nHead: {head}\n\n"
        f"Commits on this branch:\n{commit_block}\n\n"
        "Combined diff versus the base:\n"
        "```diff\n"
        f"{diff_text}\n"
        "```"
    )


def changelog_polish_system(config: Config) -> str:
    return (
        "You rewrite raw commit subjects into crisp, user-facing changelog "
        "entries. Keep each entry to one line, drop internal jargon, and keep "
        "the meaning. Do not merge or invent entries.\n\n"
        f"Write all entries in {_language(config)}.\n"
        'Return ONLY a JSON object: {"entries": ["", ""]} with exactly one '
        "rewritten entry per input line, in the same order."
    )


def changelog_polish_user(entries: List[str]) -> str:
    numbered = "\n".join(f"{i + 1}. {e}" for i, e in enumerate(entries))
    return f"Rewrite these {len(entries)} changelog entries:\n{numbered}"
