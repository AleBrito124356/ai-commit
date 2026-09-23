"""``python -m aicommit`` — same as the ``aicommit`` command.

The git hook uses this form with the absolute interpreter aicommit was
installed with, so it works even when the ``aicommit`` script is not on PATH.
"""

from .cli import run

if __name__ == "__main__":
    run()
