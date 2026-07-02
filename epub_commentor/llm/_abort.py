"""Cooperative Ctrl-C / SIGINT handling for the LLM pipeline.

A module-level :class:`threading.Event` lets every layer of the pipeline
(streaming chunks, retry sleep, executor cache lookup) react to a user
abort without each one needing its own flag. The signal handler is
**two-stage**: the first SIGINT sets the flag and prints a hint, the
second SIGINT calls :func:`os._exit` so a stuck worker cannot block
shutdown indefinitely.

Usage:

* :func:`install_sigint_handler` is called at the top of
  :func:`epub_commentor.commentor.comment_epub` and reverts on exit.
* :func:`is_abort_requested` is polled by the streaming loop in
  :mod:`epub_commentor.llm.executor` and by :class:`LLMContext`.
* :func:`reset_abort` is used by tests to start from a clean slate.

The handler is process-wide: there is exactly one, and ``install`` is
idempotent so a library user calling :func:`comment_epub` while a CLI
loop already installed one is a no-op.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
from collections.abc import Callable

_abort_event = threading.Event()
_previous_sigint: Callable | int | None = None
_handler_installed = False

_HINT = "\naborting... (press Ctrl-C again to force-kill)\n"


def is_abort_requested() -> bool:
    """True after the user (or a test) has asked the pipeline to stop."""
    return _abort_event.is_set()


def request_abort() -> None:
    """Programmatically request an abort (mainly used by tests)."""
    _abort_event.set()


def reset_abort() -> None:
    """Clear the abort flag — used by tests to isolate state."""
    _abort_event.clear()


def install_sigint_handler() -> None:
    """Install the two-stage SIGINT handler. Idempotent."""
    global _previous_sigint, _handler_installed

    def _handler(signum: int, frame: object | None) -> None:
        if _abort_event.is_set():
            # Second Ctrl-C: hard exit. Skip all cleanup so the user
            # never has to wait for a stuck stream / progress thread.
            os._exit(130)
        _abort_event.set()
        # Print to stderr with a trailing newline so the next terminal
        # prompt (if any) lands on its own line.
        try:
            sys.stderr.write(_HINT)
            sys.stderr.flush()
        except Exception:  # noqa: BLE001 - best-effort hint
            pass

    if not _handler_installed:
        _previous_sigint = signal.signal(signal.SIGINT, _handler)
        _handler_installed = True


def restore_sigint_handler() -> None:
    """Restore the SIGINT handler that was active before install.

    Safe to call when nothing was installed. Restoring ``SIG_DFL`` is
    acceptable: the default Python handler raises ``KeyboardInterrupt``,
    which the standard interpreter exit sequence handles with exit
    code 130 on its own.
    """
    global _previous_sigint, _handler_installed
    if _handler_installed:
        signal.signal(signal.SIGINT, _previous_sigint if _previous_sigint is not None else signal.SIG_DFL)
        _previous_sigint = None
        _handler_installed = False


__all__ = [
    "install_sigint_handler",
    "is_abort_requested",
    "request_abort",
    "reset_abort",
    "restore_sigint_handler",
]
