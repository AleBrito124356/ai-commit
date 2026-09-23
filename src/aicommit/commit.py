"""Conventional Commit modelling: parse, format, validate, normalize and generate.

The formatting, validation and normalization logic is deterministic and
unit-tested. Only :func:`generate_commit` touches an LLM, and it takes the
client as an argument so tests inject a fake.

Generation is defensive on purpose. Small local models drift (``"Feature"``
as a type, past-tense subjects, trailing periods, 90-character subjects), and
reasoning models wrap the answer in ``<think>`` blocks. Every reply is parsed
leniently, normalized into a valid Conventional Commit where that can be done
mechanically, and — only if problems remain — sent back to the model once with
the exact list of issues to fix.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Tuple

from .config import Config
from .llm import LLMError, extract_json, is_local_backend, strip_reasoning
from .prompts import commit_repair_user, commit_system, commit_user

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

# A gitmoji shortcode such as ``:sparkles:`` at the start of a header.
_SHORTCODE_RE = re.compile(r"^:[a-z0-9_+-]+:\s*")
# Unicode categories that make up emoji sequences (symbols, variation
# selectors, zero-width joiners, skin-tone modifiers).
_EMOJI_CATEGORIES = {"So", "Sk", "Mn", "Me", "Cf", "Cs"}

# Non-imperative openings and their imperative form. Used both to flag
# problems (validation) and to repair them (normalization).
IMPERATIVE_FIXES: Dict[str, str] = {
    "added": "add", "adds": "add", "adding": "add",
    "fixed": "fix", "fixes": "fix", "fixing": "fix",
    "updated": "update", "updates": "update", "updating": "update",
    "changed": "change", "changes": "change", "changing": "change",
    "removed": "remove", "removes": "remove", "removing": "remove",
    "created": "create", "creates": "create", "creating": "create",
    "implemented": "implement", "implements": "implement",
    "implementing": "implement",
    "refactored": "refactor", "refactors": "refactor", "refactoring": "refactor",
    "improved": "improve", "improves": "improve", "improving": "improve",
    "renamed": "rename", "renames": "rename", "renaming": "rename",
    "moved": "move", "moves": "move", "moving": "move",
    "deleted": "delete", "deletes": "delete", "deleting": "delete",
    "bumped": "bump", "bumps": "bump", "bumping": "bump",
    "upgraded": "upgrade", "upgrades": "upgrade", "upgrading": "upgrade",
    "replaced": "replace", "replaces": "replace", "replacing": "replace",
    "introduced": "introduce", "introduces": "introduce",
    "introducing": "introduce",
    "handled": "handle", "handles": "handle", "handling": "handle",
    "corrected": "correct", "corrects": "correct", "correcting": "correct",
    "enabled": "enable", "enables": "enable", "enabling": "enable",
    "disabled": "disable", "disables": "disable", "disabling": "disable",
    "extracted": "extract", "extracts": "extract", "extracting": "extract",
    "simplified": "simplify", "simplifies": "simplify",
    "simplifying": "simplify",
    "optimized": "optimize", "optimizes": "optimize", "optimizing": "optimize",
    "documented": "document", "documents": "document",
    "documenting": "document",
    "reverted": "revert", "reverts": "revert", "reverting": "revert",
    "prevented": "prevent", "prevents": "prevent", "preventing": "prevent",
    "ensured": "ensure", "ensures": "ensure", "ensuring": "ensure",
    "allowed": "allow", "allows": "allow", "allowing": "allow",
    "supported": "support", "supports": "support", "supporting": "support",
    "cleaned": "clean", "cleans": "clean", "cleaning": "clean",
    "converted": "convert", "converts": "convert", "converting": "convert",
    "migrated": "migrate", "migrates": "migrate", "migrating": "migrate",
    "merged": "merge", "merges": "merge", "merging": "merge",
    "tweaked": "tweak", "tweaks": "tweak", "tweaking": "tweak",
    "adjusted": "adjust", "adjusts": "adjust", "adjusting": "adjust",
    "made": "make", "makes": "make", "making": "make",
    "wrote": "write", "writes": "write", "writing": "write",
    "raised": "raise", "raises": "raise", "raising": "raise",
    "returned": "return", "returns": "return", "returning": "return",
    "exposed": "expose", "exposes": "expose", "exposing": "expose",
    "guarded": "guard", "guards": "guard", "guarding": "guard",
    "wrapped": "wrap", "wraps": "wrap", "wrapping": "wrap",
    "skipped": "skip", "skips": "skip", "skipping": "skip",
    "dropped": "drop", "drops": "drop", "dropping": "drop",
    "stopped": "stop", "stops": "stop", "stopping": "stop",
    "logged": "log",
    "validated": "validate", "validates": "validate", "validating": "validate",
    "checked": "check", "checks": "check", "checking": "check",
    "restored": "restore", "restores": "restore", "restoring": "restore",
    "reduced": "reduce", "reduces": "reduce", "reducing": "reduce",
    "increased": "increase", "increases": "increase", "increasing": "increase",
    "cached": "cache",
    "limited": "limit", "limiting": "limit",
    "built": "build",
    "ran": "run",
    "used": "use", "uses": "use", "using": "use",
    "included": "include", "includes": "include", "including": "include",
    "passed": "pass", "passes": "pass", "passing": "pass",
    "resolved": "resolve", "resolves": "resolve", "resolving": "resolve",
    "renders": "render", "rendered": "render", "rendering": "render",
    "generated": "generate", "generates": "generate", "generating": "generate",
    "configured": "configure", "configures": "configure",
    "configuring": "configure",
    "initialized": "initialize", "initializes": "initialize",
    "initializing": "initialize",
    "deprecated": "deprecate", "deprecates": "deprecate",
    "deprecating": "deprecate",
    "polished": "polish", "polishes": "polish", "polishing": "polish",
    "reworked": "rework", "reworks": "rework", "reworking": "rework",
    "rewrote": "rewrite", "rewrites": "rewrite", "rewriting": "rewrite",
    "splits": "split", "splitting": "split",
}

# Kept for backwards compatibility with code that imported the old name.
_NON_IMPERATIVE = set(IMPERATIVE_FIXES)

# Type spellings models produce, mapped to the Conventional Commit type.
TYPE_SYNONYMS: Dict[str, str] = {
    "feature": "feat", "features": "feat", "feat": "feat", "new": "feat",
    "add": "feat", "added": "feat", "enhancement": "feat",
    "fix": "fix", "fixes": "fix", "fixed": "fix", "bugfix": "fix",
    "bug": "fix", "hotfix": "fix", "patch": "fix",
    "doc": "docs", "docs": "docs", "documentation": "docs",
    "readme": "docs",
    "test": "test", "tests": "test", "testing": "test", "spec": "test",
    "perf": "perf", "performance": "perf", "optimization": "perf",
    "optimisation": "perf", "optimize": "perf", "speed": "perf",
    "refactor": "refactor", "refactoring": "refactor", "refactored": "refactor",
    "cleanup": "refactor", "clean": "refactor", "restructure": "refactor",
    "style": "style", "styles": "style", "format": "style",
    "formatting": "style", "lint": "style",
    "build": "build", "builds": "build", "deps": "build",
    "dependencies": "build", "dependency": "build", "packaging": "build",
    "ci": "ci", "ci/cd": "ci", "cicd": "ci", "pipeline": "ci",
    "workflow": "ci",
    "chore": "chore", "chores": "chore", "maintenance": "chore",
    "misc": "chore", "housekeeping": "chore", "config": "chore",
    "revert": "revert", "reverts": "revert", "rollback": "revert",
}

# Word boundaries where an overlong subject can be cut without mangling it.
_CLAUSE_MARKERS = (
    ", ", "; ", " - ", " — ", " so that ", " so ", " because ", " which ",
    " in order to ", " and ", " by ", " when ", " while ", " since ",
    " as well as ", " instead of ", " using ", " via ",
)
_DANGLING_WORDS = {
    "a", "an", "the", "to", "of", "for", "and", "or", "with", "in", "on",
    "at", "by", "so", "that", "from", "into", "as", "via", "using",
}
_TRUE_STRINGS = {"true", "yes", "y", "1", "on"}


def strip_leading_emoji(text: str) -> str:
    """Remove a leading gitmoji (unicode or ``:shortcode:``) from a header.

    ``"✨ feat(api): add x"`` and ``":sparkles: feat: add x"`` both become the
    plain Conventional Commit header, so commits written with ``--emoji`` stay
    readable by the parser and the changelog.
    """
    stripped = text.lstrip()
    stripped = _SHORTCODE_RE.sub("", stripped)
    i = 0
    while i < len(stripped) and (
        stripped[i].isspace() or unicodedata.category(stripped[i]) in _EMOJI_CATEGORIES
    ):
        i += 1
    return stripped[i:] if i else stripped


def _loose_bool(value: Any) -> bool:
    """``bool`` that does not treat the string ``"false"`` as true."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in _TRUE_STRINGS


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "\n".join(str(v).strip() for v in value if str(v).strip())
    return str(value).strip()


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
    def from_dict(cls, data: Any) -> "CommitMessage":
        """Build a message from a model's JSON object (or a bare header string)."""
        if isinstance(data, str):
            parsed = parse_commit(data)
            if parsed is not None:
                return parsed
            return cls(type="", subject=data.strip())
        if not isinstance(data, dict):
            return cls(type="", subject="")

        subject = _as_text(data.get("subject"))
        # Some models answer {"message": "feat(x): ..."} instead of fields.
        if not subject:
            for key in ("header", "message", "commit", "title"):
                candidate = data.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    parsed = parse_commit(candidate)
                    if parsed is not None:
                        extra_body = _as_text(data.get("body"))
                        if extra_body and not parsed.body:
                            parsed.body = extra_body
                        return parsed
                    subject = candidate.strip()
                    break

        scope = data.get("scope")
        scope = _as_text(scope) if scope else None
        footer = _as_text(data.get("footer"))
        return cls(
            type=_as_text(data.get("type")).lower(),
            subject=subject,
            scope=scope or None,
            body=_as_text(data.get("body")),
            footer=footer,
            breaking=_loose_bool(data.get("breaking", False))
            or "BREAKING CHANGE" in footer,
        )


