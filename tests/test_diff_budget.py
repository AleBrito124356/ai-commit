"""Budget-aware diff condensation, noise detection and ignore patterns."""

from __future__ import annotations

import random

from aicommit.diff import (
    annotate_bundle,
    classify_path,
    collect_staged,
    looks_generated,
    parse_diff,
    path_matches,
    plan_render,
    render_for_prompt,
)


def _fixture_plus_auth_diff() -> str:
    """The audit's regression case: a 400-line fixture drowns a 1-line fix."""
    rows = "\n".join(
        f'+  {{"id": {i}, "name": "user-{i}", "email": "user{i}@example.com"}},'
        for i in range(400)
    )
    fixture = (
        "diff --git a/data/fixtures.json b/data/fixtures.json\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        "+++ b/data/fixtures.json\n"
        "@@ -0,0 +1,400 @@\n" + rows + "\n"
    )
    auth = (
        "diff --git a/src/auth.py b/src/auth.py\n"
        "index 2222222..3333333 100644\n"
        "--- a/src/auth.py\n"
        "+++ b/src/auth.py\n"
        "@@ -10,3 +10,4 @@ def login(user, password):\n"
        "     if user.locked:\n"
        "+        raise AccountLocked(user)\n"
        "     check(password)\n"
    )
    return fixture + auth


# --- the allocator ----------------------------------------------------------- #


def test_small_source_change_survives_a_huge_fixture():
    text = _fixture_plus_auth_diff()
    assert len(text) > 12000  # over budget, so condensation must kick in
    rendered = plan_render(parse_diff(text), 12000)
    assert len(rendered.text) <= 12000
    assert "AccountLocked" in rendered.text
    assert rendered.modes["src/auth.py"] == "full"
    assert rendered.modes["data/fixtures.json"] == "condensed"
    assert rendered.stage == "mixed"
    # The fixture is still represented: stat line plus a sample of its rows.
    assert "data/fixtures.json (added, +400 -0)" in rendered.text
    assert "more changed lines" in rendered.text


def test_render_for_prompt_matches_plan_render():
    bundle = parse_diff(_fixture_plus_auth_diff())
    assert render_for_prompt(bundle, 5000) == plan_render(bundle, 5000).text


def test_full_stage_when_everything_fits():
    rendered = plan_render(parse_diff(_fixture_plus_auth_diff()), 100_000)
    assert rendered.stage == "full"
    assert set(rendered.modes.values()) == {"full"}


def test_describe_reports_stage_and_modes():
    rendered = plan_render(parse_diff(_fixture_plus_auth_diff()), 12000)
    text = rendered.describe()
    assert "mixed" in text
    assert "full: src/auth.py" in text
    assert "condensed: data/fixtures.json" in text


def _random_diff(rng: random.Random, n_files: int) -> str:
    chunks = []
    exts = [".py", ".js", ".md", ".json", ".yml", ".go"]
    dirs = ["src", "tests", "docs", "data", "lib/core", ""]
    for i in range(n_files):
        folder = rng.choice(dirs)
        name = f"file_{i}{rng.choice(exts)}"
        path = f"{folder}/{name}" if folder else name
        hunks = []
        for h in range(rng.randint(1, 4)):
            n_lines = rng.choice([1, 3, 10, 40, 150])
            width = rng.choice([10, 40, 120, 400])
            body = "\n".join(
                f"{rng.choice('+- ')}{'x' * rng.randint(1, width)}" for _ in range(n_lines)
            )
            hunks.append(f"@@ -{h * 50},3 +{h * 50},4 @@ def fn_{h}():\n{body}")
        chunks.append(
            f"diff --git a/{path} b/{path}\nindex 1111111..2222222 100644\n"
            f"--- a/{path}\n+++ b/{path}\n" + "\n".join(hunks)
        )
    return "\n".join(chunks) + "\n"


def test_property_output_never_exceeds_budget_and_names_every_file():
    rng = random.Random(1234)
    for _ in range(300):
        text = _random_diff(rng, rng.randint(1, 25))
        bundle = parse_diff(text)
        budget = rng.choice([80, 300, 1000, 2500, 6000, 12000, 40000])
        rendered = plan_render(bundle, budget)
        assert len(rendered.text) <= budget
        stat_only = "\n".join(f"# {f.stat_line()}" for f in bundle.files)
        if budget >= len(stat_only):
            for f in bundle.files:
                assert f.path in rendered.text, (f.path, budget)
        # Every file got exactly one mode.
        assert set(rendered.modes) == {f.path for f in bundle.files}


