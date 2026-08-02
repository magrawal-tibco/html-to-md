"""
Tests for scripts/lib/preprocessor.py transforms.

Each test parses a minimal HTML fragment into the div[role="main"] element
that the preprocessor operates on, runs the specific transform, and asserts
on the resulting HTML or text structure.
"""

import pytest
from bs4 import BeautifulSoup

from scripts.lib.preprocessor import (
    anchor_only_links,
    callout_divs,
    code_urls_to_links,
    ebx_callout_divs,
    fake_list_tables,
    inline_spans,
    rewrite_image_src,
    strip_chrome,
    _table_column_count,
)


def _parse(html: str) -> BeautifulSoup:
    """Wrap bare HTML in a div[role=main] and return that Tag."""
    soup = BeautifulSoup(
        f'<div role="main" id="mc-main-content">{html}</div>', "lxml"
    )
    return soup.find("div", {"role": "main"})


# ── strip_chrome ──────────────────────────────────────────────────────────────

class TestStripChrome:
    def test_removes_matching_selector(self):
        content = _parse('<p class="MCWebHelpFramesetLink">click</p><p>keep</p>')
        n = strip_chrome(content, ["p.MCWebHelpFramesetLink"])
        assert n == 1
        assert content.find("p", class_="MCWebHelpFramesetLink") is None
        assert content.find("p") is not None  # "keep" paragraph survives

    def test_removes_script_and_style_unconditionally(self):
        content = _parse("<script>alert(1)</script><style>body{}</style><p>text</p>")
        n = strip_chrome(content, [])
        assert n == 2
        assert content.find("script") is None
        assert content.find("style") is None

    def test_removes_autonumber_spans(self):
        content = _parse('<p><span class="autonumber">1.</span> Item</p>')
        n = strip_chrome(content, [])
        assert n == 1
        assert content.find("span", class_="autonumber") is None

    def test_returns_zero_when_nothing_matches(self):
        content = _parse("<p>No chrome here</p>")
        assert strip_chrome(content, ["div.toolbar"]) == 0


# ── fake_list_tables ──────────────────────────────────────────────────────────

class TestFakeListTables:
    def test_bullet_table_becomes_ul(self):
        content = _parse(
            '<table class="AutoNumber_p_Bullet">'
            "<tr><td>Item 1</td></tr>"
            "<tr><td>Item 2</td></tr>"
            "</table>"
        )
        n = fake_list_tables(content)
        assert n == 1
        ul = content.find("ul")
        assert ul is not None
        assert content.find("table") is None
        items = ul.find_all("li")
        assert len(items) == 2
        assert items[0].get_text(strip=True) == "Item 1"

    def test_number_table_becomes_ol(self):
        content = _parse(
            '<table class="AutoNumber_p_Number">'
            "<tr><td>Step 1</td></tr>"
            "<tr><td>Step 2</td></tr>"
            "</table>"
        )
        fake_list_tables(content)
        assert content.find("ol") is not None
        assert content.find("ul") is None

    def test_data_mc_autonum_digit_tiebreaker_makes_ol(self):
        """A Bullet-class table whose data-mc-autonum value starts with a digit → ol."""
        content = _parse(
            '<table class="AutoNumber_p_Bullet">'
            '<tr><td data-mc-autonum="1.">Step 1</td></tr>'
            "</table>"
        )
        fake_list_tables(content)
        assert content.find("ol") is not None

    def test_non_autonum_table_not_converted(self):
        content = _parse(
            '<table class="plain"><tr><td>A</td></tr></table>'
        )
        n = fake_list_tables(content)
        assert n == 0
        assert content.find("table") is not None

    def test_two_column_table_uses_last_cell(self):
        """MadCap emits number + content as two <td>s; content is always the last cell."""
        content = _parse(
            '<table class="AutoNumber_p_Bullet">'
            "<tr><td>(bullet)</td><td>Item text</td></tr>"
            "</table>"
        )
        fake_list_tables(content)
        li = content.find("li")
        assert li.get_text(strip=True) == "Item text"


