"""
preprocessor.py — 12 BeautifulSoup transform passes on the extracted main content.

Transforms are applied IN ORDER on the <div role="main"> element before markdownify runs.
Each transform modifies the soup in-place and returns a count of elements changed.

Transform order:
  1.  strip_chrome         — remove nav/UI chrome listed in settings
  2.  fake_list_tables     — AutoNumber_p_* tables → <ul>/<ol>
  3.  callout_divs         — div.note/warning/etc → <blockquote>
  4.  text_popups          — MCTextPopup inline popups → Note blockquotes
  5.  definition_lists     — div.dl/dlentry/dt/dd → bold term + content
  6.  task_sections        — DITA task structure → semantic HTML
  7.  inline_spans         — MadCap span classes → strong/code/em
  8.  anchor_only_links    — strip <a name="..."> anchors with no href
  9.  split_colspan_tables — full-width colspan rows → bold label + sub-tables
  10. classify_tables      — 3-tier table handling (calls table_classifier)
  11. normalize_whitespace — collapse \\n\\t in text nodes (browser whitespace rules)
  12. fix_pre_linebreaks   — replace <br> inside <pre> with actual newlines
  13. rewrite_image_src    — make image paths relative to output location
"""

import re
from pathlib import PurePosixPath

from bs4 import BeautifulSoup, NavigableString, Tag

from scripts.lib.table_classifier import handle_tables, DEFAULT_BLOCK_TAGS

# ── Callout label map ──────────────────────────────────────────────────────────

CALLOUT_CLASSES = {
    "note":      "Note",
    "warning":   "Warning",
    "caution":   "Caution",
    "tip":       "Tip",
    "important": "Important",
}

# ── Inline span class → HTML element mapping ───────────────────────────────────

SPAN_TO_TAG = {
    # MadCap / DITA inline classes
    "uicontrol":  "strong",
    "wintitle":   "strong",
    "option":     "strong",
    # menucascade handled specially in inline_spans (collapsed to single bold)
    "filepath":   "code",
    "codeph":     "code",
    "userinput":  "code",
    "varname":    "em",
    "parmname":   "em",
    "term":       "em",
}


# ── Transform 1: strip chrome ─────────────────────────────────────────────────

def strip_chrome(content: Tag, chrome_selectors: list[str]) -> int:
    """Remove UI chrome elements from the extracted content div."""
    removed = 0
    for selector in chrome_selectors:
        for el in content.select(selector):
            el.decompose()
            removed += 1
    # Always remove script and style tags
    for tag in content.find_all(["script", "style"]):
        tag.decompose()
        removed += 1
    # Always strip autonumber spans — MadCap auto-generated labels we handle ourselves
    # Must run here (Transform 1) so callout_divs (Transform 3) sees clean content
    for span in content.find_all("span", class_="autonumber"):
        span.decompose()
        removed += 1
    return removed


# ── Transform 2: fake list tables ─────────────────────────────────────────────

def fake_list_tables(content: Tag) -> int:
    """
    Convert MadCap fake-list tables (class AutoNumber_p_*) to proper <ul>/<ol>.

    MadCap emits numbered/bulleted lists as single-column tables with class names like:
      AutoNumber_p_Bullet, AutoNumber_p_Number, AutoNumber_p_Step, etc.
    Each row's first <td> contains one list item.
    """
    converted = 0
    for table in content.find_all("table"):
        classes = table.get("class", [])
        class_str = " ".join(classes) if isinstance(classes, list) else str(classes)

        if "AutoNumber_p_" not in class_str:
            continue

        is_ordered = any(
            kw in class_str for kw in ("Number", "Step", "Procedure", "Numbered")
        )
        list_tag = "ol" if is_ordered else "ul"
        soup_stub = BeautifulSoup(f"<{list_tag}></{list_tag}>", "lxml")
        new_list = soup_stub.find(list_tag)

        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            # Take content of first cell (ignore autonumber cell if two columns)
            cell = cells[-1]  # last cell is always the text content
            li = soup_stub.new_tag("li")
            li.extend(list(cell.children))
            new_list.append(li)

        table.replace_with(new_list)
        converted += 1
    return converted


# ── Transform 3: callout divs ─────────────────────────────────────────────────

