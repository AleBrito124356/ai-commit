"""Collect and shape the staged diff for the model.

Two responsibilities:

1. Parse ``git diff --cached`` into structured per-file records so we can drop
   noise (binaries, lockfiles, generated or ignored files) and reason about size.
2. Render a prompt-sized view of the diff with a per-file budget allocator.

The allocator is what decides how good the model's input is. Every file is
always listed, but instead of an all-or-nothing switch, the character budget is
shared out by *signal*: source code outranks config, config outranks tests,
tests outrank docs, and data files come last. Small files get their full patch;
a file too big for its fair share is condensed to its hunk headers plus the
first changed lines of each hunk; only when even that does not fit does a file
fall back to a one-line stat. One huge fixture can no longer push the one line
of real code out of the prompt.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .gitutil import GitError, run_git

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
    "uv.lock",
    "pdm.lock",
    "mix.lock",
    "pubspec.lock",
    "Podfile.lock",
}

# Build output / vendored code: any path segment with one of these names.
GENERATED_DIRS = {"dist", "vendor", "node_modules", "__pycache__", ".next", ".nuxt"}
# ...and these only as the first path segment (``tools/build/x.py`` is source).
GENERATED_TOP_DIRS = {"build", "coverage", "htmlcov"}
GENERATED_SUFFIXES = (
    ".min.js",
    ".min.css",
    ".map",
    ".snap",
    ".pyc",
    ".pb.go",
    "_pb2.py",
    "_pb2_grpc.py",
    ".g.dart",
    ".designer.cs",
)
_GENERATED_MARKER_RE = re.compile(
    r"@generated|code generated .* do not edit|auto-?generated.*do not (edit|modify)",
    re.IGNORECASE,
)

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$")
_RENAME_FROM_RE = re.compile(r"^rename from (?P<path>.+)$")
_RENAME_TO_RE = re.compile(r"^rename to (?P<path>.+)$")
_HUNK_RE = re.compile(r"^@@ .* @@")
_HUNK_CONTEXT_RE = re.compile(r"^@@ [^@]* @@ ?(?P<context>.*)$")

# --------------------------------------------------------------------------- #
# File classification
# --------------------------------------------------------------------------- #

CI_FILES = {
    ".gitlab-ci.yml",
    "azure-pipelines.yml",
    "Jenkinsfile",
    ".travis.yml",
    "bitbucket-pipelines.yml",
    "appveyor.yml",
    ".drone.yml",
    "cloudbuild.yaml",
    "codecov.yml",
    ".codecov.yml",
}
CI_DIRS = (".github/workflows/", ".github/actions/", ".circleci/", ".buildkite/", ".gitlab/")
BUILD_FILES = {
    "package.json",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "MANIFEST.in",
    "Pipfile",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "Gemfile",
    "composer.json",
    "Makefile",
    "CMakeLists.txt",
    "meson.build",
    "BUILD",
    "BUILD.bazel",
    "WORKSPACE",
    "environment.yml",
    "tox.ini",
    "noxfile.py",
    ".nvmrc",
    ".python-version",
    ".tool-versions",
    "runtime.txt",
}
BUILD_SUFFIXES = (".gemspec", ".csproj", ".fsproj", ".vbproj", ".sln", ".cmake")
DOC_FILES = {"LICENSE", "COPYING", "AUTHORS", "NOTICE", "CHANGELOG", "CONTRIBUTING", "README"}
DOC_SUFFIXES = (".md", ".markdown", ".rst", ".adoc", ".asciidoc", ".org", ".txt")
DOC_DIRS = {"docs", "doc", "documentation"}
TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs", "testing", "e2e", "__mocks__"}
_TEST_NAME_RE = re.compile(
    r"^(test_.+\.py|.+_test\.(py|go|rb|exs?)|conftest\.py|.+\.(test|spec)\.[cm]?[jt]sx?"
    r"|.+(Test|Tests|Spec)\.(java|kt|cs|swift|scala)|.+_spec\.rb)$"
)
DATA_DIRS = {
    "data",
    "fixtures",
    "fixture",
    "testdata",
    "test-data",
    "samples",
    "seeds",
    "datasets",
    "__snapshots__",
    "snapshots",
    "mocks",
}
DATA_SUFFIXES = (".csv", ".tsv", ".ndjson", ".jsonl", ".parquet", ".geojson")
DATA_IN_DIR_SUFFIXES = (".json", ".xml", ".yaml", ".yml", ".sql", ".txt", ".html", ".csv")
CONFIG_SUFFIXES = (".ini", ".cfg", ".toml", ".conf", ".properties", ".yaml", ".yml", ".json", ".env")
CONFIG_DIRS = {".vscode", ".idea", ".devcontainer", ".husky", ".github"}
ASSET_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".bmp", ".avif",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp3", ".mp4", ".wav", ".ogg", ".webm", ".mov", ".pdf",
)

# Relative weight of each category when sharing the prompt budget.
CATEGORY_WEIGHT: Dict[str, float] = {
    "source": 4.0,
    "build": 2.0,
    "ci": 2.0,
    "config": 2.0,
    "test": 1.5,
    "docs": 1.0,
    "data": 0.5,
    "asset": 0.3,
}


def classify_path(path: str) -> str:
    """Classify a repository path.

    Returns one of ``ci``, ``build``, ``data``, ``test``, ``docs``, ``config``,
    ``asset`` or ``source``.
    """
    posix = path.replace("\\", "/")
    pure = PurePosixPath(posix)
    name = pure.name
    lower = name.lower()
    dirs = [part.lower() for part in pure.parts[:-1]]

    if name in CI_FILES or any(posix.startswith(d) for d in CI_DIRS) or (
        posix == ".github/dependabot.yml"
    ):
        return "ci"
    if (
        name in BUILD_FILES
        or name in LOCKFILES
        or lower.endswith(".lock")
        or lower.endswith(BUILD_SUFFIXES)
        or lower.endswith((".gradle", ".gradle.kts"))
        or re.match(r"^(requirements|constraints)([-_.].*)?\.(txt|in)$", lower)
        or "requirements" in dirs and lower.endswith(".txt")
        or lower.startswith("dockerfile")
        or lower.endswith(".dockerfile")
        or re.match(r"^(docker-)?compose([-.].*)?\.ya?ml$", lower)
    ):
        return "build"
    if lower.endswith(ASSET_SUFFIXES):
        return "asset"
    if lower.endswith(DATA_SUFFIXES) or (
        lower.endswith(DATA_IN_DIR_SUFFIXES) and any(d in DATA_DIRS for d in dirs)
    ):
        return "data"
    if any(d in TEST_DIRS for d in dirs) or _TEST_NAME_RE.match(name):
        return "test"
    stem_upper = name.split(".", 1)[0].upper()
    if (
        lower.endswith(DOC_SUFFIXES)
        or stem_upper in DOC_FILES
        or any(d in DOC_DIRS for d in dirs)
    ):
        return "docs"
    if (
        name.startswith(".")
        or any(d in CONFIG_DIRS for d in dirs)
        or lower.endswith(CONFIG_SUFFIXES)
        or name == "CODEOWNERS"
        or re.match(r".+\.config\.[cm]?[jt]s$", lower)
    ):
        return "config"
    return "source"


# --------------------------------------------------------------------------- #
# Parsed diff model
# --------------------------------------------------------------------------- #


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
    # Marked by path/content heuristics or ``linguist-generated`` attributes.
    generated: bool = False
    # Matched a user ``ignore_paths`` pattern.
    ignored: bool = False

    @property
    def basename(self) -> str:
        return Path(self.path).name

    @property
    def is_lockfile(self) -> bool:
        return self.basename in LOCKFILES or self.path.endswith(".lock")

    @property
    def is_generated(self) -> bool:
        return self.generated or looks_generated(self.path)

    @property
    def is_noise(self) -> bool:
        """Files whose content we never send, but still mention by name."""
        return self.is_binary or self.is_lockfile or self.is_generated or self.ignored

    @property
    def noise_reason(self) -> Optional[str]:
        if self.ignored:
            return "ignored"
        if self.is_binary:
            return "binary"
        if self.is_lockfile:
            return "lockfile"
        if self.is_generated:
            return "generated"
        return None

    @property
    def category(self) -> str:
        return classify_path(self.path)

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

    def hunks(self) -> List[Tuple[str, List[str]]]:
        """``(hunk header, changed lines)`` pairs; context lines are dropped."""
        result: List[Tuple[str, List[str]]] = []
        current: Optional[Tuple[str, List[str]]] = None
        for line in self.body.splitlines():
            if _HUNK_RE.match(line):
                current = (line, [])
                result.append(current)
            elif current is not None and _is_change_line(line):
                current[1].append(line)
        return result

    def hunk_contexts(self) -> List[str]:
        """The function/class context git prints after each ``@@`` header."""
        out: List[str] = []
        for header in self.hunk_headers():
            match = _HUNK_CONTEXT_RE.match(header)
            if match and match.group("context").strip():
                out.append(match.group("context").strip())
        return out

    def added_lines(self) -> List[str]:
        return [ln[1:] for ln in self.body.splitlines() if ln.startswith("+") and not ln.startswith("+++")]

    def removed_lines(self) -> List[str]:
        return [ln[1:] for ln in self.body.splitlines() if ln.startswith("-") and not ln.startswith("---")]


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


def _is_change_line(line: str) -> bool:
    return (line.startswith("+") and not line.startswith("+++")) or (
        line.startswith("-") and not line.startswith("---")
    )


def _count_changes(body: str) -> Tuple[int, int]:
    additions = deletions = 0
    for line in body.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
    return additions, deletions


def looks_generated(path: str) -> bool:
    """Path-based detection of build output, minified and generated files."""
    posix = path.replace("\\", "/")
    parts = [p for p in posix.split("/") if p]
    if not parts:
        return False
    lower = parts[-1].lower()
    if lower.endswith(GENERATED_SUFFIXES):
        return True
    if parts[0].lower() in GENERATED_TOP_DIRS and len(parts) > 1:
        return True
    return any(p.lower() in GENERATED_DIRS for p in parts[:-1])


def _has_generated_marker(body: str) -> bool:
    """``@generated`` / ``Code generated ... DO NOT EDIT`` in the first lines."""
    seen = 0
    for line in body.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            if _GENERATED_MARKER_RE.search(line):
                return True
            seen += 1
            if seen >= 8:
                break
    return False


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
        file_diff = FileDiff(path=path, old_path=old_path, body=chunk.strip("\n"))

        for line in lines[1:]:
            if line.startswith("@@"):
                break  # headers are done; the rest is patch content
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
        file_diff.generated = _has_generated_marker(chunk)
        bundle.files.append(file_diff)

    return bundle


# --------------------------------------------------------------------------- #
# Ignore patterns and .gitattributes
# --------------------------------------------------------------------------- #


def path_matches(path: str, pattern: str) -> bool:
    """Glob match used by ``ignore_paths``.

    ``dir/`` matches everything under a directory of that name; other patterns
    are ``fnmatch`` globs tried against the full path and the basename, so
    ``*.pb.go``, ``src/generated/*`` and ``**/fixtures/*.json`` all work.
    """
    pattern = pattern.strip().replace("\\", "/")
    if pattern.startswith("./"):
        pattern = pattern[2:]
    pattern = pattern.lstrip("/")
    if not pattern:
        return False
    posix = path.replace("\\", "/")
    if pattern.endswith("/"):
        return posix.startswith(pattern) or f"/{pattern}" in f"/{posix}"
    name = posix.rsplit("/", 1)[-1]
    candidates = {pattern}
    stripped = pattern
    while stripped.startswith("**/"):
        stripped = stripped[3:]
        candidates.add(stripped)
    return any(
        fnmatch.fnmatchcase(posix, c) or ("/" not in c and fnmatch.fnmatchcase(name, c))
        or fnmatch.fnmatchcase(posix, f"*/{c}")
        for c in candidates
    )


