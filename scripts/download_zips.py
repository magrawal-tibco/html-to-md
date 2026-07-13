"""
download_zips.py — Download and extract ZIP files from a plain URL list.

Reads one ZIP URL per line from a text file (blank lines and lines starting
with '#' are ignored) and downloads/extracts each to the cache directory,
mirroring the same path layout used by step 02a_download_zip.py.

  cache/zip/<url-path>          — raw ZIP kept here
  cache/<url-path-parent>/...   — extracted content

Already-extracted ZIPs are skipped unless --force-rerun is set.
Already-downloaded ZIPs are re-used unless --force-download is set.

Usage:
  python scripts/download_zips.py urls.txt
         [--config config/settings.yaml]
         [--cache-dir cache]
         [--zip-cache-dir cache/zip]
         [--no-extract]
         [--dry-run]
         [--force-rerun]
         [--force-download]
"""

import argparse
import shutil
import sys
import time
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import httpx
import yaml
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.lib.reporter import Reporter


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_settings(config_path: str) -> dict:
    return yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))


def read_url_list(path: Path) -> list[str]:
    """Read one URL per line; skip blanks and # comments."""
    urls = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            urls.append(line)
    return urls


def url_to_extract_base(zip_url: str) -> str:
    """
    Derive the extraction base directory from the ZIP URL.

    TIBCO ZIPs are always published one level above their html/ root:
      https://docs.tibco.com/pub/foo/1.0/doc/tibco-foo-1-0.zip
      → extract_base = pub/foo/1.0/doc
      → after stripping the top-level folder in the ZIP, members land at
        cache/pub/foo/1.0/doc/html/...  cache/pub/foo/1.0/doc/pdf/...
    """
    path = urlparse(zip_url).path          # /pub/foo/1.0/doc/tibco-foo.zip
    parent = PurePosixPath(path).parent    # /pub/foo/1.0/doc
    return str(parent).lstrip("/")         # pub/foo/1.0/doc


def is_already_extracted(cache_dir: Path, extract_base: str) -> bool:
    """
    Heuristic check: the target directory exists and contains at least one file.
    Works for MadCap, EBX, EBX-addon, and DITA layouts.
    """
    target = cache_dir / extract_base
    if not target.exists():
        return False
    return any(target.rglob("*") if True else [])


def _download_zip(
    client: httpx.Client,
    zip_url: str,
    zip_path: Path,
    reporter: Reporter,
) -> tuple[bool, str]:
    """Stream-download a ZIP. Returns (success, reason_on_failure)."""
    try:
        with client.stream("GET", zip_url) as resp:
            if resp.status_code == 404:
                return False, "http_404"
            resp.raise_for_status()
            zip_path.parent.mkdir(parents=True, exist_ok=True)
            total = int(resp.headers.get("content-length", 0))
            with (
                open(zip_path, "wb") as fh,
                tqdm(
                    total=total or None,
                    unit="B",
                    unit_scale=True,
                    unit_divisor=1024,
                    desc=zip_path.name,
                    leave=False,
                ) as bar,
            ):
                for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                    fh.write(chunk)
                    bar.update(len(chunk))
        return True, ""
    except httpx.HTTPStatusError as e:
        return False, f"http_{e.response.status_code}"
    except Exception as e:
        return False, f"error_{type(e).__name__}"


def _extract_zip(
    zip_path: Path,
    cache_dir: Path,
    extract_base: str,
) -> tuple[bool, str, int]:
    """
    Extract all ZIP members to cache_dir/<extract_base>/.

    TIBCO ZIPs wrap content in a single top-level product folder which is
    stripped before extraction so members land at their natural sub-paths
    (html/..., pdf/..., etc.).

    Returns (success, reason_on_failure, file_count).
    """
    if not zipfile.is_zipfile(zip_path):
        return False, "corrupt_zip", 0

    file_count = 0
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            # Detect and strip a common top-level folder wrapper
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


# ── Main logic ────────────────────────────────────────────────────────────────

