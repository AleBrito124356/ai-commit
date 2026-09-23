"""The ``local`` backend: rule-based drafts with no model and no network.

This is not AI. It reads the parsed diff and applies transparent rules:

* **type** — from what kind of files changed (tests only -> ``test``, docs only
  -> ``docs``, CI config -> ``ci``, manifests/lockfiles -> ``build``,
  whitespace-only -> ``style``) and, for source code, from simple signals
  (new files or new definitions -> ``feat``, added guards/exception handling in
  a small change -> ``fix``, only removals or renames -> ``refactor``);
* **scope** — the one module the change is about (``src/auth.py`` and
  ``tests/test_auth.py`` -> ``auth``) or the deepest common directory;
* **subject** — an imperative sentence built from the names git and the diff
  expose: new function/class names, the function context of each hunk
  (``@@ ... @@ def login``), file names;
* **body** — a short per-file summary.

The output is a starting point you review, always a valid Conventional Commit.
It powers ``--backend local`` (zero setup, air-gapped, CI), the demo mode in
the README, and the git hook's fallback when the configured backend is down.
"""

from __future__ import annotations

import re
from collections import Counter, OrderedDict
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional, Sequence

from .commit import CommitMessage, CommitResult, normalize_commit, parse_commit, validate_commit
from .config import Config
from .diff import DiffBundle, FileDiff, classify_path
from .llm import LLMError
from .pr import PRContext, PRResult

LOCAL_CANNOT_REWRITE = (
    "the local backend is rule-based and cannot rewrite free text; "
    "use --backend nim or --backend ollama for that"
)

# Definitions across common languages: def/class/function/fn/func/struct/...
_DEF_RE = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:pub(?:\([^)]*\))?\s+)?"
    r"(?:static\s+)?(?:abstract\s+)?"
    r"(?:def|class|function\*?|func|fn|interface|struct|enum|trait|module|protocol)\s+"
    r"(?:\([^)]*\)\s*)?"  # Go method receiver
    r"(?P<name>[A-Za-z_$][\w$]*)"
)
_ARROW_RE = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*"
    r"(?::[^=]+)?=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)"
)
_METHOD_RE = re.compile(
    r"^\s*(?:public|private|protected|internal)\s+(?:static\s+)?(?:async\s+)?"
    r"(?:[\w<>\[\],?]+\s+)+(?P<name>[A-Za-z_]\w*)\s*\("
)
_EXCEPTION_RE = re.compile(
    r"\b(?:raise|throw\s+new|throw|except|catch\s*\()\s*\(?\s*(?P<name>[A-Z]\w*(?:Error|Exception|Locked|Denied|Invalid|Timeout|NotFound|Failure|Fault|Exit)\w*)"
)
_GUARD_RE = re.compile(
    r"\b(if\s+not\b|if\s*\(?\s*!|is\s+None|is\s+not\s+None|===?\s*null|!==?\s*null|"
    r"===?\s*undefined|\?\?|\?\.|raise\b|throw\b|except\b|catch\b|try\s*[:{]|"
    r"return\s+(None|null|nil|false)\b|guard\b|assert\b|Math\.(min|max)|clamp\()"
)
_PERF_RE = re.compile(
    r"lru_cache|functools\.cache|@cache\b|@cached|memoi[sz]e|useMemo|useCallback|"
    r"\bcache\s*=|\.cache\(|lazy_static|once_cell|Promise\.all\(|asyncio\.gather\("
)
_DEP_LINE_RE = re.compile(
    r'^\s*("?[@\w./-]+"?\s*[:=]\s*"?[\^~<>=!*]?\s*v?\d'  # "react": "^18", foo = "1.2"
    r'|["\']?[\w.-]+(\[[\w,.-]+\])?\s*(==|>=|<=|~=|!=|>|<)\s*\d'  # httpx>=0.27, "x[y]==1"
    r"|[\w.-]+\s*=\s*\{?\s*version"  # serde = { version = ...
    r"|<version>|<dependency>|</dependency>|<groupId>|<artifactId>"  # Maven
    r"|require\s+[\w./-]+\s+v\d|[\w./-]+\s+v\d+\.\d)"  # go.mod
)