# ── callout_divs ──────────────────────────────────────────────────────────────

class TestCalloutDivs:
    @pytest.mark.parametrize("css_class,expected_label", [
        ("note", "Note"),
        ("warning", "Warning"),
        ("caution", "Caution"),
        ("tip", "Tip"),
        ("important", "Important"),
        ("noteNote", "Note"),
        ("noteWarning", "Warning"),
    ])
    def test_callout_class_becomes_blockquote(self, css_class, expected_label):
        content = _parse(f'<div class="{css_class}"><p>Body text.</p></div>')
        n = callout_divs(content)
        assert n == 1
        bq = content.find("blockquote")
        assert bq is not None
        assert content.find("div", class_=css_class) is None
        strong = bq.find("strong")
        assert strong is not None
        assert expected_label in strong.get_text()

    def test_callout_body_content_is_preserved(self):
        content = _parse('<div class="note"><p>Important info.</p></div>')
        callout_divs(content)
        bq = content.find("blockquote")
        assert "Important info." in bq.get_text()

    def test_unknown_div_class_not_converted(self):
        content = _parse('<div class="custom-box"><p>text</p></div>')
        n = callout_divs(content)
        assert n == 0
        assert content.find("blockquote") is None


# ── ebx_callout_divs ──────────────────────────────────────────────────────────

class TestEbxCalloutDivs:
    def test_ebx_note_div_becomes_blockquote(self):
        content = _parse(
            '<div class="ebx_note"><h5>Note</h5><p>See this.</p></div>'
        )
        n = ebx_callout_divs(content)
        assert n == 1
        bq = content.find("blockquote")
        assert bq is not None
        assert bq.find("h5") is None  # h5 is decomposed
        assert "Note" in bq.find("strong").get_text()
        assert "See this." in bq.get_text()

    def test_ebx_seealso_uses_h5_label(self):
        content = _parse(
            '<div class="ebx_seealso"><h5>Voir aussi</h5><p>Related page.</p></div>'
        )
        ebx_callout_divs(content)
        strong = content.find("strong")
        assert "Voir aussi" in strong.get_text()

    def test_ebx_callout_without_h5_falls_back_to_class_name(self):
        content = _parse('<div class="ebx_attention"><p>Watch out.</p></div>')
        ebx_callout_divs(content)
        strong = content.find("strong")
        assert "Attention" in strong.get_text()


# ── inline_spans ──────────────────────────────────────────────────────────────

class TestInlineSpans:
    @pytest.mark.parametrize("css_class,expected_tag", [
        ("uicontrol", "strong"),
        ("wintitle", "strong"),
        ("option", "strong"),
        ("filepath", "code"),
        ("codeph", "code"),
        ("varname", "em"),
        ("parmname", "em"),
        ("term", "em"),
    ])
    def test_span_class_maps_to_html_tag(self, css_class, expected_tag):
        content = _parse(f'<p><span class="{css_class}">value</span></p>')
        inline_spans(content)
        assert content.find(expected_tag) is not None
        assert content.find("span", class_=css_class) is None

    def test_menucascade_collapses_to_strong(self):
        content = _parse(
            '<span class="menucascade">'
            "<span>File</span> &gt; <span>Open</span>"
            "</span>"
        )
        inline_spans(content)
        strong = content.find("strong")
        assert strong is not None
        text = strong.get_text(separator=" ", strip=True)
        assert "File" in text
        assert "Open" in text

    def test_var_element_becomes_em(self):
        content = _parse("<p>Set <var>hostname</var> to the server address.</p>")
        inline_spans(content)
        assert content.find("em") is not None
        assert content.find("var") is None

    def test_span_without_known_class_is_left_alone(self):
        content = _parse('<span class="custom-style">text</span>')
        inline_spans(content)
        assert content.find("span", class_="custom-style") is not None


# ── anchor_only_links ─────────────────────────────────────────────────────────

