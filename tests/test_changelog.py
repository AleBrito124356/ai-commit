"""Changelog grouping, rendering and end-to-end generation from git."""

from __future__ import annotations

from aicommit.changelog import (
    build_release,
    generate_release_notes,
    group_commits,
    render_full_changelog,
)

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