_GENERIC_DIRS = {
    "src", "lib", "libs", "app", "apps", "pkg", "internal", "source", "sources",
    "packages", "tests", "test", "spec", "specs", "docs", "doc", "cmd", "main",
    "java", "kotlin", "python", "js", "ts", "scripts", "code", ".", "",
}
_INDEX_STEMS = {"__init__", "index", "main", "mod", "lib", "__main__", "init"}
_TEST_AFFIX_RE = re.compile(
    r"^(test_|tests_)|(_test|_tests|_spec|\.test|\.spec|Test|Tests|Spec)$"
)

# Alternative types to offer for each primary type, in order.
_ALT_TYPES: Dict[str, Sequence[str]] = {
    "feat": ("refactor", "fix"),
    "fix": ("refactor", "feat"),
    "refactor": ("feat", "fix"),
    "perf": ("refactor", "feat"),
    "style": ("refactor", "chore"),
    "test": ("chore", "refactor"),
    "docs": ("chore",),
    "ci": ("build", "chore"),
    "build": ("chore",),
    "chore": ("build", "refactor"),
    "revert": ("fix",),
}

_TEXT = {
    "en": {
        "add": "add {x}",
        "update": "update {x}",
        "remove": "remove {x}",
        "rename": "rename {x} to {y}",
        "move": "move {x} to {y}",
        "format": "format {x}",
        "handle": "handle {x} in {y}",
        "cache": "cache {x}",
        "tests_add": "add tests for {x}",
        "tests_update": "update tests for {x}",
        "deps": "update dependencies",
        "regen": "regenerate {x}",
        "with_tests": " with tests",
        "and": "and",
        "more": "{n} more",
        "adds": "adds {x}",
        "removes": "removes {x}",
        "in": "in {x}",
        "renamed_from": "renamed from {x}",
        "whitespace": "whitespace only",
        "new_file": "new file",
        "deleted_file": "deleted",
        "more_files": "- ... and {n} more files",
        "not_shown": "Not analysed ({reason}): {x}",
        "workflow": "{x} workflow",
    },
    "es": {
        "add": "añade {x}",
        "update": "actualiza {x}",
        "remove": "elimina {x}",
        "rename": "renombra {x} a {y}",
        "move": "mueve {x} a {y}",
        "format": "formatea {x}",
        "handle": "maneja {x} en {y}",
        "cache": "cachea {x}",
        "tests_add": "añade pruebas para {x}",
        "tests_update": "actualiza pruebas de {x}",
        "deps": "actualiza dependencias",
        "regen": "regenera {x}",
        "with_tests": " con pruebas",
        "and": "y",
        "more": "{n} más",
        "adds": "añade {x}",
        "removes": "elimina {x}",
        "in": "en {x}",
        "renamed_from": "renombrado desde {x}",
        "whitespace": "solo espacios",
        "new_file": "archivo nuevo",
        "deleted_file": "eliminado",
        "more_files": "- ... y {n} archivos más",
        "not_shown": "Sin analizar ({reason}): {x}",
        "workflow": "flujo {x}",
    },
}


def _t(config: Config, key: str, **kwargs: Any) -> str:
    table = _TEXT.get(config.language, _TEXT["en"])
    return table[key].format(**kwargs)


# --------------------------------------------------------------------------- #
# Per-file analysis
# --------------------------------------------------------------------------- #


@dataclass
class FileInfo:
    diff: FileDiff
    category: str
    added_defs: List[str] = field(default_factory=list)
    removed_defs: List[str] = field(default_factory=list)
    contexts: List[str] = field(default_factory=list)
    exceptions: List[str] = field(default_factory=list)
    guard: bool = False
    perf: bool = False
    whitespace_only: bool = False
    dependency_only: bool = False

    @property
    def path(self) -> str:
        return self.diff.path

    @property
    def changed(self) -> int:
        return self.diff.additions + self.diff.deletions


def _definition_name(line: str) -> Optional[str]:
    for regex in (_DEF_RE, _ARROW_RE, _METHOD_RE):
        match = regex.match(line)
        if match:
            return match.group("name")
    return None


