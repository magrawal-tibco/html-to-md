"""
convert.py — PDF Release Notes → Markdown converter.

Discovers release notes PDFs already cached by the ZIP download step and converts
them to Markdown. Only PDFs whose filename contains "relnotes" or "release-notes"
are processed; other PDF types (admin guides, API references) are skipped because
their complex layouts produce lower-quality output.

Uses pymupdf (fitz) for extraction:
  - Font-size span data → heading detection (body_size calibrated per document)
  - page.find_tables()  → GFM pipe tables
  - Wingdings/bullet glyph detection → list items
  - SourceCodePro spans → inline code backticks
  - Top/bottom zone filtering → strips running headers and footers

Usage:
  python scripts/pdf/convert.py --phase phase_04
         [--config config/settings.yaml] [--dry-run] [--force-rerun]
"""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import fitz  # pymupdf
import yaml
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.lib.reporter import Reporter


# ── Font classification helpers ───────────────────────────────────────────────

# Fonts used for bullet glyphs (non-alphabetic decorative fonts)
_GLYPH_FONTS = {"Wingdings", "Wingdings-Regular", "Wingdings2", "Wingdings3",
                "Symbol", "ZapfDingbats"}

# Single-character courier codes used as sub-bullet markers (size < 10pt)
_COURIER_BULLET_CHARS = frozenset("ol\u25e6\u25cf\u2022\u25a0\u25aa\u25ab\u2013")

# Font name fragments indicating monospace / code text
_CODE_FONT_FRAGMENTS = ("SourceCode", "Consolas", "Menlo", "Inconsolata",
                        "LucidaConsole", "Mono")


def _is_glyph_span(span: dict) -> bool:
    """True if this span is a decorative bullet glyph, not real text."""
    font = span["font"]
    if any(font.startswith(g) for g in _GLYPH_FONTS):
        return True
    # Small Courier chars used as sub-bullet markers
    if "Courier" in font and span["size"] < 10:
        text = span["text"].strip()
        if len(text) <= 1 and text in _COURIER_BULLET_CHARS:
            return True
    return False


def _is_code_span(span: dict) -> bool:
    """True if this span uses a monospace / code font (not a bullet glyph)."""
    if _is_glyph_span(span):
        return False
    font = span["font"]
    return any(frag in font for frag in _CODE_FONT_FRAGMENTS)


def _is_bold(span: dict) -> bool:
    return bool(span["flags"] & 16)


def _meaningful_spans(block: dict) -> list[dict]:
    """All spans in a block whose text is non-empty and not a bare non-breaking space."""
    result = []
    for line in block["lines"]:
        for span in line["spans"]:
            t = span["text"].strip()
            if t and t != "\xa0":
                result.append(span)
    return result


# ── Block classification ──────────────────────────────────────────────────────

_BlockType = str  # 'skip' | 'h1' | 'h2' | 'h3' | 'bullet' | 'sub_bullet' | 'body'


def _classify_block(block: dict, body_size: float) -> _BlockType:
    """
    Classify a block by examining its first meaningful span.

    Heading levels:
      h1: size >= body_size + 5  (document title / large section, usually only on cover)
      h2: size >= body_size + 2  (section heading, e.g. "New Features")
      h3: bold text at body_size with short content, OR bold text after a Wingdings glyph

    Lists:
      bullet:     first span is a Wingdings glyph followed by bold text → sub-heading style
                  OR first span is a Wingdings glyph followed by regular text → bullet
      sub_bullet: first span is a small Courier glyph
    """
    spans = _meaningful_spans(block)
    if not spans:
        return "skip"

    first = spans[0]
    font  = first["font"]
    size  = first["size"]
    bold  = _is_bold(first)

    # Glyph-prefixed blocks (bullets and sub-headings)
    if _is_glyph_span(first):
        if any(font.startswith(g) for g in _GLYPH_FONTS):
            # Wingdings bullet: check if the following text is bold → treat as H3
            following_bold = any(_is_bold(s) for s in spans[1:] if not _is_glyph_span(s))
            if following_bold:
                return "h3"
            return "bullet"
        else:
            # Small Courier glyph → sub-bullet
            return "sub_bullet"

    # Plain headings by font size
    if size >= body_size + 5:
        return "h1"
    if size >= body_size + 2:
        return "h2"

    # Bold at body size with short content → H3 (e.g. bolded sub-section label)
    # Exclude sentences: headings don't end with . ? !
    if bold and size >= body_size - 1:
        all_text = "".join(s["text"] for l in block["lines"] for s in l["spans"]).strip()
        if len(all_text) < 120 and not re.search(r"[.?!]\s*$", all_text):
            return "h3"

    return "body"


# ── Text assembly ─────────────────────────────────────────────────────────────

def _assemble_block_text(block: dict, skip_leading_glyph: bool = False) -> str:
    """
    Build the text for a block by joining all spans.
    - Skips glyph spans when skip_leading_glyph is True (bullet/h3 blocks)
    - Wraps code-font spans in backticks
    - Inserts a space at large horizontal gaps (column boundaries in borderless tables)
    - Normalises whitespace
    """
    parts: list[str] = []
    glyph_skipped = not skip_leading_glyph  # if False, skip the first glyph we see

    prev_line_y: float | None = None  # y0 of the previous line in this block

    for line in block["lines"]:
        line_y = line["bbox"][1]

        # Two lines at the same y-position within one block = adjacent table columns
        # on the same PDF row (PyMuPDF merges them into one block with two "lines").
        # Different y = genuine text wrap; no separator needed.
        if prev_line_y is not None and abs(line_y - prev_line_y) < 2.0:
            if parts and not parts[-1].endswith(" "):
                parts.append(" ")

        prev_line_y = line_y
        prev_x1: float | None = None  # x1 of the previous span on this line

        for span in line["spans"]:
            text = span["text"]
            t    = text.strip()
            bbox = span.get("bbox", (0, 0, 0, 0))
            span_x0, span_x1 = bbox[0], bbox[2]

            if not t or t == "\xa0":
                # Preserve one space for whitespace-only spans inside a line
                if parts and not parts[-1].endswith(" "):
                    parts.append(" ")
                prev_x1 = span_x1
                continue

            if _is_glyph_span(span):
                if not glyph_skipped:
                    glyph_skipped = True  # discard the first glyph
                prev_x1 = span_x1
                continue

            # Insert a space at the span boundary when:
            #   (a) there is a visible horizontal gap (> 0.5 pt) — column/word boundary, OR
            #   (b) no gap but the previous span ends with an alphanumeric and this span
            #       starts with an uppercase letter — catches merged column-header pairs
            #       like "GS-17793"+"The…" or "SOAP API"+"REST API" where PDF layout
            #       positions the two spans with zero gap.
            # In well-formed PDFs, mid-word format changes share the same span;
            # genuinely adjacent spans that touch (gap=0) are rare mid-word.
            if prev_x1 is not None and parts and not parts[-1].endswith(" ") and not text.startswith(" "):
                gap = span_x0 - prev_x1
                last_char  = parts[-1][-1] if parts[-1] else ""
                first_char = text[0] if text else ""
                needs_space = (
                    gap > 0.5
                    or (gap >= 0 and last_char.isalnum() and first_char.isupper())
                )
            else:
                needs_space = False
            if needs_space:
                parts.append(" ")

            if _is_code_span(span):
                parts.append(f"`{t}`")
            else:
                parts.append(text)

            prev_x1 = span_x1

    result = "".join(parts).strip()
    # Collapse multiple internal spaces / newlines
    result = re.sub(r"\s+", " ", result)
    return result


