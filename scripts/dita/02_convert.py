"""
02_convert.py — DITA Step 2: Convert HTML to Markdown (file_dita and sdl_dita).

For each DITA manifest entry:
  1. Determine format (file_dita or sdl_dita) from zip_registry
  2. Parse cached HTML with BeautifulSoup
  3. Skip pages without DC.type meta (shell/index pages)
  4. Extract metadata (title, topic_type, guid, lang, description, toc_path)
  5. Run DITA preprocessor transforms
  6. Convert to Markdown with markdownify
  7. Prepend YAML frontmatter (includes description field)
  8. Write .md file to output/
  9. Copy images to output/ (sdl_dita: using image rename map)

Usage:
  python scripts/dita/02_convert.py --phase phase_01
         [--config config/dita_settings.yaml] [--dry-run] [--force-rerun]
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import urlparse, urljoin

import warnings
import yaml
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from markdownify import markdownify as md

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.lib.reporter import Reporter
from scripts.lib.version_registry import record_converted_versions
from scripts.dita.lib.preprocessor import dita_run_all


def load_settings(config_path: str) -> dict:
    return yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))


def load_manifest(phase: str, settings: dict) -> list[dict]:
    manifests_dir = Path(settings.get("manifests_dir", "manifests"))
    path = manifests_dir / f"manifest_{phase}.json"
    if not path.exists():
        raise FileNotFoundError(f"Manifest not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_zip_registry(phase: str, settings: dict) -> dict:
    manifests_dir = Path(settings.get("manifests_dir", "manifests"))
    path = manifests_dir / f"zip_registry_{phase}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_guid_rename_map(phase: str, version_sitemap: str, settings: dict) -> dict | None:
    manifests_dir = Path(settings.get("manifests_dir", "manifests"))
    key = urlparse(version_sitemap).path.strip("/").replace("/", "_")
    map_path = manifests_dir / f"guid_rename_map_{phase}_{key}.json"
    if not map_path.exists():
        return None
    return json.loads(map_path.read_text(encoding="utf-8"))


def url_to_cache_path(loc: str, cache_dir: Path) -> Path:
    path = urlparse(loc).path.lstrip("/")
    return cache_dir / path


def get_output_path(
    entry: dict,
    fmt: str,
    rename_map: dict | None,
    output_dir: Path,
) -> Path:
    """Determine the output .md path for a manifest entry."""
    url = entry["url"]
    if fmt == "sdl_dita" and rename_map:
        html_root = rename_map.get("html_root", "").rstrip("/")
        guid_filename = Path(urlparse(url).path).name
        slug_filename = rename_map["topics"].get(guid_filename, guid_filename)
        slug_md = re.sub(r"\.html?$", ".md", slug_filename)
        return output_dir / html_root / slug_md

    # file_dita: use output_path from manifest entry or derive from URL
    out_rel = entry.get("output_path", "")
    if out_rel:
        return output_dir / out_rel
    url_path = urlparse(url).path.lstrip("/")
    md_path = re.sub(r"\.html?$", ".md", url_path)
    return output_dir / md_path


def _extract_file_dita_toc_path(soup: BeautifulSoup) -> str:
    """Extract TOC path from #breadcrumbs .crumb links (file_dita only)."""
    crumbs = soup.select("#breadcrumbs .crumb")
    parts = []
    for crumb in crumbs:
        text = crumb.get_text(strip=True)
        if text and text.lower() not in ("home", ""):
            parts.append(text)
    # Last crumb is the current page — include all as toc_path
    return "|".join(parts)


def extract_metadata(
    soup: BeautifulSoup,
    entry: dict,
    fmt: str,
    rename_map: dict | None,
) -> dict:
    """Extract per-page metadata from HTML head tags and manifest entry."""
    html_tag = soup.find("html")
    lang = ""
    if html_tag:
        lang = html_tag.get("lang", html_tag.get("xml:lang", "en-us"))
        lang = lang.replace("_", "-").lower()

    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else ""
    if " - " in title:
        title = title.split(" - ")[0].strip()

    dc_type = soup.find("meta", attrs={"name": "DC.type"})
    topic_type = dc_type.get("content", "").lower() if dc_type else ""

    dc_id = soup.find("meta", attrs={"name": "DC.identifier"})
    guid = dc_id.get("content", "") if dc_id else ""

    description = ""
    for meta_name in ("description", "abstract"):
        meta = soup.find("meta", attrs={"name": meta_name})
        if meta and meta.get("content", "").strip():
            description = meta.get("content", "").strip()
            break

    url = entry["url"]
    guid_filename = Path(urlparse(url).path).name
    if fmt == "sdl_dita" and rename_map:
        toc_path = rename_map.get("toc_paths", {}).get(guid_filename, "")
    else:
        toc_path = _extract_file_dita_toc_path(soup)

    return {
        "title":           title,
        "lang":            lang or "en-us",
        "topic_type":      topic_type,
        "guid":            guid,
        "description":     description,
        "toc_path":        toc_path,
        "product_name":    entry.get("product_name", ""),
        "product_version": entry.get("product_version", ""),
        "doc_name":        entry.get("doc_name", ""),
        "source_url":      url,
    }


