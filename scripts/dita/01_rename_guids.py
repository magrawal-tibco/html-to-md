"""
01_rename_guids.py — DITA Step 1 (sdl_dita only): Build GUID → slug rename maps.

For each sdl_dita version:
  1. Parse suitehelp_topic_list.html to get GUID → title + TOC hierarchy
  2. Slugify each title (≤50 chars, word-boundary truncation, per-folder collision)
  3. Scan topic HTML files to build image rename map from alt text
  4. Write manifests/guid_rename_map_{phase}_{vs_key}.json

No files are renamed on disk — the map is used by 02_convert.py to determine
output paths and rewrite cross-links.

Usage:
  python scripts/dita/01_rename_guids.py --phase phase_01
         [--config config/dita_settings.yaml] [--dry-run] [--force-rerun]
"""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import yaml
from bs4 import BeautifulSoup
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.lib.reporter import Reporter


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


def _slugify(text: str, max_length: int = 50) -> str:
    """Convert title text to a URL-safe slug truncated at a word boundary."""
    slug = text.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    if not slug:
        return "topic"
    if len(slug) <= max_length:
        return slug
    truncated = slug[:max_length]
    last_dash = truncated.rfind("-")
    if last_dash > 0:
        truncated = truncated[:last_dash]
    return truncated.strip("-") or slug[:max_length].strip("-")


def _add_slug(slug: str, used: set[str]) -> str:
    """Return slug, appending -2/-3/... if already taken in the same folder."""
    if slug not in used:
        used.add(slug)
        return slug
    counter = 2
    while f"{slug}-{counter}" in used:
        counter += 1
    final = f"{slug}-{counter}"
    used.add(final)
    return final


def _parse_topic_list(html_path: Path) -> tuple[dict[str, str], dict[str, list[str]]]:
    """
    Parse suitehelp_topic_list.html.

    Returns:
      guid_to_title:     {guid_filename: title_text}
      guid_to_ancestors: {guid_filename: [ancestor_title, ...]} (breadcrumb without self)
    """
    soup = BeautifulSoup(html_path.read_bytes(), "html.parser")
    guid_to_title: dict[str, str] = {}
    guid_to_ancestors: dict[str, list[str]] = {}

    def walk(ul_tag, ancestors: list[str]) -> None:
        for li in ul_tag.find_all("li", recursive=False):
            a = li.find("a", recursive=False)
            if not a:
                continue
            href = a.get("href", "")
            title = a.get_text(strip=True)
            if href:
                guid_to_title[href] = title
                guid_to_ancestors[href] = ancestors[:]
            child_ul = li.find("ul", recursive=False)
            if child_ul:
                walk(child_ul, ancestors + ([title] if title else []))

    root_ul = soup.find("ul")
    if root_ul:
        walk(root_ul, [])
    return guid_to_title, guid_to_ancestors


def _build_topic_rename_map(
    html_root: Path,
    guid_to_title: dict[str, str],
    max_length: int,
) -> dict[str, str]:
    """
    Build {guid_filename: slug_filename} for all GUID-*.html files in html_root.

    Title from suitehelp_topic_list.html; fallback to <title> tag in the file.
    Per-folder collision handling (all sdl_dita topic files are in html_root).
    """
    guid_files = sorted(html_root.glob("GUID-*.html"))
    used_slugs: set[str] = set()
    rename_map: dict[str, str] = {}

    for guid_file in guid_files:
        guid_filename = guid_file.name
        title = guid_to_title.get(guid_filename, "")

        if not title:
            try:
                soup = BeautifulSoup(guid_file.read_bytes(), "lxml")
                title_tag = soup.find("title")
                if title_tag:
                    title = title_tag.get_text(strip=True)
                    # Strip " - Product Name" suffix common in DITA titles
                    if " - " in title:
                        title = title.split(" - ")[0].strip()
            except Exception:
                pass

        if not title:
            title = guid_file.stem

        slug = _slugify(title, max_length)
        final_slug = _add_slug(slug, used_slugs)
        rename_map[guid_filename] = final_slug + ".html"

    return rename_map


