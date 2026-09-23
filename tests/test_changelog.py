"""Changelog grouping, rendering and end-to-end generation from git."""

from __future__ import annotations

import pytest

from aicommit.changelog import (
    build_release,
    generate_full_changelog,
    generate_release_notes,
    group_commits,
    latest_tag,
    render_full_changelog,
)
from aicommit.gitutil import list_tags

COMMITS = [
    {"hash": "aaa1111", "message": "feat(api): add users endpoint"},
    {"hash": "bbb2222", "message": "fix: correct null check"},
    {"hash": "ccc3333", "message": "perf: cache repeated lookups"},
    {"hash": "ddd4444", "message": "docs: expand the readme"},
    {"hash": "eee5555", "message": "feat!: drop python 3.8 support"},
    {"hash": "fff6666", "message": "Merge branch 'topic' into main"},
    {"hash": "ggg7777", "message": "chore: bump dependencies"},
    {"hash": "hhh8888", "message": "not a conventional commit"},
]


def test_group_maps_types_to_sections():
    grouped = group_commits(COMMITS)
    # feat(api) -> Added ; breaking feat! -> Changed (breaking overrides)
    assert [e.text for e in grouped["Added"]] == ["add users endpoint"]
    # perf -> Changed and feat! (breaking) -> Changed
    assert len(grouped["Changed"]) == 2
    assert [e.text for e in grouped["Fixed"]] == ["correct null check"]


def test_group_excludes_internal_by_default():
    grouped = group_commits(COMMITS)
    all_text = [e.text for entries in grouped.values() for e in entries]
    assert "expand the readme" not in all_text
    assert "bump dependencies" not in all_text


def test_group_includes_internal_when_requested():
    grouped = group_commits(COMMITS, include_internal=True)
    changed = [e.text for e in grouped["Changed"]]
    assert "expand the readme" in changed
    assert "bump dependencies" in changed


def test_group_skips_merge_and_nonconventional():
    grouped = group_commits(COMMITS)
    all_text = [e.text for entries in grouped.values() for e in entries]
    assert not any("Merge" in t for t in all_text)
    assert "not a conventional commit" not in all_text


def test_breaking_entry_is_flagged():
    grouped = group_commits(COMMITS)
    breaking = [e for e in grouped["Changed"] if e.breaking]
    assert len(breaking) == 1
    assert "BREAKING" in breaking[0].render()


def test_render_release_orders_sections():
    release = build_release("1.2.0", COMMITS, release_date="2026-07-19")
    text = release.render()
    assert text.startswith("## [1.2.0] - 2026-07-19")
    assert text.index("### Added") < text.index("### Changed") < text.index("### Fixed")


def test_scope_and_hash_in_entry():
    grouped = group_commits(COMMITS)
    added = grouped["Added"][0].render()
    assert "**api:**" in added
    assert "`aaa1111`" in added


def test_render_full_changelog_has_header():
    section = build_release("1.0.0", COMMITS, release_date="2026-07-19").render() + "\n"
    full = render_full_changelog([section])
    assert full.startswith("# Changelog")
    assert "Keep a Changelog" in full
    assert "## [1.0.0]" in full


# --- integration with a real repo ------------------------------------------ #


def test_generate_release_notes_from_repo(git_repo):
    r = git_repo
    r.commit("feat: add search", "search.py", "print('search')")
    r.commit("fix: escape query", "search.py", "print('safe')")
    r.commit("chore: tidy", "notes.txt", "internal")

    text = generate_release_notes(
        from_tag=None,
        to_ref="HEAD",
        version="1.0.0",
        cwd=r.path,
    )
    assert "## [1.0.0]" in text
    assert "### Added" in text
    assert "add search" in text
    assert "### Fixed" in text
    # chore is internal and excluded by default.
    assert "tidy" not in text


def test_generate_release_notes_between_tags(git_repo):
    r = git_repo
    r.commit("feat: first feature", "a.py", "1")
    r.git("tag", "v1.0.0")
    r.commit("fix: patch it", "a.py", "2")
    r.git("tag", "v1.1.0")

    text = generate_release_notes(
        from_tag="v1.0.0",
        to_ref="v1.1.0",
        version="v1.1.0",
        cwd=r.path,
    )
    assert "### Fixed" in text
    assert "patch it" in text
    # The feature belongs to the previous release, not this range.
    assert "first feature" not in text


# --- emoji commits, tag ordering and Keep a Changelog headers --------------- #


