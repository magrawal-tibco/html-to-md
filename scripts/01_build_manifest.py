"""
01_build_manifest.py — Step 1: Crawl sitemaps and build a manifest JSON.

Reads a phase YAML file (list of L2 product sitemap URLs), crawls to L3 version
sitemaps, filters for HTML-only URLs, and writes a manifest JSON that drives all
downstream steps.

Usage:
  python scripts/01_build_manifest.py --phase phase_01 [--config config/settings.yaml] [--dry-run]
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yaml

# Allow running from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.lib.reporter import Reporter
from scripts.lib.sitemap_parser import build_http_client, iter_product_versions, iter_version_entries
from scripts.lib.version_registry import load_registry


def load_settings(config_path: str) -> dict:
    return yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))


def load_phase(phase_name: str, settings: dict) -> dict:
    phases_dir = Path(settings.get("manifests_dir", "manifests")).parent / "config" / "phases"
    # Try config/phases/ first, then phases/ for backwards compat
    for candidate in [
        Path("config") / "phases" / f"{phase_name}.yaml",
        Path("phases") / f"{phase_name}.yaml",
    ]:
        if candidate.exists():
            return yaml.safe_load(candidate.read_text(encoding="utf-8"))
    raise FileNotFoundError(f"Phase file not found for '{phase_name}'")


def should_skip_url(loc: str, settings: dict) -> tuple[bool, str]:
    """
    Return (True, reason) if the URL should be excluded from the manifest.
    Return (False, "") if it should be included.
    """
    parsed = urlparse(loc)
    path = parsed.path
    filename = Path(path).name

    # 1. Extension filter — only accept HTML
    suffix = Path(path).suffix.lower()
    html_exts = set(settings.get("html_extensions", [".htm", ".html"]))
    if suffix and suffix not in html_exts:
        return True, f"non-html-extension:{suffix}"

    # 2. Shell page filter
    skip_filenames = [f.lower() for f in settings.get("skip_filenames", [])]
    if filename.lower() in skip_filenames:
        return True, f"shell-page:{filename}"

    # 3. Filename pattern filter — kept here as a safety net for per-URL checks,
    #    but GUID-based versions are detected and skipped wholesale before entry
    #    iteration (see _is_dita_version), so this rarely fires in practice.
    for pattern in settings.get("skip_filename_patterns", []):
        if re.match(pattern, filename, re.IGNORECASE):
            return True, "non-madcap-dita"

    # 4. Path segment filter (javadoc, _globalpages, etc.)
    skip_segments = settings.get("skip_path_segments", [])
    for seg in skip_segments:
        if seg.rstrip("/") in path:
            return True, f"skip-path:{seg.strip('/')}"

    return False, ""


def url_to_output_path(loc: str) -> str:
    """
    Map a docs.tibco.com URL to its output .md path.
    e.g. https://docs.tibco.com/pub/foo/1.0/doc/html/Admin/file.htm
      →  pub/foo/1.0/doc/html/Admin/file.md
    """
    path = urlparse(loc).path.lstrip("/")
    return str(Path(path).with_suffix(".md"))


def infer_alias_xml_url(loc: str) -> str:
    """
    Derive the alias.xml URL for a version given one of its page URLs.
    Finds the /doc/html/ root and appends Data/Alias.xml.
    e.g. https://docs.tibco.com/pub/foo/1.0/doc/html/Admin/file.htm
      →  https://docs.tibco.com/pub/foo/1.0/doc/html/Data/Alias.xml
    """
    parsed = urlparse(loc)
    path = parsed.path
    marker = "/doc/html/"
    idx = path.find(marker)
    if idx == -1:
        # Fallback: use the directory two levels up from the file.
        # Use as_posix() + trailing slash to match the marker-found format.
        html_root = Path(path).parent.parent.as_posix() + "/"
    else:
        html_root = path[: idx + len(marker)]
    base = f"{parsed.scheme}://{parsed.netloc}"
    return f"{base}{html_root}Data/Alias.xml"


def infer_zip_url(loc: str, version_sitemap: str, product_version: str) -> str:
    """
    Derive the documentation ZIP URL for a version.
    Pattern: https://docs.tibco.com/pub/{pub_slug}/{version}/{l2_slug}-{version_dashes}_documentation.zip

    e.g. url=".../pub/dsp_gridserver/7.2.0/...", version_sitemap="...tibco-datasynapse-gridserver-manager-7-2-0.xml"
      →  https://docs.tibco.com/pub/dsp_gridserver/7.2.0/tibco-datasynapse-gridserver-manager-7-2-0_documentation.zip
    """
    parsed         = urlparse(loc)
    pub_slug       = parsed.path.split("/")[2]            # e.g. "dsp_gridserver"
    version_dashes = product_version.replace(".", "-")    # e.g. "7-2-0"
    v_stem         = Path(urlparse(version_sitemap).path).stem  # "tibco-...-7-2-0"
    l2_slug        = v_stem.removesuffix("-" + version_dashes)  # "tibco-..."
    base           = f"{parsed.scheme}://{parsed.netloc}"
    return f"{base}/pub/{pub_slug}/{product_version}/{l2_slug}-{version_dashes}_documentation.zip"


def _is_dita_version(entries: list, patterns: list[str]) -> bool:
    """
    Return True if ANY entry in this version has a GUID-based (DITA) filename.
    One GUID file is enough to classify the entire version as non-MadCap DITA output.
    """
    for entry in entries:
        filename = Path(urlparse(entry.loc).path).name
        if any(re.match(p, filename, re.IGNORECASE) for p in patterns):
            return True
    return False


def build_manifest(phase: dict, settings: dict, reporter: Reporter, dry_run: bool,
                   ignore_registry: bool = False) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Crawl all product sitemaps in the phase and return (manifest, dita_versions, empty_versions).

    manifest       — accepted pages (madcap + sdl_dita); each entry has a 'version_format' field.
                     sdl_dita versions use GUID-based filenames (SDL Trisoft output).
                     file_dita versions are detected later in Step 2a after ZIP extraction.
    dita_versions  — always empty (kept for backward compatibility); sdl_dita versions are now
                     included in manifest with version_format='sdl_dita'
    empty_versions — version sitemaps with entries but zero accepted HTML pages

    ignore_registry — if True, include versions already in converted_versions.json
    """
    delay = settings.get("http", {}).get("delay_seconds", 0.5)
    client = build_http_client(settings)
    manifest: list[dict] = []
    dita_versions: list[dict] = []
    empty_versions: list[dict] = []

    dita_patterns = settings.get("skip_filename_patterns", [])

    # Load the version registry to skip already-converted versions
    manifests_dir = Path(settings.get("manifests_dir", "manifests"))
    registry = {} if ignore_registry else load_registry(manifests_dir)
    if registry and not ignore_registry:
        reporter.info(f"Version registry loaded: {len(registry)} previously converted version(s) will be skipped")
        reporter.info("  (use --ignore-registry to include them anyway)")

    # Track alias.xml URLs per version to avoid duplicates
    seen_alias: dict[str, str] = {}  # version_sitemap_url → alias_xml_url

    products = phase.get("products", [])
    reporter.info(f"Processing {len(products)} product sitemap(s) from phase '{phase.get('name')}'")

    for product_url in products:
        reporter.info(f"  Product: {product_url}")
        try:
            for version_url, entries in iter_product_versions(client, product_url):
                reporter.info(f"    Version sitemap: {version_url} ({len(entries)} raw entries)")
                reporter.count("versions_found")

                # Registry check: skip versions that were already fully converted
                if version_url in registry:
                    rec = registry[version_url]
                    reporter.info(
                        f"      -> SKIPPED (already converted on {rec.get('converted_at', '?')}, "
                        f"phase={rec.get('phase', '?')}, {rec.get('page_count', '?')} pages)"
                    )
                    reporter.count("skipped_already_converted", len(entries))
                    reporter.count("versions_skipped_registry")
                    time.sleep(delay)
                    continue

                # Version-level format detection: GUID filenames → sdl_dita, otherwise → madcap.
                # file_dita (DITA WebHelp Responsive with regular filenames) is detected later
                # in Step 2a after ZIP extraction and stored in zip_registry.
                version_format = "sdl_dita" if (
                    dita_patterns and entries and _is_dita_version(entries, dita_patterns)
                ) else "madcap"
                if version_format == "sdl_dita":
                    reporter.count("sdl_dita_versions_found")
                    reporter.info(f"      -> SDL DITA version detected (GUID-based filenames) — included in manifest")

                version_manifest = []
                alias_xml_url = None

                for entry in entries:
                    skip, reason = should_skip_url(entry.loc, settings)
                    # sdl_dita pages have GUID filenames by design — don't re-filter by GUID pattern
                    if skip and version_format == "sdl_dita" and reason == "non-madcap-dita":
                        skip, reason = False, ""
                    if skip:
                        reporter.skip(entry.loc, reason)
                        reporter.count(f"skipped_{reason.split(':')[0]}")
                        continue

                    output_path = url_to_output_path(entry.loc)

                    # Derive alias.xml URL from first accepted entry in this version
                    if alias_xml_url is None:
                        alias_xml_url = infer_alias_xml_url(entry.loc)
                        seen_alias[version_url] = alias_xml_url

                    manifest_entry = {
                        "url":             entry.loc,
                        "lastmod":         entry.lastmod,
                        "output_path":     output_path,
                        "product_name":    entry.product_name,
                        "product_version": entry.product_version,
                        "doc_name":        entry.doc_name,
                        "access_level":    entry.access_level,
                        "version_sitemap": version_url,
                        "alias_xml_url":   alias_xml_url,
                        "zip_url":         infer_zip_url(entry.loc, version_url, entry.product_version),
                        "version_format":  version_format,
                    }
                    version_manifest.append(manifest_entry)
                    reporter.count("pages_included")

                reporter.info(f"      -> {len(version_manifest)} HTML pages accepted ({version_format})")
                if not version_manifest and entries:
                    empty_versions.append({
                        "version_sitemap":  version_url,
                        "product_sitemap":  product_url,
                        "product_name":     entries[0].product_name,
                        "product_version":  entries[0].product_version,
                        "raw_page_count":   len(entries),
                    })
                    reporter.count("versions_empty")
                manifest.extend(version_manifest)
                time.sleep(delay)

        except Exception as exc:
            reporter.fail(product_url, str(exc), step="01_build_manifest")
            reporter.count("product_errors")

    # Process version-level sitemaps (L3 URLs listed directly under 'versions:' in phase YAML)
    version_direct = phase.get("versions", [])
    if version_direct:
        reporter.info(f"Processing {len(version_direct)} directly specified version sitemap(s)")
    for version_url in version_direct:
        reporter.info(f"  Version: {version_url}")
        try:
            fetched_url, entries = iter_version_entries(client, version_url)
            reporter.info(f"    {fetched_url} ({len(entries)} raw entries)")
            reporter.count("versions_found")

            if fetched_url in registry:
                rec = registry[fetched_url]
                reporter.info(
                    f"      -> SKIPPED (already converted on {rec.get('converted_at', '?')}, "
                    f"phase={rec.get('phase', '?')}, {rec.get('page_count', '?')} pages)"
                )
                reporter.count("skipped_already_converted", len(entries))
                reporter.count("versions_skipped_registry")
                time.sleep(delay)
                continue

            version_format = "sdl_dita" if (
                dita_patterns and entries and _is_dita_version(entries, dita_patterns)
            ) else "madcap"
            if version_format == "sdl_dita":
                reporter.count("sdl_dita_versions_found")
                reporter.info(f"      -> SDL DITA version detected (GUID-based filenames) — included in manifest")

            version_manifest = []
            alias_xml_url = None

            for entry in entries:
                skip, reason = should_skip_url(entry.loc, settings)
                if skip and version_format == "sdl_dita" and reason == "non-madcap-dita":
                    skip, reason = False, ""
                if skip:
                    reporter.skip(entry.loc, reason)
                    reporter.count(f"skipped_{reason.split(':')[0]}")
                    continue

                output_path = url_to_output_path(entry.loc)

                if alias_xml_url is None:
                    alias_xml_url = infer_alias_xml_url(entry.loc)

                version_manifest.append({
                    "url":             entry.loc,
                    "lastmod":         entry.lastmod,
                    "output_path":     output_path,
                    "product_name":    entry.product_name,
                    "product_version": entry.product_version,
                    "doc_name":        entry.doc_name,
                    "access_level":    entry.access_level,
                    "version_sitemap": fetched_url,
                    "alias_xml_url":   alias_xml_url,
                    "zip_url":         infer_zip_url(entry.loc, fetched_url, entry.product_version),
                    "version_format":  version_format,
                })
                reporter.count("pages_included")

            reporter.info(f"      -> {len(version_manifest)} HTML pages accepted ({version_format})")
            if not version_manifest and entries:
                empty_versions.append({
                    "version_sitemap":  fetched_url,
                    "product_sitemap":  None,
                    "product_name":     entries[0].product_name,
                    "product_version":  entries[0].product_version,
                    "raw_page_count":   len(entries),
                })
                reporter.count("versions_empty")
            manifest.extend(version_manifest)
            time.sleep(delay)

        except Exception as exc:
            reporter.fail(version_url, str(exc), step="01_build_manifest")
            reporter.count("version_errors")

    return manifest, dita_versions, empty_versions


