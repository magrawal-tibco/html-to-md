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


# ── Markdown cleanup ──────────────────────────────────────────────────────────

def _clean_markdown(text: str) -> str:
    """Collapse excess blank lines and strip trailing whitespace."""
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return text.strip() + "\n"


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

    entries: list[dict] = []
    for pdf_path in sorted(cache_dir.glob("**/doc/pdf/*.pdf")):
        stem = pdf_path.stem
        # Filter: must be a release notes file
        stem_lower = stem.lower()
        if not any(pat.lower() in stem_lower for pat in relnotes_patterns):
            continue

        parsed = _parse_pdf_stem(stem)
        if not parsed:
            continue

        # Derive output path: mirror cache path with doc_name.md
        rel = pdf_path.relative_to(cache_dir)
        out_rel = rel.parent / f"{parsed['doc_name']}.md"

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

def _build_frontmatter(entry: dict) -> str:
    doc_name = entry["doc_name"].replace("-", " ").title()
    data = {
        "title":           doc_name,
        "source_pdf":      str(entry["pdf_path"]).replace("\\", "/"),
        "product_name":    entry["product_name"],
        "product_version": entry["product_version"],
        "doc_name":        entry["doc_name"],
    }
    data = {k: v for k, v in data.items() if v}
    return "---\n" + yaml.dump(data, allow_unicode=True, default_flow_style=False) + "---\n\n"


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

        doc.close()

        if not md_lines:
            reporter.fail(str(pdf_path), "No content extracted from PDF")
            return False

        body = _clean_markdown("\n".join(md_lines))
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
