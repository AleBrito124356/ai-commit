"""Diff parsing, file classification and prompt-size truncation."""

from __future__ import annotations

from aicommit.diff import parse_diff, render_for_prompt

SAMPLE_DIFF = """\
diff --git a/src/app.py b/src/app.py
index 83db48f..bf3d3c4 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,4 @@
 import os
+import sys

 def main():
diff --git a/README.md b/README.md
new file mode 100644
index 0000000..1234567
--- /dev/null
+++ b/README.md
@@ -0,0 +1,2 @@
+# Title
+some text
diff --git a/old.txt b/old.txt
deleted file mode 100644
index 89abcde..0000000
--- a/old.txt
+++ /dev/null
@@ -1,2 +0,0 @@
-gone
-away
diff --git a/logo.png b/logo.png
new file mode 100644
index 0000000..aaaaaaa
Binary files /dev/null and b/logo.png differ
diff --git a/package-lock.json b/package-lock.json
index 1111111..2222222 100644
--- a/package-lock.json
+++ b/package-lock.json
@@ -1,3 +1,3 @@
-  "version": "1.0.0"
+  "version": "1.0.1"
"""


def test_parse_diff_counts_files():
    bundle = parse_diff(SAMPLE_DIFF)
    assert len(bundle.files) == 5
    by_path = {f.path: f for f in bundle.files}
    assert set(by_path) == {
        "src/app.py",
        "README.md",
        "old.txt",
        "logo.png",
        "package-lock.json",
    }


def test_parse_diff_change_types():
    by_path = {f.path: f for f in parse_diff(SAMPLE_DIFF).files}
    assert by_path["src/app.py"].change_type == "modified"
    assert by_path["README.md"].change_type == "added"
    assert by_path["old.txt"].change_type == "deleted"


def test_parse_diff_addition_deletion_counts():
    by_path = {f.path: f for f in parse_diff(SAMPLE_DIFF).files}
    assert by_path["src/app.py"].additions == 1
    assert by_path["src/app.py"].deletions == 0
    assert by_path["README.md"].additions == 2
    assert by_path["old.txt"].deletions == 2


def test_binary_and_lockfile_flagged_as_noise():
    by_path = {f.path: f for f in parse_diff(SAMPLE_DIFF).files}
    assert by_path["logo.png"].is_binary is True
    assert by_path["logo.png"].is_noise is True
    assert by_path["package-lock.json"].is_lockfile is True
    assert by_path["package-lock.json"].is_noise is True


def test_content_vs_skipped_partition():
    bundle = parse_diff(SAMPLE_DIFF)
    content = {f.path for f in bundle.content_files}
    skipped = {f.path for f in bundle.skipped_files}
    assert content == {"src/app.py", "README.md", "old.txt"}
    assert skipped == {"logo.png", "package-lock.json"}


def test_rename_detection():
    diff = (
        "diff --git a/old/name.py b/new/name.py\n"
        "similarity index 95%\n"
        "rename from old/name.py\n"
        "rename to new/name.py\n"
    )
    bundle = parse_diff(diff)
    f = bundle.files[0]
    assert f.change_type == "renamed"
    assert f.path == "new/name.py"
    assert f.old_path == "old/name.py"


def test_render_full_when_under_budget():
    rendered = render_for_prompt(parse_diff(SAMPLE_DIFF), budget=10_000)
    # Real content is present.
    assert "import sys" in rendered
    # Skipped files are named but their content is never included.
    assert "logo.png" in rendered
    assert "package-lock.json" in rendered
    assert "1.0.1" not in rendered


def test_render_condenses_under_tiny_budget():
    rendered = render_for_prompt(parse_diff(SAMPLE_DIFF), budget=60)
    assert len(rendered) <= 60
    # The raw patch body should be gone at this size.
    assert "import sys" not in rendered


def test_render_empty_bundle():
    assert render_for_prompt(parse_diff(""), budget=1000) == ""
