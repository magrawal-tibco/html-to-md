"""
scripts/webworks/convert.py — WebWorks ePublisher HTML → Markdown converter.

Handles TIBCO product documentation authored in Adobe FrameMaker and published
via WebWorks ePublisher (legacy products such as ActiveMatrix BusinessWorks 5.x).

Detection: a version's cache directory contains wwhelp/books.htm.

Content structure:
  body > blockquote  — topic content
  div.N1Heading      — h1
  div.N2Heading      — h2
  div.N3Heading      — h3
  div.Bullet_outer   — unordered list item (table wrapper)
  div.Step_outer     — ordered list item (table wrapper)
  div.ListDash_outer — unordered dash list item (table wrapper)
  div.Body           — body paragraph
  div.Code / div.CodeLine — code block lines (merged)
  div.MinorHead      — h4
  div.IconNote/IconWarning/IconCaution/IconTip — blockquote callout

Usage:
  python scripts/webworks/convert.py --phase bw
  python scripts/webworks/convert.py --phase bw --dry-run
  python scripts/webworks/convert.py --phase bw --force-rerun
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import yaml
from bs4 import BeautifulSoup, NavigableString, Tag

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.reporter import Reporter
from webworks.utils import discover_webworks_versions, read_books_htm, read_files_index

_SKIP_FILENAMES = {"title.htm", "lof.htm", "lot.htm", "glossary.htm"}

# Inline span class → markdown wrapper
_SPAN_MAP = {
    "Bold":         ("**", "**"),
    "Italic":       ("_",  "_"),
    "CodeItalic":   ("`",  "`"),
    "Code":         ("`",  "`"),
    "LiveLink":     ("",   ""),   # text only — JS popup
}


# ── Inline text extraction ───────────────────────────────────────────────────

def _inline(node) -> str:
    """Recursively convert a BS4 node to inline Markdown text."""
    if isinstance(node, NavigableString):
        return str(node)

    tag = node.name
    cls = " ".join(node.get("class", []))
    href = node.get("href", "")

    if tag == "script":
        return ""

    if tag == "a":
        # Anchor-only (no href) — position marker, discard
        if not href:
            return "".join(_inline(c) for c in node.children)
        # JavaScript popup — keep text
        if href.startswith("javascript:"):
            return "".join(_inline(c) for c in node.children)
        # Real link — rewrite .htm → .md
        text = "".join(_inline(c) for c in node.children).strip()
        target = re.sub(r"\.hts?m$", ".md", href, flags=re.IGNORECASE)
        return f"[{text}]({target})" if text else ""

    if tag == "img":
        alt = node.get("alt", "")
        src = node.get("src", "")
        return f"![{alt}]({src})"

    if tag == "span":
        inner = "".join(_inline(c) for c in node.children)
        for key, (pre, post) in _SPAN_MAP.items():
            if key in cls:
                return f"{pre}{inner.strip()}{post}" if inner.strip() else ""
        return inner

    if tag == "br":
        return "  \n"

    # Any other tag — recurse
    return "".join(_inline(c) for c in node.children)


def _get_text(div) -> str:
    return "".join(_inline(c) for c in div.children).strip()


# ── Table helpers ────────────────────────────────────────────────────────────

def _is_list_table(table: Tag) -> bool:
    """True if this table is a WebWorks list/step wrapper (role=presentation)."""
    return table.get("role") == "presentation"


def _extract_list_content(outer_div: Tag) -> str:
    """Extract the text content from a Bullet_outer/Step_outer/ListDash_outer div."""
    table = outer_div.find("table")
    if not table:
        return _get_text(outer_div)
    tds = table.find_all("td")
    # Structure: td[0] = bullet/number, td[1] = content
    if len(tds) >= 2:
        return _get_text(tds[1])
    return _get_text(tds[0]) if tds else ""


def _extract_step_number(outer_div: Tag) -> str:
    """Extract '1', '2', etc. from a Step_outer div's first td."""
    table = outer_div.find("table")
    if not table:
        return "1"
    tds = table.find_all("td")
    if tds:
        raw = tds[0].get_text().strip()
        m = re.match(r"(\d+)", raw)
        return m.group(1) if m else "1"
    return "1"