@dataclass
class CommitResult:
    best: CommitMessage
    alternatives: List[CommitMessage] = field(default_factory=list)
    raw: str = ""
    # How many model calls produced this result (2 when a repair was needed).
    attempts: int = 1
    # Human-readable notes about repairs applied (shown with --verbose).
    notes: List[str] = field(default_factory=list)

    @property
    def all(self) -> List[CommitMessage]:
        return [self.best, *self.alternatives]


def parse_commit(text: str) -> Optional[CommitMessage]:
    """Parse a Conventional Commit string into a :class:`CommitMessage`.

    A leading gitmoji (``✨ feat: ...`` or ``:sparkles: feat: ...``) is
    tolerated. Returns ``None`` when the header line does not conform.
    """
    if not text or not text.strip():
        return None
    lines = text.strip().splitlines()
    header = strip_leading_emoji(lines[0].strip()).strip()
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
        if first_word in IMPERATIVE_FIXES:
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


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #

_TYPE_FIELD_RE = re.compile(
    r"^\s*(?P<type>[A-Za-z/]+)\s*(?:\((?P<scope>[^)]*)\))?\s*(?P<bang>!)?\s*:?\s*$"
)
_SUBJECT_PREFIX_RE = re.compile(
    r"^(?P<type>[A-Za-z]+)(?:\([^)]*\))?!?:\s*"
)


