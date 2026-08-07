"""Tests for heading fragment anchor rewrite functions from 05_postprocess.py."""

import importlib.util
from pathlib import Path
import tempfile
import textwrap

import pytest


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "postprocess",
        Path(__file__).parent.parent / "scripts" / "05_postprocess.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_module()
heading_slug = _mod.heading_slug
build_anchor_slug_map = _mod.build_anchor_slug_map
rewrite_fragment_anchors = _mod.rewrite_fragment_anchors


# ── heading_slug ──────────────────────────────────────────────────────────────

class TestHeadingSlug:
    def test_version_with_dots(self):
        # GFM removes dots rather than converting them to hyphens
        assert heading_slug("Version 6.2.3") == "version-623"

    def test_simple_words(self):
        assert heading_slug("New features") == "new-features"

    def test_mixed_case(self):
        assert heading_slug("Changes in Functionality") == "changes-in-functionality"

    def test_third_party_hyphen(self):
        assert heading_slug("Changes to third-party libraries") == "changes-to-third-party-libraries"

    def test_strips_inline_html(self):
        assert heading_slug("Heading <code>text</code>") == "heading-text"

    def test_parentheses_become_hyphens(self):
        # Parens are non-alphanumeric → collapsed into surrounding hyphens
        result = heading_slug("Connecting to Azure (authentication methods)")
        assert result == "connecting-to-azure-authentication-methods"

    def test_leading_trailing_stripped(self):
        # Edge case: text starting/ending with special chars
        assert heading_slug("(Introduction)") == "introduction"

    def test_single_word(self):
        assert heading_slug("Overview") == "overview"

    def test_already_slug(self):
        assert heading_slug("connecting-to-azure") == "connecting-to-azure"


# ── build_anchor_slug_map ─────────────────────────────────────────────────────

class TestBuildAnchorSlugMap:
    def test_single_file_two_headings(self, tmp_path):
        md = tmp_path / "page.md"
        md.write_text(
            '## Version 6.2.3 <a name="id1"></a>\n\n'
            '### New features <a name="id1s1"></a>\n',
            encoding="utf-8",
        )
        result = build_anchor_slug_map([md], tmp_path)
        norm = "page.md"
        assert norm in result
        assert result[norm]["id1"] == "version-623"   # GFM: dots removed
        assert result[norm]["id1s1"] == "new-features"

    def test_multiple_files(self, tmp_path):
        f1 = tmp_path / "a.md"
        f1.write_text('# Intro <a name="id1"></a>\n', encoding="utf-8")
        f2 = tmp_path / "b.md"
        f2.write_text('# Overview <a name="id1"></a>\n', encoding="utf-8")
        result = build_anchor_slug_map([f1, f2], tmp_path)
        # Same anchor ID in different files maps to different slugs
        assert result["a.md"]["id1"] == "intro"
        assert result["b.md"]["id1"] == "overview"

    def test_file_without_anchors_not_in_map(self, tmp_path):
        md = tmp_path / "plain.md"
        md.write_text("# Just a heading\n\nSome text.\n", encoding="utf-8")
        result = build_anchor_slug_map([md], tmp_path)
        assert "plain.md" not in result

    def test_duplicate_headings_get_suffixed_slugs(self, tmp_path):
        """Same heading text in one file → first gets base slug, subsequent get -1, -2, ..."""
        md = tmp_path / "relnotes.md"
        md.write_text(
            '## Version 6.2.3 <a name="id1"></a>\n'
            '### New features <a name="id1s1"></a>\n'
            '## Version 6.2.2 <a name="id2"></a>\n'
            '### New features <a name="id2s1"></a>\n'
            '## Version 6.2.1 <a name="id3"></a>\n'
            '### New features <a name="id3s1"></a>\n',
            encoding="utf-8",
        )
        result = build_anchor_slug_map([md], tmp_path)
        m = result["relnotes.md"]
        assert m["id1s1"] == "new-features"
        assert m["id2s1"] == "new-features-1"
        assert m["id3s1"] == "new-features-2"
        # Version headings are unique — no suffix needed; GFM removes dots
        assert m["id1"] == "version-623"
        assert m["id2"] == "version-622"
        assert m["id3"] == "version-621"

    def test_named_anchor_in_middle_of_content(self, tmp_path):
        md = tmp_path / "page.md"
        md.write_text(
            "Some intro text.\n\n"
            '# Connecting to Azure <a name="azure_topPage"></a>\n\n'
            "Content here.\n",
            encoding="utf-8",
        )
        result = build_anchor_slug_map([md], tmp_path)
        assert result["page.md"]["azure_topPage"] == "connecting-to-azure"


# ── rewrite_fragment_anchors ──────────────────────────────────────────────────