def _convert_content_table(table: Tag) -> str:
    """Convert a real content table (not a list wrapper) to GFM or HTML."""
    rows = []
    for tr in table.find_all("tr"):
        cells = [_get_text(td) for td in tr.find_all(["td", "th"])]
        rows.append(cells)
    if not rows:
        return ""
    # Check for complex cells
    for tr in table.find_all("tr"):
        for td in tr.find_all(["td", "th"]):
            # If the cell has block children (ul, ol, table, div with heading) → HTML passthrough
            if td.find(["ul", "ol", "table"]):
                return str(table)
    # GFM pipe table
    col_count = max(len(r) for r in rows)
    lines = []
    header = rows[0]
    while len(header) < col_count:
        header.append("")
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join(["---"] * col_count) + " |")
    for row in rows[1:]:
        while len(row) < col_count:
            row.append("")
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


# ── Callout detection ─────────────────────────────────────────────────────────

_CALLOUT_LABELS = {
    "IconNote":    "**Note:**",
    "IconWarning": "**Warning:**",
    "IconCaution": "**Caution:**",
    "IconTip":     "**Tip:**",
    "Note":        "**Note:**",
    "Warning":     "**Warning:**",
    "Caution":     "**Caution:**",
    "Tip":         "**Tip:**",
}


# ── Block-level converter ────────────────────────────────────────────────────

def _convert_blockquote(bq: Tag) -> str:
    """Convert the <blockquote> content of a WebWorks page to Markdown."""
    lines = []
    in_code_block = False
    code_lines = []

    def flush_code():
        nonlocal in_code_block, code_lines
        if code_lines:
            lines.append("```")
            lines.extend(code_lines)
            lines.append("```")
            code_lines = []
        in_code_block = False

    for child in bq.children:
        if isinstance(child, NavigableString):
            text = str(child).strip()
            if text:
                flush_code()
                lines.append(text)
            continue

        if not isinstance(child, Tag):
            continue

        tag = child.name
        cls_list = child.get("class", [])
        cls = " ".join(cls_list)

        # Skip chrome elements
        if tag == "script":
            continue
        if tag == "hr":
            continue

        # ── Code blocks ──────────────────────────────────────────────────────
        if any(c in ("Code", "CodeLine") for c in cls_list):
            code_lines.append(_get_text(child))
            in_code_block = True
            continue

        flush_code()

        # ── Headings ─────────────────────────────────────────────────────────
        if "N1Heading" in cls_list:
            lines.append(f"# {_get_text(child)}")
            continue
        if "N2Heading" in cls_list or "MinorHead" in cls_list:
            lines.append(f"## {_get_text(child)}")
            continue
        if "N3Heading" in cls_list:
            lines.append(f"### {_get_text(child)}")
            continue
        if "N4Heading" in cls_list:
            lines.append(f"#### {_get_text(child)}")
            continue

        # ── Unordered list items ─────────────────────────────────────────────
        if "Bullet_outer" in cls_list or "ListDash_outer" in cls_list:
            text = _extract_list_content(child)
            lines.append(f"- {text}")
            continue

        # ── Ordered list items (steps) ────────────────────────────────────────
        if "Step_outer" in cls_list or "StepInd_outer" in cls_list:
            num = _extract_step_number(child)
            text = _extract_list_content(child)
            lines.append(f"{num}. {text}")
            continue

        # ── Callout notes ─────────────────────────────────────────────────────
        for callout_cls, label in _CALLOUT_LABELS.items():
            if callout_cls in cls_list:
                # IconNote is a table with icon + text
                inner = _get_text(child)
                lines.append(f"> {label} {inner}")
                break
        else:
            # ── Real tables ───────────────────────────────────────────────────
            if tag == "table" and not _is_list_table(child):
                lines.append(_convert_content_table(child))
                continue

            # ── Figure title, table title ─────────────────────────────────────
            if "FigureTitle" in cls_list or "TableTitle" in cls_list:
                text = _get_text(child)
                lines.append(f"*{text}*")
                continue

            # ── Body paragraph and everything else ────────────────────────────
            if tag in ("div", "p", "blockquote"):
                text = _get_text(child)
                if text:
                    lines.append(text)
                continue

            # ── Fallback ──────────────────────────────────────────────────────
            text = child.get_text(" ", strip=True)
            if text:
                lines.append(text)

    flush_code()

    # Join paragraphs with blank lines, collapse 3+ blank lines to 2
    result = "\n\n".join(l for l in lines if l)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


# ── Frontmatter ───────────────────────────────────────────────────────────────

def _build_frontmatter(title: str, guide_dir: Path, output_path: Path,
                        product_name: str, product_version: str, doc_name: str,
                        source_url: str) -> str:
    fm = {
        "title":           title,
        "source_url":      source_url,
        "lang":            "en",
        "product_name":    product_name,
        "product_version": product_version,
        "doc_name":        doc_name,
    }
    return "---\n" + yaml.dump(fm, allow_unicode=True, sort_keys=False) + "---\n"