def _build_image_rename_map(
    html_root: Path,
    generic_alt_words: list[str],
    max_length: int,
) -> dict[str, str]:
    """
    Build {guid_image_filename: new_image_filename} from alt text in topic HTML files.
    Keeps GUID name when alt is empty, too short, or a generic word.
    """
    img_alts: dict[str, list[str]] = {}
    generic_set = {w.lower() for w in generic_alt_words}
    guid_img_re = re.compile(r"GUID-[0-9A-Fa-f-]+-display\.", re.IGNORECASE)

    for html_file in html_root.glob("GUID-*.html"):
        try:
            soup = BeautifulSoup(html_file.read_bytes(), "lxml")
        except Exception:
            continue
        for img in soup.find_all("img", src=True):
            src = img["src"]
            if not guid_img_re.match(src):
                continue
            alt = img.get("alt", "").strip()
            img_alts.setdefault(src, [])
            if alt:
                img_alts[src].append(alt)

    used_by_ext: dict[str, set[str]] = {}
    rename_map: dict[str, str] = {}

    for guid_img, alts in sorted(img_alts.items()):
        suffix = Path(guid_img).suffix.lower()
        used = used_by_ext.setdefault(suffix, set())

        best_alt = Counter(alts).most_common(1)[0][0] if alts else ""
        is_generic = (
            not best_alt
            or len(best_alt) < 4
            or best_alt.lower() in generic_set
        )
        if is_generic:
            rename_map[guid_img] = guid_img
        else:
            slug = _slugify(best_alt, max_length)
            final_slug = _add_slug(slug, used)
            rename_map[guid_img] = final_slug + suffix

    return rename_map


def build_rename_map_for_version(
    version_sitemap: str,
    settings: dict,
    zip_registry: dict,
    reporter: Reporter,
    manifest_entry: dict | None = None,
) -> dict | None:
    """Build the full rename map for one sdl_dita version."""
    cache_dir = Path(settings.get("cache_dir", "cache"))
    slug_cfg = settings.get("slug", {})
    max_length = int(slug_cfg.get("max_length", 50))
    generic_alt_words = slug_cfg.get("generic_alt_words", [])

    reg_entry = zip_registry.get(version_sitemap, {})
    html_root_str = reg_entry.get("html_root", "")
    if not html_root_str and manifest_entry:
        # Fallback: derive html_root from the manifest output_path (versions not in ZIP registry)
        output_path = manifest_entry.get("output_path", "")
        if output_path:
            html_root_str = str(Path(output_path).parent).replace("\\", "/")
            reporter.warning(
                f"html_root not in zip_registry for {version_sitemap} — "
                f"derived from manifest: {html_root_str}"
            )
    if not html_root_str:
        reporter.fail(version_sitemap, "html_root not found in zip_registry")
        return None

    html_root = cache_dir / html_root_str.rstrip("/")
    if not html_root.exists():
        reporter.fail(version_sitemap, f"html_root dir not found: {html_root}")
        return None

    topic_list_path = html_root / "suitehelp_topic_list.html"
    if not topic_list_path.exists():
        reporter.warning(f"suitehelp_topic_list.html not found for {version_sitemap} — falling back to <title> tags")
        guid_to_title, guid_to_ancestors = {}, {}
    else:
        reporter.info(f"    Parsing topic list: {topic_list_path.name}")
        guid_to_title, guid_to_ancestors = _parse_topic_list(topic_list_path)
        reporter.info(f"    Found {len(guid_to_title)} topics in topic list")

    topic_rename = _build_topic_rename_map(html_root, guid_to_title, max_length)
    reporter.info(f"    Topic rename map: {len(topic_rename)} entries")

    # Build toc_path per GUID file (ancestors|title, pipe-separated)
    toc_paths: dict[str, str] = {}
    for guid_filename, ancestors in guid_to_ancestors.items():
        title = guid_to_title.get(guid_filename, "")
        full_path = ancestors + ([title] if title else [])
        toc_paths[guid_filename] = "|".join(full_path)

    reporter.info(f"    Scanning topic files for image alt text...")
    image_rename = _build_image_rename_map(html_root, generic_alt_words, max_length)
    kept_guid = sum(1 for v in image_rename.values() if v.upper().startswith("GUID-"))
    reporter.info(
        f"    Image rename map: {len(image_rename)} images "
        f"({kept_guid} kept as GUID, no useful alt text)"
    )

    return {
        "html_root":  html_root_str,
        "topics":     topic_rename,
        "images":     image_rename,
        "toc_paths":  toc_paths,
    }


