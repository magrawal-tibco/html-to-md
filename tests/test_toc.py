"""
Tests for scripts/06_build_toc.py TOC reconstruction logic.

Covers:
  - insert_into_tree: normal insertion, deep nesting, leaf-file assignment,
    title collision warning
  - version_html_root: path depth extraction for MadCap and EBX variants
  - dir_fallback calculation: the [:-1] slice that drops the leaf segment
"""

import importlib.util
import warnings
from pathlib import Path

import pytest
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))


def _load_toc_module():
    spec = importlib.util.spec_from_file_location(
        "toc_06",
        Path(__file__).parent.parent / "scripts" / "06_build_toc.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_toc = _load_toc_module()
insert_into_tree = _toc.insert_into_tree
version_html_root = _toc.version_html_root


# ── helpers ───────────────────────────────────────────────────────────────────

def _empty_tree() -> dict:
    return {"title": "root", "file": None, "children": []}


def _entry(path: str) -> dict:
    return {"output_path": path}


# ── insert_into_tree ──────────────────────────────────────────────────────────

class TestInsertIntoTree:
    def test_single_segment_creates_leaf(self):
        tree = _empty_tree()
        insert_into_tree(tree, ["Introduction"], _entry("intro.md"))
        assert len(tree["children"]) == 1
        child = tree["children"][0]
        assert child["title"] == "Introduction"
        assert child["file"] == "intro.md"
        assert child["children"] == []

    def test_two_segments_creates_nested_leaf(self):
        tree = _empty_tree()
        insert_into_tree(tree, ["Guide", "Overview"], _entry("guide/overview.md"))
        assert len(tree["children"]) == 1
        guide = tree["children"][0]
        assert guide["title"] == "Guide"
        assert guide["file"] is None
        assert len(guide["children"]) == 1
        overview = guide["children"][0]
        assert overview["title"] == "Overview"
        assert overview["file"] == "guide/overview.md"

    def test_shared_parent_reuses_node(self):
        tree = _empty_tree()
        insert_into_tree(tree, ["Guide", "Overview"], _entry("guide/overview.md"))
        insert_into_tree(tree, ["Guide", "Details"], _entry("guide/details.md"))
        assert len(tree["children"]) == 1, "Guide node should not be duplicated"
        guide = tree["children"][0]
        assert len(guide["children"]) == 2

    def test_multiple_top_level_sections(self):
        tree = _empty_tree()
        insert_into_tree(tree, ["Install"], _entry("install.md"))
        insert_into_tree(tree, ["Config"], _entry("config.md"))
        titles = [c["title"] for c in tree["children"]]
        assert titles == ["Install", "Config"]

    def test_empty_segments_does_nothing(self):
        tree = _empty_tree()
        insert_into_tree(tree, [], _entry("page.md"))
        assert tree["children"] == []

    def test_title_collision_emits_warning(self):
        tree = _empty_tree()
        insert_into_tree(tree, ["Page"], _entry("page_v1.md"))
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            insert_into_tree(tree, ["Page"], _entry("page_v2.md"))
        assert len(caught) == 1
        msg = str(caught[0].message).lower()
        assert "collision" in msg or "overwritten" in msg
        # Second entry wins
        assert tree["children"][0]["file"] == "page_v2.md"

    def test_no_warning_when_same_file_reinserted(self):
        """Reinserting the same file at the same path must not warn."""
        tree = _empty_tree()
        insert_into_tree(tree, ["Page"], _entry("page.md"))
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            insert_into_tree(tree, ["Page"], _entry("page.md"))
        assert len(caught) == 0

    def test_deep_nesting(self):
        tree = _empty_tree()
        segs = ["A", "B", "C", "D", "Leaf"]
        insert_into_tree(tree, segs, _entry("deep/leaf.md"))
        node = tree
        for title in segs:
            node = next(c for c in node["children"] if c["title"] == title)
        assert node["file"] == "deep/leaf.md"

    def test_insertion_order_preserved(self):
        """Children should appear in insertion order."""
        tree = _empty_tree()
        pages = ["alpha.md", "beta.md", "gamma.md"]
        for p in pages:
            insert_into_tree(tree, [p], _entry(p))
        assert [c["title"] for c in tree["children"]] == pages


# ── version_html_root ─────────────────────────────────────────────────────────

class TestVersionHtmlRoot:
    def test_madcap_path(self):
        path = "pub/businessevents/6.4.0/doc/html/Admin/file.md"
        assert version_html_root(path) == "pub/businessevents/6.4.0/doc/html/"

    def test_ebx_main_path_includes_lang_segment(self):
        path = "pub/ebx/6.2.3/doc/html/en/admin/file.md"
        assert version_html_root(path, version_format="ebx") == "pub/ebx/6.2.3/doc/html/en/"

    def test_ebx_main_path_without_format_flag_stops_at_html(self):
        path = "pub/ebx/6.2.3/doc/html/en/admin/file.md"
        assert version_html_root(path) == "pub/ebx/6.2.3/doc/html/"

    def test_backslash_normalised(self):
        path = "pub\\foo\\1.0\\doc\\html\\Admin\\file.md"
        root = version_html_root(path)
        assert "\\" not in root
        assert root.endswith("/")


# ── dir_fallback calculation ──────────────────────────────────────────────────

class TestDirFallback:
    """
    The dir_fallback dict maps each directory to the toc_path segments of the
    majority page in that directory, with the *last* segment dropped (because
    the page title is appended as the final leaf by build_version_toc).

    These tests validate the Counter + [:-1] slice logic in isolation.
    """

    @staticmethod
    def _compute_fallback(toc_paths: list[str]) -> list[str]:
        from collections import Counter
        counter: Counter = Counter()
        for tp in toc_paths:
            segs = [s.strip() for s in tp.split("|") if s.strip()]
            if segs:
                counter["|".join(segs)] += 1
        if not counter:
            return []
        best = counter.most_common(1)[0][0]
        return [s.strip() for s in best.split("|") if s.strip()][:-1]

    def test_single_toc_path_drops_last_segment(self):
        result = self._compute_fallback(["Section|Subsection|Page"])
        assert result == ["Section", "Subsection"]

    def test_majority_toc_path_wins(self):
        paths = ["Section|A", "Section|A", "Section|B"]
        result = self._compute_fallback(paths)
        # "Section|A" wins (2 votes) → drop "A" → ["Section"]
        assert result == ["Section"]

    def test_single_segment_path_gives_empty_fallback(self):
        result = self._compute_fallback(["OnlyRoot"])
        assert result == []

    def test_empty_input_gives_empty_fallback(self):
        result = self._compute_fallback([])
        assert result == []

    def test_two_segment_path_gives_one_segment_fallback(self):
        result = self._compute_fallback(["Parent|Child"])
        assert result == ["Parent"]

    def test_whitespace_segments_ignored(self):
        result = self._compute_fallback([" | Subsection | Page "])
        assert result == ["Subsection"]
