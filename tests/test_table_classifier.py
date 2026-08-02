"""
Tests for scripts/lib/table_classifier.py.

Covers tier classification, header promotion, and the known recursive-find bug
where table.find("tr") descends into nested tables.
"""

import pytest
from bs4 import BeautifulSoup

from scripts.lib.table_classifier import (
    DEFAULT_BLOCK_TAGS,
    _cell_tier,
    _promote_first_row_as_header,
    classify_table,
    handle_tables,
)


def _table(html: str) -> BeautifulSoup:
    """Parse table HTML and return the <table> Tag."""
    soup = BeautifulSoup(html, "lxml")
    return soup.find("table")


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


# ── _cell_tier ────────────────────────────────────────────────────────────────

class TestCellTier:
    def test_plain_text_is_tier1(self):
        soup = BeautifulSoup("<td>plain text</td>", "lxml")
        cell = soup.find("td")
        assert _cell_tier(cell, DEFAULT_BLOCK_TAGS) == 1

    def test_inline_tags_are_tier2(self):
        for tag in ("strong", "em", "code", "a"):
            soup = BeautifulSoup(f"<td><{tag}>text</{tag}></td>", "lxml")
            cell = soup.find("td")
            assert _cell_tier(cell, DEFAULT_BLOCK_TAGS) == 2, f"<{tag}> should be tier 2"

    def test_block_list_is_tier3(self):
        soup = BeautifulSoup("<td><ul><li>item</li></ul></td>", "lxml")
        cell = soup.find("td")
        assert _cell_tier(cell, DEFAULT_BLOCK_TAGS) == 3

    def test_pre_is_tier3(self):
        soup = BeautifulSoup("<td><pre>code block</pre></td>", "lxml")
        cell = soup.find("td")
        assert _cell_tier(cell, DEFAULT_BLOCK_TAGS) == 3

    def test_nested_table_is_tier3(self):
        soup = BeautifulSoup(
            "<td><table><tr><td>inner</td></tr></table></td>", "lxml"
        )
        cell = soup.find("td")
        assert _cell_tier(cell, DEFAULT_BLOCK_TAGS) == 3

    def test_custom_block_tags_override(self):
        """When block_tags is empty, even <ul> does not trigger tier 3."""
        soup = BeautifulSoup("<td><ul><li>item</li></ul></td>", "lxml")
        cell = soup.find("td")
        assert _cell_tier(cell, set()) == 2


# ── classify_table ────────────────────────────────────────────────────────────

class TestClassifyTable:
    def test_all_text_cells_is_tier1(self):
        table = _table(
            "<table><tr><td>A</td><td>B</td></tr></table>"
        )
        assert classify_table(table) == 1

    def test_inline_html_cells_is_tier2(self):
        table = _table(
            "<table><tr><td><strong>bold</strong></td><td>text</td></tr></table>"
        )
        assert classify_table(table) == 2

    def test_block_content_cell_is_tier3(self):
        table = _table(
            "<table><tr><td><ul><li>x</li></ul></td></tr></table>"
        )
        assert classify_table(table) == 3

    def test_worst_case_cell_determines_table_tier(self):
        """One tier-3 cell in a mostly-tier-1 table → table is tier 3."""
        table = _table(
            "<table>"
            "<tr><td>plain</td><td>text</td></tr>"
            "<tr><td><pre>code</pre></td><td>more</td></tr>"
            "</table>"
        )
        assert classify_table(table) == 3

    def test_ebx_definition_list_is_always_tier3(self):
        table = _table(
            '<table class="ebx_definitionList">'
            "<tr><td>Term</td><td>Definition</td></tr>"
            "</table>"
        )
        assert classify_table(table) == 3


# ── _promote_first_row_as_header ──────────────────────────────────────────────