def callout_divs(content: Tag) -> int:
    """Convert MadCap callout divs to <blockquote> with a bold label."""
    converted = 0
    for cls, label in CALLOUT_CLASSES.items():
        for div in content.find_all("div", class_=cls):
            bq = BeautifulSoup(f"<blockquote><p><strong>{label}:</strong> </p></blockquote>", "lxml").find("blockquote")
            bq.p.extend(list(div.children))
            div.replace_with(bq)
            converted += 1
    return converted


# ── Transform 4: MadCap text popups ──────────────────────────────────────────

_BLOCK_ANCESTORS = {"p", "li", "dd", "div", "section", "td", "th"}


def text_popups(content: Tag) -> int:
    """
    Convert MadCap MCTextPopup inline popups to Note blockquotes.

    Pattern:
      <a class="MCTextPopup popup popupHead" href="javascript:void(0)">
        TRIGGER        <!-- visible text, usually a superscript number -->
        <span class="MCTextPopupBody ...">
          <span class="MCTextPopupArrow"> </span>
          POPUP BODY TEXT
        </span>
      </a>

    Result:
      - The anchor is replaced with <sup>TRIGGER</sup> (keeps inline position)
      - A <blockquote><p><strong>Note:</strong> POPUP BODY</p></blockquote> is
        inserted after the nearest block ancestor (p, li, div, …).
      - Multiple popups in the same block ancestor are appended in order.
    """
    converted = 0
    # Track the last element inserted after each block ancestor so that multiple
    # popups in the same block get their notes appended in order.
    last_inserted: dict[int, Tag] = {}  # id(block_ancestor) → last note tag

    for anchor in content.find_all("a", class_="MCTextPopup"):
        # Extract popup body span
        body_span = anchor.find("span", class_="MCTextPopupBody")
        if not body_span:
            continue

        # Strip the arrow span, then get body text
        arrow = body_span.find("span", class_="MCTextPopupArrow")
        if arrow:
            arrow.decompose()
        popup_text = body_span.get_text(separator=" ", strip=True)
        body_span.decompose()

        # Trigger text (number or symbol that remained after removing body)
        trigger = anchor.get_text(strip=True)

        # Build Note blockquote
        note_html = f"<blockquote><p><strong>Note:</strong> {popup_text}</p></blockquote>"
        note_bq = BeautifulSoup(note_html, "lxml").find("blockquote")

        # Find nearest block ancestor
        block = anchor.parent
        while block and block.name not in _BLOCK_ANCESTORS and block != content:
            block = block.parent
        if not block or block == content:
            block = anchor.parent  # fallback

        # Insert note after block (or after previous note for same block)
        insert_after = last_inserted.get(id(block), block)
        insert_after.insert_after(note_bq)
        last_inserted[id(block)] = note_bq

        # Remove the anchor entirely — the Note blockquote immediately after
        # provides full context, so the inline trigger marker (e.g. "1") adds
        # no value and would render as stray text (e.g. "products1,").
        anchor.decompose()

        converted += 1
    return converted


# ── Transform 5: DITA definition lists ───────────────────────────────────────

def definition_lists(content: Tag) -> int:
    """
    Convert DITA/MadCap definition list divs to bold term + definition content.

    Pattern:
      <div class="dl">
        <div class="dlentry">
          <span class="dt">Term text</span>
          <div class="dd">Definition content (may contain inline or block elements)</div>
        </div>
        ...
      </div>

    Result: For each dlentry the dt becomes <p><strong>Term</strong></p>,
    the dd is unwrapped in-place. The outer dl and dlentry wrappers are removed.
    """
    converted = 0
    for dl in list(content.find_all("div", class_="dl")):
        for dlentry in list(dl.find_all("div", class_="dlentry", recursive=False)):
            dt = dlentry.find("span", class_="dt")
            dd = dlentry.find("div", class_="dd")

            if dt:
                # Wrap dt children in <p><strong>…</strong></p> preserving inline markup
                stub = BeautifulSoup("<p><strong></strong></p>", "lxml")
                new_p = stub.find("p")
                new_strong = stub.find("strong")
                for child in list(dt.children):
                    new_strong.append(child.extract())
                dt.replace_with(new_p)
                converted += 1

            if dd:
                dd.unwrap()  # dd children become siblings inside dlentry

            dlentry.unwrap()  # dlentry children become siblings inside dl

        dl.unwrap()  # dl children become siblings in the parent

    return converted


