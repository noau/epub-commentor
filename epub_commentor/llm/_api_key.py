"""Resolve the LLM API key with environment-variable precedence over ``format.json``.

The single source of truth for "what is my API key?" lives here.  Both
loaders — :func:`epub_commentor.cli._construct_llm` and
:func:`scripts.utils.load_comment_llm` — call :func:`resolve_api_key` before
constructing :class:`~epub_commentor.llm.core.LLM`.

Why env-var-first
-----------------
Keeping a secret in a checked-in config file is a foot-gun: ``format.json``
gets ``git add``-ed accidentally, shared via screenshots, or pasted into
issues.  Twelve-factor style is to source secrets from the environment so
they never touch disk.  We honour that without breaking back-compat: a
user who still puts the key in ``format.json`` continues to work — the env
var simply shadows it when set.

LLM contract is unchanged
-------------------------
:class:`~epub_commentor.llm.core.LLM.__init__` still takes ``key`` as an
explicit positional argument.  Programmatic users who want full control
keep full control; this module only nudges the two CLI-style loader paths
toward the safer default.
"""

from __future__ import annotations

import os
from typing import Final

#: Environment variable consulted before ``format.json``'s ``key`` field.
#: Named project-locally (rather than ``OPENAI_API_KEY`` etc.) so the
#: same secret works regardless of which OpenAI-compatible provider the
#: user points ``format.json.url`` at.
EPUB_COMMENTOR_API_KEY_ENV_VAR: Final[str] = "EPUB_COMMENTOR_API_KEY"


def _is_unresolved_placeholder(value: str) -> bool:
    """Return True if ``value`` looks like a ``<YOUR_API_KEY>`` template stub.

    Detects the literal placeholder shipped in ``format.template.json``
    (and any other ``<…>``-bracketed stub a user might paste) so the loader
    surfaces a "missing key" error instead of forwarding the placeholder
    to the upstream provider and getting back a cryptic 401.
    """
    stripped = value.strip()
    if len(stripped) < 3:
        return True
    return stripped.startswith("<") and stripped.endswith(">")


def resolve_api_key(
    format_key: str | None,
    env_var: str = EPUB_COMMENTOR_API_KEY_ENV_VAR,
) -> str | None:
    """Return the API key with env-var precedence over ``format.json``.

    Lookup order:

    1. ``$EPUB_COMMENTOR_API_KEY`` — if set and non-empty after stripping,
       used verbatim.  Whatever sits in ``format.json.key`` is **ignored**.
    2. ``format_key`` — ``format.json``'s ``key`` field.  Empty strings
       and ``<PLACEHOLDER>`` stubs are treated as missing.

    Parameters
    ----------
    format_key : str | None
        The ``key`` field loaded from ``format.json`` (may already be
        ``None`` if the key was absent).
    env_var : str
        Override the env-var name.  Defaults to
        :data:`EPUB_COMMENTOR_API_KEY_ENV_VAR`; tests pass ``"EPub_None"``
        etc. to keep the production env untouched.

    Returns
    -------
    str | None
        The resolved key (already stripped of surrounding whitespace) or
        ``None`` if neither source yields one.  Callers are expected to
        translate the ``None`` into a clear configuration-error message.
    """
    env_value = os.environ.get(env_var, "").strip()
    if env_value:
        return env_value
    if format_key is None or not isinstance(format_key, str):
        return None
    if _is_unresolved_placeholder(format_key):
        return None
    stripped = format_key.strip()
    return stripped or None
