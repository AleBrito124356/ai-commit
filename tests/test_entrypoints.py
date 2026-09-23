"""``python -m aicommit`` and the repo-root ``cli.py`` shim both work."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from aicommit import __version__

ROOT = Path(__file__).resolve().parents[1]


def _run(args, cwd):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_python_dash_m_prints_version(tmp_path):
    result = _run(["-m", "aicommit", "--version"], cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"aicommit {__version__}"


def test_python_dash_m_help_lists_backends(tmp_path):
    result = _run(["-m", "aicommit", "--help"], cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "local" in result.stdout


def test_repo_root_shim(tmp_path):
    result = _run([str(ROOT / "cli.py"), "--version"], cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert __version__ in result.stdout