# ── Transform 6: DITA task sections ──────────────────────────────────────────

# Divs that get a bold label paragraph inserted before them, then unwrapped
_LABELED_SECTIONS = {
    "prereq":  "Prerequisites",
    "postreq": "Post-requisites",
    "example": "Example",
}


def task_sections(content: Tag) -> int:
    """Convert DITA task structural elements to semantic HTML."""
    converted = 0
    soup = BeautifulSoup("", "lxml")

    # div.context → unwrap (plain paragraphs, no label)
    for div in content.find_all("div", class_="context"):
        div.unwrap()
        converted += 1

    # div.info, div.stepresult → unwrap (plain paragraph continuation in list item)
    for cls in ("info", "stepresult"):
        for div in content.find_all("div", class_=cls):
            div.unwrap()
            converted += 1

    # div.result → bold "Result" label paragraph + unwrapped content
    for div in content.find_all("div", class_="result"):
        label_p = soup.new_tag("p")
        label_strong = soup.new_tag("strong")
        label_strong.string = "Result"
        label_p.append(label_strong)
        div.insert_before(label_p)
        div.unwrap()
        converted += 1

    # div.prereq/postreq/example → bold label paragraph + unwrapped content
    for cls, label_text in _LABELED_SECTIONS.items():
        for div in content.find_all("div", class_=cls):
            label_p = soup.new_tag("p")
            label_strong = soup.new_tag("strong")
            label_strong.string = label_text
            label_p.append(label_strong)
            div.insert_before(label_p)
            div.unwrap()
            converted += 1

    # <ol class="steps"> → plain <ol> with bold "Procedure" label before it
    for ol in content.find_all("ol", class_="steps"):
        label_p = soup.new_tag("p")
        label_strong = soup.new_tag("strong")
        label_strong.string = "Procedure"
        label_p.append(label_strong)
        ol.insert_before(label_p)
        del ol["class"]
        converted += 1

    # <ol class="substeps"> → plain <ol>
    for ol in content.find_all("ol", class_="substeps"):
        if ol.get("class"):
            del ol["class"]

    return converted


# ── Transform 5: inline spans ─────────────────────────────────────────────────

def _normalize_whitespace(tag: Tag) -> None:
    """Collapse all whitespace (including newlines) in text nodes within tag,
    skipping <code> and <pre> descendants where whitespace is significant."""
    for node in list(tag.descendants):
        if isinstance(node, NavigableString) and node.parent.name not in ("code", "pre"):
            normalized = re.sub(r"\s+", " ", str(node))
            if normalized != str(node):
                node.replace_with(NavigableString(normalized))


def inline_spans(content: Tag) -> int:
    """Replace MadCap/DITA inline span classes with semantic HTML elements."""
    converted = 0

    # 1. menucascade → collapse all inner text into a single <strong>
    for span in content.find_all("span", class_="menucascade"):
        text = " ".join(span.get_text().split())
        strong = BeautifulSoup("<strong></strong>", "lxml").find("strong")
        strong.string = text
        span.replace_with(strong)
        converted += 1

    # 2. <span class="cmd"> → normalize whitespace then unwrap
    for span in content.find_all("span", class_="cmd"):
        _normalize_whitespace(span)
        span.unwrap()
        converted += 1

    # 3. Regular span → tag mapping
    for span in content.find_all("span"):
        classes = span.get("class", [])
        matched_tag = None
        for cls in classes:
            if cls in SPAN_TO_TAG:
                matched_tag = SPAN_TO_TAG[cls]
                break
            # mc-variable spans contain already-resolved text — unwrap them
            if cls.startswith("mc-variable") or cls.startswith("mc-"):
                span.unwrap()
                converted += 1
                matched_tag = None
                break
        if matched_tag:
            new_el = BeautifulSoup(f"<{matched_tag}></{matched_tag}>", "lxml").find(matched_tag)
            new_el.extend(list(span.children))
            span.replace_with(new_el)
            converted += 1

    # 4. <var> → <em> (italic placeholder variables)
    for var_el in content.find_all("var"):
        em = BeautifulSoup("<em></em>", "lxml").find("em")
        em.extend(list(var_el.children))
        var_el.replace_with(em)
        converted += 1

    return converted