def _unique(items: Sequence[str]) -> List[str]:
    return list(OrderedDict.fromkeys(i for i in items if i))


_HUNK_CONTEXT_RE = re.compile(r"^@@ [^@]* @@ ?(?P<context>.*)$")


def enclosing_definitions(f: FileDiff) -> List[str]:
    """Names of the functions/classes that contain the changed lines.

    Starts from the context git prints after ``@@`` and updates it with any
    definition seen on an unchanged line inside the hunk, so a change in the
    first lines of ``def login`` is attributed to ``login`` even when git had
    no earlier line to show as hunk context.
    """
    names: List[str] = []
    current: Optional[str] = None
    in_hunk = False
    decorating = False  # a changed "@decorator" line waits for its def
    for line in f.body.splitlines():
        if line.startswith("@@"):
            in_hunk = True
            decorating = False
            match = _HUNK_CONTEXT_RE.match(line)
            context = match.group("context").strip() if match else ""
            current = _definition_name(context) if context else None
            continue
        if not in_hunk or not line:
            continue
        marker, text = line[0], line[1:]
        name = _definition_name(text)
        if decorating and name:
            names.append(name)
            decorating = False
        if marker == " " and name:
            current = name
        elif marker in "+-" and name is None:
            if text.strip().startswith("@"):
                decorating = True
            elif current:
                names.append(current)
    return _unique(names)


def _squash(lines: Sequence[str]) -> List[str]:
    """Lines with all whitespace removed, sorted: equal means whitespace-only."""
    return sorted("".join(line.split()) for line in lines if line.strip())


def analyze_file(f: FileDiff) -> FileInfo:
    category = "generated" if f.is_generated else classify_path(f.path)
    if f.is_lockfile:
        category = "build"
    info = FileInfo(diff=f, category=category)
    if f.is_binary:
        return info
    added = f.added_lines()
    removed = f.removed_lines()
    added_defs = _unique([_definition_name(ln) or "" for ln in added])
    removed_defs = _unique([_definition_name(ln) or "" for ln in removed])
    # A definition that appears on both sides was edited, not added/removed.
    info.added_defs = [d for d in added_defs if d not in removed_defs]
    info.removed_defs = [d for d in removed_defs if d not in added_defs]
    info.contexts = enclosing_definitions(f)
    info.exceptions = _unique(
        [m.group("name") for ln in added for m in _EXCEPTION_RE.finditer(ln)]
    )
    info.guard = any(_GUARD_RE.search(ln) for ln in added)
    info.perf = any(_PERF_RE.search(ln) for ln in added)
    if f.change_type == "modified" and (added or removed):
        info.whitespace_only = _squash(added) == _squash(removed)
    if category == "build" and (added or removed):
        changed_lines = [ln for ln in added + removed if ln.strip()]
        info.dependency_only = f.is_lockfile or all(
            _DEP_LINE_RE.match(ln) for ln in changed_lines
        )
    return info


# --------------------------------------------------------------------------- #
# Naming helpers
# --------------------------------------------------------------------------- #


def _stem(path: str) -> str:
    name = PurePosixPath(path).name
    return name.split(".", 1)[0] if not name.startswith(".") else name


def _topic(info: FileInfo) -> str:
    """The module a file is about: ``tests/test_auth.py`` -> ``auth``."""
    pure = PurePosixPath(info.path)
    stem = _stem(info.path)
    if info.category == "test":
        stem = _TEST_AFFIX_RE.sub("", stem) or stem
    if stem.lower() in _INDEX_STEMS and len(pure.parts) > 1:
        return pure.parts[-2]
    return stem


def _display_name(info: FileInfo, config: Config) -> str:
    path = info.path
    if info.category == "ci" and "/workflows/" in f"/{path}":
        return _t(config, "workflow", x=_stem(path))
    if info.category in {"source", "test"}:
        return _topic(info)
    if info.category == "docs":
        return _stem(path) if _stem(path).isupper() else PurePosixPath(path).name
    return PurePosixPath(path).name


