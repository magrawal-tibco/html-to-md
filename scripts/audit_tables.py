#!/usr/bin/env python3
"""
audit_tables.py — Audit tables in Flare-generated HTML source files.

Classifies every <table> by structural complexity and flags high-risk content.
Outputs a CSV report and a Markdown summary to ./audit-output/<name>/.

Usage:
    python scripts/audit_tables.py --phase bw_plugins_poc
    python scripts/audit_tables.py --src cache/pub/activematrix_businessworks
    python scripts/audit_tables.py --phase bw_plugins_poc --out audit-output/custom
    python scripts/audit_tables.py --src cache/pub/foo --phase my_phase --out audit-output/combined

Re-runnable: output files are overwritten on each run, so results always reflect
the current state of the source tree.
"""

import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup

sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT     = Path(__file__).resolve().parent.parent
CACHE_DIR     = REPO_ROOT / "cache"
PHASES_DIR    = REPO_ROOT / "config" / "phases"
MANIFESTS_DIR = REPO_ROOT / "manifests"

# ── File-level filters ────────────────────────────────────────────────────────

# MadCap Flare shell/frameset pages — JS-only, no content body.
SKIP_FILENAMES = frozenset(["Default.htm", "Default_CSH.htm", "Home.htm",
                             "index.htm", "wwhsec.htm"])

# SDL Trisoft DITA WebHelp GUID-based filenames — different format, skip.
_GUID_RE = re.compile(
    r"^GUID-[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}\.html?$", re.I
)


# ── Admonition class detection ────────────────────────────────────────────────
#
# In Flare source HTML, notes/warnings/tips are wrapped in elements like:
#
#   <div class="note" data-mc-autonum="Note: ">
#     <span class="autonumber"><span class="noteHeadInTable">Note: </span></span>
#     ... content with <p>, <code>, <pre> ...
#   </div>
#
# Class names found by inspecting 342 BW source files (by frequency):
#   Container (outer element — what we check):
#     note (171x), noteImportant (10x), noteCaution (3x), noteTip (3x),
#     noteWarning (1x), noteNote (1x)
#   Child labels (inside the container — NOT what we check):
#     noteHead (154x), noteHeadInTable (35x)
#   Icon images (inside the container — NOT what we check):
#     IconNote (140x), IconTip (27x), IconWarning (18x)
#
# These div.note* elements become <blockquote> after the preprocessor runs.
# The downstream AEM parser SILENTLY DROPS <blockquote> inside table cells
# (confirmed in pipeline testing) → HIGH RISK content loss.
#
# Pattern: class exactly "note"  OR  "note" + uppercase letter suffix.
# This matches the container classes and excludes noteHead/noteHeadInTable/AuthorNote.
_NOTE_CONTAINER_RE = re.compile(r"^note([A-Z]|$)")


def _has_note_container(cell) -> bool:
    """Return True if cell contains a Flare admonition (note/warning/tip) container."""
    for el in cell.find_all(True):
        if any(_NOTE_CONTAINER_RE.match(c) for c in (el.get("class") or [])):
            return True
    return False


# ── Fake-list table detection ─────────────────────────────────────────────────
#
# MadCap Flare's AutoNumber feature renders numbered/bulleted lists as tables:
#   <table><tr>
#     <td><div class="Bullet_inner">•</div></td>
#     <td><div class="Bullet_outer">Content here...</div></td>
#   </tr></table>
#
# These are detected by the preprocessor's `fake_list_tables` pass and converted
# to proper <ul>/<ol> — they do NOT need migration attention, so they're excluded
# from all other categories and counted separately.
def _is_fake_list_table(tbl) -> bool:
    """Return True if this table is a MadCap AutoNumber fake-list table."""
    cells = tbl.find_all(["td", "th"])
    if len(cells) > 6:
        # Real data tables have more cells; fake-list tables are tiny (1-2 rows)
        return False
    # Check for AutoNumber class on the table itself
    if any("AutoNumber" in c for c in (tbl.get("class") or [])):
        return True
    # Check for *_inner or *_outer div classes inside cells (Bullet_inner, Step_inner, etc.)
    for div in tbl.find_all("div", limit=8):
        cls_str = " ".join(div.get("class") or [])
        if "_inner" in cls_str or "_outer" in cls_str:
            return True
    return False


# ── Block-level tag sets ──────────────────────────────────────────────────────