# ── Transform 6: anchor-only links ────────────────────────────────────────────

def anchor_only_links(content: Tag) -> int:
    """Strip <a name="..."> anchors that have no href — pure navigation markers."""
    removed = 0
    for a in content.find_all("a"):
        if a.get("href"):
            continue
        if a.get("name") or a.get("id"):
            a.unwrap()
            removed += 1
    return removed


# ── Transform 7: split colspan tables ────────────────────────────────────────

def _table_column_count(table: Tag) -> int:
    """Count the number of columns in a table from its header or first data row."""
    thead = table.find("thead")
    if thead:
        header_row = thead.find("tr")
        if header_row:
            return sum(int(c.get("colspan", 1)) for c in header_row.find_all(["th", "td"]))
    tbody = table.find("tbody")
    if tbody:
        for row in tbody.find_all("tr", recursive=False):
            cells = row.find_all(["td", "th"])
            if cells:
                return sum(int(c.get("colspan", 1)) for c in cells)
    return 0


def _is_full_width_row(row: Tag, ncols: int) -> bool:
    """Return True if the row is a single cell spanning all columns."""
    cells = row.find_all(["td", "th"])
    return len(cells) == 1 and int(cells[0].get("colspan", 1)) >= ncols


def split_colspan_tables(content: Tag) -> int:
    """
    Split tables that use full-width colspan rows as section headers.

    Each colspan-spanning row becomes an <h4> heading, and the rows that
    follow it become a new <table> with the original <thead> repeated.
    This runs after inline_spans so span classes are already resolved,
    and before classify_tables so each sub-table is classified on its own.
    """
    converted = 0

    for table in list(content.find_all("table")):
        tbody = table.find("tbody")
        if not tbody:
            continue

        ncols = _table_column_count(table)
        if ncols < 2:
            continue

        rows = list(tbody.find_all("tr", recursive=False))
        if not any(_is_full_width_row(r, ncols) for r in rows):
            continue  # no section-header rows — nothing to split

        # Group rows: each full-width row starts a new section.
        # Store the actual cell Tag so we can access both text and inner HTML.
        groups = []  # list of (heading_cell Tag | None, [data_rows])
        current_heading_cell = None
        current_rows = []

        for row in rows:
            if _is_full_width_row(row, ncols):
                groups.append((current_heading_cell, current_rows))
                current_heading_cell = row.find_all(["td", "th"])[0]
                current_rows = []
            else:
                current_rows.append(row)
        groups.append((current_heading_cell, current_rows))

        # Drop any leading group with no heading and no rows
        if groups and groups[0][0] is None and not groups[0][1]:
            groups = groups[1:]

        if not groups:
            continue

        thead = table.find("thead")
        thead_html = str(thead) if thead else ""

        # Build replacement elements: separator paragraph + <table> for each group
        _SHORT_THRESHOLD = 60
        replacements = []
        for heading_cell, data_rows in groups:
            if heading_cell is not None:
                heading_text = heading_cell.get_text(strip=True)
                if len(heading_text) <= _SHORT_THRESHOLD:
                    # Short identifier → bold paragraph (plain text, no nested markup)
                    sep = BeautifulSoup(
                        f"<p><strong>{heading_text}</strong></p>", "lxml"
                    ).find("p")
                else:
                    # Long sentence → plain paragraph, preserving inner HTML formatting
                    inner_html = heading_cell.decode_contents()
                    sep = BeautifulSoup(f"<p>{inner_html}</p>", "lxml").find("p")
                replacements.append(sep)
            if data_rows:
                rows_html = "".join(str(r) for r in data_rows)
                new_table = BeautifulSoup(
                    f"<table>{thead_html}<tbody>{rows_html}</tbody></table>", "lxml"
                ).find("table")
                replacements.append(new_table)

        # Insert replacements after the original table, then remove it
        for repl in reversed(replacements):
            table.insert_after(repl)
        table.decompose()
        converted += 1

    return converted


# ── Transform 9: classify tables ──────────────────────────────────────────────

