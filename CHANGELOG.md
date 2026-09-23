# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The 0.1.0 entry was written by hand: that history predates Conventional Commits
in this repository and has no tag. Commits since then follow Conventional
Commits, so `aicommit changelog --include-internal` drafts the Unreleased
section from git; the entries below are that draft, edited for readers.

## [Unreleased]

### Added
- **local:** `--backend local` / `backend: local`, a rule-based backend that drafts
  commit messages and PR descriptions from the diff with no model, no key and no
  network. It is clearly labelled as not AI; every draft is a valid Conventional Commit.
- **hook:** `hook_fallback` (default on): when the configured backend fails, the git
  hook prefills the rule-based draft instead of leaving the editor empty.
- **diff:** `ignore_paths` (and `AICOMMIT_IGNORE_PATHS`) for project-specific
  generated files; minified bundles, source maps, snapshots, `dist/`, `build/`,
  `vendor/`, `node_modules/`, `@generated` headers and `linguist-generated` /
  `linguist-vendored` attributes are now skipped automatically.
- **cli:** `--verbose` now traces the diff condensation, prompt sizes, raw model
  replies and every normalization to stderr (it used to do nothing).
- **cli:** `python -m aicommit`.
- **commit:** `normalize_commit()` repairs type synonyms, scopes, mood, casing,
  trailing periods and overlong subjects; one corrective model call when a proposal
  still fails validation.
- **llm:** retries with exponential backoff (honouring `Retry-After`) on 429, 5xx and
  dropped connections; backend-specific hints for 401 and missing Ollama models.

### Changed
- **diff:** the prompt budget is shared per file by weighted fair share (source >
  build/CI/config > tests > docs > data) instead of switching every file to hunk
  headers as soon as the diff is one character over budget.
- **changelog:** the Unreleased header is `## [Unreleased]` with no date, as Keep a
  Changelog asks. The default `--from` is the nearest reachable tag (`git describe`),
  and `--full` walks tags reachable from HEAD in version order.
- **hook:** installed in the directory git really runs hooks from
  (`git rev-parse --git-path hooks`, honouring `core.hooksPath`), and runs
  `<python> -m aicommit prepare` with the interpreter recorded at install time
  before falling back to `aicommit` on PATH. `hook status` shows both.
- **pr:** fails with a clear error before contacting the backend when the branch has
  no commits of its own; falls back to `origin/<base>`; an empty title falls back to
  the first commit subject.
- **config:** `max_diff_chars` must be at least 200.

### Fixed
- **generation:** an empty model reply crashed the CLI with an `IndexError` traceback.
- **generation:** `<think>` blocks from reasoning models became the commit `chore: <think>`.
- **generation:** prose with braces around the JSON became the commit subject.
- **generation:** `"breaking": "false"` (a string) marked the commit as breaking.
- **changelog:** commits written with `--emoji` were skipped as non-conventional.
- **changelog:** a backport tag created later was treated as the latest release, so
  released commits reappeared under Unreleased.
- **hook:** under `core.hooksPath` (husky, lefthook) the hook was never run although
  `hook status` said it was installed.
- **llm:** HTTP errors other than connect/timeout escaped as tracebacks.

## [0.1.0] - 2026-07-19

### Added
- **commit:** interactive Conventional Commit generation from the staged diff, with accept / edit / regenerate / pick-alternative loop
- **pr:** pull request title and description with Summary, Changes, Testing and Risks sections
- **changelog:** deterministic, offline Keep a Changelog release notes grouped by Conventional-Commit type, with an optional `--polish` pass
- **hook:** installable `prepare-commit-msg` hook that prefills the message and safely no-ops on merge, squash, amend and `-m` commits
- **llm:** one OpenAI-compatible backend for free NVIDIA NIM or a fully local Ollama model, switchable with `--backend`
- **diff:** binary and lockfile filtering plus progressive truncation to fit the model context window
- **config:** layered `.aicommit.yaml` configuration with defaults, user, project, environment and CLI precedence