def _generated_by_attributes(paths: Sequence[str], cwd: Optional[Path]) -> set:
    """Paths marked ``linguist-generated`` or ``linguist-vendored`` in .gitattributes."""
    marked: set = set()
    for start in range(0, len(paths), 50):
        batch = list(paths[start : start + 50])
        try:
            out = run_git(
                ["check-attr", "linguist-generated", "linguist-vendored", "--", *batch],
                cwd=cwd,
            )
        except GitError:
            return marked
        for line in out.splitlines():
            # "<path>: <attribute>: <value>"
            try:
                path, _attr, value = line.rsplit(": ", 2)
            except ValueError:
                continue
            if value.strip() in {"set", "true"}:
                marked.add(path)
    return marked


def annotate_bundle(
    bundle: DiffBundle,
    ignore_paths: Iterable[str] = (),
    cwd: Optional[Path] = None,
    check_attributes: bool = True,
) -> DiffBundle:
    """Mark ignored and ``.gitattributes``-generated files in place."""
    patterns = [p for p in ignore_paths if p and p.strip()]
    for f in bundle.files:
        if patterns and any(path_matches(f.path, p) for p in patterns):
            f.ignored = True
    if check_attributes and bundle.files:
        marked = _generated_by_attributes([f.path for f in bundle.files], cwd)
        for f in bundle.files:
            if f.path in marked:
                f.generated = True
    return bundle


