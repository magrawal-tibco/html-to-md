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
    Deduplicate manifest entries by version_sitemap.
    Returns {version_sitemap: representative_entry}.
    """
    versions: dict[str, dict] = {}
    for entry in manifest:
        vs = entry.get("version_sitemap", "")
        if vs and vs not in versions:
            versions[vs] = entry
    return versions


def is_already_extracted(cache_dir: Path, html_root: str) -> bool:
    """Return True if the version's Data/Tocs/ directory exists with JS files."""
    tocs_dir = cache_dir / html_root.rstrip("/") / "Data" / "Tocs"
    return tocs_dir.exists() and any(tocs_dir.glob("*.js"))


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
        for version_sitemap, entry in tqdm(versions.items(), desc="Versions"):
            zip_url   = entry.get("zip_url", "")
            alias_url = entry.get("alias_xml_url", "")

            if not zip_url or not alias_url:
                reporter.info(f"  SKIP {version_sitemap} — missing zip_url or alias_xml_url")
                zip_missing[version_sitemap] = {
                    "zip_url":  zip_url,
                    "reason":   "missing_zip_url",
                    "fallback": "web_crawl",
                }
                reporter.count("zip_missing")
                continue

            html_root = alias_xml_to_html_root(alias_url)
            reporter.info(f"  Version: {version_sitemap}")
            reporter.info(f"    html_root: {html_root}")

            # Skip if already extracted (unless --force-rerun)
            if not force_rerun and is_already_extracted(cache_dir, html_root):
                reporter.info("    -> Already extracted (Data/Tocs/ present) — skipping")
                reporter.count("zip_already_extracted")
                zip_registry[version_sitemap] = {
                    "zip_url":      zip_url,
                    "html_root":    html_root,
                    "extracted_at": "previously",
                    "file_count":   -1,
                }
                continue

            # Disk-space guard
            if not has_enough_disk_space(min_free_gb):
                free_gb = shutil.disk_usage(".").free / (1024 ** 3)
                reporter.info(
                    f"    -> SKIP: only {free_gb:.1f} GB free, need {min_free_gb} GB"
                )
                zip_missing[version_sitemap] = {
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
                zip_missing[version_sitemap] = {
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
                zip_missing[version_sitemap] = {
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

            zip_registry[version_sitemap] = {
                "zip_url":      zip_url,
                "html_root":    html_root,
                "extracted_at": datetime.now().isoformat(timespec="seconds"),
                "file_count":   file_count,
            }
            time.sleep(delay)

    return zip_registry, zip_missing


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
    else:
        reporter.info("Dry run — no files written")

    report = reporter.finish()
    return 0 if report["error_count"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