# ── Font calibration ──────────────────────────────────────────────────────────

def calibrate_body_size(doc: fitz.Document) -> float:
    """
    Detect the body font size by finding the most-common span size
    in the body zone (8%–92% of page height) across the first 5 content pages.
    Falls back to 12.0 if detection fails.
    """
    size_chars: Counter = Counter()
    pages_sampled = 0

    for page_idx in range(1, min(6, len(doc))):  # skip cover page (0)
        page = doc[page_idx]
        h    = page.rect.height
        body_top    = h * 0.08
        body_bottom = h * 0.92

        for block in page.get_text("dict")["blocks"]:
            if block["type"] != 0:
                continue
            if block["bbox"][3] < body_top or block["bbox"][1] > body_bottom:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    t = span["text"].strip()
                    if not t or t == "\xa0" or _is_glyph_span(span):
                        continue
                    size_chars[round(span["size"], 1)] += len(t)

        pages_sampled += 1
        if pages_sampled >= 5:
            break

    if not size_chars:
        return 12.0
    return size_chars.most_common(1)[0][0]


# ── Repeated-element detection ────────────────────────────────────────────────

def collect_repeated_h3_texts(doc: fitz.Document, min_pages: int = 2) -> frozenset[str]:
    """
    Return the assembled text of bold, short blocks that appear identically on
    min_pages or more pages across the document.  These are repeating table column
    header rows (e.g. "Key Summary", "SOAP API REST API") that PDF generators
    repeat at the top of every continuation page and at the start of each sub-table.
    They should be suppressed rather than rendered as H3 headings.
    """
    from collections import Counter
    counts: Counter = Counter()
    for page in doc:
        seen: set[str] = set()
        for block in page.get_text("dict")["blocks"]:
            if block["type"] != 0:
                continue
            spans = [s for line in block["lines"] for s in line["spans"]]
            if not spans or not _is_bold(spans[0]):
                continue
            text = _assemble_block_text(block)
            if 0 < len(text) < 60 and text not in seen:
                seen.add(text)
                counts[text] += 1
    return frozenset(text for text, n in counts.items() if n >= min_pages)


# ── Table rendering ───────────────────────────────────────────────────────────

def _render_table(table) -> str:
    """Render a pymupdf TableFinder table to GFM or HTML."""
    rows = table.extract()
    if not rows:
        return ""

    # Normalize None cells
    rows = [["" if cell is None else str(cell).strip() for cell in row] for row in rows]

    # Use GFM if cells are simple (no newlines, reasonable length)
    complex_cell = any(
        "\n" in cell or len(cell) > 150
        for row in rows for cell in row
    )

    if not complex_cell and rows:
        header = rows[0]
        sep    = ["---"] * len(header)
        lines  = ["| " + " | ".join(header) + " |",
                  "| " + " | ".join(sep)    + " |"]
        for row in rows[1:]:
            lines.append("| " + " | ".join(row) + " |")
        return "\n".join(lines)
    else:
        # HTML passthrough for complex tables
        td_rows = ""
        for i, row in enumerate(rows):
            tag = "th" if i == 0 else "td"
            cells = "".join(f"<{tag}>{c}</{tag}>" for c in row)
            td_rows += f"<tr>{cells}</tr>\n"
        return f"<table>\n{td_rows}</table>"


# ── Leading-H3 split ──────────────────────────────────────────────────────────

def _extract_leading_h3(block: dict, body_size: float) -> tuple[str, str] | None:
    """
    Detect a block whose first span(s) are bold body-size text followed by non-bold
    body text — i.e. an inline sub-heading that PDF did not separate into its own block.

    Returns (h3_text, body_text) when found, or None.

    Example PDF pattern:
      [bold] "Creating and Configuring the Redshift Connection Resource"
      [regular] "By using the Default Credentials Provider Chain..."
    → ("Creating and Configuring the Redshift Connection Resource",
       "By using the Default Credentials Provider Chain...")
    """
    spans = _meaningful_spans(block)
    if not spans:
        return None

    first = spans[0]
    # Must start with bold text at body size (not a larger heading, not a glyph)
    if not (_is_bold(first) and body_size - 1 <= first["size"] < body_size + 2
            and not _is_glyph_span(first)):
        return None

    # Partition spans into the leading bold run and the remainder
    split_idx = 0
    for i, span in enumerate(spans):
        if _is_bold(span) and not _is_glyph_span(span):
            split_idx = i + 1
        else:
            break

    if split_idx >= len(spans):
        return None  # entire block is bold → normal H3 classification handles it

    # Build H3 text from bold-prefix spans (simple join; heading text is never code)
    h3_text = re.sub(r"\s+", " ",
                     " ".join(s["text"].strip() for s in spans[:split_idx])).strip()

    # Reject if it looks like a sentence rather than a heading label
    if not h3_text or len(h3_text) >= 120 or re.search(r"[.?!]\s*$", h3_text):
        return None

    # Build body text from remaining spans, preserving code formatting
    body_parts: list[str] = []
    for span in spans[split_idx:]:
        t = span["text"].strip()
        if not t:
            continue
        body_parts.append(f"`{t}`" if _is_code_span(span) else t)
    body_text = re.sub(r"\s+", " ", " ".join(body_parts)).strip()

    return (h3_text, body_text) if body_text else None