def build_frontmatter(meta: dict) -> str:
    data = {
        "title":           meta["title"],
        "source_url":      meta["source_url"],
        "lang":            meta["lang"],
        "topic_type":      meta["topic_type"],
        "guid":            meta["guid"],
        "description":     meta["description"],
        "toc_path":        meta["toc_path"],
        "product_name":    meta["product_name"],
        "product_version": meta["product_version"],
        "doc_name":        meta["doc_name"],
    }
    data = {k: v for k, v in data.items() if v or v == 0}
    return "---\n" + yaml.dump(data, allow_unicode=True, default_flow_style=False) + "---\n\n"


_MC_STYLE_RE = re.compile(r"mc-table-style\s*:[^;\"]+;?", re.IGNORECASE)


def _clean_table_html(table) -> str:
    for el in table.find_all(True):
        if el.get("class"):
            del el["class"]
        if el.get("style"):
            cleaned = _MC_STYLE_RE.sub("", el["style"]).strip().strip(";").strip()
            if cleaned:
                el["style"] = cleaned
            else:
                del el["style"]
        for attr in ("cellspacing", "cellpadding", "border"):
            if el.get(attr) is not None:
                del el[attr]
        if el.name == "col":
            el.decompose()
    return str(table)


def extract_passthrough_tables(content) -> dict[str, str]:
    tables = {}
    for i, table in enumerate(
        content.find_all("table", attrs={"data-converter-passthrough": "true"})
    ):
        placeholder = f"%%PASSTHROUGH-TABLE-{i}%%"
        del table["data-converter-passthrough"]
        tables[placeholder] = _clean_table_html(table)
        p = BeautifulSoup(f"<p>{placeholder}</p>", "lxml").find("p")
        table.replace_with(p)
    return tables


def restore_passthrough_tables(md_text: str, tables: dict[str, str]) -> str:
    for placeholder, html in tables.items():
        md_text = md_text.replace(placeholder, f"\n{html}\n")
    return md_text


def clean_markdown(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = re.sub(r'(?<=\S)\*\*(?=[a-zA-Z0-9])', '** ', text)
    text = re.sub(r'(?<=\S)`(?=[a-zA-Z0-9])', '` ', text)
    return text.strip() + "\n"


def copy_images(
    html_bytes: bytes,
    page_url: str,
    cache_dir: Path,
    output_dir: Path,
    rename_map: dict | None,
    skip_prefixes: list[str],
    dry_run: bool,
) -> int:
    soup = BeautifulSoup(html_bytes, "lxml")
    copied = 0
    guid_img_re = re.compile(r"GUID-[0-9A-Fa-f-]+-display\.", re.IGNORECASE)

    for img in soup.find_all("img", src=True):
        src = img["src"]
        if src.startswith("data:") or src.startswith("http"):
            continue
        if any(src.startswith(pfx) or f"/{pfx}" in src for pfx in skip_prefixes):
            continue
        abs_url = urljoin(page_url, src)
        cached = url_to_cache_path(abs_url, cache_dir)
        if not cached.exists():
            continue
        img_filename = cached.name
        if rename_map and guid_img_re.match(img_filename):
            new_filename = rename_map.get("images", {}).get(img_filename, img_filename)
        else:
            new_filename = img_filename
        img_path_in_url = urlparse(abs_url).path.lstrip("/")
        dest = output_dir / Path(img_path_in_url).parent / new_filename
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cached, dest)
        copied += 1
    return copied


def convert_entry(
    entry: dict,
    fmt: str,
    fmt_settings: dict,
    rename_map: dict | None,
    settings: dict,
    cache_dir: Path,
    output_dir: Path,
    reporter: Reporter,
    dry_run: bool,
    force_rerun: bool,
) -> bool:
    """Convert one manifest entry from HTML to Markdown. Returns True on success."""
    url        = entry["url"]
    cache_path = url_to_cache_path(url, cache_dir)
    out_path   = get_output_path(entry, fmt, rename_map, output_dir)

    if not cache_path.exists():
        reporter.fail(url, "cached HTML not found — run Step 2 first")
        return False

    if out_path.exists() and not dry_run and not force_rerun:
        reporter.count("pages_already_done")
        return True

    try:
        html_bytes = cache_path.read_bytes()
        soup = BeautifulSoup(html_bytes, "lxml")

        # Skip shell pages that lack DC.type meta
        if not soup.find("meta", attrs={"name": "DC.type"}):
            reporter.count("pages_skipped_no_dc_type")
            return True

        meta = extract_metadata(soup, entry, fmt, rename_map)

        content_selector = fmt_settings.get("content_selector", "article")
        content = soup.select_one(content_selector)
        if content is None:
            reporter.fail(url, f"content not found with selector: {content_selector}")
            return False

        if not meta["title"]:
            h1 = content.find("h1")
            meta["title"] = h1.get_text(strip=True) if h1 else Path(url).stem

        chrome_selectors = fmt_settings.get("chrome_selectors", [])
        block_tags = set(settings.get("tables", {}).get("passthrough_block_tags", []))
        page_url_path = urlparse(url).path

        transform_stats = dita_run_all(content, chrome_selectors, page_url_path, block_tags)
        for k, v in transform_stats.items():
            reporter.count(f"transform_{k}", v)

        passthrough_tables = extract_passthrough_tables(content)
        if passthrough_tables:
            reporter.count("transform_tier3_passthrough", len(passthrough_tables))

        md_body = md(
            str(content),
            heading_style="ATX",
            bullets="-",
            newline_style="backslash",
        )
        md_body = clean_markdown(md_body)

        if passthrough_tables:
            md_body = restore_passthrough_tables(md_body, passthrough_tables)
            md_body = clean_markdown(md_body)

        frontmatter = build_frontmatter(meta)
        final_content = frontmatter + md_body

        if not dry_run:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(final_content, encoding="utf-8")

        skip_prefixes = settings.get("image_skip_prefixes", [])
        images_copied = copy_images(
            html_bytes, url, cache_dir, output_dir, rename_map, skip_prefixes, dry_run
        )
        reporter.count("images_copied", images_copied)
        reporter.count("pages_converted")
        return True

    except Exception as exc:
        reporter.fail(url, f"{type(exc).__name__}: {exc}")
        return False


