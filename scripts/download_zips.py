"""
download_zips.py — Download ZIP files from a plain URL list.

Reads one ZIP URL per line from a text file (blank lines and lines starting
with '#' are ignored) and downloads each to the cache/zip directory.

  cache/zip/<url-path>   — downloaded ZIPs land here

Already-downloaded ZIPs are skipped unless --force-download is set.

Usage:
  python scripts/download_zips.py urls.txt
         [--config config/settings.yaml]
         [--zip-cache-dir cache/zip]
         [--dry-run]
         [--force-download]
"""

import argparse
import sys
import time
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


# ── Main logic ────────────────────────────────────────────────────────────────

def process_urls(
    urls: list[str],
    settings: dict,
    reporter: Reporter,
    zip_cache_dir: Path,
    dry_run: bool,
    force_download: bool,
) -> None:
    http_cfg = settings.get("http", {})
    delay    = http_cfg.get("delay_seconds", 0.5)

    client = httpx.Client(
        headers={"User-Agent": http_cfg.get("user_agent", "tibco-docs-converter/1.0")},
        timeout=httpx.Timeout(connect=http_cfg.get("timeout_connect", 10),
                              read=600, write=10, pool=10),
        follow_redirects=True,
    )

    counts = {"skipped": 0, "downloaded": 0, "failed": 0}

    with client:
        for zip_url in tqdm(urls, desc="ZIPs", unit="zip"):
            zip_url_path = urlparse(zip_url).path.lstrip("/")
            zip_path     = zip_cache_dir / zip_url_path

            reporter.info(f"  {zip_url}")

            if not force_download and zip_path.exists():
                size_kb = zip_path.stat().st_size // 1024
                reporter.info(f"    -> Already downloaded ({size_kb:,} KB) — skipping")
                counts["skipped"] += 1
                continue

            if dry_run:
                reporter.info(f"    [dry-run] Would download → {zip_path}")
                continue

            ok, reason = _download_zip(client, zip_url, zip_path, reporter)
            if not ok:
                reporter.info(f"    -> Failed: {reason}")
                counts["failed"] += 1
                time.sleep(delay)
                continue

            size_kb = zip_path.stat().st_size // 1024
            reporter.info(f"    Downloaded: {size_kb:,} KB → {zip_path}")
            counts["downloaded"] += 1
            time.sleep(delay)

    reporter.info(
        f"\nDone — downloaded: {counts['downloaded']}  "
        f"skipped: {counts['skipped']}  "
        f"failed: {counts['failed']}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download ZIP files listed in a text file"
    )
    parser.add_argument(
        "url_file",
        help="Text file with one ZIP URL per line (# comments and blank lines ignored)",
    )
    parser.add_argument("--config",        default="config/settings.yaml")
    parser.add_argument("--zip-cache-dir", default=None,
                        help="Where to save ZIPs (default: from settings, usually cache/zip)")
    parser.add_argument("--dry-run",       action="store_true",
                        help="Print what would be downloaded without writing files")
    parser.add_argument("--force-download", action="store_true",
                        help="Re-download even if the ZIP already exists in cache")
    args = parser.parse_args()

    url_file = Path(args.url_file)
    if not url_file.exists():
        print(f"Error: URL file not found: {url_file}", file=sys.stderr)
        return 1

    settings      = load_settings(args.config)
    zip_cache_dir = Path(args.zip_cache_dir or
                         settings.get("zip", {}).get("zip_cache_dir", "cache/zip"))

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
        f"dry_run={args.dry_run} force_download={args.force_download} ==="
    )
    reporter.info(f"ZIP cache: {zip_cache_dir.resolve()}")
    reporter.info(f"URL file : {url_file.resolve()}")

    process_urls(urls, settings, reporter, zip_cache_dir,
                 dry_run=args.dry_run, force_download=args.force_download)

    reporter.finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
