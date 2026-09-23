"""The rule-based ``local`` backend: drafts must be sensible and always valid."""

from __future__ import annotations

import pytest

from aicommit.commit import generate_commit, validate_commit
from aicommit.config import Config
from aicommit.diff import parse_diff
from aicommit.heuristic import LocalBackend, draft_commit, draft_pr, enclosing_definitions
from aicommit.llm import LLMClient, LLMError
from aicommit.pr import collect_pr_context, generate_pr


def modified(path: str, body: str, context: str = "") -> str:
    return (
        f"diff --git a/{path} b/{path}\nindex 1111111..2222222 100644\n"
        f"--- a/{path}\n+++ b/{path}\n@@ -1,3 +1,4 @@ {context}\n{body}\n"
    )


def added(path: str, body: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\nindex 0000000..1111111\n"
        f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,3 @@\n{body}\n"
    )


def deleted(path: str, body: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\ndeleted file mode 100644\nindex 1111111..0000000\n"
        f"--- a/{path}\n+++ /dev/null\n@@ -1,3 +0,0 @@\n{body}\n"
    )


def renamed(old: str, new: str) -> str:
    return (
        f"diff --git a/{old} b/{new}\nsimilarity index 100%\n"
        f"rename from {old}\nrename to {new}\n"
    )