def _canonical_type(raw: str, allowed: List[str]) -> str:
    key = raw.strip().lower()
    if key in allowed:
        return key
    mapped = TYPE_SYNONYMS.get(key)
    if mapped and mapped in allowed:
        return mapped
    return key


def _clean_scope(scope: Optional[str]) -> Optional[str]:
    if scope is None:
        return None
    cleaned = scope.strip().strip("`'\"()[]").strip()
    if cleaned.lower() in {"", "null", "none", "n/a", "na", "-", "global", "all"}:
        return None
    cleaned = re.sub(r"\s+", "-", cleaned.lower())
    return cleaned or None


def _is_acronym_or_identifier(word: str) -> bool:
    """``API``, ``README``, ``GitHub``, ``iOS``: casing carries meaning."""
    letters = [c for c in word if c.isalpha()]
    if len(letters) < 2:
        return False
    return any(c.isupper() for c in word[1:])


def shorten_subject(subject: str, limit: int) -> str:
    """Trim ``subject`` to at most ``limit`` chars at a sensible boundary.

    Prefers cutting at a clause boundary (``, `` / `` so that `` / `` and ``)
    that keeps at least ~40% of the limit; otherwise cuts at the last word
    boundary and drops dangling connector words like ``to`` or ``the``.
    """
    subject = subject.strip()
    if len(subject) <= limit:
        return subject
    min_keep = max(12, int(limit * 0.4))
    best = -1
    for marker in _CLAUSE_MARKERS:
        pos = subject.rfind(marker, 0, limit + 1)
        if pos >= min_keep:
            best = max(best, pos)
    if best > 0:
        cut = subject[:best]
    else:
        space = subject.rfind(" ", 0, limit + 1)
        cut = subject[:space] if space >= min_keep else subject[:limit]
    words = cut.rstrip(" ,;:-—").split(" ")
    while len(words) > 1 and words[-1].lower() in _DANGLING_WORDS:
        words.pop()
    return " ".join(words).rstrip(" ,;:-—.")[:limit]