def _join(names: Sequence[str], config: Config, limit: int = 2) -> str:
    names = _unique(list(names))
    word = _t(config, "and")
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) <= limit:
        return f"{', '.join(names[:-1])} {word} {names[-1]}"
    shown = ", ".join(names[:limit])
    return f"{shown} {word} {_t(config, 'more', n=len(names) - limit)}"


def _scope_from(infos: Sequence[FileInfo]) -> Optional[str]:
    relevant = [i for i in infos if i.category in {"source", "test"}] or list(infos)
    topics = _unique([_topic(i) for i in relevant])
    if len(topics) == 1:
        return _clean_scope(topics[0])
    return _common_dir_scope(relevant)


def _common_dir_scope(infos: Sequence[FileInfo]) -> Optional[str]:
    """Deepest non-generic directory shared by every file (``src`` is generic)."""
    relevant = [i for i in infos if i.category in {"source", "test"}] or list(infos)
    dirs = [PurePosixPath(i.path).parts[:-1] for i in relevant]
    common: List[str] = []
    for parts in zip(*dirs):
        if len(set(parts)) != 1:
            break
        common.append(parts[0])
    for part in reversed(common):
        if part.lower() not in _GENERIC_DIRS:
            return _clean_scope(part)
    return None


def _clean_scope(value: str) -> Optional[str]:
    cleaned = re.sub(r"[^a-z0-9_./-]+", "-", value.lower()).strip("-.")
    return cleaned[:24] or None


# --------------------------------------------------------------------------- #
# Drafting
# --------------------------------------------------------------------------- #


@dataclass
class _Draft:
    type: str
    subject: str
    scope: Optional[str]


def _source_draft(sources: List[FileInfo], config: Config) -> _Draft:
    added_files = [i for i in sources if i.diff.change_type == "added"]
    deleted = [i for i in sources if i.diff.change_type == "deleted"]
    renamed = [i for i in sources if i.diff.change_type == "renamed"]
    modified = [i for i in sources if i.diff.change_type == "modified"]
    new_defs = _unique([d for i in modified + renamed for d in i.added_defs])
    gone_defs = _unique([d for i in modified + renamed for d in i.removed_defs])
    contexts = _unique([c for i in modified for c in i.contexts])
    exceptions = _unique([e for i in modified for e in i.exceptions])
    changed = sum(i.changed for i in modified)
    additions = sum(i.diff.additions for i in sources)
    deletions = sum(i.diff.deletions for i in sources)
    scope = _scope_from(sources)
    names = [_display_name(i, config) for i in sources]

    if deleted and len(deleted) == len(sources):
        return _Draft("refactor", _t(config, "remove", x=_join(names, config)), scope)
    if renamed and len(renamed) == len(sources) and not new_defs and not gone_defs:
        if len(renamed) == 1:
            r = renamed[0].diff
            old, new = PurePosixPath(r.old_path or ""), PurePosixPath(r.path)
            if old.name == new.name:
                subject = _t(config, "move", x=old.name, y=f"{new.parent}/")
            else:
                subject = _t(config, "rename", x=old.name, y=new.name)
        else:
            subject = _t(config, "move", x=_join(names, config), y=f"{PurePosixPath(renamed[0].path).parent}/")
        return _Draft("refactor", subject, scope)
    if added_files:
        return _Draft("feat", _t(config, "add", x=_join([_display_name(i, config) for i in added_files], config)), scope)
    if new_defs:
        return _Draft("feat", _t(config, "add", x=_join(new_defs, config)), scope)
    focus = _join(contexts or names, config)
    if any(i.perf for i in modified):
        return _Draft("perf", _t(config, "cache", x=focus), scope)
    if any(i.guard for i in modified) and changed <= 30:
        if exceptions:
            return _Draft("fix", _t(config, "handle", x=exceptions[0], y=focus), scope)
        return _Draft("fix", _t(config, "update", x=focus), scope)
    if gone_defs:
        return _Draft("refactor", _t(config, "remove", x=_join(gone_defs, config)), scope)
    kind = "feat" if additions > deletions else "refactor"
    return _Draft(kind, _t(config, "update", x=focus), scope)


