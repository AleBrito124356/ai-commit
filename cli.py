#!/usr/bin/env python3
"""Run aicommit without installing it.

This is a convenience shim for hacking on the tool from a checkout. Once you
`pip install -e .` (or `pipx install .`), the `aicommit` command does the same
thing and is what the git hook calls.

    python cli.py            # interactive commit
    python cli.py pr --base main
    python cli.py changelog --from v1.0.0
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the src-layout package importable when running from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from aicommit.cli import run  # noqa: E402

if __name__ == "__main__":
    run()
