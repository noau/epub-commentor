#!/usr/bin/env python3
"""User-facing driver: load credentials from ``format.json`` and run the
full annotation pipeline against an EPUB.

Usage:
    poetry run python scripts/comment_epub.py path/to/book.epub
    poetry run python scripts/comment_epub.py path/to/book.epub --synopsis "A cookbook"

The script is a thin wrapper around ``epub_commentor.cli:main`` — it adds
nothing on top of the CLI surface. Kept as a separate entry point so users
running the project from the repo root (without ``poetry install``) have
an obvious command to invoke.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make ``epub_commentor`` importable when the script is run from a
# checkout (where the package isn't installed in editable mode yet).
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from epub_commentor.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
