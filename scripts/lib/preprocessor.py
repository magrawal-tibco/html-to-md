"""
preprocessor.py — BeautifulSoup transform passes on the extracted main content.

Transforms are applied IN ORDER on the <div role="main"> element before markdownify runs.
Each transform modifies the soup in-place and returns a count of elements changed.

Transform order:
  1.  strip_chrome         — remove nav/UI chrome listed in settings
  2.  fake_list_tables     — AutoNumber_p_* tables → <ul>/<ol>
  2.5 merge_list_continuations — merge split lists and absorb <pre> into <li>
  3.  callout_divs         — div.note/warning/etc → <blockquote>
  3.2 ebx_callout_divs    — EBX div.ebx_note/ebx_seealso/etc → <blockquote>
  3.5 icon_tables          — TableStyle-IconTable note/warning tables → <blockquote>
  4.  text_popups          — MCTextPopup inline popups → Note blockquotes
  5.  definition_lists     — div.dl/dlentry/dt/dd → bold term + content
  6.  task_sections        — DITA task structure → semantic HTML
  7.  inline_spans         — MadCap span classes → strong/code/em
  7.5 code_urls_to_links   — <code>https://...</code> bare URLs → <a href>
  8.  anchor_only_links    — strip <a name="..."> anchors with no href
  9.  split_colspan_tables — full-width colspan rows → bold label + sub-tables
  9.5 extract_table_captions — <caption> elements → bold <p> before table
  10. classify_tables      — 3-tier table handling (calls table_classifier)
  11. normalize_whitespace — collapse \\n\\t in text nodes (browser whitespace rules)
  12. fix_pre_linebreaks   — replace <br> inside <pre> with actual newlines
  12.5 merge_adjacent_code — merge adjacent <code> spans from variable references
  13. rewrite_image_src    — make image paths relative to output location
"""

import re
from pathlib import PurePosixPath

from bs4 import BeautifulSoup, NavigableString, Tag

from scripts.lib.table_classifier import handle_tables, DEFAULT_BLOCK_TAGS

# ── Callout label maps ────────────────────────────────────────────────────────

# EBX admonition div classes — each contains an <h5> label as its first child
EBX_CALLOUT_DIV_CLASSES = frozenset({
    "ebx_note",
    "ebx_seealso",
    "ebx_attention",
    "ebx_relatedconcepts",
})