# ── Page conversion ───────────────────────────────────────────────────────────

def _convert_page(
    page: fitz.Page,
    body_size: float,
    repeated_h3_texts: frozenset[str] = frozenset(),
) -> list[str]:
    """
    Convert one PDF page to a list of Markdown line strings.
    Skips running headers and footers by zone (top 8% / bottom 8%).
    Joins wrapped list-item text back onto the preceding bullet using x-indent tracking.
    Skips bold blocks whose text appears on 3+ pages (repeated table column headers).
    """
    h           = page.rect.height
    header_line = h * 0.08
    footer_line = h * 0.92

    # Find table bounding boxes so we can skip those blocks
    table_finder = page.find_tables()
    table_rects  = [t.bbox for t in table_finder.tables]

    def _overlaps_table(bbox) -> bool:
        bx0, by0, bx1, by1 = bbox
        for tx0, ty0, tx1, ty1 in table_rects:
            if bx0 < tx1 and bx1 > tx0 and by0 < ty1 and by1 > ty0:
                return True
        return False

    # Items: (y, x0, block_type, markdown_text)
    raw_items: list[tuple[float, float, str, str]] = []

    # ── Text blocks ──
    blocks = page.get_text("dict", sort=True)["blocks"]
    for block in blocks:
        if block["type"] != 0:
            continue  # image block
        bbox = block["bbox"]
        if bbox[3] < header_line or bbox[1] > footer_line:
            continue
        if _overlaps_table(bbox):
            continue

        btype = _classify_block(block, body_size)
        if btype == "skip":
            continue

        # Body blocks that start with a bold run followed by non-bold text are
        # inline sub-headings the PDF did not separate — split into H3 + body.
        if btype == "body":
            split = _extract_leading_h3(block, body_size)
            if split:
                h3_text, body_text = split
                if h3_text not in repeated_h3_texts:
                    raw_items.append((bbox[1], bbox[0], "h3", f"### {h3_text}"))
                if body_text:
                    raw_items.append((bbox[1] + 0.01, bbox[0], "body", body_text))
                continue

        skip_glyph = btype in ("bullet", "sub_bullet", "h3")
        text = _assemble_block_text(block, skip_leading_glyph=skip_glyph)
        if not text:
            continue

        # Suppress bold blocks whose exact text appears on 3+ pages — these are
        # repeating table column headers ("Key Summary", "SOAP API REST API") that
        # the PDF renders at the top of each continuation page and before sub-tables.
        if btype == "h3" and text in repeated_h3_texts:
            continue

        if btype == "h1":
            md = f"# {text}"
        elif btype == "h2":
            md = f"## {text}"
        elif btype == "h3":
            md = f"### {text}"
        elif btype == "bullet":
            md = f"- {text}"
        elif btype == "sub_bullet":
            md = f"  - {text}"
        else:
            md = text

        raw_items.append((bbox[1], bbox[0], btype, md))

    # ── Tables ──
    for table in table_finder.tables:
        ty0 = table.bbox[1]
        if ty0 < header_line or ty0 > footer_line:
            continue
        rendered = _render_table(table)
        if rendered:
            raw_items.append((ty0, table.bbox[0], "table", rendered))

    raw_items.sort(key=lambda x: x[0])

    # Estimate page left margin from x0 of body-type blocks.
    # Continuation text is indented further right than this margin.
    body_x0s = [x0 for _, x0, bt, _ in raw_items if bt == "body"]
    page_margin     = min(body_x0s) if body_x0s else 0.0
    indent_threshold = page_margin + 10.0

    # Y-proximity pass: body blocks at the same vertical position but different X
    # positions are table cells from adjacent columns — join them with a space.
    # Threshold: same row if |Δy| < 1.5 × body_size; different column if Δx > body_size.
    col_merged: list[tuple[float, float, str, str]] = []
    for item in raw_items:
        y, x0, btype, md = item
        if col_merged and btype == "body":
            prev_y, prev_x0, prev_btype, prev_md = col_merged[-1]
            if (prev_btype == "body"
                    and abs(y - prev_y) < body_size * 1.5
                    and x0 > prev_x0 + body_size):
                col_merged[-1] = (prev_y, prev_x0, prev_btype, prev_md + " " + md)
                continue
        col_merged.append(item)

    # Continuation-joining pass:
    # Body block immediately following a list item AND indented past the page margin
    # → wrapped bullet text; append it to the preceding bullet rather than a new line.
    merged: list[tuple[float, float, str, str]] = []
    for item in col_merged:
        y, x0, btype, md = item
        if merged and btype == "body":
            prev_y, prev_x0, prev_btype, prev_md = merged[-1]
            if prev_btype in ("bullet", "sub_bullet") and x0 > indent_threshold:
                merged[-1] = (prev_y, prev_x0, prev_btype, prev_md + " " + md)
                continue
        merged.append(item)

    return [md for _, _, _, md in merged]


# ── TOC page detection ────────────────────────────────────────────────────────

_LIST_RE = re.compile(r"^(\s*- |\d+\. )")


def _is_toc_page(page_lines: list[str]) -> bool:
    """Return True if this page looks like a Table of Contents (should be skipped)."""
    # Reliable signal: a heading whose text is exactly "Contents" / "Table of Contents"
    for line in page_lines:
        if re.match(r"^#+\s*(Table of )?Contents\s*$", line.strip(), re.IGNORECASE):
            return True
    # Heuristic: majority of non-empty lines end with a concatenated page number.
    # PDF layout sometimes merges dots+number directly onto the preceding word, giving
    # "New Features3" or "Installation Guide 12" instead of "New Features......3".
    non_empty = [l for l in page_lines if l.strip()]
    if len(non_empty) < 3:
        return False
    toc_like = sum(
        1 for l in non_empty
        if re.search(r"[A-Za-z\u00ae\u00a9\u2122®©]\d{1,3}$", l.strip())  # "Title3"
        or re.search(r"\s+\d{1,3}$", l.strip())                             # "Title 3"
    )
    return toc_like / len(non_empty) > 0.6


# ── Table row reconstruction ─────────────────────────────────────────────────

_ISSUE_KEY_RE = re.compile(r"^(GS-\d+)\s+(.*)", re.DOTALL)


