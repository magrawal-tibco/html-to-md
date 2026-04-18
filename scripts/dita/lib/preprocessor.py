"""
preprocessor.py — DITA-specific BeautifulSoup transforms for file_dita and sdl_dita.

Imports shared transforms from scripts.lib.preprocessor and adds DITA-specific ones.
"""

import re
from bs4 import BeautifulSoup, Tag

from scripts.lib.preprocessor import (
    definition_lists,
    task_sections,
    inline_spans,
    anchor_only_links,
    split_colspan_tables,
    normalize_whitespace,
    fix_pre_linebreaks,
    rewrite_image_src,
    classify_tables,
)
from scripts.lib.table_classifier import DEFAULT_BLOCK_TAGS

# ── Callout label map ──────────────────────────────────────────────────────────

_DITA_CALLOUT_LABELS = {
    "note":        "Note",
    "warning":     "Warning",
    "caution":     "Caution",
    "tip":         "Tip",
    "important":   "Important",
    "attention":   "Attention",
    "danger":      "Danger",
    "fastpath":    "Fastpath",
    "remember":    "Remember",
    "restriction": "Restriction",
}


# ── Transform 1: strip chrome ─────────────────────────────────────────────────

def strip_chrome(content: Tag, chrome_selectors: list[str]) -> int:
    """Remove UI chrome elements from the extracted content div."""
    removed = 0
    for selector in chrome_selectors:
        for el in content.select(selector):
            el.decompose()
            removed += 1
    for tag in content.find_all(["script", "style"]):
        tag.decompose()
        removed += 1
    return removed


# ── Transform 2: DITA callout divs ───────────────────────────────────────────

def dita_callout_divs(content: Tag) -> int:
    """
    Convert DITA note/callout divs to <blockquote> with a bold label.

    Handles two patterns:
    1. file_dita: <div class="note tip note_tip"><span class="note__title">Tip:</span> ...</div>
    2. sdl_dita:  <div class="note tip">...</div> or <div class="tip">...</div>
    """
    converted = 0
    for div in list(content.find_all("div")):
        classes = div.get("class", [])
        if isinstance(classes, str):
            classes = classes.split()
        matched_label = None
        for cls in classes:
            if cls in _DITA_CALLOUT_LABELS:
                matched_label = _DITA_CALLOUT_LABELS[cls]
                break
        if matched_label is None:
            continue
        # Remove <span class="note__title"> if present (file_dita style)
        title_span = div.find("span", class_="note__title")
        if title_span:
            title_span.decompose()
        bq = BeautifulSoup(
            f"<blockquote><p><strong>{matched_label}:</strong> </p></blockquote>", "lxml"
        ).find("blockquote")
        bq.p.extend(list(div.children))
        div.replace_with(bq)
        converted += 1
    return converted


# ── Transform 3: DITA task step cleanup ──────────────────────────────────────

def dita_task_steps(content: Tag) -> int:
    """
    Clean up DITA task step markup:
    - Unwrap <div class="tasklabel"> (keep inner heading)
    - Unwrap <span class="ph cmd"> (keep text)
    - Strip class from <ol class="steps|steps-unordered">
    - Strip class from <li class="step|stepexpand|li">
    - Unwrap <div class="itemgroup|info">
    """
    converted = 0

    for div in list(content.find_all("div", class_="tasklabel")):
        div.unwrap()
        converted += 1

    for span in list(content.find_all("span", class_="ph")):
        if "cmd" in (span.get("class") or []):
            span.unwrap()
            converted += 1

    for ol in list(content.find_all("ol")):
        classes = ol.get("class", [])
        if any(c in classes for c in ("steps", "steps-unordered")):
            del ol["class"]
            converted += 1

    for li in list(content.find_all("li")):
        classes = li.get("class", [])
        if any(c in classes for c in ("step", "stepexpand", "li")):
            if li.get("class"):
                del li["class"]
            converted += 1

    for div in list(content.find_all("div", class_="itemgroup")):
        div.unwrap()
        converted += 1

    for div in list(content.find_all("div", class_="info")):
        div.unwrap()
        converted += 1

    return converted


# ── Transform 4: strip shortdesc class ───────────────────────────────────────

def strip_shortdesc_class(content: Tag) -> int:
    """Remove class='shortdesc' from <p> — preserves the paragraph, removes noise."""
    stripped = 0
    for p in content.find_all("p", class_="shortdesc"):
        del p["class"]
        stripped += 1
    return stripped


# ── Main entry point ──────────────────────────────────────────────────────────

def dita_run_all(
    content: Tag,
    chrome_selectors: list[str],
    page_url_path: str,
    block_tags: set[str] | None = None,
) -> dict:
    """Run all DITA transforms in order. Returns stats dict."""
    stats = {}
    stats["chrome_removed"]     = strip_chrome(content, chrome_selectors)
    stats["dita_callouts"]      = dita_callout_divs(content)
    stats["dita_task_steps"]    = dita_task_steps(content)
    stats["shortdesc_stripped"] = strip_shortdesc_class(content)
    stats["definition_lists"]   = definition_lists(content)
    stats["task_sections"]      = task_sections(content)
    stats["inline_spans"]       = inline_spans(content)
    stats["anchor_links"]       = anchor_only_links(content)
    stats["colspan_tables"]     = split_colspan_tables(content)
    table_counts                = classify_tables(content, block_tags or DEFAULT_BLOCK_TAGS)
    stats.update(table_counts)
    stats["ws_normalized"]      = normalize_whitespace(content)
    stats["pre_linebreaks"]     = fix_pre_linebreaks(content)
    stats["images_rewritten"]   = rewrite_image_src(content, page_url_path)
    return stats
