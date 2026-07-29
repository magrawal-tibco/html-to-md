"""
02a_download_zip.py — Step 2a: Download and extract per-version documentation ZIPs.

For each unique version in the manifest, downloads the full WebHelp2 ZIP from
docs.tibco.com and extracts it into the cache directory. This gives:
  - Authoritative TOC JS files (Data/Tocs/*.js) for Step 6
  - All HTML pages and images in one request per version instead of hundreds

Versions where the ZIP is unavailable are written to zip_missing_{phase}.json
and fall back to individual page downloading in Step 2.

Already-extracted versions (Data/Tocs/ present and non-empty) are skipped unless
--force-rerun is set.

Usage:
  python scripts/02a_download_zip.py --phase phase_01 [--config config/settings.yaml]
                                     [--dry-run] [--force-rerun]
"""

import argparse
import json
import shutil
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import httpx
import yaml
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.lib.manifest_utils import infer_alias_xml_url, should_skip_url
from scripts.lib.reporter import Reporter


def load_settings(config_path: str) -> dict:
    return yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))


def load_manifest(phase: str, settings: dict) -> list[dict]:
    manifests_dir = Path(settings.get("manifests_dir", "manifests"))
    path = manifests_dir / f"manifest_{phase}.json"
    if not path.exists():
        raise FileNotFoundError(f"Manifest not found: {path}. Run Step 1 first.")
    return json.loads(path.read_text(encoding="utf-8"))


def alias_xml_to_html_root(alias_xml_url: str) -> str:
    """
    Derive the html_root cache prefix from an alias_xml_url.
    e.g. https://docs.tibco.com/pub/foo/1.0/doc/html/Data/Alias.xml
      →  pub/foo/1.0/doc/html/
    """
    path = urlparse(alias_xml_url).path   # /pub/foo/1.0/doc/html/Data/Alias.xml
    return PurePosixPath(path).parent.parent.as_posix().lstrip("/") + "/"


def collect_versions(manifest: list[dict]) -> dict[str, dict]:
    """
    Deduplicate manifest entries by version key.
    - New format (version-level entry): keyed by version_url
    - Old format (per-page entry):      keyed by version_sitemap
    Returns {key: representative_entry}.
    """
    versions: dict[str, dict] = {}
    for entry in manifest:
        if "version_url" in entry and "url" not in entry:
            # New format: standalone version-level entry
            key = entry["version_url"]
        else:
            # Old format: per-page entry grouped by version_sitemap
            key = entry.get("version_sitemap", "")
        if key and key not in versions:
            versions[key] = entry
    return versions


def is_already_extracted(cache_dir: Path, html_root: str) -> bool:
    """Return True if the version's content has already been extracted from a ZIP."""
    root = cache_dir / html_root.rstrip("/")
    # MadCap: Data/Tocs/ with JS files
    tocs_dir = root / "Data" / "Tocs"
    if tocs_dir.exists() and any(tocs_dir.glob("*.js")):
        return True
    # DITA WebHelp Responsive: static/body.js
    if (root / "static" / "body.js").exists():
        return True
    # EBX: look for ebx_common.css at root level or one directory deep (lang/ or module/)
    for check_dir in ([root] + list(root.iterdir() if root.exists() else [])):
        if check_dir.is_dir() and (check_dir / "resources" / "stylesheets" / "ebx_common.css").exists():
            return True
    # EBX addon: module subdirs under doc/ when doc/html/ doesn't exist
    if not root.exists():
        doc_dir = root.parent
        if doc_dir.exists():
            for subdir in doc_dir.iterdir():
                if subdir.is_dir() and (subdir / "resources" / "stylesheets" / "ebx_common.css").exists():
                    return True
    return False


def detect_format(cache_dir: Path, html_root: str) -> str:
    """
    Detect the documentation format from the extracted ZIP contents.
    Returns 'madcap', 'file_dita', 'sdl_dita', or 'unknown'.
    """
    root = cache_dir / html_root.rstrip("/")
    if (root / "static" / "body.js").exists():
        # DITA WebHelp Responsive — distinguish by whether topic files are GUID-named
        guid_files = list(root.glob("GUID-*.html"))
        return "sdl_dita" if guid_files else "file_dita"
    tocs_dir = root / "Data" / "Tocs"
    if tocs_dir.exists() and any(tocs_dir.glob("*.js")):
        return "madcap"
    # EBX: look for ebx_common.css at root level or one directory deep (lang/ or module/)
    for check_dir in ([root] + list(root.iterdir() if root.exists() else [])):
        if check_dir.is_dir() and (check_dir / "resources" / "stylesheets" / "ebx_common.css").exists():
            return "ebx"
    # EBX addon: module subdirs under doc/ when doc/html/ doesn't exist
    if not root.exists():
        doc_dir = root.parent
        if doc_dir.exists():
            for subdir in doc_dir.iterdir():
                if subdir.is_dir() and (subdir / "resources" / "stylesheets" / "ebx_common.css").exists():
                    return "ebx"
    return "unknown"