def _fix_table_rows(md_lines: list[str]) -> list[str]:
    """
    Post-process assembled markdown lines to reconstruct GFM table rows from body
    text that was not captured by find_tables():

    1. SOAP-API class-name rows: "com.X com.Y" → "| com.X | com.Y |"
       (table_type="soap_api" set when "| SOAP API | REST API |" header is seen)

    2. Issue-key rows (GS-NNNNN ...): formatted as "| GS-NNNNN | summary |"
       - Multi-line summaries are joined into a single cell
       - "| Key | Summary |" + "| --- | --- |" inserted before the first issue
         row of any sub-section that lacks a detected table header

    Blank lines inside an active table context are suppressed to avoid breaking
    GFM rendering (they occur at page boundaries in the source PDF).
    """
    result: list[str] = []
    table_type: str | None = None   # "soap_api" | "key_summary" | None
    pending_key    = ""             # issue key being accumulated
    pending_summary = ""            # issue summary (may span multiple source lines)

    def flush_pending() -> None:
        nonlocal pending_key, pending_summary, table_type
        if not pending_key:
            return
        if table_type != "key_summary":
            # Add table header for sub-sections that had no detected table
            if result and result[-1].strip():
                result.append("")
            result.append("| Key | Summary |")
            result.append("| --- | --- |")
            table_type = "key_summary"
        summary = re.sub(r"\s+", " ", pending_summary).strip()
        result.append(f"| {pending_key} | {summary} |")
        pending_key = ""
        pending_summary = ""

    for line in md_lines:
        stripped = line.strip()

        # ── Section heading ──────────────────────────────────────────────────
        if stripped.startswith("#"):
            flush_pending()
            if result and result[-1].strip().startswith("|"):
                result.append("")
            table_type = None
            result.append(line)
            continue

        # ── Already-rendered GFM table lines ────────────────────────────────
        if stripped.startswith("|"):
            flush_pending()
            if "SOAP API" in stripped or "REST API" in stripped:
                table_type = "soap_api"
            elif "Key" in stripped and "Summary" in stripped:
                table_type = "key_summary"
            result.append(line)
            continue

        # ── Blank line ───────────────────────────────────────────────────────
        if not stripped:
            if pending_key or table_type is not None:
                pass  # suppress blank lines inside a table context
            else:
                result.append(line)
            continue

        # ── Issue key row: GS-NNNNN <summary text> ──────────────────────────
        m = _ISSUE_KEY_RE.match(stripped)
        if m:
            flush_pending()
            pending_key     = m.group(1)
            pending_summary = m.group(2)
            continue

        # ── Continuation of a pending issue summary ──────────────────────────
        if pending_key:
            pending_summary += " " + stripped
            continue

        # ── SOAP-API table body rows (two class names separated by a space) ──
        if table_type == "soap_api" and " " in stripped and not stripped.startswith("-"):
            col1, col2 = stripped.split(" ", 1)
            result.append(f"| {col1} | {col2} |")
            continue

        # ── Regular body text ────────────────────────────────────────────────
        # Any non-table, non-issue body text resets the table context so we
        # don't accidentally swallow paragraph text into a table.
        flush_pending()
        table_type = None
        result.append(line)

    flush_pending()
    return result


# ── Callout normalisation ─────────────────────────────────────────────────────

_CALLOUT_RE = re.compile(
    r'^(Note|Warning|Caution|Important|Tip)\s*:\s*(.+)',
    re.IGNORECASE,
)
_CALLOUT_LABELS = {
    "note": "Note", "warning": "Warning", "caution": "Caution",
    "important": "Important", "tip": "Tip",
}


def _fix_callouts(md_lines: list[str]) -> list[str]:
    """
    Convert bare "Note: ..." / "Warning: ..." lines (as produced by PDF extraction)
    into blockquote callouts matching the HTML pipeline format:

      Note: Some text.  →  > **Note:** Some text.
    """
    result: list[str] = []
    for line in md_lines:
        m = _CALLOUT_RE.match(line.strip())
        if m:
            label = _CALLOUT_LABELS[m.group(1).lower()]
            body  = m.group(2).strip()
            result.append(f"> **{label}:** {body}")
        else:
            result.append(line)
    return result


# ── Platform support table reconstruction ────────────────────────────────────

# Matches a 2-column header produced when PyMuPDF find_tables() only detected
# the Status and As-of-Version columns, leaving Platform names as body text.
_PLATFORM_HDR_RE = re.compile(r"^\|\s*Status\s*\|\s*As\s+of\s+(?:Version|Release)\s*\|$", re.IGNORECASE)
_PLATFORM_ROW_RE = re.compile(r"^(.+?)\s+(Added|Removed)\s+(\d+\.\d+(?:\.\d+)?)\s*$")


def _fix_platform_table(md_lines: list[str]) -> list[str]:
    """
    Detect a 2-column '| Status | As of Version |' table followed by body-text
    platform rows and replace with a proper 3-column table.

    Input pattern:
        | Status | As of Version |
        | --- | --- |
        Apple Mac OS 13.x 64-bit on x86-64 Added 6.1.0
        Microsoft Windows 11 64-bit on x86-64 Added 6.1.0

    Output:
        | Platform | Status | As of Version |
        | --- | --- | --- |
        | Apple Mac OS 13.x 64-bit on x86-64 | Added | 6.1.0 |
        | Microsoft Windows 11 64-bit on x86-64 | Added | 6.1.0 |
    """
    result, i = [], 0
    while i < len(md_lines):
        s = md_lines[i].strip()
        if _PLATFORM_HDR_RE.match(s):
            j = i + 1
            # Consume the separator row if present
            if j < len(md_lines) and _ISSUES_SEP_RE.match(md_lines[j].strip()):
                j += 1
            # Collect body-text rows that match "Platform Added|Removed Version".
            # PDFs sometimes split a platform name across two lines, e.g.:
            #   macOS Added 6.3.3
            #   13.x 64-bit on x86-64        ← continuation of platform name
            # Detect continuations: short line, no sentence-ending period,
            # doesn't start a new table/heading element.
            rows: list[tuple[str, str, str]] = []
            while j < len(md_lines):
                cur = md_lines[j].strip()
                m = _PLATFORM_ROW_RE.match(cur)
                if m:
                    rows.append((m.group(1).strip(), m.group(2), m.group(3)))
                    j += 1
                    # Consume continuation lines (OS version detail on next line)
                    while j < len(md_lines):
                        cont = md_lines[j].strip()
                        if (cont
                                and not _PLATFORM_ROW_RE.match(cont)
                                and not cont.startswith("|")
                                and not cont.startswith("#")
                                and not cont.endswith(".")
                                and len(cont) < 80):
                            plat, st, ver = rows[-1]
                            rows[-1] = (plat + " " + cont, st, ver)
                            j += 1
                        else:
                            break
                else:
                    break
            if rows:
                result.extend([
                    "| Platform | Status | As of Version |",
                    "| --- | --- | --- |",
                ])
                result.extend(f"| {p} | {st} | {v} |" for p, st, v in rows)
                i = j
            else:
                result.append(md_lines[i])
                i += 1
        else:
            result.append(md_lines[i])
            i += 1
    return result


