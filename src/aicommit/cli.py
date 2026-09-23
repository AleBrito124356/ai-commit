"""Command-line interface for aicommit.

    aicommit                 # interactive Conventional Commit for the staged diff
    aicommit pr              # PR title + description vs a base branch
    aicommit changelog       # Keep a Changelog release notes
    aicommit hook install    # prefill messages automatically on `git commit`
    aicommit prepare         # print one message to stdout (used by the hook)
    aicommit init            # write a starter .aicommit.yaml

Backends: ``nim`` (free NVIDIA cloud), ``ollama`` (local model) or ``local``
(rule-based drafts from the diff, no model and no network).

The interactive commit loop lets you accept, edit, regenerate, or pick an
alternative before anything is written.
"""

from __future__ import annotations

import contextlib
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import click
import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from . import __version__
from .changelog import (
    generate_full_changelog,
    generate_release_notes,
    latest_tag,
)
from .commit import CommitMessage, generate_commit, parse_commit, validate_commit
from .config import SAMPLE_CONFIG, Config, load_config
from .diff import collect_staged, plan_render
from .gitutil import (
    GitError,
    commit_with_message,
    has_staged_changes,
    is_git_repo,
    run_git,
    stage_all,
)
from .hook import install as hook_install
from .hook import status as hook_status
from .hook import uninstall as hook_uninstall
from .heuristic import draft_commit as local_draft_commit
from .llm import LLMClient, LLMError, is_local_backend
from .pr import collect_pr_context, generate_pr

def _make_console(stderr: bool = False) -> Console:
    """A Rich console that stays sane on legacy Windows consoles.

    Reconfiguring the stream to UTF-8 (with replacement) and disabling the
    legacy Windows renderer keeps glyphs like check marks and gitmoji from
    crashing on a cp1252 code page — they degrade instead of raising.
    """
    stream = sys.stderr if stderr else sys.stdout
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):  # pragma: no cover - non-reconfigurable stream
        pass
    return Console(stderr=stderr, legacy_windows=False)


console = _make_console()
err_console = _make_console(stderr=True)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    help="Write commit messages, PR descriptions and changelogs from your diff.",
    rich_markup_mode="rich",
)
hook_app = typer.Typer(help="Install or remove the prepare-commit-msg hook.")
app.add_typer(hook_app, name="hook")


@dataclass
class AppState:
    overrides: Dict[str, object] = field(default_factory=dict)
    verbose: bool = False


def _fail(message: str, code: int = 1) -> "typer.Exit":
    err_console.print(f"[bold red]error:[/] {escape(message)}")
    return typer.Exit(code)


def _debug(state: AppState, message: str) -> None:
    """--verbose output. Goes to stderr so stdout stays clean for pipes."""
    if state.verbose:
        err_console.print(f"[dim]{escape(message)}[/]", highlight=False)


def _status(state: AppState, text: str):
    """A spinner, unless --verbose is printing trace lines underneath it."""
    if state.verbose:
        return contextlib.nullcontext()
    return console.status(text, spinner="dots")


class _TracingClient:
    """Wraps a backend and logs prompt sizes and raw replies for --verbose."""

    def __init__(self, inner: Any, log: Callable[[str], None]):
        self._inner = inner
        self._log = log

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def complete(
        self, system: str, user: str, temperature: float = 0.3, max_tokens: int = 1024
    ) -> str:
        backend = getattr(self._inner, "backend", "?")
        model = getattr(self._inner, "model", "?")
        self._log(
            f"prompt -> {backend}/{model}: system {len(system):,} + user "
            f"{len(user):,} chars, temperature {temperature}, max_tokens {max_tokens}"
        )
        started = time.monotonic()
        reply = self._inner.complete(
            system, user, temperature=temperature, max_tokens=max_tokens
        )
        elapsed = time.monotonic() - started
        finish = getattr(self._inner, "last_finish_reason", None)
        attempts = getattr(self._inner, "last_attempts", None)
        self._log(
            f"reply <- {len(reply or ''):,} chars in {elapsed:.1f}s"
            f" (finish_reason={finish}, http attempts={attempts})"
        )
        self._log("raw reply:\n" + (reply if reply else "<empty>"))
        return reply


def _build_config(state: AppState) -> Config:
    try:
        return load_config(cli_overrides=state.overrides)
    except (ValueError, OSError) as exc:
        raise _fail(str(exc))


def _make_client(config: Config, state: Optional[AppState] = None) -> Any:
    try:
        client = LLMClient.from_config(config)
    except LLMError as exc:
        raise _fail(str(exc))
    if state is not None and state.verbose:
        return _TracingClient(client, lambda msg: _debug(state, msg))
    return client