def _normalize(
    msg: CommitMessage, config: Config
) -> Tuple[CommitMessage, List[str]]:
    notes: List[str] = []
    allowed = list(config.allowed_types)

    # --- type (may arrive as "Feature", "feat(api)", "fix!" or "Bug Fix") --- #
    raw_type = msg.type or ""
    new_type = raw_type
    scope = msg.scope
    breaking = msg.breaking
    typed = _TYPE_FIELD_RE.match(raw_type)
    if typed:
        new_type = typed.group("type")
        if typed.group("scope") and not scope:
            scope = typed.group("scope")
        if typed.group("bang"):
            breaking = True
    else:
        new_type = raw_type.replace(" ", "")
    new_type = _canonical_type(new_type, allowed)
    if new_type != raw_type:
        notes.append(f"type '{raw_type}' -> '{new_type}'")

    # --- scope ------------------------------------------------------------- #
    new_scope = _clean_scope(scope)
    if new_scope != msg.scope:
        notes.append(f"scope {msg.scope!r} -> {new_scope!r}")

    # --- subject ----------------------------------------------------------- #
    subject = " ".join(_as_text(msg.subject).split())
    original_subject = subject
    subject = strip_leading_emoji(subject).strip().strip("`\"'").strip()
    prefix = _SUBJECT_PREFIX_RE.match(subject)
    if prefix and _canonical_type(prefix.group("type"), allowed) in allowed:
        subject = subject[prefix.end() :]
    subject = subject.rstrip(" .!;:")
    if subject:
        first, _, rest = subject.partition(" ")
        if config.language == "en":
            imperative = IMPERATIVE_FIXES.get(first.lower())
            if imperative:
                first = imperative
        if not _is_acronym_or_identifier(first):
            first = first[:1].lower() + first[1:]
        subject = f"{first} {rest}".strip() if rest else first
    if len(subject) > config.subject_max_length:
        subject = shorten_subject(subject, config.subject_max_length)
    if subject != original_subject:
        notes.append(f"subject '{original_subject}' -> '{subject}'")

    return (
        replace(msg, type=new_type, scope=new_scope, subject=subject, breaking=breaking),
        notes,
    )


def normalize_commit(msg: CommitMessage, config: Config) -> CommitMessage:
    """Mechanically repair common model drift into a valid Conventional Commit.

    * maps type synonyms (``Feature`` -> ``feat``, ``bugfix`` -> ``fix``, ...)
      and lowercases the type; ``feat(api)!`` in the type field is split up;
    * cleans the scope (empty/``null`` -> none, lowercase, spaces -> ``-``);
    * strips a repeated ``type:`` prefix, quotes and a trailing period from the
      subject, turns a past-tense or 3rd-person opener into the imperative
      (English only), lowercases the first letter unless it is an acronym, and
      trims an overlong subject at a word boundary.
    """
    return _normalize(msg, config)[0]


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #

_BULLET_RE = re.compile(r"^\s*(?:[-*>•]|\d+[.)])\s+")