def main():
    parser = argparse.ArgumentParser(description="Step 1: Build sitemap manifest")
    parser.add_argument("--phase",            required=True, help="Phase name, e.g. phase_01")
    parser.add_argument("--config",           default="config/settings.yaml")
    parser.add_argument("--dry-run",          action="store_true", help="Parse sitemaps but do not write manifest")
    parser.add_argument("--ignore-registry",  action="store_true",
                        help="Include versions already in converted_versions.json instead of skipping them")
    args = parser.parse_args()

    settings = load_settings(args.config)
    phase    = load_phase(args.phase, settings)

    # Set up run directory
    from datetime import datetime
    logs_dir = Path(settings.get("logs_dir", "logs"))
    run_dir  = logs_dir / args.phase / datetime.now().strftime("%Y%m%d-%H%M%S")
    reporter = Reporter(run_dir, "01_manifest", dry_run=args.dry_run)

    reporter.info(f"=== Step 1: Build Manifest | phase={args.phase} dry_run={args.dry_run} "
                  f"ignore_registry={args.ignore_registry} ===")

    manifest, dita_versions, empty_versions = build_manifest(
        phase, settings, reporter, args.dry_run,
        ignore_registry=args.ignore_registry,
    )

    reporter.info(f"Manifest complete: {len(manifest)} pages across "
                  f"{reporter._counts.get('versions_found', 0)} versions")
    sdl_dita_count = reporter._counts.get("sdl_dita_versions_found", 0)
    if sdl_dita_count:
        reporter.info(f"SDL DITA versions: {sdl_dita_count} version(s) included in manifest with version_format=sdl_dita")
    if empty_versions:
        reporter.info(f"Empty versions: {len(empty_versions)} version(s) with no accepted HTML pages")

    if not args.dry_run:
        manifests_dir = Path(settings.get("manifests_dir", "manifests"))
        manifests_dir.mkdir(parents=True, exist_ok=True)

        out_path = manifests_dir / f"manifest_{args.phase}.json"
        out_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        reporter.info(f"Manifest written to {out_path}")

        if dita_versions:
            dita_path = manifests_dir / f"dita_versions_{args.phase}.json"
            dita_path.write_text(
                json.dumps(dita_versions, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            reporter.info(f"DITA versions written to {dita_path}")

        empty_path = manifests_dir / f"empty_versions_{args.phase}.json"
        empty_path.write_text(
            json.dumps(empty_versions, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        reporter.info(f"Empty versions written to {empty_path}")
    else:
        reporter.info("Dry run — manifest not written")

    report = reporter.finish()
    return 0 if report["error_count"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