class TestPromoteFirstRowAsHeader:
    def test_no_thead_promotes_first_tr_to_thead(self):
        table = _table(
            "<table>"
            "<tr><td>Col A</td><td>Col B</td></tr>"
            "<tr><td>data1</td><td>data2</td></tr>"
            "</table>"
        )
        result = _promote_first_row_as_header(table)
        assert result is True
        thead = table.find("thead")
        assert thead is not None
        assert thead.find("th") is not None
        assert thead.find("td") is None

    def test_existing_thead_with_td_converts_to_th(self):
        table = _table(
            "<table>"
            "<thead><tr><td>H1</td><td>H2</td></tr></thead>"
            "<tbody><tr><td>d1</td><td>d2</td></tr></tbody>"
            "</table>"
        )
        result = _promote_first_row_as_header(table)
        assert result is True
        thead = table.find("thead")
        assert all(c.name == "th" for c in thead.find_all(["td", "th"]))

    def test_existing_thead_with_th_is_unchanged(self):
        table = _table(
            "<table>"
            "<thead><tr><th>H1</th><th>H2</th></tr></thead>"
            "<tbody><tr><td>d1</td><td>d2</td></tr></tbody>"
            "</table>"
        )
        result = _promote_first_row_as_header(table)
        assert result is False

    def test_nested_table_bug_find_tr_descends_into_nested_table(self):
        """
        Documents the known bug: table.find("tr") without recursive=False
        returns the nested table's <tr> instead of the outer table's first row.

        The outer table has no <thead>; its only direct row contains a nested table.
        _promote_first_row_as_header should promote the outer row, but instead
        promotes the inner row because find("tr") is recursive.

        This test will FAIL when the bug is fixed — update it to assert the
        correct behaviour (outer row promoted) at that point.
        """
        table = _table(
            "<table>"
            "<tr>"
            "  <td>"
            "    <table><tr><td>inner</td></tr></table>"
            "  </td>"
            "</tr>"
            "<tr><td>outer data</td></tr>"
            "</table>"
        )
        _promote_first_row_as_header(table)
        # With the bug present: the inner <tr> is what find("tr") returns,
        # so its <td> gets converted to <th> and it ends up in a new <thead>.
        # The correct fix would promote the outer table's first <tr> instead.
        thead = table.find("thead")
        assert thead is not None
        # Bug: the promoted row is the inner one (contains "inner")
        assert "inner" in thead.get_text()
        # Correct behaviour after fix: promoted row should contain the outer td


# ── handle_tables ─────────────────────────────────────────────────────────────

class TestHandleTables:
    def test_tier1_table_counts_correctly(self):
        soup = _soup(
            "<div><table><tr><td>A</td><td>B</td></tr></table></div>"
        )
        counts = handle_tables(soup)
        assert counts["tier1"] == 1
        assert counts["tier2"] == 0
        assert counts["tier3"] == 0

    def test_tier3_table_gets_passthrough_attribute(self):
        soup = _soup(
            "<div><table><tr><td><ul><li>x</li></ul></td></tr></table></div>"
        )
        handle_tables(soup)
        table = soup.find("table")
        assert table.get("data-converter-passthrough") == "true"

    def test_tier1_and_tier2_tables_get_no_passthrough_attribute(self):
        soup = _soup(
            "<div>"
            "<table><tr><td>plain</td></tr></table>"
            "<table><tr><td><strong>bold</strong></td></tr></table>"
            "</div>"
        )
        handle_tables(soup)
        for table in soup.find_all("table"):
            assert table.get("data-converter-passthrough") is None

    def test_nested_table_inside_passthrough_is_skipped(self):
        """Tables nested inside a tier-3 table are not classified separately."""
        soup = _soup(
            "<div>"
            "<table>"
            "  <tr><td>"
            "    <ul><li>"
            "      <table><tr><td>inner</td></tr></table>"
            "    </li></ul>"
            "  </td></tr>"
            "</table>"
            "</div>"
        )
        counts = handle_tables(soup)
        assert counts["tier3"] == 1
        assert counts["tier1"] == 0  # inner table skipped

    def test_multiple_tables_counted_separately(self):
        soup = _soup(
            "<div>"
            "<table><tr><td>plain</td></tr></table>"
            "<table><tr><td><pre>code</pre></td></tr></table>"
            "<table><tr><td><em>italic</em></td></tr></table>"
            "</div>"
        )
        counts = handle_tables(soup)
        assert counts["tier1"] == 1
        assert counts["tier2"] == 1
        assert counts["tier3"] == 1

    def test_tier1_and_tier2_tables_get_header_promoted(self):
        """Tier 1/2 tables without a <thead> should have one added."""
        soup = _soup(
            "<div><table><tr><td>H</td></tr><tr><td>D</td></tr></table></div>"
        )
        handle_tables(soup)
        table = soup.find("table")
        assert table.find("thead") is not None

    def test_tier3_table_does_not_get_header_promoted(self):
        """Tier 3 tables are left untouched — no header promotion."""
        soup = _soup(
            "<div><table>"
            "<tr><td><ul><li>x</li></ul></td></tr>"
            "<tr><td>data</td></tr>"
            "</table></div>"
        )
        handle_tables(soup)
        table = soup.find("table")
        assert table.find("thead") is None