CASES = {
    # name: (diff, expected type, expected scope, expected subject)
    "tests-only": (
        added("tests/test_auth.py", "+def test_login_locked():\n+    assert True"),
        "test", None, "add tests for auth",
    ),
    "docs-only": (
        modified("README.md", "-old\n+new") + modified("docs/install.md", "-a\n+b"),
        "docs", None, "update README and install.md",
    ),
    "ci-only": (
        modified(".github/workflows/ci.yml", "-  python: 3.11\n+  python: 3.12"),
        "ci", None, "update ci workflow",
    ),
    "lockfile-only": (
        modified("poetry.lock", "-x = 1\n+x = 2"),
        "build", "deps", "update dependencies",
    ),
    "dependency-bump": (
        modified("pyproject.toml", '-    "httpx>=0.26",\n+    "httpx>=0.27",')
        + modified("package.json", '-    "react": "^18.2.0",\n+    "react": "^18.3.1",'),
        "build", "deps", "update dependencies",
    ),
    "new-module": (
        added("src/payments/retry.py", "+def retry_payment(p):\n+    return p"),
        "feat", "payments", "add retry",
    ),
    "new-function": (
        modified("src/aicommit/diff.py", "+def plan_render(bundle, budget):\n+    return None", "class DiffBundle:"),
        "feat", "diff", "add plan_render",
    ),
    "deletion": (
        deleted("src/legacy.py", "-def old():\n-    pass"),
        "refactor", None, "remove legacy",
    ),
    "rename": (
        renamed("src/util.py", "src/helpers.py"),
        "refactor", None, "rename util.py to helpers.py",
    ),
    "move": (
        renamed("util.py", "src/util.py"),
        "refactor", None, "move util.py to src/",
    ),
    "guard-fix": (
        modified("src/auth.py", "     if user.locked:\n+        raise AccountLocked(user)\n     check(password)", "def login(user, password):"),
        "fix", "auth", "handle AccountLocked in login",
    ),
    "whitespace-only": (
        modified("src/app.py", "-def f( x ):\n+def f(x):"),
        "style", None, "format app",
    ),
    "perf": (
        modified("src/repo.py", "+@lru_cache(maxsize=128)\n def lookup(key):", "class Repo:"),
        "perf", "repo", "cache lookup",
    ),
    "config-only": (
        modified(".gitignore", "+.venv/"),
        "chore", None, "update .gitignore",
    ),
    "generated-only": (
        modified("dist/bundle.js", "-a\n+b"),
        "chore", None, "regenerate bundle.js",
    ),
    "mixed-source-and-tests": (
        modified("src/aicommit/diff.py", "+def plan_render(bundle, budget):\n+    return None", "class DiffBundle:")
        + modified("tests/test_diff.py", "+def test_plan():\n+    pass"),
        "feat", "diff", "add plan_render with tests",
    ),
    "mixed-docs-and-ci": (
        modified("README.md", "-a\n+b\n+c\n+d") + modified(".github/workflows/ci.yml", "-x\n+y"),
        "docs", None, "update README and ci workflow",
    ),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_draft_branches(name):
    diff, type_, scope, subject = CASES[name]
    result = draft_commit(parse_diff(diff), Config())
    best = result.best
    assert (best.type, best.scope, best.subject) == (type_, scope, subject), best.header()
    for candidate in result.all:
        assert validate_commit(candidate, Config()) == [], candidate.header()
    assert len({c.header() for c in result.all}) == len(result.all)


@pytest.mark.parametrize("name", sorted(CASES))
def test_drafts_are_valid_in_spanish_and_with_emoji(name):
    diff = CASES[name][0]
    cfg = Config(language="es", emoji=True)
    result = draft_commit(parse_diff(diff), cfg)
    for candidate in result.all:
        assert validate_commit(candidate, cfg) == [], candidate.header()


def test_spanish_subject():
    diff = CASES["guard-fix"][0]
    best = draft_commit(parse_diff(diff), Config(language="es")).best
    assert best.header() == "fix(auth): maneja AccountLocked en login"


def test_body_lists_files_and_skipped_noise():
    diff = CASES["guard-fix"][0] + modified("dist/app.min.js", "-a\n+b")
    best = draft_commit(parse_diff(diff), Config()).best
    assert "- src/auth.py (+1 -0): in login" in best.body
    assert "Not analysed (generated): dist/app.min.js" in best.body


def test_issue_reference_from_hint_goes_to_footer():
    diff = CASES["guard-fix"][0]
    best = draft_commit(parse_diff(diff), Config(), hint="closes #212").best
    assert best.footer == "Closes #212"
    assert draft_commit(parse_diff(diff), Config(), hint="see org/repo#7").best.footer == "Refs org/repo#7"
    result = draft_commit(parse_diff(diff), Config(), hint="make it snappy")
    assert result.best.footer == ""
    assert any("hint is ignored" in note for note in result.notes)


def test_respects_restricted_allowed_types():
    cfg = Config(allowed_types=["feat", "fix", "chore"])
    for name, (diff, *_rest) in CASES.items():
        result = draft_commit(parse_diff(diff), cfg)
        for candidate in result.all:
            assert validate_commit(candidate, cfg) == [], (name, candidate.header())


def test_long_names_are_trimmed_to_the_subject_limit():
    diff = modified(
        "src/service.py",
        "+def reconcile_all_outstanding_customer_invoices_with_the_ledger(x):\n+    pass",
    )
    cfg = Config(subject_max_length=30)
    result = draft_commit(parse_diff(diff), cfg)
    assert len(result.best.subject) <= 30
    assert validate_commit(result.best, cfg) == []


def test_empty_diff_is_an_error():
    with pytest.raises(LLMError, match="empty"):
        draft_commit(parse_diff(""), Config())


def test_enclosing_definitions_uses_context_inside_the_hunk():
    diff = (
        "diff --git a/auth.py b/auth.py\nindex 1..2 100644\n--- a/auth.py\n+++ b/auth.py\n"
        "@@ -1,2 +1,4 @@\n def login(user):\n+    if user.locked:\n+        raise AccountLocked(user)\n"
        "     return token(user)\n"
    )
    assert enclosing_definitions(parse_diff(diff).files[0]) == ["login"]


# --- the backend object -------------------------------------------------------- #


def test_from_config_returns_local_backend():
    backend = LLMClient.from_config(Config(backend="local"))
    assert isinstance(backend, LocalBackend)
    assert backend.backend == "local"


def test_local_backend_refuses_free_text():
    with pytest.raises(LLMError, match="rule-based"):
        LocalBackend().complete("system", "user")


def test_generate_commit_dispatches_to_local_backend():
    diff = CASES["guard-fix"][0]
    backend = LocalBackend()
    # With the parsed bundle...
    result = generate_commit("ignored", Config(), backend, bundle=parse_diff(diff))
    assert result.best.header() == "fix(auth): handle AccountLocked in login"
    # ...or from the rendered diff text when no bundle is given.
    again = generate_commit(diff, Config(), LocalBackend())
    assert again.best.header() == result.best.header()


def test_regenerate_rotates_through_candidates():
    diff = parse_diff(CASES["guard-fix"][0])
    backend = LocalBackend()
    first = backend.draft_commit(diff, Config())
    second = backend.draft_commit(diff, Config())
    assert second.best.header() == first.alternatives[0].header()
    assert {c.header() for c in second.all} == {c.header() for c in first.all}


# --- PR drafts ------------------------------------------------------------------- #


@pytest.fixture
def feature_repo(git_repo):
    r = git_repo
    r.commit("chore: initial", "README.md", "# shop\n")
    r.write("legacy.py", "old = 1\n")
    r.commit("chore: add legacy", None)
    r.git("checkout", "-q", "-b", "feature/retries")
    r.commit("feat(payments)!: retry failed payments", "src/payments.py", "def pay():\n    retry()\n")
    r.commit("fix(payments): cap retries", "src/payments.py", "def pay():\n    retry(max=3)\n")
    r.commit("test(payments): cover retries", "tests/test_payments.py", "def test_pay():\n    pass\n")
    r.write("requirements.txt", "httpx>=0.27\n")
    r.git("rm", "-q", "legacy.py")
    r.commit("build: pin httpx", None)
    return r


def test_draft_pr_is_deterministic_and_complete(feature_repo):
    context = collect_pr_context(base="main", config=Config(), cwd=feature_repo.path)
    pr = draft_pr(context, Config())
    assert pr.title == "Retry failed payments"
    assert pr.summary.startswith("4 commits on `feature/retries` compared with `main`")
    assert "1 feature, 1 fix, 1 test change, 1 build change" in pr.summary
    assert pr.changes[0] == "feat(payments)!: retry failed payments"
    assert pr.changes[1] == "fix(payments): cap retries"
    assert pr.testing == ["Test files changed: `tests/test_payments.py`"]
    risks = "\n".join(pr.risks)
    assert "Breaking change: feat(payments)!: retry failed payments" in risks
    assert "Deletes 1 file: `legacy.py`" in risks
    assert "`requirements.txt`" in risks
    # Same input, same output.
    assert draft_pr(context, Config()) == pr


def test_generate_pr_uses_local_backend(feature_repo):
    result = generate_pr(base="main", config=Config(backend="local"), client=LocalBackend(), cwd=feature_repo.path)
    assert result.title == "Retry failed payments"
    md = result.to_markdown()
    assert "## Risks" in md and "Breaking change" in md


def test_draft_pr_without_risks_or_tests_says_so(git_repo):
    r = git_repo
    r.commit("chore: initial", "README.md", "x\n")
    r.git("checkout", "-q", "-b", "docs")
    r.commit("docs: explain setup", "README.md", "setup\n")
    context = collect_pr_context(base="main", config=Config(), cwd=r.path)
    pr = draft_pr(context, Config(language="es"))
    assert pr.title == "Explain setup"
    assert "No cambió ningún archivo de prueba" in pr.testing[0]
    assert pr.risks[0].startswith("No se detectaron")