# ── Known Issues table reconstruction ────────────────────────────────────────

_ISSUES_HDR_RE  = re.compile(r"^\|\s*Key\s*\|", re.IGNORECASE)
_ISSUES_SEP_RE  = re.compile(r"^\|\s*-")
_ISSUES_KEY_RE  = re.compile(r"^([A-Z][A-Z0-9]*-\d+)\s*(.*)", re.DOTALL)
_ISSUES_SUMM_RE = re.compile(r"^(?:###\s+)?Summary:?\s*(.*)", re.IGNORECASE)   # colon optional
_ISSUES_WKRD_RE = re.compile(r"^(?:###\s+)?Workaround:?\s*(.*)", re.IGNORECASE) # colon optional
_ISSUES_NOTE_RE = re.compile(r"^###\s+Note:?\s*(.*)", re.IGNORECASE)            # H3 note header
_OL_ITEM_RE2    = re.compile(r"^\d+\.\s+(.+)")
_NOTE_BQ_RE     = re.compile(r"^>\s*\*\*Note:\*\*\s*(.*)", re.IGNORECASE)
_MD_CODE_RE     = re.compile(r"`([^`]+)`")


def _md_inline_to_html(text: str) -> str:
    """Convert markdown inline code spans to <code> elements, merging adjacent spans."""
    merged = re.sub(r"`([^`]+)``([^`]+)`", r"`\1\2`", text)
    return _MD_CODE_RE.sub(r"<code>\1</code>", merged)


def _build_issue_cell(summary: str, wa_parts: list, use_labels: bool = True) -> str:
    """Render summary + workaround as HTML cell content.

    use_labels=True  (Known Issues):   wraps content in <strong>Summary:</strong> /
                                       <strong>Workaround:</strong> labels.
    use_labels=False (Closed Issues):  renders plain paragraphs without bold labels.
    """
    html: list[str] = []
    if summary:
        if use_labels:
            html.append(f"<p><strong>Summary:</strong> {_md_inline_to_html(summary)}</p>")
        else:
            html.append(f"<p>{_md_inline_to_html(summary)}</p>")
    if not wa_parts:
        return "".join(html)

    intro_parts: list[str] = []
    ol_items:    list[str] = []
    notes:       list[str] = []
    for kind, content in wa_parts:
        if kind == "text":
            if ol_items:
                ol_items[-1] += " " + content
            else:
                intro_parts.append(content)
        elif kind == "ol_item":
            ol_items.append(content)
        elif kind == "note":
            notes.append(content)

    intro = _md_inline_to_html(" ".join(intro_parts).strip())
    if use_labels:
        html.append(f"<p><strong>Workaround:</strong> {intro}</p>" if intro
                    else "<p><strong>Workaround:</strong></p>")
    elif intro:
        html.append(f"<p>{intro}</p>")
    if ol_items:
        lis = "".join(f"<li>{_md_inline_to_html(li)}</li>" for li in ol_items)
        html.append(f"<ol>{lis}</ol>")
    for note in notes:
        html.append(f"<blockquote><strong>Note:</strong> {_md_inline_to_html(note)}</blockquote>")
    return "".join(html)