def _require_repo() -> None:
    if not is_git_repo():
        raise _fail("not inside a git repository")


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"aicommit {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    backend: Optional[str] = typer.Option(
        None,
        "--backend",
        "-b",
        help="nim (free NVIDIA cloud), ollama (local model) or local (rule-based, no AI).",
    ),
    model: Optional[str] = typer.Option(
        None, "--model", help="Override the model id for the chosen backend."
    ),
    language: Optional[str] = typer.Option(
        None, "--language", "-l", help="Output language: en or es."
    ),
    emoji: Optional[bool] = typer.Option(
        None, "--emoji/--no-emoji", help="Prefix messages with a gitmoji."
    ),
    add_all: bool = typer.Option(
        False, "--all", "-a", help="Stage all changes (git add -A) before generating."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Accept the first suggestion without prompting."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show the message but do not create the commit."
    ),
    hint: Optional[str] = typer.Option(
        None, "--hint", help="Extra guidance passed to the model (e.g. an issue id)."
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Trace to stderr: diff condensation, prompt sizes, raw model replies.",
    ),
    _version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True, help="Show version."
    ),
) -> None:
    overrides: Dict[str, object] = {}
    if backend is not None:
        overrides["backend"] = backend
    if model is not None:
        overrides["model"] = model
    if language is not None:
        overrides["language"] = language
    if emoji is not None:
        overrides["emoji"] = emoji

    ctx.obj = AppState(overrides=overrides, verbose=verbose)

    if ctx.invoked_subcommand is None:
        _run_commit(ctx.obj, add_all=add_all, yes=yes, dry_run=dry_run, hint=hint or "")


# --------------------------------------------------------------------------- #
# Commit flow
# --------------------------------------------------------------------------- #

_LEADING_EMOJI_RE = re.compile(r"^\s*[^\w\s()!:]+\s*", re.UNICODE)


def _format(msg: CommitMessage, config: Config) -> str:
    return msg.format(emoji=config.emoji, include_body=config.include_body)


def _message_from_text(text: str) -> Optional[CommitMessage]:
    """Parse working text back into a message, tolerating a leading emoji."""
    lines = text.strip().splitlines()
    if not lines:
        return None
    header = _LEADING_EMOJI_RE.sub("", lines[0])
    rebuilt = "\n".join([header, *lines[1:]])
    return parse_commit(rebuilt)


def _validation_lines(text: str, config: Config) -> List[str]:
    msg = _message_from_text(text)
    if msg is None:
        return ["message does not follow the Conventional Commits format"]
    return validate_commit(msg, config)


def _render(
    text: str, candidates: List[CommitMessage], config: Config, rule_based: bool = False
) -> None:
    console.print()
    console.print(
        Panel(
            Text(text.rstrip(), style="bold"),
            title="Proposed commit (rule-based draft, no AI)" if rule_based else "Proposed commit",
            border_style="yellow" if rule_based else "cyan",
            expand=False,
        )
    )

    issues = _validation_lines(text, config)
    if issues:
        for issue in issues:
            console.print(f"  [yellow]![/] {issue}")
    else:
        console.print("  [green]✓[/] valid Conventional Commit")

    if len(candidates) > 1:
        table = Table(
            title="Alternatives",
            show_header=True,
            header_style="dim",
            box=None,
            pad_edge=False,
        )
        table.add_column("#", justify="right", style="cyan", no_wrap=True)
        table.add_column("Header")
        for i, msg in enumerate(candidates, start=1):
            table.add_row(str(i), msg.header(emoji=config.emoji))
        console.print(table)


def _report_generation(state: AppState, result: Any) -> None:
    for note in getattr(result, "notes", []) or []:
        _debug(state, f"normalized: {note}")
    attempts = getattr(result, "attempts", 1)
    if attempts > 1:
        _debug(state, f"model calls: {attempts} (one corrective retry)")


def _do_commit(text: str) -> None:
    commit_with_message(text.strip() + "\n")
    short = run_git(["rev-parse", "--short", "HEAD"]).strip()
    console.print(f"[green]✓ committed[/] [cyan]{short}[/]")