class TestAnchorOnlyLinks:
    def test_name_anchor_is_unwrapped(self):
        content = _parse('<a name="section-1">target</a>')
        n = anchor_only_links(content)
        assert n == 1
        assert content.find("a") is None
        assert "target" in content.get_text()

    def test_id_anchor_is_unwrapped(self):
        content = _parse('<a id="top"></a>')
        n = anchor_only_links(content)
        assert n == 1
        assert content.find("a") is None

    def test_href_link_is_preserved(self):
        content = _parse('<a href="page.htm">Link</a>')
        n = anchor_only_links(content)
        assert n == 0
        assert content.find("a") is not None

    def test_href_with_name_is_preserved(self):
        content = _parse('<a href="page.htm" name="sec">Link</a>')
        n = anchor_only_links(content)
        assert n == 0
        assert content.find("a") is not None


# ── code_urls_to_links ────────────────────────────────────────────────────────

class TestCodeUrlsToLinks:
    def test_bare_https_url_in_code_becomes_link(self):
        content = _parse("<code>https://example.com/path</code>")
        n = code_urls_to_links(content)
        assert n == 1
        a = content.find("a")
        assert a is not None
        assert a["href"] == "https://example.com/path"
        assert content.find("code") is None

    def test_bare_http_url_in_code_becomes_link(self):
        content = _parse("<code>http://docs.example.org</code>")
        n = code_urls_to_links(content)
        assert n == 1

    def test_code_with_non_url_is_not_converted(self):
        content = _parse("<code>some-command --flag</code>")
        n = code_urls_to_links(content)
        assert n == 0
        assert content.find("code") is not None

    def test_bare_scheme_only_is_not_converted(self):
        """http:// with no host should not become a link."""
        content = _parse("<code>http://</code>")
        n = code_urls_to_links(content)
        assert n == 0


# ── _table_column_count (colspan crash bug) ───────────────────────────────────

class TestTableColumnCount:
    def test_counts_simple_header_row(self):
        content = _parse(
            "<table><thead><tr><th>A</th><th>B</th><th>C</th></tr></thead></table>"
        )
        table = content.find("table")
        assert _table_column_count(table) == 3

    def test_counts_colspan_in_header(self):
        content = _parse(
            '<table><thead><tr><th colspan="2">AB</th><th>C</th></tr></thead></table>'
        )
        table = content.find("table")
        assert _table_column_count(table) == 3

    def test_non_integer_colspan_defaults_to_one(self):
        """Non-integer colspan values (e.g. 'auto') are treated as 1 rather than crashing."""
        content = _parse(
            '<table><thead><tr><th colspan="auto">A</th></tr></thead></table>'
        )
        table = content.find("table")
        assert _table_column_count(table) == 1

    def test_falls_back_to_first_body_row_without_thead(self):
        content = _parse(
            "<table><tbody>"
            "<tr><td>A</td><td>B</td></tr>"
            "</tbody></table>"
        )
        table = content.find("table")
        assert _table_column_count(table) == 2

    def test_empty_table_returns_zero(self):
        content = _parse("<table></table>")
        table = content.find("table")
        assert _table_column_count(table) == 0


# ── rewrite_image_src ─────────────────────────────────────────────────────────

class TestRewriteImageSrc:
    def test_relative_src_is_preserved(self):
        """Relative src paths are left unchanged — output mirrors URL structure."""
        content = _parse('<img src="../images/figure1.png"/>')
        img = content.find("img")
        result = rewrite_image_src(content, "/pub/product/1.0/doc/html/page/topic.htm")
        assert result == 0
        assert img["src"] == "../images/figure1.png"

    def test_absolute_urls_are_skipped(self):
        content = _parse('<img src="https://example.com/logo.png"/>')
        result = rewrite_image_src(content, "/pub/product/1.0/doc/html/page/topic.htm")
        assert result == 0
        assert content.find("img")["src"] == "https://example.com/logo.png"

    def test_empty_src_is_skipped(self):
        content = _parse('<img src=""/>')
        result = rewrite_image_src(content, "/pub/product/1.0/doc/html/page/topic.htm")
        assert result == 0
        assert content.find("img")["src"] == ""
