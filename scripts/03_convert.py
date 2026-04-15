"""
03_convert.py — Step 3: Convert cached HTML to Markdown.

For each entry in the manifest:
  1. Parse cached HTML with BeautifulSoup
  2. Extract metadata from <html> attributes and coveo data in manifest
  3. Run 8 preprocessor transforms on the main content div
  4. Convert to Markdown with markdownify
  5. Prepend YAML frontmatter
  6. Write .md file to output/ (mirroring URL path)
  7. Copy images to output/ alongside the .md file

Usage:
  python scripts/03_convert.py --phase phase_01 [--config config/settings.yaml] [--dry-run]
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import urlparse

import warnings
import yaml
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from markdownify import markdownify as md

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.lib.preprocessor import run_all as run_preprocessor
from scripts.lib.reporter import Reporter


def load_settings(config_path: str) -> dict:
    return yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))


def load_manifest(phase: str, settings: dict) -> list[dict]:
    manifests_dir = Path(settings.get("manifests_dir", "manifests"))
    path = manifests_dir / f"manifest_{phase}.json"
    if not path.exists():
        raise FileNotFoundError(f"Manifest not found: {path}. Run Step 1 first.")
    return json.loads(path.read_text(encoding="utf-8"))


def url_to_cache_path(loc: str, cache_dir: Path) -> Path:
    path = urlparse(loc).path.lstrip("/")
    return cache_dir / path


def extract_page_metadata(soup: BeautifulSoup, entry: dict) -> dict:
    """Extract per-page metadata from the <html> tag and manifest entry."""
    html_tag = soup.find("html")
    attrs = html_tag.attrs if html_tag else {}

    # Topic type from class attribute (concept, task, reference)
    classes = attrs.get("class", "")
    if isinstance(classes, list):
        classes = " ".join(classes)
    topic_type = ""
    for t in ("concept", "task", "reference"):
        if t in classes:
            topic_type = t
            break

    # TOC path — pipe-separated breadcrumb
    toc_path = attrs.get("data-mc-toc-path", "")

    # Language
    lang = attrs.get("lang", attrs.get("xml:lang", "en-us"))

    # Title: prefer <title> tag, fallback to first h1 in content
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else ""

    return {
        "title":           title,
        "lang":            lang,
        "topic_type":      topic_type,
        "toc_path":        toc_path,
        "product_name":    entry.get("product_name", ""),
        "product_version": entry.get("product_version", ""),
        "doc_name":        entry.get("doc_name", ""),
        "source_url":      entry["url"],
    }


def build_frontmatter(meta: dict, extra: dict | None = None) -> str:
    """Render YAML frontmatter block."""
    data = {
        "title":           meta["title"],
        "source_url":      meta["source_url"],
        "lang":            meta["lang"],
        "topic_type":      meta["topic_type"],
        "toc_path":        meta["toc_path"],
        "product_name":    meta["product_name"],
        "product_version": meta["product_version"],
        "doc_name":        meta["doc_name"],
    }
    if extra:
        data.update(extra)
    # Remove empty fields
    data = {k: v for k, v in data.items() if v or v == 0}
    return "---\n" + yaml.dump(data, allow_unicode=True, default_flow_style=False) + "---\n\n"


def copy_images(html_content: bytes, page_url: str, cache_dir: Path,
                output_dir: Path, output_md_path: Path,
                skip_prefixes: list[str], dry_run: bool) -> int:
    """Copy images referenced in the HTML to the output directory."""
    from urllib.parse import urljoin
    soup = BeautifulSoup(html_content, "lxml")
    copied = 0
    output_page_dir = output_md_path.parent

    for img in soup.find_all("img", src=True):
        src = img["src"]
        if src.startswith("data:") or src.startswith("http"):
            continue
        if any(src.startswith(pfx) or f"/{pfx}" in src for pfx in skip_prefixes):
            continue
        # Resolve image URL and find its cached path
        abs_url = urljoin(page_url, src)
        cached  = url_to_cache_path(abs_url, cache_dir)
        if not cached.exists():
            continue
        # Place image relative to the output .md file
        img_path_in_url = urlparse(abs_url).path.lstrip("/")
        dest = output_dir / img_path_in_url
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cached, dest)
        copied += 1
    return copied


def clean_markdown(text: str) -> str:
    """Post-process markdownify output to clean up common artefacts."""
    # Collapse 3+ consecutive blank lines to 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Strip trailing whitespace on each line
    text = "\n".join(line.rstrip() for line in text.splitlines())
    # Fix inline markers running into adjacent text with no space.
    # markdownify strips trailing whitespace inside inline elements, so
    # <strong>Enter </strong>to  →  **Enter**to  (space lost).
    # Lookbehind (?<=\S) targets only *closing* markers (preceded by content),
    # so opening markers like "**word" are not affected.
    text = re.sub(r'(?<=\S)\*\*(?=[a-zA-Z0-9])', '** ', text)
    text = re.sub(r'(?<=\S)`(?=[a-zA-Z0-9])', '` ', text)
    return text.strip() + "\n"


_MC_STYLE_RE = re.compile(r"mc-table-style\s*:[^;\"]+;?", re.IGNORECASE)


def _clean_table_html(table) -> str:
    """Strip MadCap-specific attributes from a table before HTML passthrough."""
    for el in table.find_all(True):
        # Remove MadCap class names (keep element, remove noisy classes)
        if el.get("class"):
            del el["class"]
        # Strip mc-table-style from inline style; remove style if empty after
        if el.get("style"):
            cleaned = _MC_STYLE_RE.sub("", el["style"]).strip().strip(";").strip()
            if cleaned:
                el["style"] = cleaned
            else:
                del el["style"]
        # Remove cellspacing and other layout-only attributes
        for attr in ("cellspacing", "cellpadding", "border"):
            if el.get(attr) is not None:
                del el[attr]
        # Remove <col> elements — carry no semantic content
        if el.name == "col":
            el.decompose()
    return str(table)


def extract_passthrough_tables(content) -> dict[str, str]:
    """
    Find all Tier 3 tables (marked data-converter-passthrough="true"),
    replace each with a unique placeholder, and return {placeholder: raw_html}.
    Called before markdownify so the complex tables are not converted to GFM.
    """
    tables = {}
    for i, table in enumerate(
        content.find_all("table", attrs={"data-converter-passthrough": "true"})
    ):
        placeholder = f"%%PASSTHROUGH-TABLE-{i}%%"
        del table["data-converter-passthrough"]  # remove internal marker
        tables[placeholder] = _clean_table_html(table)
        # Replace the table in the tree with a plain paragraph holding the token
        p = BeautifulSoup(f"<p>{placeholder}</p>", "lxml").find("p")
        table.replace_with(p)
    return tables


def restore_passthrough_tables(md_text: str, tables: dict[str, str]) -> str:
    """Substitute placeholder tokens back with raw HTML table strings."""
    for placeholder, html in tables.items():
        md_text = md_text.replace(placeholder, f"\n{html}\n")
    return md_text


def convert_entry(
    entry: dict,
    settings: dict,
    cache_dir: Path,
    output_dir: Path,
    reporter: Reporter,
    dry_run: bool,
    force_rerun: bool = False,
) -> bool:
    """Convert one manifest entry from HTML to Markdown. Returns True on success."""
    url        = entry["url"]
    cache_path = url_to_cache_path(url, cache_dir)
    out_path   = output_dir / entry["output_path"]

    if not cache_path.exists():
        reporter.fail(url, "cached HTML not found — run Step 2 first")
        return False

    if out_path.exists() and not dry_run and not force_rerun:
        reporter.count("pages_already_done")
        return True

    try:
        html_bytes = cache_path.read_bytes()
        soup = BeautifulSoup(html_bytes, "lxml")

        # Extract metadata
        meta = extract_page_metadata(soup, entry)

        # Find main content — try selectors in order from settings
        content = None
        content_selectors = settings.get("content_selectors", ["div[role='main']#mc-main-content"])
        selector_used = None
        for selector in content_selectors:
            content = soup.select_one(selector)
            if content:
                selector_used = selector
                break

        if content is None:
            reporter.fail(url, "main content div not found")
            reporter.count("missing_content_div")
            return False

        reporter.count(f"selector:{selector_used}")

        # Update title from first h1 if <title> tag was empty
        if not meta["title"]:
            h1 = content.find("h1")
            meta["title"] = h1.get_text(strip=True) if h1 else Path(url).stem

        # Run preprocessor transforms
        page_url_path = urlparse(url).path
        chrome_selectors = settings.get("chrome_selectors", [])
        block_tags = set(settings.get("tables", {}).get("passthrough_block_tags", []))
        transform_stats = run_preprocessor(content, chrome_selectors, page_url_path, block_tags)
        for k, v in transform_stats.items():
            reporter.count(f"transform_{k}", v)

        # Extract Tier 3 passthrough tables before markdownify runs
        passthrough_tables = extract_passthrough_tables(content)
        if passthrough_tables:
            reporter.count("transform_tier3_passthrough", len(passthrough_tables))

        # Convert to Markdown
        md_body = md(
            str(content),
            heading_style="ATX",
            bullets="-",
            newline_style="backslash",
        )
        md_body = clean_markdown(md_body)

        # Restore passthrough tables as raw HTML blocks
        if passthrough_tables:
            md_body = restore_passthrough_tables(md_body, passthrough_tables)
            md_body = clean_markdown(md_body)

        # Build final file content
        frontmatter = build_frontmatter(meta)
        final_content = frontmatter + md_body

        if not dry_run:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(final_content, encoding="utf-8")

        # Copy images
        skip_prefixes = settings.get("image_skip_prefixes", [])
        images_copied = copy_images(
            html_bytes, url, cache_dir, output_dir, out_path, skip_prefixes, dry_run
        )
        reporter.count("images_copied", images_copied)
        reporter.count("pages_converted")
        return True

    except Exception as exc:
        reporter.fail(url, f"{type(exc).__name__}: {exc}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Step 3: Convert HTML to Markdown")
    parser.add_argument("--phase",        required=True)
    parser.add_argument("--config",       default="config/settings.yaml")
    parser.add_argument("--dry-run",      action="store_true")
    parser.add_argument("--force-rerun",  action="store_true",
                        help="Re-convert pages that are already done")
    args = parser.parse_args()

    settings   = load_settings(args.config)
    manifest   = load_manifest(args.phase, settings)
    cache_dir  = Path(settings.get("cache_dir", "cache"))
    output_dir = Path(settings.get("output_dir", "output"))

    from datetime import datetime
    logs_dir = Path(settings.get("logs_dir", "logs"))
    run_dir  = logs_dir / args.phase / datetime.now().strftime("%Y%m%d-%H%M%S")
    reporter = Reporter(run_dir, "03_convert", dry_run=args.dry_run)

    force_rerun = args.force_rerun
    reporter.info(f"=== Step 3: Convert | phase={args.phase} dry_run={args.dry_run} force_rerun={force_rerun} ===")
    reporter.info(f"Manifest: {len(manifest)} entries")

    for entry in tqdm(manifest, desc="Converting"):
        convert_entry(entry, settings, cache_dir, output_dir, reporter, args.dry_run, force_rerun)

    report = reporter.finish()
    return 0 if report["error_count"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