def _run_commit(
    state: AppState, add_all: bool, yes: bool, dry_run: bool, hint: str
) -> None:
    _require_repo()
    config = _build_config(state)

    if add_all:
        try:
            stage_all()
        except GitError as exc:
            raise _fail(str(exc))

    if not has_staged_changes():
        console.print(
            "[yellow]Nothing staged.[/] Stage changes with "
            "[cyan]git add[/] (or run with [cyan]-a[/]), then try again."
        )
        raise typer.Exit(0)

    bundle = collect_staged(ignore_paths=config.ignore_paths)
    rendered = plan_render(bundle, config.max_diff_chars)
    diff_view = rendered.text
    _debug(state, rendered.describe())
    client = _make_client(config, state)

    try:
        with _status(state, "[cyan]Generating commit message..."):
            result = generate_commit(diff_view, config, client, hint=hint, bundle=bundle)
    except (LLMError, GitError) as exc:
        raise _fail(str(exc))
    _report_generation(state, result)
    rule_based = is_local_backend(client)

    candidates = result.all
    working_text = _format(candidates[0], config)
    regen_temp = min(0.9, config.temperature + 0.4)

    while True:
        _render(working_text, candidates, config, rule_based=rule_based)

        if yes:
            break

        action = Prompt.ask(
            "  [bold]Action[/] "
            "[dim]a=accept  e=edit  r=regenerate  #=pick alt  q=quit[/]",
            default="a",
            show_default=False,
        ).strip().lower()

        if action in {"a", "accept", "y", "yes", ""}:
            break
        if action in {"e", "edit"}:
            edited = click.edit(working_text)
            if edited is not None and edited.strip():
                working_text = edited.strip() + "\n"
            continue
        if action in {"r", "regen", "regenerate"}:
            try:
                with _status(state, "[cyan]Regenerating..."):
                    result = generate_commit(
                        diff_view,
                        config,
                        client,
                        hint=hint,
                        temperature=regen_temp,
                        bundle=bundle,
                    )
            except LLMError as exc:
                console.print(f"  [red]{escape(str(exc))}[/]")
                continue
            _report_generation(state, result)
            candidates = result.all
            working_text = _format(candidates[0], config)
            continue
        if action.isdigit():
            idx = int(action) - 1
            if 0 <= idx < len(candidates):
                working_text = _format(candidates[idx], config)
            else:
                console.print("  [red]no alternative with that number[/]")
            continue
        if action in {"q", "quit", "n", "no"}:
            console.print("[yellow]Aborted.[/] Nothing was committed.")
            raise typer.Exit(1)
        console.print("  [red]unrecognized action[/]")

    if dry_run:
        console.print("\n[dim]--dry-run: not committing.[/] Final message:\n")
        console.print(working_text.rstrip())
        raise typer.Exit(0)

    try:
        _do_commit(working_text)
    except GitError as exc:
        raise _fail(str(exc))


# --------------------------------------------------------------------------- #
# prepare (used by the git hook)
# --------------------------------------------------------------------------- #


@app.command()
def prepare(ctx: typer.Context) -> None:
    """Print one generated commit message to stdout. Silent on any failure.

    Used by the git hook. When the configured backend fails (no key, Ollama
    down, rate limited) and ``hook_fallback`` is on, a rule-based draft from
    the local backend is printed instead, so the editor never opens empty.
    """
    state: AppState = ctx.obj
    try:
        if not is_git_repo() or not has_staged_changes():
            return
        config = load_config(cli_overrides=state.overrides)
        bundle = collect_staged(ignore_paths=config.ignore_paths)
    except Exception:  # noqa: BLE001 - the hook must never block a commit
        return
    try:
        diff_view = plan_render(bundle, config.max_diff_chars).text
        client = LLMClient.from_config(config)
        result = generate_commit(diff_view, config, client, bundle=bundle)
    except Exception as exc:  # noqa: BLE001 - fall back or stay silent
        if not config.hook_fallback or config.backend == "local":
            return
        _debug(state, f"backend failed ({exc}); using the rule-based draft")
        try:
            result = local_draft_commit(bundle, config)
        except Exception:  # noqa: BLE001
            return
    text = result.best.format(emoji=config.emoji, include_body=config.include_body)
    sys.stdout.write(text.rstrip() + "\n")


# --------------------------------------------------------------------------- #
# pr
# --------------------------------------------------------------------------- #


@app.command()
def pr(
    ctx: typer.Context,
    base: Optional[str] = typer.Option(
        None, "--base", help="Base branch to compare against (default: config pr_base)."
    ),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o", help="Write the markdown to a file instead of stdout."
    ),
) -> None:
    """Generate a PR title and description from this branch versus a base."""
    state: AppState = ctx.obj
    _require_repo()
    config = _build_config(state)
    try:
        context = collect_pr_context(base=base, config=config)
    except GitError as exc:
        raise _fail(str(exc))
    _debug(
        state,
        f"pr: {len(context.commits)} commits on {context.head_name} vs {context.base}",
    )
    _debug(state, context.rendered.describe())
    client = _make_client(config, state)
    if is_local_backend(client):
        err_console.print(
            "[dim]local backend: rule-based summary of commits and diffstat, no AI[/]"
        )

    try:
        with _status(state, "[cyan]Summarizing branch..."):
            result = generate_pr(config=config, client=client, context=context)
    except (LLMError, GitError) as exc:
        raise _fail(str(exc))

    markdown = f"# {result.title}\n\n{result.to_markdown()}"

    if output:
        output.write_text(markdown, encoding="utf-8")
        console.print(f"[green]✓[/] wrote PR description to [cyan]{output}[/]")
    else:
        sys.stdout.write(markdown)
        if not markdown.endswith("\n"):
            sys.stdout.write("\n")