# Tags that count as block-level content inside a cell
BLOCK_TAGS = frozenset([
    "p", "ul", "ol", "dl", "blockquote", "pre", "code",
    "div", "h1", "h2", "h3", "h4", "h5", "h6", "table",
    "figure", "details", "summary", "aside",
])

# Tags already handled by specific categories — what's left after excluding these
# becomes OTHER_BLOCK_ELEMENTS. We exclude div here (too noisy) and instead
# flag uncommon structural tags: headings, blockquote, figure, aside, details.
_OTHER_BLOCK_TRIGGER_TAGS = frozenset([
    "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "figure", "aside", "details", "summary",
])


# ── Table classification ──────────────────────────────────────────────────────

def classify_table(tbl) -> dict:
    """
    Classify a single <table> element.

    Returns a dict with:
      fake_list   — bool: True if this is a preprocessor-handled AutoNumber table
      row_count   — int
      col_count   — int
      categories  — list[str]: matched category names, sorted alphabetically
      high_risk   — bool: True if NOTE_OR_ADMONITION is matched
      other_block_tags — list[str]: tag names triggering OTHER_BLOCK_ELEMENTS
    """
    rows      = tbl.find_all("tr")
    all_cells = tbl.find_all(["td", "th"])
    row_count = len(rows)
    col_count = max((len(r.find_all(["td", "th"])) for r in rows), default=0)

    # Exclude fake-list tables from all other categories
    if _is_fake_list_table(tbl):
        return {
            "fake_list":       True,
            "row_count":       row_count,
            "col_count":       col_count,
            "categories":      ["FAKE_LIST"],
            "high_risk":       False,
            "other_block_tags": [],
        }

    cats             = set()
    other_block_tags = set()

    # ── (e) MERGED_CELLS ──────────────────────────────────────────────────────
    # Any cell carries colspan > 1 or rowspan > 1 — can break GFM pipe-table output.
    for cell in all_cells:
        try:
            if int(cell.get("colspan", 1)) > 1 or int(cell.get("rowspan", 1)) > 1:
                cats.add("MERGED_CELLS")
                break
        except (ValueError, TypeError):
            pass

    # ── (f) NESTED_TABLE ──────────────────────────────────────────────────────
    # A <table> element inside any cell — complex to flatten into Markdown.
    if tbl.find("table"):
        cats.add("NESTED_TABLE")

    # ── (g) WIDE ──────────────────────────────────────────────────────────────
    # More than 5 columns — GFM tables get unwieldy; consider splitting or transposing.
    if col_count > 5:
        cats.add("WIDE")

    # ── Per-cell analysis ─────────────────────────────────────────────────────
    for cell in all_cells:
        # Look at direct block-level children of this cell only (not grandchildren)
        direct_blocks = [
            c for c in cell.children
            if hasattr(c, "name") and c.name and c.name in BLOCK_TAGS
        ]
        block_names = [c.name for c in direct_blocks]

        # ── (b) MULTI_PARAGRAPH ───────────────────────────────────────────────
        # Cell has more than one <p>, or a <p> plus any other block sibling.
        # Both cases make the cell content harder to represent in a single GFM cell.
        p_count  = block_names.count("p")
        non_p    = [n for n in block_names if n != "p"]
        if p_count > 1 or (p_count >= 1 and non_p):
            cats.add("MULTI_PARAGRAPH")

        # ── (c) NOTE_OR_ADMONITION ────────────────────────────────────────────
        # Cell contains <div class="note*"> — becomes <blockquote> after preprocessor.
        # <blockquote> inside table cells is SILENTLY DROPPED by the AEM parser.
        # See admonition class discovery notes above.
        if _has_note_container(cell):
            cats.add("NOTE_OR_ADMONITION")

        # ── (d) LIST_IN_CELL ──────────────────────────────────────────────────
        # Cell contains <ul>, <ol>, or <dl>.
        # Downstream parser preserves these — confirmed safe, but complex to convert.
        if cell.find(["ul", "ol", "dl"]):
            cats.add("LIST_IN_CELL")

        # ── (h) IMAGE_IN_CELL ─────────────────────────────────────────────────
        if cell.find("img"):
            cats.add("IMAGE_IN_CELL")

        # ── (i) CODE_IN_CELL ──────────────────────────────────────────────────
        if cell.find(["pre", "code"]):
            cats.add("CODE_IN_CELL")

        # ── (j) OTHER_BLOCK_ELEMENTS ──────────────────────────────────────────
        # Structural block tags that are NOT covered by the categories above.
        # Flagging these catches edge cases (heading inside a cell, <figure>, etc.)
        # that would surprise the converter. List the tag names for human review.
        for block in direct_blocks:
            if block.name in _OTHER_BLOCK_TRIGGER_TAGS:
                other_block_tags.add(block.name)

    if other_block_tags:
        cats.add("OTHER_BLOCK_ELEMENTS")

    # ── (a) SIMPLE ────────────────────────────────────────────────────────────
    # No complexity flags — all cells contain only inline content (text, spans, links).
    if not cats:
        cats.add("SIMPLE")

    # HIGH_RISK: NOTE_OR_ADMONITION tables → confirmed silent content loss pipeline.
    high_risk = "NOTE_OR_ADMONITION" in cats

    return {
        "fake_list":        False,
        "row_count":        row_count,
        "col_count":        col_count,
        "categories":       sorted(cats),
        "high_risk":        high_risk,
        "other_block_tags": sorted(other_block_tags),
    }