def _non_source_draft(all_infos: List[FileInfo], config: Config) -> _Draft:
    # Generated files ride along (snapshots with tests, bundles with config);
    # they only decide the type when nothing else changed.
    infos = [i for i in all_infos if i.category != "generated"] or all_infos
    categories = {i.category for i in infos}
    names = [_display_name(i, config) for i in infos]
    all_added = all(i.diff.change_type == "added" for i in infos)
    all_deleted = all(i.diff.change_type == "deleted" for i in infos)
    verb = "add" if all_added else "remove" if all_deleted else "update"

    if categories == {"generated"}:
        return _Draft("chore", _t(config, "regen", x=_join(names, config)), None)
    if categories <= {"test", "data"} and "test" in categories:
        tests = [i for i in infos if i.category == "test"]
        topic = _join([_topic(i) for i in tests], config)
        grew = any(i.added_defs for i in tests) or all(
            i.diff.change_type == "added" for i in tests
        )
        key = "tests_add" if grew else "tests_update"
        return _Draft("test", _t(config, key, x=topic), _scope_from(tests))
    if all(i.whitespace_only for i in infos):
        return _Draft("style", _t(config, "format", x=_join(names, config)), _scope_from(infos))
    if categories == {"docs"}:
        return _Draft("docs", _t(config, verb, x=_join(names, config)), None)
    if categories == {"ci"}:
        return _Draft("ci", _t(config, verb, x=_join(names, config)), None)
    if categories == {"build"}:
        if all(i.dependency_only for i in infos):
            return _Draft("build", _t(config, "deps"), "deps")
        return _Draft("build", _t(config, verb, x=_join(names, config)), None)
    # Mixed non-source changes: name the heaviest category's files.
    weights = Counter()
    for i in infos:
        weights[i.category] += i.changed or 1
    top = weights.most_common(1)[0][0]
    top_type = {"docs": "docs", "ci": "ci", "build": "build", "test": "test"}.get(top, "chore")
    top_names = [_display_name(i, config) for i in infos if i.category == top]
    others = [_display_name(i, config) for i in infos if i.category != top]
    if len(top_names) + len(others) <= 3:
        subject_names = top_names + others
    else:
        subject_names = top_names + [_t(config, "more", n=len(others))]
    return _Draft(top_type, _t(config, verb, x=_join(subject_names, config, limit=3)), None)


def _body(infos: List[FileInfo], skipped: List[FileDiff], config: Config) -> str:
    lines: List[str] = []
    shown = [i for i in infos if i.category != "generated"]
    for info in shown[:6]:
        f = info.diff
        details: List[str] = []
        if f.change_type == "added":
            details.append(_t(config, "new_file"))
        elif f.change_type == "deleted":
            details.append(_t(config, "deleted_file"))
        elif f.change_type == "renamed":
            details.append(_t(config, "renamed_from", x=f.old_path))
        if info.whitespace_only:
            details.append(_t(config, "whitespace"))
        if info.added_defs and f.change_type != "added":
            details.append(_t(config, "adds", x=", ".join(info.added_defs[:4])))
        elif info.added_defs:
            details.append(", ".join(info.added_defs[:4]))
        if info.removed_defs:
            details.append(_t(config, "removes", x=", ".join(info.removed_defs[:4])))
        if not info.added_defs and not info.removed_defs and info.contexts:
            details.append(_t(config, "in", x=", ".join(info.contexts[:3])))
        suffix = f": {'; '.join(details)}" if details else ""
        lines.append(f"- {f.path} (+{f.additions} -{f.deletions}){suffix}")
    if len(shown) > 6:
        lines.append(_t(config, "more_files", n=len(shown) - 6))
    if skipped:
        by_reason: Dict[str, List[str]] = OrderedDict()
        for f in skipped:
            by_reason.setdefault(f.noise_reason or "skipped", []).append(f.path)
        for reason, paths in by_reason.items():
            listed = ", ".join(paths[:4]) + (f" (+{len(paths) - 4})" if len(paths) > 4 else "")
            lines.append(_t(config, "not_shown", reason=reason, x=listed))
    return "\n".join(lines)


