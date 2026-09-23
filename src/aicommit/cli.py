"""Command-line interface for aicommit.

    aicommit                 # interactive Conventional Commit for the staged diff
    aicommit pr              # PR title + description vs a base branch
    aicommit changelog       # Keep a Changelog release notes
    aicommit hook install    # prefill messages automatically on `git commit`
    aicommit prepare         # print one message to stdout (used by the hook)
    aicommit init            # write a starter .aicommit.yaml

The interactive commit loop lets you accept, edit, regenerate, or pick an
alternative before anything is written.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import click
import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from . import __version__
from .changelog import (
    generate_release_notes,
    latest_tag,
    ref_date,
    render_full_changelog,
)
from .commit import CommitMessage, generate_commit, parse_commit, validate_commit
from .config import SAMPLE_CONFIG, Config, load_config
from .diff import collect_staged, render_for_prompt
from .gitutil import (
    GitError,
    commit_with_message,
    has_staged_changes,
    is_git_repo,
    list_tags,
    run_git,
    stage_all,
)
from .hook import install as hook_install
from .hook import status as hook_status
from .hook import uninstall as hook_uninstall
from .llm import LLMClient, LLMError
from .pr import generate_pr

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
    err_console.print(f"[bold red]error:[/] {message}")
    return typer.Exit(code)


def _build_config(state: AppState) -> Config:
    try:
        return load_config(cli_overrides=state.overrides)
    except (ValueError, OSError) as exc:
        raise _fail(str(exc))


def _make_client(config: Config) -> LLMClient:
    try:
        return LLMClient.from_config(config)
    except LLMError as exc:
        raise _fail(str(exc))


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
        None, "--backend", "-b", help="nim (free NVIDIA cloud) or ollama (local)."
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
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose errors."),
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


def _render(text: str, candidates: List[CommitMessage], config: Config) -> None:
    console.print()
    console.print(
        Panel(
            Text(text.rstrip(), style="bold"),
            title="Proposed commit",
            border_style="cyan",
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
    diff_view = render_for_prompt(bundle, config.max_diff_chars)
    client = _make_client(config)

    try:
        with console.status("[cyan]Generating commit message...", spinner="dots"):
            result = generate_commit(diff_view, config, client, hint=hint)
    except (LLMError, GitError) as exc:
        raise _fail(str(exc))

    candidates = result.all
    working_text = _format(candidates[0], config)
    regen_temp = min(0.9, config.temperature + 0.4)

    while True:
        _render(working_text, candidates, config)

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
                with console.status("[cyan]Regenerating...", spinner="dots"):
                    result = generate_commit(
                        diff_view, config, client, hint=hint, temperature=regen_temp
                    )
            except LLMError as exc:
                console.print(f"  [red]{exc}[/]")
                continue
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
    """Print one generated commit message to stdout. Silent on any failure."""
    state: AppState = ctx.obj
    try:
        if not is_git_repo() or not has_staged_changes():
            return
        config = load_config(cli_overrides=state.overrides)
        bundle = collect_staged(ignore_paths=config.ignore_paths)
        diff_view = render_for_prompt(bundle, config.max_diff_chars)
        client = LLMClient.from_config(config)
        result = generate_commit(diff_view, config, client)
        text = result.best.format(
            emoji=config.emoji, include_body=config.include_body
        )
        sys.stdout.write(text.rstrip() + "\n")
    except Exception:  # noqa: BLE001 - the hook must never block a commit
        return


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
    client = _make_client(config)

    try:
        with console.status("[cyan]Summarizing branch...", spinner="dots"):
            result = generate_pr(base=base, config=config, client=client)
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
    client = _make_client(config) if polish else None

    try:
        if full:
            text = _full_changelog(config, client, include_internal, polish)
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


def _full_changelog(
    config: Config,
    client: Optional[LLMClient],
    include_internal: bool,
    polish: bool,
) -> str:
    tags = list_tags()
    sections: List[str] = []

    unreleased = generate_release_notes(
        from_tag=tags[-1] if tags else None,
        to_ref="HEAD",
        version="Unreleased",
        config=config,
        client=client,
        polish=polish,
        include_internal=include_internal,
    )
    sections.append(unreleased)

    for i in range(len(tags) - 1, -1, -1):
        previous = tags[i - 1] if i > 0 else None
        section = generate_release_notes(
            from_tag=previous,
            to_ref=tags[i],
            version=tags[i],
            config=config,
            client=client,
            polish=polish,
            include_internal=include_internal,
            release_date=ref_date(tags[i]),
        )
        sections.append(section)

    return render_full_changelog(sections)


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
    console.print(f"[green]✓[/] {message}")


@hook_app.command("uninstall")
def hook_uninstall_cmd() -> None:
    """Remove the aicommit hook (restoring any backup)."""
    _require_repo()
    try:
        message = hook_uninstall()
    except (GitError, OSError) as exc:
        raise _fail(str(exc))
    console.print(f"[green]✓[/] {message}")


@hook_app.command("status")
def hook_status_cmd() -> None:
    """Report whether the hook is installed and managed by aicommit."""
    _require_repo()
    st = hook_status()
    if st.managed:
        console.print(f"[green]installed[/] and managed by aicommit at {st.path}")
    elif st.installed:
        console.print(f"[yellow]a hook exists but is not managed by aicommit:[/] {st.path}")
    else:
        console.print("[dim]no prepare-commit-msg hook installed[/]")


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