# ── File-level helpers ────────────────────────────────────────────────────────

def _near_heading(tbl) -> str:
    """Find the nearest preceding h1–h4 for human context in the report."""
    for sibling in tbl.find_all_previous(["h1", "h2", "h3", "h4"]):
        text = sibling.get_text(" ", strip=True)
        if text:
            return text[:100]
    return ""


def _content_preview(tbl) -> str:
    """First 120 chars of the table's text content (whitespace-collapsed)."""
    text = re.sub(r"\s+", " ", tbl.get_text(" ", strip=True))
    return text[:120]


def audit_file(fpath: Path, src_root: Path) -> list[dict]:
    """
    Parse one HTML file and return one audit record per table.
    Returns [] if the file has no tables or cannot be read.
    """
    try:
        html = fpath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    # Quick pre-filter: skip files with no tables at all (fast string check)
    if "<table" not in html:
        return []

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []

    # Prefer the main content area to avoid classifying nav/chrome tables
    main = soup.find("div", attrs={"role": "main", "id": "mc-main-content"})
    search_root = main if main else soup

    tables = search_root.find_all("table")
    if not tables:
        return []

    rel_path = fpath.relative_to(src_root).as_posix()
    records  = []

    for idx, tbl in enumerate(tables, start=1):
        result = classify_table(tbl)
        records.append({
            "file_path":        rel_path,
            "table_index":      idx,
            "row_count":        result["row_count"],
            "col_count":        result["col_count"],
            "categories":       "|".join(result["categories"]),
            "high_risk":        "HIGH_RISK — confirmed silent content loss" if result["high_risk"] else "",
            "near_heading":     "" if result["fake_list"] else _near_heading(tbl),
            "other_block_tags": ",".join(result["other_block_tags"]),
            "content_preview":  "" if result["fake_list"] else _content_preview(tbl),
        })

    return records


# ── File collection ───────────────────────────────────────────────────────────

def _is_auditable(fpath: Path) -> bool:
    """Return False for shell pages, GUID-based DITA files, and non-HTML."""
    if fpath.name in SKIP_FILENAMES:
        return False
    if _GUID_RE.match(fpath.name):
        return False
    return True


def collect_files_from_src(src_dir: Path) -> tuple[list[Path], Path]:
    """Recursively collect all .htm/.html files under src_dir."""
    files = [
        f for ext in ("*.htm", "*.html")
        for f in src_dir.rglob(ext)
        if _is_auditable(f)
    ]
    return files, src_dir


def collect_files_from_manifest(phase_name: str) -> tuple[list[Path], Path]:
    """
    Load manifests/manifest_<phase_name>.json and return (cache file paths, src_root).
    Uses manifest entries to find exactly the files the pipeline would process,
    rather than scanning the whole cache directory.
    """
    mf = MANIFESTS_DIR / f"manifest_{phase_name}.json"
    if not mf.exists():
        print(f"ERROR: manifest not found: {mf}", file=sys.stderr)
        sys.exit(1)

    entries = json.loads(mf.read_text(encoding="utf-8"))
    files   = []
    missing = 0

    for entry in entries:
        # Non-page entries (version metadata in WebWorks manifests) have no "url"
        if "url" not in entry:
            continue

        # Use cache_path (ZIP-based products) if present; otherwise derive from URL
        if "cache_path" in entry and entry["cache_path"]:
            fpath = CACHE_DIR / entry["cache_path"]
        else:
            url_path = urlparse(entry["url"]).path.lstrip("/")
            fpath = CACHE_DIR / url_path

        if fpath.suffix.lower() not in (".htm", ".html"):
            continue
        if not _is_auditable(fpath):
            continue
        if not fpath.exists():
            missing += 1
            continue
        files.append(fpath)

    if missing:
        print(f"  ({missing} manifest entries skipped — not in cache)", file=sys.stderr)

    return files, CACHE_DIR