def test_emoji_prefixed_commits_are_grouped():
    commits = [
        {"hash": "a1", "message": "✨ feat(api): add users endpoint"},
        {"hash": "b2", "message": "\U0001f41b fix: guard nulls"},
        {"hash": "c3", "message": ":zap: perf: cache lookups"},
    ]
    grouped = group_commits(commits)
    assert [e.text for e in grouped["Added"]] == ["add users endpoint"]
    assert grouped["Added"][0].scope == "api"
    assert [e.text for e in grouped["Fixed"]] == ["guard nulls"]
    assert [e.text for e in grouped["Changed"]] == ["cache lookups"]


def test_emoji_commits_from_a_real_repo(git_repo):
    r = git_repo
    r.commit("✨ feat(api): add users endpoint", "api.py", "1")
    r.commit("\U0001f41b fix: guard nulls", "api.py", "2")
    text = generate_release_notes(version="1.0.0", cwd=r.path)
    assert "### Added\n- **api:** add users endpoint" in text
    assert "### Fixed\n- guard nulls" in text


def test_unreleased_header_has_no_date(git_repo):
    r = git_repo
    r.commit("feat: add search", "s.py", "1")
    text = generate_release_notes(cwd=r.path)
    assert text.splitlines()[0] == "## [Unreleased]"
    r2 = build_release("Unreleased", [], release_date="2026-01-01")
    assert r2.header() == "## [Unreleased]"
    empty = generate_release_notes(from_tag="HEAD", cwd=r.path)
    assert empty == "## [Unreleased]\n\n_No user-facing changes._\n"
    # Versioned releases keep their date, v-prefix untouched.
    assert build_release("v1.2.0", [], release_date="2026-07-19").header() == (
        "## [v1.2.0] - 2026-07-19"
    )


@pytest.fixture
def backport_repo(git_repo):
    """v2.0.0 is the newest release; v1.9.1 is a backport tagged *later* on an
    older commit. Tag creation dates are pinned so the order is deterministic."""
    r = git_repo
    r.commit("feat: one", "a.py", "1")
    r.commit("feat: two", "a.py", "2")
    r.git("tag", "-a", "v2.0.0", "-m", "v2", env={"GIT_COMMITTER_DATE": "2026-03-01T10:00:00"})
    r.commit("fix: three", "a.py", "3")
    r.git(
        "tag", "-a", "v1.9.1", "-m", "backport", "HEAD~2",
        env={"GIT_COMMITTER_DATE": "2026-03-05T10:00:00"},
    )
    return r


def test_latest_tag_is_the_nearest_reachable_not_the_newest_created(backport_repo):
    r = backport_repo
    # Sorting by creation date (the old behaviour) put v1.9.1 last.
    by_date = r.git("tag", "--sort=creatordate").split()
    assert by_date[-1] == "v1.9.1"
    assert latest_tag(cwd=r.path) == "v2.0.0"
    assert list_tags(cwd=r.path) == ["v1.9.1", "v2.0.0"]


def test_already_released_commits_are_not_unreleased(backport_repo):
    r = backport_repo
    text = generate_release_notes(from_tag=latest_tag(cwd=r.path), cwd=r.path)
    assert "three" in text
    assert "two" not in text


def test_full_changelog_sections_follow_versions(backport_repo):
    r = backport_repo
    text = generate_full_changelog(cwd=r.path)
    assert text.startswith("# Changelog")
    unreleased = text.index("## [Unreleased]")
    v2 = text.index("## [v2.0.0]")
    v191 = text.index("## [v1.9.1]")
    assert unreleased < v2 < v191
    assert "three" in text[unreleased:v2]
    assert "two" in text[v2:v191] and "one" not in text[v2:v191]
    assert "one" in text[v191:]


def test_full_changelog_ignores_tags_not_reachable_from_head(git_repo):
    r = git_repo
    r.commit("feat: base", "a.py", "1")
    r.git("checkout", "-q", "-b", "other")
    r.commit("feat: elsewhere", "b.py", "1")
    r.git("tag", "v9.0.0")
    r.git("checkout", "-q", "main")
    r.commit("fix: here", "a.py", "2")
    text = generate_full_changelog(cwd=r.path)
    assert "v9.0.0" not in text
    assert "here" in text


def test_latest_tag_without_tags_is_none(git_repo):
    r = git_repo
    r.commit("feat: x", "a.py", "1")
    assert latest_tag(cwd=r.path) is None