def has_enough_disk_space(min_free_gb: float) -> bool:
    free_gb = shutil.disk_usage(".").free / (1024 ** 3)
    return free_gb >= min_free_gb


def _download_zip(
    client: httpx.Client,
    zip_url: str,
    zip_path: Path,
    reporter: Reporter,
) -> tuple[bool, str]:
    """Stream-download a ZIP file. Returns (success, reason_on_failure)."""
    try:
        with client.stream("GET", zip_url) as resp:
            if resp.status_code == 404:
                return False, "http_404"
            resp.raise_for_status()
            zip_path.parent.mkdir(parents=True, exist_ok=True)
            with open(zip_path, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                    f.write(chunk)
        return True, ""
    except httpx.HTTPStatusError as e:
        return False, f"http_{e.response.status_code}"
    except Exception as e:
        return False, f"error_{type(e).__name__}"


def _extract_zip(
    zip_path: Path,
    cache_dir: Path,
    html_root: str,
) -> tuple[bool, str, int]:
    """
    Extract all ZIP members to cache_dir/<version_root>/ where version_root is
    two levels above html_root (stripping the trailing doc/html/).

    TIBCO ZIPs wrap everything in one top-level product folder
    (e.g. tibco-foo-1-2-3/) and then mirror the full URL path from the version
    root downward (doc/html/..., pdf/..., etc.). We strip the product folder
    and extract relative to the version root so paths match url_to_cache_path().

    Returns (success, reason_on_failure, file_count).
    """
    if not zipfile.is_zipfile(zip_path):
        return False, "corrupt_zip", 0

    # extract_base = pub/foo/1.0/doc  (html_root = pub/foo/1.0/doc/html/)
    # TIBCO ZIPs contain paths starting with html/... so they land correctly
    # at cache/<extract_base>/html/... = cache/pub/foo/1.0/doc/html/...
    extract_base = Path(html_root.rstrip("/")).parent

    file_count = 0
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            # Detect and strip a common top-level directory wrapper.
            top_dirs = {
                m.filename.replace("\\", "/").split("/")[0]
                for m in zf.infolist()
                if "/" in m.filename.replace("\\", "/")
            }
            strip_prefix = (top_dirs.pop() + "/") if len(top_dirs) == 1 else ""

            for member in zf.infolist():
                rel = member.filename.replace("\\", "/")
                if strip_prefix and rel.startswith(strip_prefix):
                    rel = rel[len(strip_prefix):]
                if not rel or rel.endswith("/"):
                    continue
                dest = cache_dir / extract_base / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                file_count += 1
        return True, "", file_count
    except zipfile.BadZipFile:
        return False, "corrupt_zip", file_count
    except Exception as e:
        return False, f"extract_error_{type(e).__name__}", file_count


def process_versions(
    versions: dict[str, dict],
    settings: dict,
    reporter: Reporter,
    dry_run: bool,
    force_rerun: bool,
) -> tuple[dict, dict]:
    """
    Download and extract ZIPs for all versions.
    Returns (zip_registry, zip_missing).
    """
    zip_cfg       = settings.get("zip", {})
    cache_dir     = Path(settings.get("cache_dir", "cache"))
    zip_cache_dir = Path(zip_cfg.get("zip_cache_dir", "cache/zip"))
    min_free_gb   = float(zip_cfg.get("min_free_gb", 20))
    store_zip     = zip_cfg.get("store_zip", True)
    http_cfg      = settings.get("http", {})
    delay         = http_cfg.get("delay_seconds", 0.5)

    zip_registry: dict = {}
    zip_missing:  dict = {}

    client = httpx.Client(
        headers={"User-Agent": http_cfg.get("user_agent", "tibco-docs-converter/1.0")},
        timeout=httpx.Timeout(
            connect=http_cfg.get("timeout_connect", 10),
            read=600,   # ZIPs can be several hundred MB — generous timeout
            write=10,
            pool=10,
        ),
        follow_redirects=True,
    )

    with client:
        for version_key, entry in tqdm(versions.items(), desc="Versions"):
            is_new_format = "version_url" in entry and "url" not in entry
            zip_url = entry.get("zip_url", "")

            if is_new_format:
                pub_slug        = entry.get("pub_slug", "")
                product_version = entry.get("product_version", "")
                if not zip_url or not pub_slug or not product_version:
                    reporter.info(f"  SKIP {version_key} — missing zip_url, pub_slug, or product_version")
                    zip_missing[version_key] = {
                        "zip_url":  zip_url,
                        "reason":   "missing_fields",
                        "fallback": "web_crawl",
                    }
                    reporter.count("zip_missing")
                    continue
                html_root = f"pub/{pub_slug}/{product_version}/doc/html/"
            else:
                alias_url = entry.get("alias_xml_url", "")
                if not zip_url or not alias_url:
                    reporter.info(f"  SKIP {version_key} — missing zip_url or alias_xml_url")
                    zip_missing[version_key] = {
                        "zip_url":  zip_url,
                        "reason":   "missing_zip_url",
                        "fallback": "web_crawl",
                    }
                    reporter.count("zip_missing")
                    continue
                html_root = alias_xml_to_html_root(alias_url)

            reporter.info(f"  Version: {version_key}")
            reporter.info(f"    html_root: {html_root}")

            # Skip if already extracted (unless --force-rerun)
            if not force_rerun and is_already_extracted(cache_dir, html_root):
                fmt = detect_format(cache_dir, html_root)
                reporter.info(f"    -> Already extracted (format={fmt}) — skipping")
                reporter.count("zip_already_extracted")
                zip_registry[version_key] = {
                    "zip_url":      zip_url,
                    "html_root":    html_root,
                    "extracted_at": "previously",
                    "file_count":   -1,
                    "format":       fmt,
                }
                continue

            # Disk-space guard
            if not has_enough_disk_space(min_free_gb):
                free_gb = shutil.disk_usage(".").free / (1024 ** 3)
                reporter.info(
                    f"    -> SKIP: only {free_gb:.1f} GB free, need {min_free_gb} GB"
                )
                zip_missing[version_key] = {
                    "zip_url":  zip_url,
                    "reason":   "disk_space",
                    "fallback": "web_crawl",
                }
                reporter.count("zip_missing")
                continue

            zip_url_path = urlparse(zip_url).path.lstrip("/")
            zip_path     = zip_cache_dir / zip_url_path

            if dry_run:
                reporter.info(f"    [dry-run] Would download: {zip_url}")
                reporter.info(f"    [dry-run] Would extract to: {cache_dir / html_root}")
                reporter.count("zip_dry_run")
                continue

            # Reuse cached ZIP if already downloaded and valid
            if zip_path.exists() and zipfile.is_zipfile(zip_path):
                reporter.info(f"    Reusing cached ZIP: {zip_path}")
                reporter.count("zip_cached")
                ok, fail_reason = True, ""
            else:
                # Download
                reporter.info(f"    Downloading: {zip_url}")
                ok, fail_reason = _download_zip(client, zip_url, zip_path, reporter)
            if not ok:
                reporter.info(f"    -> Download failed: {fail_reason}")
                zip_missing[version_key] = {
                    "zip_url":  zip_url,
                    "reason":   fail_reason,
                    "fallback": "web_crawl",
                }
                reporter.count("zip_missing")
                time.sleep(delay)
                continue

            size_kb = zip_path.stat().st_size // 1024
            reporter.info(f"    Downloaded: {size_kb:,} KB → {zip_path}")
            reporter.count("zip_downloaded")

            # Extract
            ok, fail_reason, file_count = _extract_zip(zip_path, cache_dir, html_root)
            if not ok:
                reporter.info(f"    -> Extraction failed: {fail_reason}")
                zip_missing[version_key] = {
                    "zip_url":  zip_url,
                    "reason":   fail_reason,
                    "fallback": "web_crawl",
                }
                reporter.count("zip_missing")
                zip_path.unlink(missing_ok=True)
                time.sleep(delay)
                continue

            reporter.info(f"    Extracted {file_count} files to {cache_dir / html_root.rstrip('/')}")
            reporter.count("zip_extracted")

            if not store_zip:
                zip_path.unlink(missing_ok=True)
                reporter.count("zip_deleted")

            zip_registry[version_key] = {
                "zip_url":      zip_url,
                "html_root":    html_root,
                "extracted_at": datetime.now().isoformat(timespec="seconds"),
                "file_count":   file_count,
                "format":       detect_format(cache_dir, html_root),
            }
            time.sleep(delay)

    return zip_registry, zip_missing


def _scan_ebx_from_nav(
    html_dir: Path,
    cache_dir: Path,
    html_root: str,
    version_url: str,
    zip_url: str,
    product_name: str,
    product_version: str,
    alias_xml_url: str,
    version_format: str,
    settings: dict,
) -> list[dict]:
    """For EBX: derive ordered page list from index.html nav tree instead of file scan."""
    import re as _re
    from bs4 import BeautifulSoup

    skip_filenames_set = set(settings.get("skip_filenames", []))
    skip_patterns = [
        _re.compile(p, _re.IGNORECASE)
        for p in settings.get("skip_filename_patterns", [])
    ]

    # Find all index.html files that have an EBX nav:
    # EBX main  → html_dir/en/index.html (and fr/ if present)
    # EBX addon → html_dir/{module}/index.html for each module
    html_extensions = set(settings.get("html_extensions", [".htm", ".html"]))
    index_candidates = sorted(html_dir.rglob("index.html"))
    abs_cache_dir = cache_dir.resolve()

    results: list[dict] = []
    seen: set[str] = set()

    for index_path in index_candidates:
        try:
            soup = BeautifulSoup(index_path.read_bytes(), "html.parser")
        except Exception:
            continue
        nav_ul = soup.select_one("div#ebx_NavigationPagesList > ul")
        if not nav_ul:
            continue
        base_dir = index_path.parent

        def walk(ul) -> None:
            for li in ul.find_all("li", recursive=False):
                href = ""
                span = li.find("span", recursive=False)
                if span:
                    # 6.x style: <li><span><a href="...">Title</a></span>
                    a = span.find("a")
                    if a:
                        href = a.get("href", "").split("?")[0].split("#")[0]
                else:
                    # 4.x style: <li><a href="..."><span>Title</span></a>
                    a = li.find("a", recursive=False)
                    if a:
                        href = a.get("href", "").split("?")[0].split("#")[0]

                if href and Path(href).suffix.lower() in html_extensions:
                    fname = Path(href).name
                    if fname not in skip_filenames_set and not any(
                        p.match(fname) for p in skip_patterns
                    ):
                        resolved = (base_dir / href).resolve()
                        key = str(resolved)
                        if key not in seen and resolved.exists():
                            seen.add(key)
                            rel = resolved.relative_to(abs_cache_dir)
                            rel_to_html_dir = resolved.relative_to(html_dir.resolve())
                            url = f"https://docs.tibco.com/{html_root}/{rel_to_html_dir.as_posix()}"
                            results.append({
                                "url":             url,
                                "lastmod":         "",
                                "output_path":     str(rel.with_suffix(".md")),
                                "cache_path":      rel.as_posix(),
                                "product_name":    product_name,
                                "product_version": product_version,
                                "doc_name":        "",
                                "access_level":    "public",
                                "version_sitemap": version_url,
                                "alias_xml_url":   alias_xml_url,
                                "zip_url":         zip_url,
                                "version_format":  version_format,
                            })

                child_ul = li.find("ul", recursive=False)
                if child_ul:
                    walk(child_ul)

        walk(nav_ul)

    return results


def scan_extracted_pages(
    cache_dir: Path,
    entry: dict,
    zip_registry_entry: dict,
    settings: dict,
) -> list[dict]:
    """
    Scan extracted HTML files and return per-page manifest entries for a
    new-format version-level entry. Sets version_sitemap=version_url so
    all downstream steps (2, 3, 4, 5, 6) treat it like a normal per-page entry.
    """
    version_url     = entry["version_url"]
    zip_url         = entry["zip_url"]
    product_name    = entry.get("product_name", "")
    product_version = entry.get("product_version", "")
    html_root       = zip_registry_entry["html_root"].rstrip("/")
    version_format  = zip_registry_entry.get("format", entry.get("version_format", "unknown"))

    html_dir = cache_dir / html_root
    # EBX addon: ZIPs use doc/{module}/ instead of doc/html/ — fall back to parent
    if not html_dir.exists():
        parent = html_dir.parent
        if parent.exists():
            html_dir = parent

    alias_xml_url = f"https://docs.tibco.com/{html_root}/Data/Alias.xml"

    # EBX: derive page list from index.html nav tree (TOC-first)
    if version_format == "ebx":
        return _scan_ebx_from_nav(
            html_dir, cache_dir, html_root, version_url, zip_url,
            product_name, product_version, alias_xml_url, version_format, settings,
        )

    page_entries = []
    for suffix in ("*.htm", "*.html"):
        for htm in sorted(html_dir.rglob(suffix)):
            rel = htm.relative_to(cache_dir)
            url = f"https://docs.tibco.com/{html_root}/{htm.relative_to(html_dir).as_posix()}"
            skip, _ = should_skip_url(url, settings)
            if skip:
                continue
            page_entries.append({
                "url":             url,
                "lastmod":         "",
                "output_path":     str(rel.with_suffix(".md")),
                "cache_path":      rel.as_posix(),
                "product_name":    product_name,
                "product_version": product_version,
                "doc_name":        "",
                "access_level":    "public",
                "version_sitemap": version_url,
                "alias_xml_url":   alias_xml_url,
                "zip_url":         zip_url,
                "version_format":  version_format,
            })
    return page_entries


def main():
    parser = argparse.ArgumentParser(description="Step 2a: Download and extract version ZIPs")
    parser.add_argument("--phase",        required=True, help="Phase name, e.g. phase_01")
    parser.add_argument("--config",       default="config/settings.yaml")
    parser.add_argument("--dry-run",      action="store_true",
                        help="Show what would be downloaded without writing files")
    parser.add_argument("--force-rerun",  action="store_true",
                        help="Re-download and re-extract even if already present")
    args = parser.parse_args()

    settings = load_settings(args.config)

    if not settings.get("zip", {}).get("enabled", True):
        print("ZIP download disabled in settings (zip.enabled=false) — skipping step 2a")
        return 0

    manifest = load_manifest(args.phase, settings)
    versions = collect_versions(manifest)

    from datetime import datetime as _dt
    logs_dir = Path(settings.get("logs_dir", "logs"))
    run_dir  = logs_dir / args.phase / _dt.now().strftime("%Y%m%d-%H%M%S")
    reporter = Reporter(run_dir, "02a_zip", dry_run=args.dry_run)

    reporter.info(
        f"=== Step 2a: Download ZIPs | phase={args.phase} "
        f"dry_run={args.dry_run} force_rerun={args.force_rerun} ==="
    )
    reporter.info(f"Manifest: {len(manifest)} entries across {len(versions)} version(s)")

    zip_registry, zip_missing = process_versions(
        versions, settings, reporter, args.dry_run, args.force_rerun
    )

    reporter.info(
        f"Done: {len(zip_registry)} extracted, {len(zip_missing)} missing/failed"
    )

    if not args.dry_run:
        manifests_dir = Path(settings.get("manifests_dir", "manifests"))
        manifests_dir.mkdir(parents=True, exist_ok=True)

        reg_path = manifests_dir / f"zip_registry_{args.phase}.json"
        reg_path.write_text(
            json.dumps(zip_registry, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        reporter.info(f"ZIP registry written to {reg_path}")

        if zip_missing:
            miss_path = manifests_dir / f"zip_missing_{args.phase}.json"
            miss_path.write_text(
                json.dumps(zip_missing, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            reporter.info(f"ZIP missing written to {miss_path}")

        # Expand new-format version-level entries to per-page entries
        cache_dir = Path(settings.get("cache_dir", "cache"))
        new_manifest: list[dict] = []
        expanded_count = 0
        for entry in manifest:
            if "version_url" in entry and "url" not in entry:
                version_url = entry["version_url"]
                if version_url in zip_registry:
                    page_entries = scan_extracted_pages(
                        cache_dir, entry, zip_registry[version_url], settings
                    )
                    new_manifest.extend(page_entries)
                    expanded_count += len(page_entries)
                    reporter.info(f"  Expanded {version_url}: {len(page_entries)} pages")
                else:
                    new_manifest.append(entry)
            else:
                new_manifest.append(entry)

        if expanded_count:
            reporter.info(f"Manifest expanded: {expanded_count} new-format page entries added")
            manifest_path = manifests_dir / f"manifest_{args.phase}.json"
            manifest_path.write_text(
                json.dumps(new_manifest, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            reporter.info(f"Manifest rewritten with {len(new_manifest)} total entries")
    else:
        reporter.info("Dry run — no files written")

    report = reporter.finish()
    return 0 if report["error_count"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