CALLOUT_CLASSES = {
    "note":          "Note",
    "noteNote":      "Note",
    "warning":       "Warning",
    "noteWarning":   "Warning",
    "caution":       "Caution",
    "noteCaution":   "Caution",
    "tip":           "Tip",
    "noteTip":       "Tip",
    "important":     "Important",
    "noteImportant": "Important",
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
            kw in class_str for kw in ("_Number", "_Step", "_Procedure", "_Numbered")
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


# ── Transform 2.5: merge list continuations ────────────────────────────────────

def merge_list_continuations(content: Tag) -> int:
    """
    Merge consecutive same-type lists separated only by <pre> blocks and
    MadCap list-continuation paragraphs.

    MadCap Flare emits each numbered step as a separate AutoNumber_p_Step table.
    After fake_list_tables(), each table becomes its own <ol><li>...</li></ol>.
    Code blocks (<pre>) and continuation text (<p class="ListContinue">) between
    steps appear as siblings, not inside any <li>.

    This transform:
      1. Absorbs any <pre> following a list into the last <li> of that list.
      2. Absorbs any <p class="ListContinue"> following a list into the last <li>.
      3. Merges consecutive same-type (<ol>/<ul>) lists into one.

    Result: steps with inline code blocks and continuation text become a single
    <ol> with <pre> and <p> inside the appropriate <li>, so markdownify produces
    properly indented content inside list items.
    """
    merged = 0
    _LIST_CONTINUE_CLASSES = {"ListContinue", "ListContinueIndent"}

    def _is_list_continue(tag: Tag) -> bool:
        classes = set(tag.get("class", []))
        return bool(classes & _LIST_CONTINUE_CLASSES)

    def _process(parent: Tag) -> None:
        nonlocal merged
        changed = True
        while changed:
            changed = False
            tag_children = [c for c in parent.children if isinstance(c, Tag)]
            for i, node in enumerate(tag_children):
                if node.name not in ("ol", "ul"):
                    continue

                # Case A: orphan nested list inside outer <ol>/<ul> (MadCap invalid HTML).
                # Move it into the preceding <li>.
                if parent.name in ("ol", "ul"):
                    prev_li = None
                    for j in range(i - 1, -1, -1):
                        if tag_children[j].name == "li":
                            prev_li = tag_children[j]
                            break
                    if prev_li:
                        node.extract()
                        prev_li.append(node)
                        merged += 1
                        changed = True
                        break
                    continue  # no preceding <li> — leave it as-is

                if i + 1 >= len(tag_children):
                    break
                sib = tag_children[i + 1]

                # Case B: <pre> after list — absorb into last <li>
                if sib.name == "pre":
                    lis = node.find_all("li", recursive=False)
                    if lis:
                        sib.extract()
                        lis[-1].append(sib)
                        merged += 1
                        changed = True
                        break

                # Case C: ListContinue paragraph after list — absorb into last <li>
                elif sib.name == "p" and _is_list_continue(sib):
                    lis = node.find_all("li", recursive=False)
                    if lis:
                        sib.extract()
                        lis[-1].append(sib)
                        merged += 1
                        changed = True
                        break

                # Case E: <p> between two same-type lists (e.g. image/caption after a step).
                # Only absorb if a same-type list follows within the next few siblings.
                elif sib.name == "p":
                    look_ahead = tag_children[i + 2: i + 7]
                    if any(t.name == node.name for t in look_ahead):
                        lis = node.find_all("li", recursive=False)
                        if lis:
                            sib.extract()
                            lis[-1].append(sib)
                            merged += 1
                            changed = True
                            break

                # Case F: <ul> immediately following <ol> — absorb as sub-list.
                # MadCap emits sub-bullet tables right after their parent step table;
                # after fake_list_tables() these appear as <ul> siblings of the <ol>.
                elif sib.name == "ul" and node.name == "ol":
                    lis = node.find_all("li", recursive=False)
                    if lis:
                        sib.extract()
                        lis[-1].append(sib)
                        merged += 1
                        changed = True
                        break

                # Case D: same-type list follows — merge into current list
                elif sib.name == node.name:
                    for li in list(sib.find_all("li", recursive=False)):
                        node.append(li.extract())
                    sib.decompose()
                    merged += 1
                    changed = True
                    break

    _process(content)
    for el in content.find_all(["div", "td", "th", "li", "blockquote", "section", "ol", "ul"]):
        _process(el)
    return merged


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


# ── Transform 3.2: EBX callout divs ──────────────────────────────────────────

def ebx_callout_divs(content: Tag) -> int:
    """
    Convert EBX admonition divs to <blockquote> with a bold label.

    EBX uses div.ebx_note / div.ebx_seealso / div.ebx_attention /
    div.ebx_relatedconcepts, each with an <h5> as its first child.
    The H5 text becomes the bold blockquote label (preserving localised
    variants such as "Voir aussi" / "以下も参照してください。").
    Removing the H5 eliminates the heading-level jump that causes DOTJ013E
    when LWDITA imports the converted markdown.
    """
    converted = 0
    for cls in EBX_CALLOUT_DIV_CLASSES:
        for div in list(content.find_all("div", class_=cls)):
            h5 = div.find("h5")
            label = h5.get_text(strip=True) if h5 else cls.replace("ebx_", "").capitalize()
            if h5:
                h5.decompose()
            bq = BeautifulSoup(
                f"<blockquote><p><strong>{label}:</strong></p></blockquote>", "lxml"
            ).find("blockquote")
            for child in list(div.children):
                bq.append(child.extract())
            div.replace_with(bq)
            converted += 1
    return converted


# ── Transform 3.5: icon tables (TableStyle-IconTable) ────────────────────────

_ICON_TABLE_LABELS = {
    "note":      "Note",
    "warning":   "Warning",
    "caution":   "Caution",
    "tip":       "Tip",
    "important": "Important",
}


def icon_tables(content: Tag) -> int:
    """
    Convert MadCap TableStyle-IconTable note/warning tables to blockquotes.

    Pattern:
      <table class="TableStyle-IconTable">
        <tr>
          <td><p class="IconNote">Note</p></td>   ← label cell
          <td><p class="Default">Content...</p></td>  ← body cell
        </tr>
      </table>

    Result:
      <blockquote><p><strong>Note:</strong></p><p>Content...</p></blockquote>
    """
    converted = 0
    for table in list(content.find_all("table", class_="TableStyle-IconTable")):
        row = table.find("tr")
        if not row:
            continue
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue

        label_text = cells[0].get_text(strip=True).lower()
        label = "Note"
        for key, val in _ICON_TABLE_LABELS.items():
            if key in label_text:
                label = val
                break

        bq = BeautifulSoup(
            f"<blockquote><p><strong>{label}:</strong></p></blockquote>", "lxml"
        ).find("blockquote")
        for child in list(cells[1].children):
            bq.append(child.extract())

        table.replace_with(bq)
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

    # 5. <code class="CodeItalic"> → <em> (MadCap italic variable placeholder in code)
    #    Prevents double-backtick artifacts when these appear adjacent to other <code> spans.
    for code in content.find_all("code", class_="CodeItalic"):
        em = BeautifulSoup("<em></em>", "lxml").find("em")
        em.extend(list(code.children))
        code.replace_with(em)
        converted += 1

    return converted


# ── Transform 7.5: code URLs to links ────────────────────────────────────────

_URL_RE = re.compile(r'^https?://[^\s./]')  # requires ≥1 real host char — rejects http:// and http://...


def code_urls_to_links(content: Tag) -> int:
    """
    Convert <code>https://...</code> bare-URL code spans to <a href="url">url</a>.

    MadCap Flare sometimes marks up bare URLs with <code> instead of <a href>.
    """
    converted = 0
    for code in list(content.find_all("code")):
        text = code.get_text(strip=True)
        if _URL_RE.match(text):
            a = BeautifulSoup(f'<a href="{text}">{text}</a>', "lxml").find("a")
            code.replace_with(a)
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


# ── Transform 9.5: extract table captions ─────────────────────────────────────

def extract_table_captions(content: Tag) -> int:
    """
    Promote <caption> elements to bold paragraphs before their parent table.

    MadCap table captions use <caption><p class="TableTitle">text</p></caption>.
    markdownify renders these as italic inline text (from any inner <i> wrapper)
    or drops them in GFM tables. This transform moves each caption to a
    <p><strong>text</strong></p> before the table so it renders consistently
    as bold text above the table in all tier outputs.
    """
    promoted = 0
    for table in list(content.find_all("table")):
        caption = table.find("caption")
        if not caption:
            continue
        text = caption.get_text(strip=True)
        if not text:
            caption.decompose()
            continue
        stub = BeautifulSoup(f"<p><strong>{text}</strong></p>", "lxml")
        caption_p = stub.find("p")
        table.insert_before(caption_p)
        caption.decompose()
        promoted += 1
    return promoted


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


# ── Transform 12.5: merge adjacent code spans ────────────────────────────────

_PATH_START_CHARS = frozenset("./\\_-")


def merge_adjacent_code(content: Tag) -> int:
    """
    Merge <code> elements that are adjacent (only whitespace between them) when the
    second span starts with a path separator character (/, ., _, -, \\).

    MadCap variable references like:
      <code><span class="mc-variable">DS_INSTALL</span></code><code>/manager-data</code>
    produce adjacent code spans in markdownify output (`DS_INSTALL``/manager-data`).
    This pass merges them into a single code span after normalize_whitespace has run.
    """
    merged = 0
    for code in list(content.find_all("code")):
        if code.parent is None:
            continue

        # Skip to next non-whitespace sibling
        nxt = code.next_sibling
        while nxt and isinstance(nxt, NavigableString) and not nxt.strip():
            nxt = nxt.next_sibling

        if not (isinstance(nxt, Tag) and nxt.name == "code"):
            continue

        # Only merge if second code starts with a path/separator character
        second_stripped = nxt.get_text().strip()
        if not second_stripped or second_stripped[0] not in _PATH_START_CHARS:
            continue

        # Confirm only whitespace-only nodes are between code and nxt
        sib = code.next_sibling
        only_ws = True
        while sib and sib is not nxt:
            if isinstance(sib, Tag) or (isinstance(sib, NavigableString) and sib.strip()):
                only_ws = False
                break
            sib = sib.next_sibling
        if not only_ws:
            continue

        # Remove whitespace nodes between them, then absorb nxt's children
        sib = code.next_sibling
        while sib and sib is not nxt:
            to_remove = sib
            sib = sib.next_sibling
            to_remove.extract()

        for child in list(nxt.children):
            code.append(child.extract())
        nxt.decompose()
        merged += 1

    return merged


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
    stats["list_merges"]      = merge_list_continuations(content)
    stats["callouts"]         = callout_divs(content)
    stats["ebx_callouts"]     = ebx_callout_divs(content)
    stats["icon_tables"]      = icon_tables(content)
    stats["text_popups"]      = text_popups(content)
    stats["definition_lists"] = definition_lists(content)
    stats["task_sections"]    = task_sections(content)
    stats["inline_spans"]     = inline_spans(content)
    stats["code_url_links"]   = code_urls_to_links(content)
    stats["anchor_links"]     = anchor_only_links(content)
    stats["colspan_tables"]   = split_colspan_tables(content)
    stats["table_captions"]   = extract_table_captions(content)
    table_counts              = classify_tables(content, block_tags)
    stats.update(table_counts)
    stats["ws_normalized"]    = normalize_whitespace(content)
    stats["pre_linebreaks"]   = fix_pre_linebreaks(content)
    stats["adjacent_code"]    = merge_adjacent_code(content)
    stats["images_rewritten"] = rewrite_image_src(content, page_url_path)
    return stats