class TestRewriteFragmentAnchors:
    def _make_env(self, files: dict) -> tuple:
        """Create a temp dir with given {rel_path: content} files.
        Returns (tmp_path, anchor_map).
        """
        tmp = Path(tempfile.mkdtemp())
        paths = []
        for rel, content in files.items():
            p = tmp / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            paths.append(p)
        anchor_map = build_anchor_slug_map(paths, tmp)
        return tmp, anchor_map

    # -- in-page fragments --

    def test_inpage_fragment_rewritten(self):
        tmp, anchor_map = self._make_env({
            "relnotes.md": '## Version 6.2.3 <a name="id1"></a>\n'
        })
        body = "[Version 6.2.3](#id1)"
        result, count = rewrite_fragment_anchors(body, "relnotes.md", anchor_map, tmp)
        assert count == 1
        assert result == "[Version 6.2.3](#version-623)"

    def test_multiple_inpage_fragments(self):
        tmp, anchor_map = self._make_env({
            "relnotes.md": (
                '## Version 6.2.3 <a name="id1"></a>\n'
                '### New features <a name="id1s1"></a>\n'
            )
        })
        body = "[V 6.2.3](#id1) and [features](#id1s1)"
        result, count = rewrite_fragment_anchors(body, "relnotes.md", anchor_map, tmp)
        assert count == 2
        assert "#version-623" in result
        assert "#new-features" in result

    def test_inpage_duplicate_fragments_rewritten_distinctly(self):
        """Duplicate headings: each anchor gets the right suffixed slug."""
        tmp, anchor_map = self._make_env({
            "relnotes.md": (
                '## Version 6.2.3 <a name="id1"></a>\n'
                '### New features <a name="id1s1"></a>\n'
                '## Version 6.2.2 <a name="id2"></a>\n'
                '### New features <a name="id2s1"></a>\n'
                '## Version 6.2.1 <a name="id3"></a>\n'
                '### New features <a name="id3s1"></a>\n'
            )
        })
        body = (
            "- [Version 6.2.3](#id1)\n"
            "  - [New features](#id1s1)\n"
            "- [Version 6.2.2](#id2)\n"
            "  - [New features](#id2s1)\n"
            "- [Version 6.2.1](#id3)\n"
            "  - [New features](#id3s1)\n"
        )
        result, count = rewrite_fragment_anchors(body, "relnotes.md", anchor_map, tmp)
        assert count == 6
        assert "(#version-623)" in result
        assert "(#new-features)" in result
        assert "(#version-622)" in result
        assert "(#new-features-1)" in result
        assert "(#version-621)" in result
        assert "(#new-features-2)" in result

    def test_unknown_inpage_anchor_unchanged(self):
        tmp, anchor_map = self._make_env({
            "page.md": '## Known <a name="known"></a>\n'
        })
        body = "[link](#unknown_anchor)"
        result, count = rewrite_fragment_anchors(body, "page.md", anchor_map, tmp)
        assert count == 0
        assert result == body

    # -- cross-file Markdown fragments --

    def test_crossfile_md_fragment_rewritten(self):
        tmp, anchor_map = self._make_env({
            "release_note/relnotes.md": "content\n",
            "user_guide/user_azure.md": '# Connecting to Azure <a name="azure_topPage"></a>\n',
        })
        body = "[Azure](../user_guide/user_azure.md#azure_topPage)"
        result, count = rewrite_fragment_anchors(
            body, "release_note/relnotes.md", anchor_map, tmp
        )
        assert count == 1
        assert result == "[Azure](../user_guide/user_azure.md#connecting-to-azure)"

    def test_crossfile_unknown_anchor_unchanged(self):
        tmp, anchor_map = self._make_env({
            "a.md": "content\n",
            "b.md": '# Section <a name="known"></a>\n',
        })
        body = "[link](b.md#no_such_id)"
        result, count = rewrite_fragment_anchors(body, "a.md", anchor_map, tmp)
        assert count == 0
        assert result == body

    # -- HTML href fragments --

    def test_html_href_fragment_rewritten(self):
        tmp, anchor_map = self._make_env({
            "release_note/relnotes.md": "content\n",
            "user_guide/user_azure.md": '# Connecting to Azure <a name="azure_topPage"></a>\n',
        })
        body = '<a href="../user_guide/user_azure.md#azure_topPage">text</a>'
        result, count = rewrite_fragment_anchors(
            body, "release_note/relnotes.md", anchor_map, tmp
        )
        assert count == 1
        assert 'href="../user_guide/user_azure.md#connecting-to-azure"' in result

    def test_html_href_unknown_fragment_unchanged(self):
        tmp, anchor_map = self._make_env({
            "a.md": "content\n",
            "b.md": '# Section <a name="real_id"></a>\n',
        })
        body = '<a href="b.md#ghost_id">text</a>'
        result, count = rewrite_fragment_anchors(body, "a.md", anchor_map, tmp)
        assert count == 0
        assert result == body

    # -- no-op cases --

    def test_no_fragments_returns_zero(self):
        tmp, anchor_map = self._make_env({
            "page.md": '## Heading <a name="h1"></a>\n'
        })
        body = "# Heading\n\nSome text with [a link](other.md) and no fragment.\n"
        result, count = rewrite_fragment_anchors(body, "page.md", anchor_map, tmp)
        assert count == 0
        assert result == body

    def test_markdown_blockquote_lines_untouched(self):
        tmp, anchor_map = self._make_env({
            "page.md": '## Heading <a name="h1"></a>\n'
        })
        body = "> **Note:** some text\n> more text\n"
        result, count = rewrite_fragment_anchors(body, "page.md", anchor_map, tmp)
        assert count == 0
        assert result == body

    # -- idempotency --

    def test_idempotent(self):
        tmp, anchor_map = self._make_env({
            "relnotes.md": '## Version 6.2.3 <a name="id1"></a>\n'
        })
        body = "[Version 6.2.3](#id1)"
        result1, count1 = rewrite_fragment_anchors(body, "relnotes.md", anchor_map, tmp)
        result2, count2 = rewrite_fragment_anchors(result1, "relnotes.md", anchor_map, tmp)
        assert count1 == 1
        assert count2 == 0
        assert result1 == result2
