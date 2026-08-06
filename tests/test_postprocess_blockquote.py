"""Tests for rewrite_blockquotes_in_tables() from 05_postprocess.py."""

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_rewrite_fn():
    """Load rewrite_blockquotes_in_tables from 05_postprocess.py via importlib."""
    spec = importlib.util.spec_from_file_location(
        "postprocess",
        Path(__file__).parent.parent / "scripts" / "05_postprocess.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.rewrite_blockquotes_in_tables


rewrite_blockquotes_in_tables = _load_rewrite_fn()


# ---------------------------------------------------------------------------
# Basic replacement
# ---------------------------------------------------------------------------

class TestRewriteBlockquotesInTables:

    def test_single_blockquote_replaced(self):
        body = '<table><td><blockquote><p><strong>Note:</strong></p><p>Text.</p></blockquote></td></table>'
        result, count = rewrite_blockquotes_in_tables(body)
        assert count == 1
        assert '<div class="note-inline">' in result
        assert "<blockquote>" not in result
        assert "</blockquote>" not in result

    def test_multiple_blockquotes_all_replaced(self):
        body = (
            '<td><blockquote><p><strong>Note:</strong></p><p>First.</p></blockquote></td>'
            '<td><blockquote><p><strong>Note:</strong></p><p>Second.</p></blockquote></td>'
        )
        result, count = rewrite_blockquotes_in_tables(body)
        assert count == 2
        assert result.count('<div class="note-inline">') == 2
        assert "<blockquote>" not in result
        assert "</blockquote>" not in result

    def test_inner_content_is_preserved(self):
        inner = "<p><strong>Note:</strong></p>  <p>Body text here.</p>"
        body = f"<blockquote>{inner}</blockquote>"
        result, count = rewrite_blockquotes_in_tables(body)
        assert count == 1
        assert inner in result

    def test_inner_list_is_preserved(self):
        body = "<blockquote><p><strong>Note:</strong></p><ul><li>A</li><li>B</li></ul></blockquote>"
        result, count = rewrite_blockquotes_in_tables(body)
        assert count == 1
        assert "<ul><li>A</li><li>B</li></ul>" in result
        assert "<blockquote>" not in result

    def test_inner_div_is_preserved(self):
        body = '<blockquote><p><strong>Note:</strong></p><div class="inner">content</div></blockquote>'
        result, count = rewrite_blockquotes_in_tables(body)
        assert count == 1
        assert '<div class="inner">content</div>' in result
        # Two divs: note-inline wrapper + inner div
        assert result.count("<div") == 2
        assert result.count("</div>") == 2
        assert "<blockquote>" not in result
        assert "</blockquote>" not in result

    # ---------------------------------------------------------------------------
    # No-op cases
    # ---------------------------------------------------------------------------

    def test_markdown_blockquote_unchanged(self):
        body = "> **Note:** This is a Markdown blockquote.\n> More text.\n"
        result, count = rewrite_blockquotes_in_tables(body)
        assert count == 0
        assert result == body

    def test_no_blockquotes_returns_zero_and_identical_body(self):
        body = "# Heading\n\nJust a paragraph.\n"
        result, count = rewrite_blockquotes_in_tables(body)
        assert count == 0
        assert result == body

    def test_empty_body(self):
        result, count = rewrite_blockquotes_in_tables("")
        assert count == 0
        assert result == ""

    # ---------------------------------------------------------------------------
    # Tag boundary correctness
    # ---------------------------------------------------------------------------

    def test_closing_tag_replaced(self):
        body = "<blockquote><p>text</p></blockquote>"
        result, _ = rewrite_blockquotes_in_tables(body)
        assert "</blockquote>" not in result
        assert result.endswith("</div>")

    def test_opening_tag_replaced(self):
        body = "<blockquote><p>text</p></blockquote>"
        result, _ = rewrite_blockquotes_in_tables(body)
        assert "<blockquote>" not in result
        assert result.startswith('<div class="note-inline">')

    # ---------------------------------------------------------------------------
    # Realistic EBX output pattern
    # ---------------------------------------------------------------------------

    def test_realistic_ebx_note_inside_table_cell(self):
        body = (
            '<table class="ebx_definitionList">'
            " <tr> <td><p><strong>Import mode:</strong></p>"
            "<blockquote><p><strong>Note:</strong></p>"
            "  <p>Depending on resources, you might have issues.</p>"
            "</blockquote>"
            " <p>Other text.</p></td></tr> </table>"
        )
        result, count = rewrite_blockquotes_in_tables(body)
        assert count == 1
        assert '<div class="note-inline">' in result
        assert "Depending on resources" in result
        assert "<blockquote>" not in result
        assert "</blockquote>" not in result

    # ---------------------------------------------------------------------------
    # Idempotency
    # ---------------------------------------------------------------------------

    def test_idempotent_second_run_no_change(self):
        body = "<blockquote><p>text</p></blockquote>"
        result1, count1 = rewrite_blockquotes_in_tables(body)
        result2, count2 = rewrite_blockquotes_in_tables(result1)
        assert count1 == 1
        assert count2 == 0
        assert result1 == result2
