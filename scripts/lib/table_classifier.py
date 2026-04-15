"""
table_classifier.py — 3-tier table classification for MadCap Flare HTML.

Tier 1: Text-only cells → convert to GFM pipe table (markdownify handles this)
Tier 2: Cells with inline HTML only (a, strong, em, code, span, br) → GFM pipe table
Tier 3: Cells with block content (ul, ol, pre, nested table, h1-h6, blockquote)
        → leave as raw HTML, mark with data-converter-passthrough="true"

Settings key: tables.passthrough_block_tags (list of tag names forcing Tier 3)
"""

from bs4 import BeautifulSoup, Tag

# Default block tags that force Tier 3; overridden by settings.yaml
DEFAULT_BLOCK_TAGS = {"ul", "ol", "pre", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "table"}

# Tags that are acceptable in Tier 2 (inline only)
INLINE_TAGS = {"a", "strong", "em", "b", "i", "code", "span", "br", "sub", "sup", "abbr"}


def _cell_tier(cell: Tag, block_tags: set[str]) -> int:
    """Return the tier (1, 2, or 3) for a single table cell.

    Tier 3 is triggered only by true block-level content in block_tags
    (ul, ol, pre, blockquote, table, h1-h6). Tags like <p> and <div>
    that appear commonly in cells are treated as Tier 2 — they get
    flattened to inline text by markdownify.
    """
    for child in cell.descendants:
        if isinstance(child, Tag):
            if child.name.lower() in block_tags:
                return 3
    # Check whether there is any inline HTML at all
    for child in cell.children:
        if isinstance(child, Tag):
            return 2
    return 1


def classify_table(table: Tag, block_tags: set[str] | None = None) -> int:
    """
    Return the tier for a whole table (worst-case cell determines the table tier).
    """
    if block_tags is None:
        block_tags = DEFAULT_BLOCK_TAGS
    tier = 1
    for cell in table.find_all(["td", "th"]):
        cell_t = _cell_tier(cell, block_tags)
        if cell_t > tier:
            tier = cell_t
        if tier == 3:
            break
    return tier


def _promote_first_row_as_header(table: Tag) -> bool:
    """
    If a table has no <thead>, promote the first data row to <thead> with <th> cells.

    GFM pipe tables require a header row. Without a <thead>, markdownify generates
    blank |  |  | headers. This promotes the first <tr> to fix that.
    Returns True if a row was promoted.
    """
    if table.find("thead"):
        return False  # already has a header

    first_row = table.find("tr")
    if not first_row:
        return False

    # Skip if row already uses <th> cells
    if first_row.find("th"):
        return False

    tds = first_row.find_all("td", recursive=False)
    if not tds:
        return False

    # Convert <td> → <th> in-place
    for td in tds:
        td.name = "th"

    # Detach the row, wrap in a new <thead>, insert at the top of the table
    first_row.extract()
    thead = BeautifulSoup("<thead></thead>", "lxml").find("thead")
    thead.append(first_row)
    table.insert(0, thead)
    return True


def handle_tables(soup: BeautifulSoup, block_tags: set[str] | None = None) -> dict[str, int]:
    """
    Classify and handle all tables in the soup in-place.

    Tier 1 & 2: left as-is (markdownify converts them to GFM).
               First data row is promoted to <thead>/<th> if none exists.
    Tier 3: marked with data-converter-passthrough="true" so postprocessor
            can report them for manual review.

    Returns counts: {"tier1": n, "tier2": n, "tier3": n}
    """
    if block_tags is None:
        block_tags = DEFAULT_BLOCK_TAGS

    counts = {"tier1": 0, "tier2": 0, "tier3": 0}

    for table in soup.find_all("table"):
        # Skip tables already nested inside a passthrough table
        parent = table.find_parent("table", attrs={"data-converter-passthrough": "true"})
        if parent:
            continue

        # Promote first data row to header if the table has no <thead>
        _promote_first_row_as_header(table)

        tier = classify_table(table, block_tags)
        counts[f"tier{tier}"] += 1

        if tier == 3:
            table["data-converter-passthrough"] = "true"

    return counts