def process_urls(
    urls: list[str],
    settings: dict,
    reporter: Reporter,
    cache_dir: Path,
    zip_cache_dir: Path,
    dry_run: bool,
    force_rerun: bool,
    force_download: bool,
    no_extract: bool,
) -> None:
    http_cfg   = settings.get("http", {})
    zip_cfg    = settings.get("zip", {})
    delay      = http_cfg.get("delay_seconds", 0.5)
    store_zip  = zip_cfg.get("store_zip", True)
    min_free   = float(zip_cfg.get("min_free_gb", 20))

    client = httpx.Client(
        headers={"User-Agent": http_cfg.get("user_agent", "tibco-docs-converter/1.0")},
        timeout=httpx.Timeout(connect=http_cfg.get("timeout_connect", 10),
                              read=600, write=10, pool=10),
        follow_redirects=True,
    )

    counts = {"skipped": 0, "downloaded": 0, "extracted": 0, "failed": 0}

    with client:
        for zip_url in tqdm(urls, desc="ZIPs", unit="zip"):
            extract_base = url_to_extract_base(zip_url)
            zip_url_path = urlparse(zip_url).path.lstrip("/")
            zip_path     = zip_cache_dir / zip_url_path

            reporter.info(f"  {zip_url}")
            reporter.info(f"    extract_base : {extract_base}")

            # ── already extracted? ────────────────────────────────────────────
            if not force_rerun and is_already_extracted(cache_dir, extract_base):
                reporter.info("    -> Already extracted — skipping")
                counts["skipped"] += 1
                continue

            if dry_run:
                reporter.info(f"    [dry-run] Would download → {zip_path}")
                reporter.info(f"    [dry-run] Would extract  → {cache_dir / extract_base}/")
                continue

            # ── disk-space guard ──────────────────────────────────────────────
            free_gb = shutil.disk_usage(".").free / (1024 ** 3)
            if free_gb < min_free:
                reporter.info(f"    -> SKIP: only {free_gb:.1f} GB free, need {min_free} GB")
                counts["failed"] += 1
                continue

            # ── download ──────────────────────────────────────────────────────
            if not force_download and zip_path.exists() and zipfile.is_zipfile(zip_path):
                reporter.info(f"    Reusing cached ZIP: {zip_path}")
            else:
                reporter.info(f"    Downloading...")
                ok, reason = _download_zip(client, zip_url, zip_path, reporter)
                if not ok:
                    reporter.info(f"    -> Download failed: {reason}")
                    counts["failed"] += 1
                    time.sleep(delay)
                    continue
                size_kb = zip_path.stat().st_size // 1024
                reporter.info(f"    Downloaded: {size_kb:,} KB")
                counts["downloaded"] += 1

            # ── extract ───────────────────────────────────────────────────────
            if no_extract:
                reporter.info("    --no-extract: skipping extraction")
                continue

            ok, reason, file_count = _extract_zip(zip_path, cache_dir, extract_base)
            if not ok:
                reporter.info(f"    -> Extraction failed: {reason}")
                counts["failed"] += 1
                zip_path.unlink(missing_ok=True)
                time.sleep(delay)
                continue

            reporter.info(f"    Extracted {file_count} files → {cache_dir / extract_base}/")
            counts["extracted"] += 1

            if not store_zip:
                zip_path.unlink(missing_ok=True)
                reporter.info("    ZIP deleted (store_zip=false)")

            time.sleep(delay)

    reporter.info(
        f"\nDone — downloaded: {counts['downloaded']}  "
        f"extracted: {counts['extracted']}  "
        f"skipped: {counts['skipped']}  "
        f"failed: {counts['failed']}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download and extract ZIP files from a plain URL list"
    )
    parser.add_argument(
        "url_file",
        help="Text file with one ZIP URL per line (# comments and blank lines ignored)",
    )
    parser.add_argument("--config",         default="config/settings.yaml")
    parser.add_argument("--cache-dir",      default=None,
                        help="Override cache directory (default: from settings)")
    parser.add_argument("--zip-cache-dir",  default=None,
                        help="Override zip cache directory (default: from settings)")
    parser.add_argument("--no-extract",     action="store_true",
                        help="Download only — do not extract the ZIPs")
    parser.add_argument("--dry-run",        action="store_true",
                        help="Print what would happen without writing any files")
    parser.add_argument("--force-rerun",    action="store_true",
                        help="Re-download and re-extract even if already present")
    parser.add_argument("--force-download", action="store_true",
                        help="Re-download even if a cached ZIP already exists")
    args = parser.parse_args()

    url_file = Path(args.url_file)
    if not url_file.exists():
        print(f"Error: URL file not found: {url_file}", file=sys.stderr)
        return 1

    settings = load_settings(args.config)
    zip_cfg  = settings.get("zip", {})

    cache_dir     = Path(args.cache_dir     or settings.get("cache_dir", "cache"))
    zip_cache_dir = Path(args.zip_cache_dir or zip_cfg.get("zip_cache_dir", "cache/zip"))

    urls = read_url_list(url_file)
    if not urls:
        print(f"No URLs found in {url_file}", file=sys.stderr)
        return 1

    from datetime import datetime
    logs_dir = Path(settings.get("logs_dir", "logs"))
    run_dir  = logs_dir / "download_zips" / datetime.now().strftime("%Y%m%d-%H%M%S")
    reporter = Reporter(run_dir, "download_zips", dry_run=args.dry_run)

    reporter.info(
        f"=== download_zips | {len(urls)} URL(s) | "
        f"dry_run={args.dry_run} force_rerun={args.force_rerun} ==="
    )
    reporter.info(f"Cache    : {cache_dir.resolve()}")
    reporter.info(f"ZIP cache: {zip_cache_dir.resolve()}")
    reporter.info(f"URL file : {url_file.resolve()}")

    process_urls(
        urls, settings, reporter,
        cache_dir, zip_cache_dir,
        dry_run=args.dry_run,
        force_rerun=args.force_rerun,
        force_download=args.force_download,
        no_extract=args.no_extract,
    )

    reporter.finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