def main():
    parser = argparse.ArgumentParser(description="DITA Step 2: Convert HTML to Markdown")
    parser.add_argument("--phase",       required=True)
    parser.add_argument("--config",      default="config/dita_settings.yaml")
    parser.add_argument("--dry-run",     action="store_true")
    parser.add_argument("--force-rerun", action="store_true")
    args = parser.parse_args()

    settings     = load_settings(args.config)
    manifest     = load_manifest(args.phase, settings)
    zip_registry = load_zip_registry(args.phase, settings)
    cache_dir    = Path(settings.get("cache_dir", "cache"))
    output_dir   = Path(settings.get("output_dir", "output"))

    from datetime import datetime
    logs_dir = Path(settings.get("logs_dir", "logs"))
    run_dir  = logs_dir / args.phase / datetime.now().strftime("%Y%m%d-%H%M%S")
    reporter = Reporter(run_dir, "dita_02_convert", dry_run=args.dry_run)

    reporter.info(
        f"=== DITA Step 2: Convert | phase={args.phase} "
        f"dry_run={args.dry_run} force_rerun={args.force_rerun} ==="
    )

    # Collect DITA versions and their formats
    dita_versions: dict[str, str] = {}  # {version_sitemap: format}
    for vs, reg in zip_registry.items():
        fmt = reg.get("format", "")
        if fmt in ("file_dita", "sdl_dita"):
            dita_versions[vs] = fmt
    # Fallback: manifest version_format for sdl_dita (pre-Step-2a runs)
    for entry in manifest:
        vs = entry.get("version_sitemap", "")
        if vs and vs not in dita_versions and entry.get("version_format") == "sdl_dita":
            dita_versions[vs] = "sdl_dita"

    n_file = sum(1 for f in dita_versions.values() if f == "file_dita")
    n_sdl  = sum(1 for f in dita_versions.values() if f == "sdl_dita")
    reporter.info(f"DITA versions: {len(dita_versions)} ({n_file} file_dita, {n_sdl} sdl_dita)")

    if not dita_versions:
        reporter.info("No DITA versions found. Exiting.")
        reporter.finish()
        return 0

    # Load rename maps for sdl_dita versions
    rename_maps: dict[str, dict | None] = {}
    for vs, fmt in dita_versions.items():
        if fmt == "sdl_dita":
            rm = load_guid_rename_map(args.phase, vs, settings)
            if rm is None:
                reporter.info(f"  WARNING: No rename map for {vs} — run DITA step 1 first")
            rename_maps[vs] = rm
        else:
            rename_maps[vs] = None

    file_dita_settings = settings.get("file_dita", {})
    sdl_dita_settings  = settings.get("sdl_dita", {})

    version_errors: dict[str, int] = {vs: 0 for vs in dita_versions}
    dita_entries = [e for e in manifest if e.get("version_sitemap", "") in dita_versions]
    reporter.info(f"Manifest entries for DITA: {len(dita_entries)}")

    for entry in tqdm(dita_entries, desc="Converting DITA"):
        vs           = entry.get("version_sitemap", "")
        fmt          = dita_versions[vs]
        fmt_settings = file_dita_settings if fmt == "file_dita" else sdl_dita_settings
        rename_map   = rename_maps.get(vs)

        ok = convert_entry(
            entry, fmt, fmt_settings, rename_map, settings,
            cache_dir, output_dir, reporter, args.dry_run, args.force_rerun
        )
        if not ok:
            version_errors[vs] = version_errors.get(vs, 0) + 1

    manifests_dir = Path(settings.get("manifests_dir", "manifests"))
    newly_registered = record_converted_versions(
        dita_entries, version_errors, args.phase, manifests_dir, dry_run=args.dry_run
    )
    if newly_registered:
        reporter.info(f"Version registry: {len(newly_registered)} version(s) registered")
        for vs in newly_registered:
            reporter.info(f"  + {vs}")

    report = reporter.finish()
    return 0 if report["error_count"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