# --------------------------------------------------------------------------- #
# changelog
# --------------------------------------------------------------------------- #


@app.command()
def changelog(
    ctx: typer.Context,
    from_tag: Optional[str] = typer.Option(
        None, "--from", help="Start tag/ref (default: latest tag). Ignored with --full."
    ),
    to: str = typer.Option("HEAD", "--to", help="End ref for the release."),
    version: Optional[str] = typer.Option(
        None, "--version", help="Version label for the section (default: Unreleased)."
    ),
    full: bool = typer.Option(
        False, "--full", help="Regenerate the entire CHANGELOG across all tags."
    ),
    polish: bool = typer.Option(
        False, "--polish", help="Rewrite entries with the LLM (needs a backend)."
    ),
    include_internal: bool = typer.Option(
        False, "--include-internal", help="Include docs/chore/ci/test/build/style."
    ),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o", help="Write to a file instead of stdout."
    ),
) -> None:
    """Produce Keep a Changelog release notes grouped by Conventional-Commit type."""
    state: AppState = ctx.obj
    _require_repo()
    config = _build_config(state)
    if polish and config.backend == "local":
        raise _fail(
            "--polish rewrites entries with a model, and the local backend is "
            "rule-based. Use --backend nim or --backend ollama, or drop --polish."
        )
    client = _make_client(config, state) if polish else None

    try:
        if full:
            text = generate_full_changelog(
                config=config,
                client=client,
                include_internal=include_internal,
                polish=polish,
            )
        else:
            start = from_tag if from_tag is not None else latest_tag()
            text = generate_release_notes(
                from_tag=start,
                to_ref=to,
                version=version,
                config=config,
                client=client,
                polish=polish,
                include_internal=include_internal,
            )
    except (LLMError, GitError) as exc:
        raise _fail(str(exc))

    if output:
        output.write_text(text, encoding="utf-8")
        console.print(f"[green]✓[/] wrote changelog to [cyan]{output}[/]")
    else:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")


# --------------------------------------------------------------------------- #
# hook
# --------------------------------------------------------------------------- #


@hook_app.command("install")
def hook_install_cmd(
    force: bool = typer.Option(
        False, "--force", help="Overwrite an existing backup if present."
    ),
) -> None:
    """Install the prepare-commit-msg hook in the current repository."""
    _require_repo()
    try:
        message = hook_install(force=force)
    except (GitError, OSError, FileExistsError) as exc:
        raise _fail(str(exc))
    console.print(f"[green]✓[/] {escape(message)}")


@hook_app.command("uninstall")
def hook_uninstall_cmd() -> None:
    """Remove the aicommit hook (restoring any backup)."""
    _require_repo()
    try:
        message = hook_uninstall()
    except (GitError, OSError) as exc:
        raise _fail(str(exc))
    console.print(f"[green]✓[/] {escape(message)}")


@hook_app.command("status")
def hook_status_cmd() -> None:
    """Report whether the hook is installed where git will actually run it."""
    _require_repo()
    st = hook_status()
    where = escape(str(st.path))
    if st.managed:
        console.print(f"[green]installed[/] and managed by aicommit at {where}")
    elif st.installed:
        console.print(f"[yellow]a hook exists but is not managed by aicommit:[/] {where}")
    else:
        console.print(f"[dim]no prepare-commit-msg hook installed[/] (hooks dir: {escape(str(st.hooks_dir))})")
    if st.hooks_path_config:
        console.print(
            f"  git runs hooks from core.hooksPath = {escape(st.hooks_path_config)}"
        )
    if st.managed:
        if st.interpreter and st.interpreter_exists:
            console.print(f"  runs: {escape(st.interpreter)} -m aicommit prepare")
        elif st.interpreter:
            console.print(
                f"  [yellow]recorded interpreter is gone:[/] {escape(st.interpreter)}"
                " — falls back to `aicommit` on PATH; reinstall with "
                "`aicommit hook install`"
            )
        else:
            console.print("  runs: `aicommit` from PATH")


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #


@app.command()
def init(
    force: bool = typer.Option(False, "--force", help="Overwrite an existing file."),
) -> None:
    """Write a starter .aicommit.yaml in the current directory."""
    target = Path.cwd() / ".aicommit.yaml"
    if target.exists() and not force:
        raise _fail(f"{target} already exists; pass --force to overwrite")
    target.write_text(SAMPLE_CONFIG, encoding="utf-8")
    console.print(f"[green]✓[/] wrote [cyan]{target}[/]")


def run() -> None:  # console-script entry point
    app()


if __name__ == "__main__":  # pragma: no cover
    run()