_ISSUE_RE = re.compile(
    r"(?i)\b(?P<verb>close[sd]?|fix(?:e[sd])?|resolve[sd]?|refs?|see)?\s*(?P<ref>[\w./-]*#\d+)"
)


def _footer_from_hint(hint: str) -> str:
    refs = []
    for match in _ISSUE_RE.finditer(hint or ""):
        verb = (match.group("verb") or "Refs").capitalize()
        if verb.lower() in {"ref", "see"}:
            verb = "Refs"
        refs.append(f"{verb} {match.group('ref')}")
    return "\n".join(_unique(refs))


_CODE_TYPES = {"feat", "fix", "refactor", "perf", "style", "test", "revert"}


def _mentions(subject: str, word: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", subject, re.IGNORECASE) is not None


def _non_redundant_scope(draft: "_Draft", infos: Sequence[FileInfo]) -> Optional[str]:
    """``feat(retry): add retry`` -> ``feat(payments): add retry`` or no scope."""
    if not draft.scope or not _mentions(draft.subject, draft.scope):
        return draft.scope
    parent = _common_dir_scope(infos)
    if parent and parent != draft.scope and not _mentions(draft.subject, parent):
        return parent
    return None


def _pick_type(preferred: str, config: Config) -> str:
    allowed = list(config.allowed_types)
    for candidate in (preferred, "chore", "refactor", "feat", "fix"):
        if candidate in allowed:
            return candidate
    return allowed[0]


def draft_commit(bundle: DiffBundle, config: Config, hint: str = "") -> CommitResult:
    """Build a rule-based :class:`CommitResult` from the parsed diff."""
    if bundle.is_empty:
        raise LLMError("nothing to describe: the diff is empty")

    analysed = [analyze_file(f) for f in bundle.files if not f.ignored]
    infos = [i for i in analysed if not (i.diff.is_binary and i.category != "asset")]
    infos = infos or analysed
    if not infos:
        infos = [analyze_file(f) for f in bundle.files]
    skipped = [f for f in bundle.files if f.is_noise and not f.is_lockfile]

    sources = [i for i in infos if i.category in {"source", "asset"}]
    real_sources = [i for i in sources if i.category == "source"]
    if real_sources and not all(i.whitespace_only for i in real_sources):
        draft = _source_draft(real_sources, config)
        if any(i.category == "test" for i in infos):
            with_tests = draft.subject + _t(config, "with_tests")
            if len(with_tests) <= config.subject_max_length:
                draft.subject = with_tests
            draft.scope = _scope_from(real_sources + [i for i in infos if i.category == "test"])
    elif sources and not real_sources:  # assets only (images, fonts)
        names = [_display_name(i, config) for i in sources]
        verb = "add" if all(i.diff.change_type == "added" for i in sources) else "update"
        draft = _Draft("chore", _t(config, verb, x=_join(names, config)), None)
    else:
        draft = _non_source_draft(infos, config)

    draft.scope = _non_redundant_scope(draft, infos)
    body = _body(infos, skipped, config)
    footer = _footer_from_hint(hint)

    def build(type_: str, scope: Optional[str]) -> CommitMessage:
        msg = CommitMessage(
            type=_pick_type(type_, config),
            scope=scope,
            subject=draft.subject,
            body=body,
            footer=footer,
        )
        return normalize_commit(msg, config)

    primary = build(draft.type, draft.scope)
    variants: List[CommitMessage] = []
    alt_types = [t for t in _ALT_TYPES.get(primary.type, ()) if t in config.allowed_types]
    if alt_types:
        variants.append(build(alt_types[0], primary.scope))
    if primary.scope:
        variants.append(replace(primary, scope=None))
    elif primary.type in _CODE_TYPES:
        broader = _common_dir_scope(infos)
        if broader and not _mentions(draft.subject, broader):
            variants.append(replace(primary, scope=broader))
    variants.extend(build(t, primary.scope) for t in alt_types[1:])

    seen = {primary.header()}
    alternatives: List[CommitMessage] = []
    for candidate in variants:
        if candidate.header() in seen or validate_commit(candidate, config):
            continue
        seen.add(candidate.header())
        alternatives.append(candidate)

    notes = ["local backend: rule-based draft, no model involved"]
    if hint and not footer:
        notes.append("local backend: free-text --hint is ignored (issue refs like #12 go to the footer)")
    return CommitResult(
        best=primary,
        alternatives=alternatives[: max(config.n_alternatives, 0)],
        raw="",
        attempts=0,
        notes=notes,
    )


# --------------------------------------------------------------------------- #
# PR drafting
# --------------------------------------------------------------------------- #

_TYPE_PRIORITY = ["feat", "fix", "perf", "refactor", "revert", "docs", "test", "build", "ci", "style", "chore"]
_TYPE_LABEL = {
    "en": {
        "feat": ("feature", "features"), "fix": ("fix", "fixes"),
        "perf": ("performance change", "performance changes"),
        "refactor": ("refactor", "refactors"), "revert": ("revert", "reverts"),
        "docs": ("docs change", "docs changes"), "test": ("test change", "test changes"),
        "build": ("build change", "build changes"), "ci": ("CI change", "CI changes"),
        "style": ("style change", "style changes"), "chore": ("chore", "chores"),
        None: ("other commit", "other commits"),
    },
    "es": {
        "feat": ("funcionalidad", "funcionalidades"), "fix": ("corrección", "correcciones"),
        "perf": ("mejora de rendimiento", "mejoras de rendimiento"),
        "refactor": ("refactorización", "refactorizaciones"), "revert": ("reversión", "reversiones"),
        "docs": ("cambio de docs", "cambios de docs"), "test": ("cambio de pruebas", "cambios de pruebas"),
        "build": ("cambio de build", "cambios de build"), "ci": ("cambio de CI", "cambios de CI"),
        "style": ("cambio de estilo", "cambios de estilo"), "chore": ("tarea", "tareas"),
        None: ("otro commit", "otros commits"),
    },
}
_PR_TEXT = {
    "en": {
        "summary": "{n} {commits} on `{head}` compared with `{base}`: {counts}. "
        "Touches {files} {file_word} (+{add} -{dele}){areas}.",
        "commit": ("commit", "commits"),
        "file": ("file", "files"),
        "areas": ", mostly in {x}",
        "tests_changed": "Test files changed: {x}",
        "no_tests": "No test files changed on this branch; verify manually.",
        "breaking": "Breaking change: {x}",
        "deletes": "Deletes {n} {file_word}: {x}",
        "deps": "Changes dependencies or build files: {x}",
        "ci": "Changes CI configuration: {x}",
        "migrations": "Includes database migrations: {x}",
        "no_risks": "No breaking commits, deletions, dependency, CI or migration changes detected.",
    },
    "es": {
        "summary": "{n} {commits} en `{head}` respecto a `{base}`: {counts}. "
        "Toca {files} {file_word} (+{add} -{dele}){areas}.",
        "commit": ("commit", "commits"),
        "file": ("archivo", "archivos"),
        "areas": ", sobre todo en {x}",
        "tests_changed": "Archivos de prueba modificados: {x}",
        "no_tests": "No cambió ningún archivo de prueba en esta rama; verificar a mano.",
        "breaking": "Cambio incompatible: {x}",
        "deletes": "Elimina {n} {file_word}: {x}",
        "deps": "Cambia dependencias o archivos de build: {x}",
        "ci": "Cambia la configuración de CI: {x}",
        "migrations": "Incluye migraciones de base de datos: {x}",
        "no_risks": "No se detectaron commits incompatibles, borrados, ni cambios de dependencias, CI o migraciones.",
    },
}


def _plural(pair: Sequence[str], n: int) -> str:
    return pair[0] if n == 1 else pair[1]


def _list(paths: Sequence[str], limit: int = 5) -> str:
    shown = ", ".join(f"`{p}`" for p in paths[:limit])
    return shown + (f" (+{len(paths) - limit})" if len(paths) > limit else "")


def draft_pr(context: PRContext, config: Config) -> PRResult:
    """Deterministic Summary / Changes / Testing / Risks from commits and diffstat."""
    lang = config.language if config.language in _PR_TEXT else "en"
    text = _PR_TEXT[lang]
    labels = _TYPE_LABEL[lang]

    oldest_first = list(reversed(context.commits))
    parsed = [(subject, parse_commit(subject)) for subject in oldest_first]

    def priority(item) -> int:
        msg = item[1]
        return _TYPE_PRIORITY.index(msg.type) if msg and msg.type in _TYPE_PRIORITY else len(_TYPE_PRIORITY)

    ranked = sorted(parsed, key=priority)  # stable: oldest first within a type
    lead_subject, lead = ranked[0]
    title = (lead.subject if lead else lead_subject).strip().rstrip(".")
    title = title[:1].upper() + title[1:]

    counts = Counter(msg.type if msg and msg.type in labels else None for _s, msg in parsed)
    ordered_types = [t for t in _TYPE_PRIORITY if t in counts] + ([None] if None in counts else [])
    count_text = ", ".join(f"{counts[t]} {_plural(labels[t], counts[t])}" for t in ordered_types)

    files = context.bundle.files
    add = sum(f.additions for f in files)
    dele = sum(f.deletions for f in files)
    top_dirs = Counter()
    for f in files:
        parts = PurePosixPath(f.path).parts
        top_dirs[f"{parts[0]}/" if len(parts) > 1 else parts[0]] += f.additions + f.deletions or 1
    areas = ""
    if len(top_dirs) > 1:
        areas = text["areas"].format(x=_join([d for d, _n in top_dirs.most_common(2)], config))
    summary = text["summary"].format(
        n=len(parsed),
        commits=_plural(text["commit"], len(parsed)),
        head=context.head_name,
        base=context.base,
        counts=count_text,
        files=len(files),
        file_word=_plural(text["file"], len(files)),
        add=add,
        dele=dele,
        areas=areas,
    )

    changes = [subject for subject, _msg in ranked]

    tests = [f.path for f in files if classify_path(f.path) == "test"]
    testing = [text["tests_changed"].format(x=_list(tests))] if tests else [text["no_tests"]]

    risks: List[str] = []
    for subject, msg in parsed:
        if msg is not None and msg.breaking:
            risks.append(text["breaking"].format(x=subject))
    deleted = [f.path for f in files if f.change_type == "deleted"]
    if deleted:
        risks.append(
            text["deletes"].format(n=len(deleted), file_word=_plural(text["file"], len(deleted)), x=_list(deleted))
        )
    build = [f.path for f in files if classify_path(f.path) == "build"]
    if build:
        risks.append(text["deps"].format(x=_list(build)))
    ci = [f.path for f in files if classify_path(f.path) == "ci"]
    if ci:
        risks.append(text["ci"].format(x=_list(ci)))
    migrations = [f.path for f in files if "migration" in f.path.lower()]
    if migrations:
        risks.append(text["migrations"].format(x=_list(migrations)))
    if not risks:
        risks.append(text["no_risks"])

    return PRResult(title=title, summary=summary, changes=changes, testing=testing, risks=risks)


# --------------------------------------------------------------------------- #
# The backend object
# --------------------------------------------------------------------------- #


class LocalBackend:
    """Backend that drafts from the diff with rules instead of a model.

    ``regenerate`` in the interactive loop rotates through the alternatives,
    since the rules are deterministic and would otherwise repeat themselves.
    """

    backend = "local"
    model = "rule-based"

    def __init__(self) -> None:
        self._drafts = 0
        self.last_finish_reason: Optional[str] = None
        self.last_attempts = 0

    def complete(
        self,
        system: str,
        user: str,
        temperature: float = 0.3,
        max_tokens: int = 1024,
    ) -> str:
        raise LLMError(LOCAL_CANNOT_REWRITE)

    def draft_commit(self, bundle: DiffBundle, config: Config, hint: str = "") -> CommitResult:
        result = draft_commit(bundle, config, hint=hint)
        candidates = result.all
        shift = self._drafts % len(candidates)
        self._drafts += 1
        if shift:
            rotated = candidates[shift:] + candidates[:shift]
            result = replace(result, best=rotated[0], alternatives=rotated[1:])
        return result

    def draft_pr(self, context: PRContext, config: Config) -> PRResult:
        return draft_pr(context, config)