def collect_staged(
    cwd: Optional[Path] = None, ignore_paths: Iterable[str] = ()
) -> DiffBundle:
    """Read and parse the staged diff (``git diff --cached``)."""
    text = run_git(
        ["diff", "--cached", "--no-color", "--no-ext-diff", "-U3", "-M"],
        cwd=cwd,
    )
    return annotate_bundle(parse_diff(text), ignore_paths, cwd=cwd)


# --------------------------------------------------------------------------- #
# Prompt rendering
# --------------------------------------------------------------------------- #

MAX_LINE_CHARS = 240


@dataclass
class RenderedDiff:
    """A prompt-sized diff view plus a record of how it was produced."""

    text: str
    budget: int
    stage: str  # empty | full | mixed | stat | truncated
    modes: Dict[str, str] = field(default_factory=dict)  # path -> mode
    source_chars: int = 0  # size of the uncondensed view

    def describe(self) -> str:
        """One-line summary for ``--verbose``."""
        groups: Dict[str, List[str]] = {}
        for path, mode in self.modes.items():
            groups.setdefault(mode, []).append(path)
        parts = [
            f"{mode}: {', '.join(paths)}"
            for mode, paths in sorted(groups.items(), key=lambda kv: kv[0])
        ]
        detail = "; ".join(parts) if parts else "no files"
        return (
            f"diff stage '{self.stage}': {self.source_chars:,} -> "
            f"{len(self.text):,} chars (budget {self.budget:,}) - {detail}"
        )