def _find_header_in_text(text: str) -> Optional[CommitMessage]:
    """Scan every line for a ``type(scope): subject`` header."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        candidate = _BULLET_RE.sub("", line).strip().strip("`*_\"'").strip()
        candidate = strip_leading_emoji(candidate)
        match = _HEADER_RE.match(candidate)
        if not match:
            continue
        rest = [ln for ln in lines[index + 1 :] if not ln.strip().startswith("```")]
        parsed = parse_commit("\n".join([candidate, *rest]))
        if parsed is not None:
            return parsed
    return None


def _first_meaningful_line(text: str) -> str:
    for line in text.splitlines():
        cleaned = _BULLET_RE.sub("", line).strip().strip("`*_\"'").strip()
        if cleaned and not cleaned.startswith("```"):
            return cleaned
    return ""


def _parse_generation(raw: str, config: Config) -> Tuple[CommitResult, bool]:
    """Turn a raw model reply into a :class:`CommitResult`, defensively.

    Returns ``(result, understood)``; ``understood`` is False when no JSON and
    no Conventional Commit header could be found and the result is only a
    best-effort ``chore`` built from the first line of prose.
    """
    if not raw or not raw.strip():
        raise LLMError("model returned an empty reply")
    answer = strip_reasoning(raw)
    if not answer:
        raise LLMError(
            "model returned only reasoning (<think>...) and no answer; "
            "raise the token limit or use a non-reasoning model"
        )

    try:
        data = extract_json(answer, expected_keys=("primary", "subject", "type"))
    except LLMError:
        found = _find_header_in_text(answer)
        if found is not None:
            return CommitResult(best=found, alternatives=[], raw=raw), True
        line = _first_meaningful_line(answer)
        fallback = CommitMessage(type="chore", subject=line[:200])
        return CommitResult(best=fallback, alternatives=[], raw=raw), False

    primary_data = data.get("primary") if "primary" in data else data
    best = CommitMessage.from_dict(primary_data)

    alternatives: List[CommitMessage] = []
    raw_alts = data.get("alternatives") or []
    if isinstance(raw_alts, (dict, str)):
        raw_alts = [raw_alts]
    for alt in raw_alts:
        candidate = CommitMessage.from_dict(alt)
        if candidate.subject:
            alternatives.append(candidate)

    understood = bool(best.subject) or bool(alternatives)
    return CommitResult(best=best, alternatives=alternatives, raw=raw), understood


def _normalize_result(result: CommitResult, config: Config) -> CommitResult:
    notes = list(result.notes)
    best, best_notes = _normalize(result.best, config)
    notes.extend(best_notes)
    alternatives: List[CommitMessage] = []
    seen = {best.header()}
    for alt in result.alternatives:
        fixed, _ = _normalize(alt, config)
        if fixed.subject and fixed.header() not in seen:
            seen.add(fixed.header())
            alternatives.append(fixed)
    return replace(result, best=best, alternatives=alternatives, notes=notes)


def generate_commit(
    diff_text: str,
    config: Config,
    client: Any,
    hint: str = "",
    temperature: Optional[float] = None,
    repair: bool = True,
    bundle: Any = None,
) -> CommitResult:
    """Ask the model for a primary commit plus alternatives.

    The reply is parsed leniently and normalized. If the primary suggestion
    still fails validation, the model gets exactly one corrective call listing
    the problems; the first valid candidate across both answers wins.

    With the rule-based ``local`` backend no prompt is built: the draft comes
    straight from ``bundle`` (the parsed diff), or from ``diff_text`` parsed
    again when no bundle is given.
    """
    if is_local_backend(client):
        if bundle is None:
            from .diff import parse_diff

            bundle = parse_diff(diff_text)
        return client.draft_commit(bundle, config, hint=hint)

    system = commit_system(config)
    user = commit_user(diff_text, config.n_alternatives, hint=hint)
    temp = config.temperature if temperature is None else temperature
    raw = client.complete(system, user, temperature=temp, max_tokens=1500)
    if not (raw or "").strip():
        reason = getattr(client, "last_finish_reason", None)
        detail = " (it hit the token limit)" if reason == "length" else ""
        raise LLMError(f"model returned an empty reply{detail}")

    parsed, understood = _parse_generation(raw, config)
    result = _normalize_result(parsed, config)
    issues = validate_commit(result.best, config)
    if not understood:
        issues = ["the reply was not the requested JSON object", *issues]
    if not issues or not repair:
        return result

    repair_user = commit_repair_user(user, raw, issues)
    try:
        raw2 = client.complete(
            system, repair_user, temperature=min(temp, 0.2), max_tokens=1500
        )
        parsed2, understood2 = _parse_generation(raw2, config)
    except LLMError as exc:
        result.notes.append(f"repair call failed: {exc}")
        result.attempts = 2
        return result
    second = _normalize_result(parsed2, config)

    candidates = [*(second.all if understood2 else []), *result.all]
    # Stable sort: valid candidates first, then the ones with fewest problems.
    ordered = sorted(candidates, key=lambda c: len(validate_commit(c, config)))
    best = ordered[0]
    seen = {best.header()}
    alternatives: List[CommitMessage] = []
    for candidate in ordered[1:]:
        if candidate.header() not in seen:
            seen.add(candidate.header())
            alternatives.append(candidate)
    notes = [
        *result.notes,
        "repair: " + "; ".join(issues),
        *second.notes,
    ]
    return CommitResult(
        best=best,
        alternatives=alternatives[: max(config.n_alternatives, 1) + 1],
        raw=raw2,
        attempts=2,
        notes=notes,
    )
