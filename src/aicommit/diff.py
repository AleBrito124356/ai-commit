"""Collect and shape the staged diff for the model.

Two responsibilities:

1. Parse ``git diff --cached`` into structured per-file records so we can drop
   noise (binaries, lockfiles) and reason about size.
2. Render a prompt-sized view of the diff. Big diffs are condensed
   progressively — full patch, then hunk headers only, then a one-line stat per
   file — so we never blow past the model's context window.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .gitutil import run_git

# Generated files that add tokens but no signal.
LOCKFILES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "npm-shrinkwrap.json",
    "poetry.lock",
    "Pipfile.lock",
    "Cargo.lock",
    "composer.lock",
    "Gemfile.lock",
    "go.sum",
    "flake.lock",
    "bun.lockb",
    "packages.lock.json",
}

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$")
_RENAME_FROM_RE = re.compile(r"^rename from (?P<path>.+)$")
_RENAME_TO_RE = re.compile(r"^rename to (?P<path>.+)$")
_HUNK_RE = re.compile(r"^@@ .* @@")


@dataclass
class FileDiff:
    """One file's worth of a unified diff."""

    path: str
    change_type: str = "modified"  # added | modified | deleted | renamed
    old_path: Optional[str] = None
    is_binary: bool = False
    additions: int = 0
    deletions: int = 0
    body: str = ""  # the raw patch text for this file (header + hunks)

    @property
    def basename(self) -> str:
        return Path(self.path).name

    @property
    def is_lockfile(self) -> bool:
        return self.basename in LOCKFILES or self.path.endswith(".lock")

    @property
    def is_noise(self) -> bool:
        """Files whose content we never send, but still mention by name."""
        return self.is_binary or self.is_lockfile

    def stat_line(self) -> str:
        verb = {
            "added": "added",
            "deleted": "deleted",
            "renamed": f"renamed from {self.old_path}",
            "modified": "modified",
        }[self.change_type]
        counts = f"+{self.additions} -{self.deletions}"
        return f"{self.path} ({verb}, {counts})"

    def hunk_headers(self) -> List[str]:
        return [ln for ln in self.body.splitlines() if _HUNK_RE.match(ln)]


@dataclass
class DiffBundle:
    files: List[FileDiff] = field(default_factory=list)

    @property
    def content_files(self) -> List[FileDiff]:
        """Files whose patch we are willing to show the model."""
        return [f for f in self.files if not f.is_noise]

    @property
    def skipped_files(self) -> List[FileDiff]:
        return [f for f in self.files if f.is_noise]

    @property
    def is_empty(self) -> bool:
        return not self.files

    @property
    def total_additions(self) -> int:
        return sum(f.additions for f in self.files)

    @property
    def total_deletions(self) -> int:
        return sum(f.deletions for f in self.files)


def _count_changes(body: str) -> tuple[int, int]:
    additions = deletions = 0
    for line in body.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
    return additions, deletions


def parse_diff(text: str) -> DiffBundle:
    """Parse the output of ``git diff`` into a :class:`DiffBundle`.

    Pure function: no git calls, so it is trivially unit-testable with fixtures.
    """
    bundle = DiffBundle()
    if not text.strip():
        return bundle

    # Split into per-file chunks on the "diff --git" boundary while keeping it.
    chunks: List[str] = []
    current: List[str] = []
    for line in text.splitlines():
        if line.startswith("diff --git "):
            if current:
                chunks.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        chunks.append("\n".join(current))

    for chunk in chunks:
        lines = chunk.splitlines()
        header = _DIFF_HEADER_RE.match(lines[0]) if lines else None
        if not header:
            continue

        path = header.group("b")
        old_path = header.group("a")
        file_diff = FileDiff(path=path, old_path=old_path, body=chunk)

        for line in lines[1:]:
            if line.startswith("new file mode"):
                file_diff.change_type = "added"
            elif line.startswith("deleted file mode"):
                file_diff.change_type = "deleted"
            elif line.startswith("Binary files") or "GIT binary patch" in line:
                file_diff.is_binary = True
            elif _RENAME_FROM_RE.match(line):
                file_diff.change_type = "renamed"
                file_diff.old_path = _RENAME_FROM_RE.match(line).group("path")
            elif _RENAME_TO_RE.match(line):
                file_diff.change_type = "renamed"
                file_diff.path = _RENAME_TO_RE.match(line).group("path")
            elif line.startswith("+++ b/"):
                file_diff.path = line[len("+++ b/") :]
            elif line.startswith("--- a/") and file_diff.change_type != "renamed":
                file_diff.old_path = line[len("--- a/") :]

        file_diff.additions, file_diff.deletions = _count_changes(chunk)
        bundle.files.append(file_diff)

    return bundle


def collect_staged(cwd: Optional[Path] = None) -> DiffBundle:
    """Read and parse the staged diff (``git diff --cached``)."""
    text = run_git(
        ["diff", "--cached", "--no-color", "--no-ext-diff", "-U3"],
        cwd=cwd,
    )
    return parse_diff(text)


def render_stat_summary(bundle: DiffBundle) -> str:
    lines = [f.stat_line() for f in bundle.files]
    return "\n".join(lines)


def render_for_prompt(bundle: DiffBundle, budget: int) -> str:
    """Return a diff view that fits inside ``budget`` characters.

    Condensation happens in three stages, applied only as needed:

    * **full** — the entire patch for every content file.
    * **headers** — file stat line plus its ``@@`` hunk headers.
    * **stat** — a single stat line per file.

    Skipped files (binaries, lockfiles) are always listed by name only.
    """
    if bundle.is_empty:
        return ""

    skipped = bundle.skipped_files
    skipped_note = ""
    if skipped:
        names = ", ".join(f.stat_line() for f in skipped)
        skipped_note = f"\n\n# Not shown (binary or lockfile): {names}\n"

    def assemble(bodies: List[str]) -> str:
        return "\n".join(bodies).strip() + skipped_note

    # Stage 1: everything, full fidelity.
    full = assemble([f.body for f in bundle.content_files])
    if len(full) <= budget:
        return full

    # Stage 2: stat line + hunk headers per file.
    header_blocks: List[str] = []
    for f in bundle.content_files:
        headers = f.hunk_headers()
        block = f"# {f.stat_line()}"
        if headers:
            block += "\n" + "\n".join(headers)
        header_blocks.append(block)
    headers_view = assemble(header_blocks)
    if len(headers_view) <= budget:
        return headers_view

    # Stage 3: one stat line per file, hard-truncated to the budget.
    stat_view = assemble([f"# {f.stat_line()}" for f in bundle.content_files])
    if len(stat_view) <= budget:
        return stat_view
    return stat_view[: budget - 3].rstrip() + "..."