def _vs_key(version_sitemap: str) -> str:
    return urlparse(version_sitemap).path.strip("/").replace("/", "_")


def main():
    parser = argparse.ArgumentParser(description="DITA Step 1: Build GUID rename maps (sdl_dita)")
    parser.add_argument("--phase",       required=True)
    parser.add_argument("--config",      default="config/dita_settings.yaml")
    parser.add_argument("--dry-run",     action="store_true")
    parser.add_argument("--force-rerun", action="store_true")
    args = parser.parse_args()

    settings     = load_settings(args.config)
    manifest     = load_manifest(args.phase, settings)
    zip_registry = load_zip_registry(args.phase, settings)
    manifests_dir = Path(settings.get("manifests_dir", "manifests"))

    from datetime import datetime
    logs_dir = Path(settings.get("logs_dir", "logs"))
    run_dir  = logs_dir / args.phase / datetime.now().strftime("%Y%m%d-%H%M%S")
    reporter = Reporter(run_dir, "dita_01_rename", dry_run=args.dry_run)

    reporter.info(
        f"=== DITA Step 1: Rename GUIDs | phase={args.phase} "
        f"dry_run={args.dry_run} force_rerun={args.force_rerun} ==="
    )

    # Collect sdl_dita versions (deduplicated)
    sdl_versions: dict[str, dict] = {}
    for entry in manifest:
        vs = entry.get("version_sitemap", "")
        if not vs or vs in sdl_versions:
            continue
        fmt = zip_registry.get(vs, {}).get("format", "")
        if fmt == "sdl_dita" or entry.get("version_format") == "sdl_dita":
            sdl_versions[vs] = entry

    reporter.info(f"Found {len(sdl_versions)} sdl_dita version(s)")
    if not sdl_versions:
        reporter.info("Nothing to do.")
        reporter.finish()
        return 0

    for version_sitemap in tqdm(sdl_versions, desc="Versions"):
        reporter.info(f"  Processing: {version_sitemap}")

        key      = _vs_key(version_sitemap)
        map_path = manifests_dir / f"guid_rename_map_{args.phase}_{key}.json"

        if map_path.exists() and not args.force_rerun:
            reporter.info(f"    Rename map already exists — skipping (use --force-rerun to rebuild)")
            reporter.count("versions_skipped")
            continue

        rename_map = build_rename_map_for_version(
            version_sitemap, settings, zip_registry, reporter,
            manifest_entry=sdl_versions[version_sitemap],
        )
        if rename_map is None:
            reporter.count("versions_failed")
            continue

        if not args.dry_run:
            map_path.write_text(
                json.dumps(rename_map, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            reporter.info(f"    Written: {map_path}")
        else:
            reporter.info(f"    [dry-run] Would write: {map_path}")
            reporter.info(f"      Topics: {len(rename_map['topics'])} entries")
            reporter.info(f"      Images: {len(rename_map['images'])} entries")

        reporter.count("versions_processed")

    report = reporter.finish()
    return 0 if report["error_count"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