# ── Discovery ─────────────────────────────────────────────────────────────────



def _get_manifest_meta(manifest: list[dict], product_slug: str, version: str
                        ) -> tuple[str, str, str]:
    """Return (product_name, doc_name, base_url_prefix) from manifest entries."""
    for entry in manifest:
        url = entry.get("url", "")
        pv = entry.get("product_version", "")
        pn = entry.get("product_name", "")
        dn = entry.get("doc_name", "")
        if pv == version and product_slug.split("/")[-1] in url:
            return pn, dn, ""
    return "", "", ""


# ── Conversion ────────────────────────────────────────────────────────────────

def _convert_file(htm_path: Path, title: str, guide_dir: Path,
                  output_path: Path, product_name: str, product_version: str,
                  doc_name: str, base_url: str, dry_run: bool) -> bool:
    """Parse one WebWorks .htm file and write output .md. Returns True on success."""
    raw = htm_path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(raw, "html.parser")

    page_title = soup.find("title")
    page_title = page_title.get_text(strip=True) if page_title else title

    bq = soup.find("blockquote")
    if not bq:
        return False

    # Build source URL from the file path relative to cache root
    rel = htm_path.as_posix()
    if "cache/" in rel:
        rel_url_path = rel.split("cache/", 1)[1]
        source_url = f"{base_url}/{rel_url_path}"
    else:
        source_url = rel

    md_body = _convert_blockquote(bq)
    frontmatter = _build_frontmatter(
        page_title, guide_dir, output_path,
        product_name, product_version, doc_name, source_url
    )
    content = frontmatter + "\n" + md_body + "\n"

    if not dry_run:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding="utf-8")

    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="WebWorks ePublisher HTML → Markdown converter"
    )
    parser.add_argument("--phase",       required=True)
    parser.add_argument("--config",      default="config/settings.yaml")
    parser.add_argument("--dry-run",     action="store_true")
    parser.add_argument("--force-rerun", action="store_true")
    args = parser.parse_args()

    settings   = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    cache_dir  = Path(settings.get("cache_dir", "cache"))
    output_dir = Path(settings.get("output_dir", "output"))
    base_url   = settings.get("base_url", "https://docs.tibco.com")
    logs_dir   = Path(settings.get("logs_dir", "logs"))
    manifests_dir = Path(settings.get("manifests_dir", "manifests"))

    manifest_path = manifests_dir / f"manifest_{args.phase}.json"
    manifest = []
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    run_dir  = logs_dir / args.phase / datetime.now().strftime("%Y%m%d-%H%M%S")
    reporter = Reporter(run_dir, "webworks_convert", dry_run=args.dry_run)
    reporter.info(f"=== WebWorks Convert | phase={args.phase} dry_run={args.dry_run} ===")

    versions_found = list(discover_webworks_versions(cache_dir))
    if not versions_found:
        reporter.info("No WebWorks versions found in cache — nothing to do.")
        reporter.finish()
        return 0

    reporter.info(f"Found {len(versions_found)} WebWorks version(s).")

    for version_html_root, product_slug, version in versions_found:
        reporter.info(f"Processing: {product_slug} {version}")
        product_name, doc_name, _ = _get_manifest_meta(manifest, product_slug, version)

        guides = read_books_htm(version_html_root)
        for guide_dir in guides:
            if not guide_dir.exists():
                reporter.warning(f"Guide dir not found: {guide_dir}")
                continue

            entries = read_files_index(guide_dir)
            for rel_href, title in entries:
                # Skip non-topic files
                fname = Path(rel_href).name.lower()
                if fname in _SKIP_FILENAMES:
                    reporter.count("skipped_non_topic")
                    continue

                htm_path = guide_dir / rel_href
                if not htm_path.exists():
                    reporter.warning(f"Missing: {htm_path}")
                    reporter.count("missing_htm")
                    continue

                # Output path mirrors cache path under output_dir
                rel_to_cache = htm_path.relative_to(cache_dir)
                output_path = output_dir / rel_to_cache.with_suffix(".md")

                if output_path.exists() and not args.force_rerun:
                    reporter.count("skipped_already_done")
                    continue

                try:
                    ok = _convert_file(
                        htm_path, title, guide_dir, output_path,
                        product_name, version, doc_name,
                        base_url, args.dry_run,
                    )
                    if ok:
                        reporter.count("converted")
                    else:
                        reporter.skip(str(htm_path), "no blockquote content")
                        reporter.count("skipped_no_content")
                except Exception as e:
                    reporter.fail(str(htm_path), str(e))

    reporter.finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