def _fix_issue_tables(md_lines: list[str]) -> list[str]:
    """
    Reconstruct Known Issues / Closed Issues pseudo-tables into HTML tables.

    Section detection (from the nearest preceding H1 heading):
    - "Closed Issues" → column "Summary", plain paragraphs (no bold labels)
    - "Known Issues"  → column "Description", bold Summary/Workaround labels

    Handles PDF layout variations:
    - Key + Summary on same line: "BWAR-182 Summary: ..."
    - Summary before key:         "### Summary\nBWAR-235 : text"
    - H3-prefixed labels:         "### Summary" / "### Workaround" (colon optional)
    - Leading ': ' artifact:      ": text" after a label line
    - Numbered steps with inline code continuations
    - Two-line notes:             "### Note:\ntext"
    - Blockquote notes:           "> **Note:** text" (already from _fix_callouts)
    - No table header:            bare issue keys in Closed Issues section
    - Multi-line table header:    "| Key | Summary |\n| --- | --- |" as one string
    - Compound keys:              "BPDK-554/ BPDK-586 : text"
    - Bold-label artifacts:       "### Apply" (button name rendered as heading)
    """
    result:         list[str]  = []
    mode:           str | None = None   # None | "saw_sep" | "in_table"
    rows:           list[str]  = []
    col_name:       str        = "Description"
    use_labels:     bool       = True
    current_h1:     str        = ""
    pending_note:   bool       = False  # True when "### Note:" seen; next line is note text
    saved_hdr_line: str        = ""     # original "| Key | ... |" line saved for pass-through re-emit

    cur_key:   str        = ""
    section:   str | None = None
    sum_parts: list[str]  = []
    wa_parts:  list       = []
    pre_sum:   list[str]  = []

    def _strip_art(text: str) -> str:
        """Strip leading PDF artifacts: compound-key prefix '/ KEY :' and bare ': '."""
        text = re.sub(r"^/\s*[A-Z][A-Z0-9]*-\d+\s*:\s*", "", text)
        text = re.sub(r"^:\s*", "", text)
        return text.strip()

    def _is_issues_h1(h1: str) -> bool:
        low = h1.lower()
        return "closed issue" in low or "known issue" in low

    def flush() -> None:
        nonlocal cur_key, section, sum_parts, wa_parts, pre_sum, pending_note
        if cur_key:
            desc = _build_issue_cell(" ".join(sum_parts), wa_parts, use_labels=use_labels)
            rows.append(f"<tr><td>{cur_key}</td><td>{desc}</td></tr>")
        cur_key = ""; section = None; sum_parts = []; wa_parts = []; pre_sum = []; pending_note = False

    def emit() -> None:
        if rows:
            result.extend(["<table>", f"<tr><th>Key</th><th>{col_name}</th></tr>",
                           *rows, "</table>", ""])
        rows.clear()

    for line in md_lines:
        s = line.strip()

        # ── pass-through mode ─────────────────────────────────────────────────
        if mode is None:
            m_h1 = re.match(r"^#\s+(.+)", s)
            if m_h1:
                current_h1 = m_h1.group(1).strip()
            if _ISSUES_HDR_RE.match(s):
                is_closed      = "closed" in current_h1.lower()
                col_name       = "Summary" if is_closed else "Description"
                use_labels     = not is_closed
                saved_hdr_line = line   # save original header for potential pass-through re-emit
                mode           = "saw_sep"
                continue  # discard header row (re-emitted if table is already rendered)
            # Auto-detect: bare issue key in Closed/Known Issues H1, no table header
            if _is_issues_h1(current_h1) and _ISSUES_KEY_RE.match(s):
                is_closed  = "closed" in current_h1.lower()
                col_name   = "Summary" if is_closed else "Description"
                use_labels = not is_closed
                mode = "in_table"
                # fall through to in_table processing for this line
            else:
                result.append(line)
                continue

        # ── saw separator row ─────────────────────────────────────────────────
        if mode == "saw_sep":
            if _ISSUES_SEP_RE.match(s):
                mode = "in_table"
            elif not s:
                pass  # skip blank line, keep looking
            else:
                result.extend([f"| Key | {col_name} |", line])
                mode = None
            continue

        # ── in_table mode ─────────────────────────────────────────────────────

        # H1/H2 heading ends the table (H3 does not); update current_h1 for H1
        if re.match(r"^#{1,2}\s", s) and not re.match(r"^###", s):
            flush(); emit(); mode = None
            m_h1 = re.match(r"^#\s+(.+)", s)
            if m_h1:
                current_h1 = m_h1.group(1).strip()
            result.append(line); continue

        # Pending two-line note: capture text on the line after "### Note:"
        if pending_note:
            pending_note = False
            if s:
                wa_parts.append(("note", _strip_art(s)))
            continue

        if not s:
            continue  # skip blank lines within table

        # GFM separator row (| --- | --- |) — skip, already entered in_table
        if _ISSUES_SEP_RE.match(s):
            continue

        # Already-rendered GFM data row: find_tables() extracted the full table.
        # If no reconstruction has started yet, exit mode and pass the row through,
        # re-emitting the header row that was discarded when entering saw_sep mode.
        if s.startswith("|") and not rows and not cur_key:
            flush(); emit(); mode = None
            if saved_hdr_line:
                result.extend([saved_hdr_line, "| --- | --- |"])
                saved_hdr_line = ""
            result.append(line)
            continue

        # H3 Note header: "### Note:" or "### Note: text"
        m = _ISSUES_NOTE_RE.match(s)
        if m:
            rest = _strip_art(m.group(1))
            if rest:
                wa_parts.append(("note", rest))
            else:
                pending_note = True
            continue

        # Blockquote note already converted by _fix_callouts
        m = _NOTE_BQ_RE.match(s)
        if m:
            wa_parts.append(("note", m.group(1).strip()))
            continue

        # Summary label (### Summary / Summary: / ### Summary: text)
        m = _ISSUES_SUMM_RE.match(s)
        if m:
            rest = _strip_art(m.group(1))
            if cur_key and section == "workaround":
                flush()
            section = "summary"
            if rest:
                (sum_parts if cur_key else pre_sum).append(rest)
            continue

        # Workaround label
        m = _ISSUES_WKRD_RE.match(s)
        if m:
            rest = _strip_art(m.group(1))
            section = "workaround"
            if rest:
                wa_parts.append(("text", rest))
            continue

        # Issue key (KEY-NNN)
        m = _ISSUES_KEY_RE.match(s)
        if m:
            key, rest = m.group(1), _strip_art(m.group(2))
            if cur_key and cur_key != key:
                flush()
            if not cur_key:
                cur_key = key; sum_parts = list(pre_sum); pre_sum = []
            if rest:
                ms = _ISSUES_SUMM_RE.match(rest)
                mw = _ISSUES_WKRD_RE.match(rest)
                if ms:
                    section = "summary"
                    r2 = _strip_art(ms.group(1))
                    if r2: sum_parts.append(r2)
                elif mw:
                    section = "workaround"
                    r2 = _strip_art(mw.group(1))
                    if r2: wa_parts.append(("text", r2))
                else:
                    if section in ("summary", None):
                        section = "summary"; sum_parts.append(rest)
                    else:
                        wa_parts.append(("text", rest))
            continue

        # Ordered list item (numbered workaround step)
        m = _OL_ITEM_RE2.match(s)
        if m and section == "workaround":
            wa_parts.append(("ol_item", m.group(1))); continue

        # Code span continuation (backtick-prefixed) in workaround
        if s.startswith("`") and section == "workaround" and wa_parts:
            last_kind, last_content = wa_parts[-1]
            if last_kind in ("text", "ol_item"):
                wa_parts[-1] = (last_kind, last_content + " " + s)
            else:
                wa_parts.append(("text", s))
            continue

        # Regular body text — strip PDF heading artifacts ("### ButtonName")
        s_clean = re.sub(r"^###\s+", "", _strip_art(s))
        if not s_clean:
            continue
        if section == "summary":
            # Route to pre_sum when no key yet so the text isn't lost when the
            # key is seen and sum_parts is reset to list(pre_sum).
            (sum_parts if cur_key else pre_sum).append(s_clean)
        elif section == "workaround":
            if wa_parts and wa_parts[-1][0] == "ol_item":
                k2, c2 = wa_parts[-1]; wa_parts[-1] = (k2, c2 + " " + s_clean)
            else:
                wa_parts.append(("text", s_clean))
        else:
            section = "summary"
            (sum_parts if cur_key else pre_sum).append(s_clean)

    if mode == "in_table":
        flush(); emit()
    return result


# ── Heading level correction ─────────────────────────────────────────────────

_RELNOTES_TOP_SECTIONS = frozenset([
    "new features",
    "changes in functionality",
    "changes in platform support",
    "changes in platform support and functionality",
    "deprecated features",
    "deprecated and removed features",
    "removed features",
    "migration and compatibility",
    "closed issues",
    "known issues",
    "open issues",
])