def render_stat_summary(bundle: DiffBundle) -> str:
    lines = [f.stat_line() for f in bundle.files]
    return "\n".join(lines)


def _skipped_note(skipped: List[FileDiff], room: int) -> str:
    """Name every skipped file, compressing the list if it would hog the budget."""
    if not skipped:
        return ""
    label = "\n\n# Not shown (binary, lockfile, generated or ignored): "
    detailed = label + ", ".join(f.stat_line() for f in skipped) + "\n"
    if len(detailed) <= room:
        return detailed
    names = label + ", ".join(f.path for f in skipped) + "\n"
    if len(names) <= room:
        return names
    return f"{label}{len(skipped)} files\n"


def _clip(line: str) -> str:
    return line if len(line) <= MAX_LINE_CHARS else line[: MAX_LINE_CHARS - 3] + "..."


def _condense(f: FileDiff, limit: int) -> str:
    """Stat line + hunk headers + the first N changed lines of each hunk.

    ``N`` is the largest value (binary search) whose rendering fits ``limit``.
    If even the bare hunk headers do not fit, as many headers as fit are kept.
    """
    head = f"# {f.stat_line()} [condensed]"
    if len(head) > limit:
        return f"# {f.stat_line()}"[:limit]
    hunks = f.hunks()
    if not hunks:
        return head

    def render(n: int) -> str:
        out = [head]
        for header, changed in hunks:
            out.append(_clip(header))
            out.extend(_clip(line) for line in changed[:n])
            if len(changed) > n:
                out.append(f"  ... {len(changed) - n} more changed lines")
        return "\n".join(out)

    if len(render(0)) > limit:
        out = [head]
        size = len(head)
        for i, (header, _changed) in enumerate(hunks):
            remaining = len(hunks) - i
            marker = f"\n  ... {remaining} more hunks"
            line = _clip(header)
            if size + 1 + len(line) + len(f"\n  ... {remaining - 1} more hunks") > limit:
                if size + len(marker) <= limit:
                    out.append(marker.lstrip("\n"))
                break
            out.append(line)
            size += 1 + len(line)
        return "\n".join(out)

    lo, hi = 0, max(len(changed) for _h, changed in hunks)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(render(mid)) <= limit:
            lo = mid
        else:
            hi = mid - 1
    return render(lo)