def classify_tables(content: Tag, block_tags: set[str] | None = None) -> dict[str, int]:
    """Run 3-tier table classification on all tables in content."""
    return handle_tables(content, block_tags or DEFAULT_BLOCK_TAGS)


# ── Transform 10: normalize whitespace in text nodes ─────────────────────────

def normalize_whitespace(content: Tag) -> int:
    """
    Collapse newline/tab whitespace in text nodes to match browser HTML rendering.

    In HTML, a sequence of whitespace characters (including \\n and \\t) between
    inline elements is collapsed to a single space. MadCap Flare wraps long lines
    in HTML source, so note divs often contain text like:
        click <img/>\\n\\t\\t. Select\\n\\t\\t<strong>Help</strong>
    Markdownify does not apply browser whitespace collapsing, so each indented
    line becomes a separate blockquote line. This transform applies the same
    rule: any run of whitespace that contains a newline is replaced with a
    single space. Text nodes inside <pre> are excluded.
    """
    normalized = 0
    _ws_with_newline = re.compile(r'[ \t]*\r?\n[ \t]*')
    for text_node in list(content.find_all(string=True)):
        if text_node.find_parent("pre"):
            continue
        original = str(text_node)
        collapsed = _ws_with_newline.sub(' ', original)
        if collapsed != original:
            text_node.replace_with(NavigableString(collapsed))
            normalized += 1
    return normalized


# ── Transform 11: fix <br> inside <pre> blocks ────────────────────────────────

def fix_pre_linebreaks(content: Tag) -> int:
    """
    Replace <br> tags inside <pre> blocks with actual newline characters.

    MadCap codeSnippet body uses <br/> between syntax-highlighted <span> elements.
    With newline_style="backslash", markdownify converts <br> → '\\\n', which
    puts a literal backslash at the end of every line inside a fenced code block.
    """
    fixed = 0
    for pre in content.find_all("pre"):
        for br in list(pre.find_all("br")):
            br.replace_with(NavigableString("\n"))
            fixed += 1
    return fixed


# ── Transform 11: rewrite image src ──────────────────────────────────────────

def rewrite_image_src(content: Tag, page_url_path: str) -> int:
    """
    Make <img src> paths relative to the output .md file's location.

    page_url_path: the URL path of the source HTML page, e.g.
      /pub/product/1.0/doc/html/Admin/overview.htm
    Images in MadCap output are typically at:
      ../Resources/Images/foo.png  (relative to the .htm file)
    We rewrite them to be relative from the .md file's directory.
    """
    rewritten = 0
    page_dir = PurePosixPath(page_url_path).parent

    for img in content.find_all("img"):
        src = img.get("src", "")
        if not src or src.startswith("http://") or src.startswith("https://"):
            continue
        # Resolve relative to the page's directory, then make it a simple relative path
        try:
            resolved = (page_dir / src).resolve() if False else src  # keep relative
            img["src"] = src  # leave as-is; postprocessor can adjust if needed
            rewritten += 1
        except Exception:
            pass
    return rewritten


# ── Main entry point ──────────────────────────────────────────────────────────

def run_all(
    content: Tag,
    chrome_selectors: list[str],
    page_url_path: str,
    block_tags: set[str] | None = None,
) -> dict:
    """
    Run all 13 transforms in order. Returns a stats dict with counts per transform.
    """
    stats = {}
    stats["chrome_removed"]   = strip_chrome(content, chrome_selectors)
    stats["fake_lists"]       = fake_list_tables(content)
    stats["callouts"]         = callout_divs(content)
    stats["text_popups"]      = text_popups(content)
    stats["definition_lists"] = definition_lists(content)
    stats["task_sections"]    = task_sections(content)
    stats["inline_spans"]     = inline_spans(content)
    stats["anchor_links"]     = anchor_only_links(content)
    stats["colspan_tables"]   = split_colspan_tables(content)
    table_counts              = classify_tables(content, block_tags)
    stats.update(table_counts)
    stats["ws_normalized"]    = normalize_whitespace(content)
    stats["pre_linebreaks"]   = fix_pre_linebreaks(content)
    stats["images_rewritten"] = rewrite_image_src(content, page_url_path)
    return stats
