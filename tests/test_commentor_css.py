"""Unit tests for the bundled :file:`commentary.css` asset.

Verifies the stylesheet's contents and the loader used by
:mod:`epub_commentor.pipeline.inject`.
"""

from __future__ import annotations

from importlib import resources

from epub_commentor.pipeline.inject import _load_commentary_css


def _read_data_file() -> str:
    return resources.files("epub_commentor.data").joinpath("commentary.css").read_text(encoding="utf-8")


class TestCommentaryCss:
    def test_three_kind_classes_present(self) -> None:
        css = _read_data_file()
        # Strip /* ... */ comments so the test doesn't accidentally match
        # the property names mentioned in the header.
        rules_only = "\n".join(
            line
            for line in css.splitlines()
            if line.strip() and not line.strip().startswith("/*") and not line.strip().startswith("*")
        )
        assert ".commentary-intro" in rules_only
        assert ".commentary-summary" in rules_only
        assert ".commentary-note" in rules_only

    def test_no_box_shadow_no_background_color(self) -> None:
        css = _read_data_file()
        rules_only = "\n".join(
            line
            for line in css.splitlines()
            if line.strip() and not line.strip().startswith("/*") and not line.strip().startswith("*")
        )
        # E-ink friendliness: no shadow, no background color
        assert "box-shadow" not in rules_only
        assert "background-color" not in rules_only

    def test_shared_base_class_has_break_inside_avoid(self) -> None:
        css = _read_data_file()
        assert ".commentary" in css
        # Look for the base selector and the break rule somewhere below it
        # before any kind-specific override
        base_idx = css.find(".commentary {")
        assert base_idx > -1
        base_block_end = css.find("}", base_idx)
        base_block = css[base_idx:base_block_end]
        assert "break-inside" in base_block or "page-break-inside" in base_block

    def test_loader_returns_bytes(self) -> None:
        loaded = _load_commentary_css()
        assert isinstance(loaded, bytes)
        assert len(loaded) > 0

    def test_loader_matches_data_file(self) -> None:
        loaded = _load_commentary_css()
        on_disk = resources.files("epub_commentor.data").joinpath("commentary.css").read_bytes()
        assert loaded == on_disk