def test_condensed_file_keeps_hunk_headers_with_function_context():
    body = "\n".join(f"+    line_{i} = compute({i})" for i in range(300))
    diff = (
        "diff --git a/src/engine.py b/src/engine.py\n"
        "index 1..2 100644\n--- a/src/engine.py\n+++ b/src/engine.py\n"
        "@@ -1,3 +1,303 @@ class Engine:\n" + body + "\n"
    )
    rendered = plan_render(parse_diff(diff), 1500)
    assert len(rendered.text) <= 1500
    assert "@@ -1,3 +1,303 @@ class Engine:" in rendered.text
    assert "line_0 = compute(0)" in rendered.text
    assert rendered.modes["src/engine.py"] == "condensed"


def test_hunk_headers_are_kept_even_when_no_changed_line_fits():
    hunks = "\n".join(
        f"@@ -{i * 10},2 +{i * 10},3 @@ def handler_{i}():\n+    x = {i}" for i in range(40)
    )
    diff = (
        "diff --git a/src/routes.py b/src/routes.py\nindex 1..2 100644\n"
        "--- a/src/routes.py\n+++ b/src/routes.py\n" + hunks + "\n"
    )
    rendered = plan_render(parse_diff(diff), 400)
    assert len(rendered.text) <= 400
    assert "def handler_0()" in rendered.text
    assert "more hunks" in rendered.text


def test_very_long_lines_are_clipped_when_condensing():
    long_line = "+" + "y" * 5000
    diff = (
        "diff --git a/src/big.py b/src/big.py\nindex 1..2 100644\n"
        "--- a/src/big.py\n+++ b/src/big.py\n@@ -1 +1,3 @@\n"
        + "\n".join([long_line] * 3)
        + "\n"
    )
    rendered = plan_render(parse_diff(diff), 2000)
    assert len(rendered.text) <= 2000
    assert "y" * 300 not in rendered.text


def test_huge_skip_list_is_compressed_to_fit():
    diff = "".join(
        f"diff --git a/dist/chunk_{i}.js b/dist/chunk_{i}.js\nindex 1..2 100644\n"
        f"--- a/dist/chunk_{i}.js\n+++ b/dist/chunk_{i}.js\n@@ -1 +1 @@\n-a\n+b\n"
        for i in range(300)
    ) + (
        "diff --git a/src/app.py b/src/app.py\nindex 1..2 100644\n"
        "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-old()\n+new_behaviour()\n"
    )
    rendered = plan_render(parse_diff(diff), 3000)
    assert len(rendered.text) <= 3000
    assert "new_behaviour" in rendered.text
    assert "300 files" in rendered.text


# --- noise detection ---------------------------------------------------------- #


def _single_file_diff(path: str, first_line: str = "+hello") -> str:
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\nindex 0..1\n"
        f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n{first_line}\n"
    )


def test_generated_paths_are_noise():
    for path in [
        "static/app.min.js",
        "static/app.min.css",
        "static/app.js.map",
        "tests/__snapshots__/view.test.js.snap",
        "dist/index.js",
        "packages/ui/dist/index.js",
        "build/lib/module.py",
        "vendor/github.com/x/y.go",
        "web/node_modules/left-pad/index.js",
        "api/users.pb.go",
        "proto/users_pb2.py",
    ]:
        assert looks_generated(path), path
        f = parse_diff(_single_file_diff(path)).files[0]
        assert f.is_noise, path
        assert f.noise_reason == "generated"


def test_source_paths_are_not_generated():
    for path in ["src/app.js", "tools/build/script.py", "src/distance.py", "lib/vendor.py"]:
        assert not looks_generated(path), path


def test_generated_marker_in_content_is_noise():
    diff = _single_file_diff("src/models.go", "+// Code generated by protoc-gen-go. DO NOT EDIT.")
    f = parse_diff(diff).files[0]
    assert f.is_generated and f.is_noise
    diff2 = _single_file_diff("src/schema.ts", "+/* @generated */")
    assert parse_diff(diff2).files[0].is_noise