def plan_render(bundle: DiffBundle, budget: int) -> RenderedDiff:
    """Build a diff view that fits inside ``budget`` characters.

    * **full** — everything fits: every content file's full patch.
    * **mixed** — each file first gets its one-line stat; the rest of the
      budget is shared by weighted fair share (source > config > tests > docs >
      data). Files are visited cheapest-first, so small files keep their full
      patch and hand unused budget on to bigger ones; a file that does not fit
      its share is condensed to fit it.
    * **stat** / **truncated** — only stat lines fit (cut to the budget if even
      those are too long).

    Skipped files (binary, lockfile, generated, ignored) are always named but
    their content is never included. The result never exceeds ``budget``.
    """
    budget = max(0, int(budget))
    if bundle.is_empty:
        return RenderedDiff(text="", budget=budget, stage="empty")

    content = bundle.content_files
    skipped = bundle.skipped_files
    modes: Dict[str, str] = {f.path: f"skipped:{f.noise_reason}" for f in skipped}

    full_note = _skipped_note(skipped, room=10**9)
    full_text = "\n".join(f.body for f in content).strip() + full_note
    source_chars = len(full_text)
    if len(full_text) <= budget:
        modes.update({f.path: "full" for f in content})
        return RenderedDiff(full_text, budget, "full", modes, source_chars)

    note = _skipped_note(skipped, room=max(budget // 4, 0))
    stats = {f.path: f"# {f.stat_line()}" for f in content}
    joins = max(len(content) - 1, 0)
    reserved = sum(len(s) for s in stats.values()) + joins + len(note)

    if reserved > budget or not content:
        stat_view = ("\n".join(stats[f.path] for f in content)).strip() + note
        modes.update({f.path: "stat" for f in content})
        if len(stat_view) <= budget:
            return RenderedDiff(stat_view, budget, "stat", modes, source_chars)
        cut = stat_view[: max(budget - 3, 0)].rstrip() + "..." if budget >= 3 else ""
        return RenderedDiff(cut[:budget], budget, "truncated", modes, source_chars)

    pool = budget - reserved
    blocks: Dict[str, str] = {}

    def weight(f: FileDiff) -> float:
        return CATEGORY_WEIGHT.get(f.category, 1.0)

    def extra_full(f: FileDiff) -> int:
        return max(len(f.body) - len(stats[f.path]), 0)

    order = sorted(content, key=lambda f: extra_full(f) / weight(f))
    remaining_weight = sum(weight(f) for f in content)
    for f in order:
        w = weight(f)
        share = int(pool * w / remaining_weight) if remaining_weight > 0 else pool
        remaining_weight -= w
        stat = stats[f.path]
        if extra_full(f) <= share:
            blocks[f.path] = f.body
            modes[f.path] = "full"
            pool -= extra_full(f)
            continue
        condensed = _condense(f, len(stat) + share)
        if len(condensed) > len(stat) and "\n" in condensed:
            blocks[f.path] = condensed
            modes[f.path] = "condensed"
            pool -= len(condensed) - len(stat)
        else:
            blocks[f.path] = stat
            modes[f.path] = "stat"

    text = "\n".join(blocks[f.path] for f in content) + note
    if len(text) > budget:  # pragma: no cover - defensive; accounting is exact
        text = text[: max(budget - 3, 0)] + "..."
    stage = "mixed" if any(m != "stat" for p, m in modes.items() if p in stats) else "stat"
    return RenderedDiff(text, budget, stage, modes, source_chars)


def render_for_prompt(bundle: DiffBundle, budget: int) -> str:
    """Return a diff view that fits inside ``budget`` characters.

    See :func:`plan_render` for how the budget is shared between files.
    """
    return plan_render(bundle, budget).text