def _demote_nested_h1(md_lines: list[str]) -> list[str]:
    """
    Convert H1 sub-headings to H2 when they appear inside a known top-level section.
    E.g. "# Migrating from Release..." under "# Migration and Compatibility" → ## .
    """
    result: list[str] = []
    in_section = False
    for line in md_lines:
        m = re.match(r"^(#+)\s+(.+)", line.rstrip())
        if m and m.group(1) == "#":
            heading_text = m.group(2).strip().lower()
            if heading_text in _RELNOTES_TOP_SECTIONS:
                in_section = True
                result.append(line)
            elif in_section:
                result.append("## " + m.group(2).strip())
            else:
                result.append(line)
        else:
            result.append(line)
    return result


# ── Markdown cleanup ──────────────────────────────────────────────────────────

_STRIP_SECTIONS = re.compile(
    r"^#\s+(TIBCO\s+Documentation\s+and\s+Support\s*Services|Legal\s+and\s+Third[- ]Party\s+Notices)\s*$",
    re.IGNORECASE,
)


def _remove_boilerplate_sections(text: str) -> str:
    """Drop global boilerplate sections (Support Services, Legal Notices) and everything after."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if _STRIP_SECTIONS.match(line.strip()):
            # Drop this heading and everything that follows
            lines = lines[:i]
            break
    return "\n".join(lines)


def _clean_markdown(text: str) -> str:
    """Collapse excess blank lines and strip trailing whitespace."""
    text = _remove_boilerplate_sections(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return text.strip() + "\n"


# ── Release date extraction ───────────────────────────────────────────────────

_MONTH_NAMES = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
}
_MONTH_YEAR_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{4})",
    re.IGNORECASE,
)


def _parse_release_date(text: str) -> str | None:
    m = _MONTH_YEAR_RE.search(text)
    if not m:
        return None
    return f"{m.group(2)}-{_MONTH_NAMES[m.group(1).lower()]}"


def _find_release_date(pdf_path: Path, doc: fitz.Document) -> str | None:
    """Return 'YYYY-MM' release date. Tries Home.htm first; falls back to PDF cover page."""
    from bs4 import BeautifulSoup

    # Primary: ga-date span in Home.htm at the version HTML root
    doc_dir = pdf_path.parent.parent   # .../version/doc/
    for rel in ("html/_templates/Home.htm", "html/Home.htm"):
        home_path = doc_dir / rel
        if home_path.exists():
            try:
                soup = BeautifulSoup(home_path.read_bytes(), "lxml")
                span = soup.select_one("#ga-date span")
                if span:
                    rd = _parse_release_date(span.get_text(strip=True))
                    if rd:
                        return rd
            except Exception:
                pass

    # Fallback: scan PDF cover page (page 0) for a Month YYYY pattern
    if len(doc) > 0:
        cover_text = doc[0].get_text()
        rd = _parse_release_date(cover_text)
        if rd:
            return rd

    return None


# ── PDF discovery ─────────────────────────────────────────────────────────────

_VERSION_RE = re.compile(r"_(\d+\.\d+(?:\.\d+)?)_")


def _parse_pdf_stem(stem: str) -> dict:
    """
    Parse TIB_<product>_<version>_<docname> stem.
    Returns {product_slug, version, doc_name} or {} if not parseable.
    """
    if not stem.startswith("TIB_"):
        return {}
    m = _VERSION_RE.search(stem)
    if not m:
        return {}
    version      = m.group(1)
    product_slug = stem[4: m.start(1) - 1]  # between 'TIB_' and '_version'
    doc_name     = stem.split("_")[-1]
    return {"product_slug": product_slug, "version": version, "doc_name": doc_name}


def discover_pdfs(cache_dir: Path, manifest: list[dict], settings: dict) -> list[dict]:
    """
    Find release-notes PDFs in the cache and return a list of entry dicts.

    Only PDFs whose filename stem contains a pattern from settings.pdf.relnotes_patterns
    (default: ["relnotes", "release-notes"]) are returned.
    """
    relnotes_patterns = settings.get("pdf", {}).get(
        "relnotes_patterns", ["relnotes", "release-notes"]
    )

    # Build manifest lookup: (product_slug_fragment, version) → product_name
    manifest_lookup: dict[tuple[str, str], str] = {}
    for entry in manifest:
        url     = entry.get("url", "")
        version = entry.get("product_version", "")
        name    = entry.get("product_name", "")
        if url and version and name:
            # Use the URL path segment just after the base as the slug fragment
            path_parts = urlparse(url).path.strip("/").split("/")
            if len(path_parts) >= 2:
                manifest_lookup[(path_parts[1].lower(), version)] = name

    # Scope the PDF search to only the version doc-dirs present in this phase's manifest.
    # This avoids re-processing PDFs from every other phase that has ever run.
    phase_pdf_dirs: set[Path] = set()
    for entry in manifest:
        url = entry.get("url", "")
        if not url:
            continue
        path = urlparse(url).path.strip("/")   # e.g. pub/dsp_gridserver/7.2.0/doc/html/f.htm
        parts = path.split("/")
        try:
            doc_idx = parts.index("doc")
            phase_pdf_dirs.add(cache_dir / "/".join(parts[: doc_idx + 1]) / "pdf")
        except ValueError:
            continue

    all_pdfs: list[Path] = []
    for pdf_dir in phase_pdf_dirs:
        if pdf_dir.is_dir():
            all_pdfs.extend(pdf_dir.glob("*.pdf"))

    entries: list[dict] = []
    for pdf_path in sorted(all_pdfs):
        stem = pdf_path.stem
        # Filter: must be a release notes file
        stem_lower = stem.lower()
        if not any(pat.lower() in stem_lower for pat in relnotes_patterns):
            continue

        parsed = _parse_pdf_stem(stem)
        if not parsed:
            continue

        # Derive output path: place under doc/relnotes/ instead of doc/pdf/
        rel = pdf_path.relative_to(cache_dir)
        out_rel = rel.parent.parent / "relnotes" / f"{parsed['doc_name']}.md"

        # Look up canonical product name from manifest
        slug_lower = parsed["product_slug"].lower()
        product_name = ""
        for (path_slug, ver), name in manifest_lookup.items():
            if parsed["version"] == ver and slug_lower in path_slug:
                product_name = name
                break

        entries.append({
            "pdf_path":        pdf_path,
            "output_path":     out_rel,
            "product_slug":    parsed["product_slug"],
            "product_name":    product_name,
            "product_version": parsed["version"],
            "doc_name":        parsed["doc_name"],
        })

    return entries


# ── Frontmatter ───────────────────────────────────────────────────────────────

_DOC_NAME_DISPLAY = {
    "relnotes":     "Release Notes",
    "release-notes": "Release Notes",
    "releasenotes": "Release Notes",
}


def _build_frontmatter(entry: dict) -> str:
    raw_doc_name = entry["doc_name"]
    doc_name = _DOC_NAME_DISPLAY.get(
        raw_doc_name.lower(), raw_doc_name.replace("-", " ").title()
    )

    # Strip trailing version number that manifests sometimes append to product_name
    product_name = re.sub(
        r"\s+" + re.escape(entry["product_version"]) + r"\s*$",
        "",
        entry["product_name"],
    ).strip()

    title = f"{product_name} {entry['product_version']} {doc_name}"

    data = {
        "doc_name":        doc_name,
        "product_name":    product_name,
        "product_version": entry["product_version"],
        "release_date":    entry.get("release_date", ""),
        "title":           title,
    }
    data = {k: v for k, v in data.items() if v}
    return "---\n" + yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=True) + "---\n\n"


# ── Per-file conversion ───────────────────────────────────────────────────────

def convert_pdf(
    entry: dict,
    output_dir: Path,
    reporter: Reporter,
    dry_run: bool,
    force_rerun: bool,
) -> bool:
    """Convert one release notes PDF to Markdown. Returns True on success."""
    pdf_path = entry["pdf_path"]
    out_path = output_dir / entry["output_path"]

    if out_path.exists() and not dry_run and not force_rerun:
        reporter.count("pdfs_already_done")
        return True

    try:
        doc = fitz.open(str(pdf_path))
    except Exception as exc:
        reporter.fail(str(pdf_path), f"Cannot open PDF: {exc}")
        return False

    if doc.is_encrypted:
        reporter.fail(str(pdf_path), "PDF is encrypted")
        doc.close()
        return False

    try:
        body_size = calibrate_body_size(doc)
        reporter.count(f"body_size:{body_size}")

        repeated_h3_texts = collect_repeated_h3_texts(doc)

        md_lines: list[str] = []
        for page_idx, page in enumerate(doc):
            # Skip cover page (title, logo, version — not body content)
            if page_idx == 0:
                reporter.count("pages_cover_skipped")
                continue

            page_lines = _convert_page(page, body_size, repeated_h3_texts)

            # Skip blank pages
            if not page_lines:
                continue

            # Skip TOC pages
            if _is_toc_page(page_lines):
                reporter.count("pages_toc_skipped")
                continue

            # Insert blank line between pages, but not when a bullet list continues
            # across a page break (which would split the list into separate blocks).
            if md_lines:
                last_content  = next((l for l in reversed(md_lines) if l.strip()), "")
                first_content = next((l for l in page_lines if l.strip()), "")
                if not (_LIST_RE.match(last_content) and _LIST_RE.match(first_content)):
                    md_lines.append("")

            md_lines.extend(page_lines)

        # Resolve release date while doc is still open (fallback reads PDF page 0)
        release_date = _find_release_date(entry["pdf_path"], doc)
        doc.close()

        if not md_lines:
            reporter.fail(str(pdf_path), "No content extracted from PDF")
            return False

        # Flatten any multi-line elements (e.g. table header+separator in one PDF block)
        flat_lines: list[str] = []
        for l in md_lines:
            flat_lines.extend(l.splitlines())

        processed = _fix_issue_tables(_fix_callouts(_fix_platform_table(_fix_table_rows(flat_lines))))
        processed = _demote_nested_h1(processed)
        body = _clean_markdown("\n".join(processed))

        if release_date:
            entry = {**entry, "release_date": release_date}

        frontmatter = _build_frontmatter(entry)
        final_content = frontmatter + body

        if not dry_run:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(final_content, encoding="utf-8")

        reporter.count("pdfs_converted")
        return True

    except Exception as exc:
        reporter.fail(str(pdf_path), f"{type(exc).__name__}: {exc}")
        try:
            doc.close()
        except Exception:
            pass
        return False


# ── CLI ───────────────────────────────────────────────────────────────────────

def load_settings(config_path: str) -> dict:
    return yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))


def load_manifest(phase: str, settings: dict) -> list[dict]:
    manifests_dir = Path(settings.get("manifests_dir", "manifests"))
    path = manifests_dir / f"manifest_{phase}.json"
    if not path.exists():
        # Manifest not required — proceed with empty (product_name enrichment skipped)
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(
        description="Convert release notes PDFs from cache to Markdown"
    )
    parser.add_argument("--phase",       required=True)
    parser.add_argument("--config",      default="config/settings.yaml")
    parser.add_argument("--dry-run",     action="store_true")
    parser.add_argument("--force-rerun", action="store_true")
    args = parser.parse_args()

    settings   = load_settings(args.config)
    manifest   = load_manifest(args.phase, settings)
    cache_dir  = Path(settings.get("cache_dir", "cache"))
    output_dir = Path(settings.get("output_dir", "output"))

    from datetime import datetime
    logs_dir = Path(settings.get("logs_dir", "logs"))
    run_dir  = logs_dir / args.phase / datetime.now().strftime("%Y%m%d-%H%M%S")
    reporter = Reporter(run_dir, "pdf_convert", dry_run=args.dry_run)

    reporter.info(
        f"=== PDF Relnotes Convert | phase={args.phase} "
        f"dry_run={args.dry_run} force_rerun={args.force_rerun} ==="
    )

    pdf_entries = discover_pdfs(cache_dir, manifest, settings)
    reporter.info(f"Found {len(pdf_entries)} release notes PDF(s) in cache")
    for e in pdf_entries:
        reporter.info(f"  {e['pdf_path'].name} → {e['output_path']}")

    if not pdf_entries:
        reporter.info("Nothing to convert.")
        reporter.finish()
        return 0

    failed = 0
    for entry in tqdm(pdf_entries, desc="Converting PDFs"):
        ok = convert_pdf(entry, output_dir, reporter, args.dry_run, args.force_rerun)
        if not ok:
            failed += 1

    report = reporter.finish()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
