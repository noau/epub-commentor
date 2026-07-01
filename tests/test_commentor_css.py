"""Unit tests for the bundled :file:`commentary.css` asset.

Verifies the stylesheet's contents and the loader used by
:mod:`epub_commentor.pipeline.inject`.
"""

from __future__ import annotations

from importlib import resources

from epub_commentor.pipeline.inject import _load_commentary_css


def _read_data_file() -> str:
    return resources.files("epub_commentor.data").joinpath("commentary.css").read_text(encoding="utf-8")


def _selector_block(css: str, selector: str) -> str:
    """Return the body of the first ``selector { ... }`` block, or fail loudly."""
    idx = css.find(selector + " {")
    assert idx > -1, f"missing {selector} block in commentary.css"
    end = css.find("}", idx)
    return css[idx:end]


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

    def test_no_italic_in_note(self) -> None:
        """Part IX #6 — .commentary-note must not be italic.

        Italic slants are harder to sustain on e-ink displays, and the
        companion-reader aesthetic deliberately keeps notes upright.
        """
        css = _read_data_file()
        block = _selector_block(css, ".commentary-note")
        assert "font-style" not in block, f"italic must be removed by Part IX #6; got: {block!r}"

    def test_kind_left_border_hierarchy(self) -> None:
        """Companion visual hierarchy, carried by line style rather than width.

        All three kinds share the same 5px left-rule width so the rules align
        vertically on the page (mixing 2/3/5px previously caused the rules to
        land at different x-offsets). The hierarchy is now:
          intro   = dashed  (lightest marker)
          note    = double  (mid-weight pencil mark)
          summary = solid   (heaviest recap)
        """
        import re

        css = _read_data_file()

        def first_border_left(selector: str) -> tuple[int, str]:
            block = _selector_block(css, selector)
            # border-left shorthand: <width> <style> <color>
            m = re.search(
                r"border-left[^:]*:\s*\D*?(\d+)px\s+(\w+)", block
            )
            assert m, f"no border-left shorthand in {selector} block: {block!r}"
            return int(m.group(1)), m.group(2)

        intro_px, intro_style = first_border_left(".commentary-intro")
        note_px, note_style = first_border_left(".commentary-note")
        summary_px, summary_style = first_border_left(".commentary-summary")

        # Width invariant: all three are 5px so the rules align vertically.
        assert intro_px == note_px == summary_px == 5, (
            f"all three kinds must share the same 5px left-rule width "
            f"for vertical alignment; got intro={intro_px}px, "
            f"note={note_px}px, summary={summary_px}px"
        )

        # Line-style invariant: each kind uses a distinct style so the
        # hierarchy is still readable when widths are equal.
        styles = {intro_style, note_style, summary_style}
        assert len(styles) == 3, (
            f"the three kinds must use three distinct line styles to carry "
            f"the visual hierarchy; got intro={intro_style!r}, "
            f"note={note_style!r}, summary={summary_style!r}"
        )