# ── Report writers ────────────────────────────────────────────────────────────

CSV_FIELDS = [
    "file_path", "table_index", "row_count", "col_count",
    "categories", "high_risk", "near_heading",
    "other_block_tags", "content_preview",
]


def write_csv(records: list[dict], out_dir: Path) -> Path:
    """Write per-table CSV report."""
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "tables.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(records)
    return csv_path


def write_summary(records: list[dict], out_dir: Path, source_label: str) -> Path:
    """Write Markdown summary with category counts and top-10 complex tables."""
    total          = len(records)
    fake_list_count = sum(1 for r in records if "FAKE_LIST" in r["categories"])
    real_records   = [r for r in records if "FAKE_LIST" not in r["categories"]]
    real_total     = len(real_records)
    high_risk_count = sum(1 for r in real_records if r["high_risk"])

    cat_counter = Counter()
    for r in real_records:
        for cat in r["categories"].split("|"):
            cat_counter[cat] += 1

    # Tables with 2+ non-SIMPLE categories (highest conversion effort)
    multi_cat = [
        r for r in real_records
        if len([c for c in r["categories"].split("|") if c != "SIMPLE"]) >= 2
    ]
    multi_cat.sort(key=lambda r: -len(r["categories"].split("|")))

    lines = [
        f"# Table Audit — {source_label}\n\n",
        f"| | |\n|---|---|\n",
        f"| **Source** | `{source_label}` |\n",
        f"| **Total tables** | {total:,} |\n",
        f"| **Fake-list tables** (preprocessor-handled, excluded below) | {fake_list_count:,} |\n",
        f"| **Real data tables** | {real_total:,} |\n",
        f"| **HIGH RISK** (NOTE_OR_ADMONITION → confirmed silent content loss) | {high_risk_count:,} |\n",
        f"| **Multi-category tables** (highest conversion effort) | {len(multi_cat):,} |\n\n",
    ]

    lines += [
        "## Category Breakdown (real tables only)\n\n",
        "| Category | Count | % of real tables | Notes |\n",
        "|---|---|---|---|\n",
    ]
    NOTES = {
        "SIMPLE":             "Plain text cells — convert directly to GFM pipe table",
        "MULTI_PARAGRAPH":    "Cells with multiple `<p>` or mixed block content",
        "NOTE_OR_ADMONITION": "⚠ div.note* → blockquote after conversion → SILENTLY DROPPED by AEM",
        "LIST_IN_CELL":       "Cells with `<ul>/<ol>/<dl>` — preserved by downstream parser",
        "MERGED_CELLS":       "colspan or rowspan > 1 — not supported in GFM",
        "NESTED_TABLE":       "Table inside a cell — must flatten or convert to raw HTML",
        "WIDE":               "More than 5 columns",
        "IMAGE_IN_CELL":      "Cells with `<img>`",
        "CODE_IN_CELL":       "Cells with `<pre>` or `<code>`",
        "OTHER_BLOCK_ELEMENTS": "Headings, `<blockquote>`, `<figure>`, etc. — review needed",
        "FAKE_LIST":          "AutoNumber/Bullet_inner — converted to `<ul>/<ol>` by preprocessor",
    }
    for cat, count in cat_counter.most_common():
        pct  = 100 * count / real_total if real_total else 0
        note = NOTES.get(cat, "")
        lines.append(f"| `{cat}` | {count:,} | {pct:.1f}% | {note} |\n")

    lines += [
        f"\n## Top 10 Most Complex Tables ({len(multi_cat):,} multi-category total)\n\n",
        "Tables matching the most categories have the highest conversion effort.\n\n",
        "| File | # | Rows | Cols | Categories | High Risk | Near Heading |\n",
        "|---|---|---|---|---|---|---|\n",
    ]
    for r in multi_cat[:10]:
        cats    = r["categories"]
        hr      = "**⚠ HIGH RISK**" if r["high_risk"] else ""
        heading = (r["near_heading"] or "")[:60].replace("|", "\\|")
        fpath   = r["file_path"].replace("|", "\\|")
        lines.append(
            f"| `{fpath}` | {r['table_index']} | {r['row_count']} | {r['col_count']} "
            f"| {cats} | {hr} | {heading} |\n"
        )

    if cat_counter.get("OTHER_BLOCK_ELEMENTS", 0):
        # Summarize the actual other-block tag names seen
        other_tags = Counter()
        for r in real_records:
            if r["other_block_tags"]:
                for t in r["other_block_tags"].split(","):
                    other_tags[t.strip()] += 1
        lines += [
            "\n## OTHER_BLOCK_ELEMENTS — Tag Inventory\n\n",
            "Tags found as direct cell children that aren't covered by other categories:\n\n",
            "| Tag | Occurrences |\n|---|---|\n",
        ]
        for tag, cnt in other_tags.most_common():
            lines.append(f"| `<{tag}>` | {cnt} |\n")

    lines += [
        "\n---\n",
        "_Generated by `scripts/audit_tables.py`. Re-run to refresh after source changes._\n",
    ]

    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "summary.md"
    md_path.write_text("".join(lines), encoding="utf-8")
    return md_path


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit tables in Flare-generated HTML source files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--phase", metavar="NAME",
        help="Phase name — reads manifests/manifest_<NAME>.json for the file list",
    )
    parser.add_argument(
        "--src", metavar="DIR", type=Path,
        help="Source directory to scan recursively for .htm/.html files",
    )
    parser.add_argument(
        "--out", metavar="DIR", type=Path,
        help="Output directory (default: audit-output/<phase_or_src_name>)",
    )
    args = parser.parse_args()

    if not args.phase and not args.src:
        parser.error("Provide at least --phase or --src (or both).")

    # ── Collect files ─────────────────────────────────────────────────────────
    all_files: list[Path] = []
    src_root: Path = CACHE_DIR

    if args.src:
        src_dir = args.src if args.src.is_absolute() else REPO_ROOT / args.src
        if not src_dir.is_dir():
            print(f"ERROR: --src not found: {src_dir}", file=sys.stderr)
            sys.exit(1)
        files, src_root = collect_files_from_src(src_dir)
        all_files.extend(files)
        print(f"--src: {len(files):,} files under {src_dir}")

    if args.phase:
        files, manifest_root = collect_files_from_manifest(args.phase)
        # If --src was also given, keep the --src root; otherwise use CACHE_DIR
        if not args.src:
            src_root = manifest_root
        all_files.extend(files)
        # Deduplicate (in case --src and --phase overlap)
        seen    = set()
        unique  = []
        for f in all_files:
            if f not in seen:
                seen.add(f)
                unique.append(f)
        all_files = unique
        print(f"--phase {args.phase}: {len(files):,} files from manifest")

    print(f"Total files to audit: {len(all_files):,}")

    # ── Determine output directory ─────────────────────────────────────────────
    label   = args.phase or (args.src.name if args.src else "audit")
    out_dir = args.out if args.out else REPO_ROOT / "audit-output" / label

    # ── Audit ─────────────────────────────────────────────────────────────────
    all_records: list[dict] = []
    for i, fpath in enumerate(all_files, start=1):
        if i % 500 == 0 or i == len(all_files):
            print(f"  {i:,}/{len(all_files):,} files …")
        all_records.extend(audit_file(fpath, src_root))

    total_tables = len(all_records)
    fake_count   = sum(1 for r in all_records if "FAKE_LIST" in r["categories"])
    high_risk    = sum(1 for r in all_records if r["high_risk"])
    print(f"\nResults: {total_tables:,} tables ({fake_count:,} fake-list, "
          f"{total_tables - fake_count:,} real data, {high_risk:,} HIGH RISK)")

    # ── Write outputs ──────────────────────────────────────────────────────────
    csv_path = write_csv(all_records, out_dir)
    md_path  = write_summary(all_records, out_dir, label)

    print(f"\nCSV:     {csv_path}")
    print(f"Summary: {md_path}")


if __name__ == "__main__":
    main()
