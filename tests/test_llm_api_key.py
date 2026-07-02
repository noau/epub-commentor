"""Unit tests for :func:`epub_commentor.llm._api_key.resolve_api_key`.

The CLI / ``scripts/utils.py`` rely on this helper to decide "what is my
LLM secret?". Two rules dominate:

1. ``$EPUB_COMMENTOR_API_KEY`` (or the overridden ``env_var``) **always wins**
   over ``format.json``'s ``key`` field when set and non-empty.
2. ``<PLACEHOLDER>`` strings in ``format.json`` (the literal stub shipped
   in ``format.template.json``) are treated as missing so the loader
   surfaces a clean config error instead of forwarding the stub upstream.

Tests use :func:`pytest.MonkeyPatch.context` to mutate the env per-test
and reset it automatically — no risk of leaking ``EPUB_COMMENTOR_API_KEY``
into the developer's shell.
"""

from __future__ import annotations

import pytest

from epub_commentor.llm._api_key import (
    EPUB_COMMENTOR_API_KEY_ENV_VAR,
    resolve_api_key,
)

# Project env var (mirrors the constant so an accidental rename in
# _api_key.py trips these tests loudly).
_ENV_VAR = EPUB_COMMENTOR_API_KEY_ENV_VAR


class TestEnvVarPrecedence:
    """``$EPUB_COMMENTOR_API_KEY`` shadows the format.json key."""

    def test_env_var_overrides_format_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_ENV_VAR, "sk-env-value")
        assert resolve_api_key("sk-format-value") == "sk-env-value"

    def test_env_var_used_when_format_key_is_placeholder(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A ``<PLACEHOLDER>`` in format.json does not shadow the env var."""
        monkeypatch.setenv(_ENV_VAR, "sk-env-value")
        assert resolve_api_key("<YOUR_API_KEY>") == "sk-env-value"

    def test_env_var_used_when_format_key_is_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(_ENV_VAR, "sk-env-value")
        assert resolve_api_key(None) == "sk-env-value"

    def test_env_var_strips_surrounding_whitespace(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Pasting the key with stray spaces is a common copy-paste slip."""
        monkeypatch.setenv(_ENV_VAR, "  sk-env-value  ")
        assert resolve_api_key("sk-format-value") == "sk-env-value"


class TestFormatKeyFallback:
    """When env var is absent, ``format.json``'s key is used as-is."""

    def test_format_key_used_when_env_var_unset(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(_ENV_VAR, raising=False)
        assert resolve_api_key("sk-format-value") == "sk-format-value"

    def test_format_key_used_when_env_var_is_empty_string(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``export EPUB_COMMENTOR_API_KEY=""`` should NOT shadow the file."""
        monkeypatch.setenv(_ENV_VAR, "")
        assert resolve_api_key("sk-format-value") == "sk-format-value"

    def test_format_key_used_when_env_var_is_whitespace(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Trimmed-empty env var is treated as absent, not as a real key."""
        monkeypatch.setenv(_ENV_VAR, "   ")
        assert resolve_api_key("sk-format-value") == "sk-format-value"

    def test_format_key_surrounding_whitespace_stripped(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(_ENV_VAR, raising=False)
        assert resolve_api_key("  sk-format-value  ") == "sk-format-value"


class TestMissingKey:
    """Both sources absent → ``None`` (caller surfaces config error)."""

    def test_returns_none_when_env_unset_and_format_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(_ENV_VAR, raising=False)
        assert resolve_api_key(None) is None

    def test_returns_none_when_env_unset_and_format_empty_string(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(_ENV_VAR, raising=False)
        assert resolve_api_key("") is None

    def test_returns_none_when_env_unset_and_format_whitespace(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(_ENV_VAR, raising=False)
        assert resolve_api_key("   ") is None


class TestPlaceholderDetection:
    """``<YOUR_API_KEY>``-style placeholders are NOT valid keys."""

    def test_default_placeholder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(_ENV_VAR, raising=False)
        assert resolve_api_key("<YOUR_API_KEY>") is None

    def test_placeholder_with_spaces(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``format.json`` is hand-edited; allow sloppy whitespace."""
        monkeypatch.delenv(_ENV_VAR, raising=False)
        assert resolve_api_key("  <YOUR_API_KEY>  ") is None

    def test_short_angle_bracketed_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Below the 3-char heuristic threshold, treat as placeholder."""
        monkeypatch.delenv(_ENV_VAR, raising=False)
        assert resolve_api_key("<x>") is None

    def test_diamond_brackets_with_real_content_inside(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``<sk-fake>`` is short but doesn't start with ``<`` AND end with ``>``
        *after* the angle-pair heuristic runs.

        Here ``<sk-fake>`` does start with ``<`` and end with ``>`` — so it
        is treated as a placeholder. This guards against users who paste a
        snippet like ``<sk-...redacted...>`` from an upstream error message.
        """
        monkeypatch.delenv(_ENV_VAR, raising=False)
        assert resolve_api_key("<sk-redacted>") is None

    def test_real_key_with_angle_brackets_in_middle_passes(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Keys containing ``<`` / ``>`` in the middle (rare but valid) still pass."""
        monkeypatch.delenv(_ENV_VAR, raising=False)
        assert resolve_api_key("sk-a<b>c-real") == "sk-a<b>c-real"


class TestCustomEnvVarName:
    """``env_var`` parameter overrides the default name (used in tests only)."""

    def test_custom_env_var_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EPub_TEST_OTHER_VAR", "sk-other-value")
        # Production env var unset → should not be read even if its name
        # overlaps the default constant.
        monkeypatch.delenv(_ENV_VAR, raising=False)
        assert resolve_api_key("sk-format-value", env_var="EPub_TEST_OTHER_VAR") == "sk-other-value"

    def test_default_env_var_ignored_when_custom_overridden(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the caller names a different var, the default name is bypassed."""
        monkeypatch.setenv(_ENV_VAR, "sk-env-value")
        # env_var override → only the custom name is consulted.
        assert resolve_api_key("sk-format-value", env_var="EPub_TEST_DOES_NOT_EXIST") == "sk-format-value"


class TestNonStringInputs:
    """``format_key`` is read from a JSON file; protect against odd types."""

    def test_int_format_key_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A user might write ``"key": 12345`` in JSON; that's not a key."""
        monkeypatch.delenv(_ENV_VAR, raising=False)
        assert resolve_api_key(12345) is None  # type: ignore[arg-type]

    def test_list_format_key_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(_ENV_VAR, raising=False)
        assert resolve_api_key(["sk-array"]) is None  # type: ignore[arg-type]

    def test_bool_format_key_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(_ENV_VAR, raising=False)
        assert resolve_api_key(True) is None  # type: ignore[arg-type]


class TestConstantName:
    """Smoke test on the exported constant — pin it against accidental rename."""

    def test_env_var_constant(self) -> None:
        assert EPUB_COMMENTOR_API_KEY_ENV_VAR == "EPUB_COMMENTOR_API_KEY"