def test_patch_lines_that_look_like_headers_are_not_misparsed():
    diff = (
        "diff --git a/notes.txt b/notes.txt\nindex 1..2 100644\n"
        "--- a/notes.txt\n+++ b/notes.txt\n@@ -1,2 +1,2 @@\n"
        "--- a/elsewhere.txt\n"
        "+Binary files are fine to mention\n"
    )
    f = parse_diff(diff).files[0]
    assert f.old_path == "notes.txt"
    assert f.is_binary is False


def test_skipped_generated_file_is_named_but_not_shown():
    diff = _single_file_diff("dist/bundle.js", "+secret_minified_content()") + _single_file_diff(
        "src/app.py", "+print('hi')"
    )
    rendered = plan_render(parse_diff(diff), 5000)
    assert "dist/bundle.js" in rendered.text
    assert "secret_minified_content" not in rendered.text
    assert rendered.modes["dist/bundle.js"] == "skipped:generated"


def test_path_matches_patterns():
    assert path_matches("src/generated/api.ts", "src/generated/")
    assert path_matches("pkg/generated/api.ts", "generated/")
    assert path_matches("api/users.pb.go", "*.pb.go")
    assert path_matches("deep/nested/users.pb.go", "**/*.pb.go")
    assert path_matches("tests/fixtures/big.json", "tests/fixtures/*.json")
    assert path_matches("a/b/fixtures/big.json", "**/fixtures/*.json")
    assert path_matches("schema.graphql", "./schema.graphql")
    assert not path_matches("src/app.py", "*.pb.go")
    assert not path_matches("src/app.py", "")


def test_ignore_paths_mark_files_as_noise():
    diff = _single_file_diff("src/generated/api.ts", "+export const x = 1") + _single_file_diff(
        "src/app.py", "+print('hi')"
    )
    bundle = annotate_bundle(parse_diff(diff), ["src/generated/"], check_attributes=False)
    by_path = {f.path: f for f in bundle.files}
    assert by_path["src/generated/api.ts"].ignored
    assert by_path["src/generated/api.ts"].noise_reason == "ignored"
    assert not by_path["src/app.py"].is_noise
    rendered = plan_render(bundle, 5000)
    assert "export const x" not in rendered.text
    assert "src/generated/api.ts" in rendered.text


def test_gitattributes_linguist_generated_is_respected(git_repo):
    r = git_repo
    r.commit("chore: init", "README.md", "hi\n")
    r.write(
        ".gitattributes",
        "schema/*.ts linguist-generated=true\nthird_party/** linguist-vendored\n",
    )
    r.write("schema/types.ts", "export type Secret = 'generated';\n")
    r.write("third_party/lib/x.js", "vendored()\n")
    r.write("src/app.ts", "export const app = 1;\n")
    r.git("add", "-A")
    bundle = collect_staged(cwd=r.path)
    by_path = {f.path: f for f in bundle.files}
    assert by_path["schema/types.ts"].is_generated
    assert by_path["third_party/lib/x.js"].is_generated
    assert not by_path["src/app.ts"].is_noise
    rendered = plan_render(bundle, 5000)
    assert "Secret" not in rendered.text
    assert "export const app" in rendered.text


def test_collect_staged_applies_ignore_paths(git_repo):
    r = git_repo
    r.commit("chore: init", "README.md", "hi\n")
    r.write("gen/out.py", "GENERATED = True\n")
    r.write("src/app.py", "print('hi')\n")
    r.git("add", "-A")
    bundle = collect_staged(cwd=r.path, ignore_paths=["gen/"])
    assert [f.path for f in bundle.content_files] == ["src/app.py"]


def test_classify_path_categories():
    cases = {
        "src/aicommit/cli.py": "source",
        "tests/test_cli.py": "test",
        "pkg/auth_test.go": "test",
        "web/src/app.test.tsx": "test",
        "README.md": "docs",
        "docs/guide/install.rst": "docs",
        "LICENSE": "docs",
        ".github/workflows/ci.yml": "ci",
        ".gitlab-ci.yml": "ci",
        "package.json": "build",
        "requirements-dev.txt": "build",
        "poetry.lock": "build",
        "Dockerfile": "build",
        "docker-compose.yml": "build",
        ".gitignore": "config",
        ".aicommit.yaml": "config",
        "config/settings.toml": "config",
        "data/fixtures.json": "data",
        "tests/fixtures/users.json": "data",
        "exports/report.csv": "data",
        "assets/logo.png": "asset",
    }
    for path, expected in cases.items():
        assert classify_path(path) == expected, (path, classify_path(path))
