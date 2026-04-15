"""
02_download.py — Step 2: Async download of HTML pages, images, and alias.xml files.

Reads the manifest JSON produced by Step 1 and downloads:
  - Each HTML page → cache/<url-path>.htm
  - Images referenced in each HTML → cache/<url-path-dir>/images/
  - alias.xml for each version → cache/<version-html-root>/Data/Alias.xml

Already-cached files are skipped unless --force-refresh is set.
Uses httpx async with configurable concurrency.

Usage:
  python scripts/02_download.py --phase phase_01 [--config config/settings.yaml]
                                [--dry-run] [--force-refresh]
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin, urlparse

import httpx
import warnings
import yaml
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from tqdm import tqdm

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

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


def url_to_cache_path(loc: str, cache_dir: Path) -> Path:
    """Map a URL to its local cache path."""
    path = urlparse(loc).path.lstrip("/")
    return cache_dir / path


def extract_image_urls(html_content: bytes, page_url: str, skip_prefixes: list[str]) -> list[str]:
    """Parse HTML and return absolute image URLs to download."""
    soup = BeautifulSoup(html_content, "lxml")
    image_urls = []
    for img in soup.find_all("img", src=True):
        src = img["src"]
        if src.startswith("data:"):
            continue
        # Skip Skins, Scripts, Stylesheets
        if any(src.startswith(pfx) or f"/{pfx}" in src for pfx in skip_prefixes):
            continue
        abs_url = urljoin(page_url, src)
        if abs_url.startswith("http"):
            image_urls.append(abs_url)
    return image_urls


async def download_one(
    client: httpx.AsyncClient,
    url: str,
    dest: Path,
    max_retries: int,
    backoff: float,
    reporter: Reporter,
    dry_run: bool,
    warn_only: bool = False,
) -> bool:
    """Download a single URL to dest. Returns True on success.

    warn_only: if True, failures are logged as warnings (not errors) and do
               not increment error_count — used for non-critical assets like
               alias.xml where absence is acceptable.
    """
    def _record_failure(msg: str):
        if warn_only:
            reporter.warning(f"WARN {url} — {msg}")
            reporter.count("alias_failed")
        else:
            reporter.fail(url, msg)

    for attempt in range(1, max_retries + 1):
        try:
            resp = await client.get(url)
            if resp.status_code == 404:
                _record_failure("HTTP 404")
                return False
            resp.raise_for_status()
            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(resp.content)
            return True
        except httpx.HTTPStatusError as e:
            if attempt == max_retries:
                _record_failure(f"HTTP {e.response.status_code}")
                return False
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.TimeoutException) as e:
            if attempt == max_retries:
                _record_failure(f"Network error: {type(e).__name__}")
                return False
            await asyncio.sleep(backoff ** attempt)
    return False


async def download_phase(
    manifest: list[dict],
    settings: dict,
    reporter: Reporter,
    dry_run: bool,
    force_refresh: bool,
):
    http_cfg      = settings.get("http", {})
    cache_dir     = Path(settings.get("cache_dir", "cache"))
    concurrency   = http_cfg.get("concurrency", 20)
    delay         = http_cfg.get("delay_seconds", 0.5)
    max_retries   = http_cfg.get("max_retries", 3)
    backoff       = float(http_cfg.get("backoff_factor", 2))
    user_agent    = http_cfg.get("user_agent", "tibco-docs-converter/1.0")
    timeout       = httpx.Timeout(
        connect=http_cfg.get("timeout_connect", 10),
        read=http_cfg.get("timeout_read", 30),
        write=10, pool=10,
    )
    skip_prefixes = settings.get("image_skip_prefixes", [])

    semaphore = asyncio.Semaphore(concurrency)

    # Collect alias.xml URLs — one per version (deduplicated)
    alias_urls: dict[str, str] = {}  # alias_xml_url → version_sitemap (for dedup)
    for entry in manifest:
        au = entry.get("alias_xml_url")
        if au and au not in alias_urls:
            alias_urls[au] = entry.get("version_sitemap", "")

    async with httpx.AsyncClient(
        headers={"User-Agent": user_agent},
        timeout=timeout,
        follow_redirects=True,
    ) as client:

        # ── Download HTML pages ────────────────────────────────────────────
        reporter.info(f"Downloading {len(manifest)} HTML pages (concurrency={concurrency})")

        async def fetch_page(entry: dict):
            url  = entry["url"]
            dest = url_to_cache_path(url, cache_dir)
            async with semaphore:
                if dest.exists() and not force_refresh:
                    reporter.count("pages_cached")
                    return
                ok = await download_one(client, url, dest, max_retries, backoff, reporter, dry_run)
                if ok:
                    reporter.count("pages_downloaded")
                    await asyncio.sleep(delay)

                    # Download images found in this page
                    if not dry_run and dest.exists():
                        html_bytes = dest.read_bytes()
                        img_urls = extract_image_urls(html_bytes, url, skip_prefixes)
                        for img_url in img_urls:
                            img_dest = url_to_cache_path(img_url, cache_dir)
                            if img_dest.exists() and not force_refresh:
                                reporter.count("images_cached")
                                continue
                            img_ok = await download_one(
                                client, img_url, img_dest, max_retries, backoff, reporter, dry_run
                            )
                            if img_ok:
                                reporter.count("images_downloaded")

        tasks = [fetch_page(entry) for entry in manifest]
        for coro in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="Pages"):
            await coro

        # ── Download alias.xml files ───────────────────────────────────────
        reporter.info(f"Downloading {len(alias_urls)} alias.xml file(s)")

        async def fetch_alias(alias_url: str):
            dest = url_to_cache_path(alias_url, cache_dir)
            async with semaphore:
                if dest.exists() and not force_refresh:
                    reporter.count("alias_cached")
                    return
                ok = await download_one(client, alias_url, dest, max_retries, backoff, reporter, dry_run, warn_only=True)
                if ok:
                    # Check if it's truly empty (just the root element)
                    if not dry_run and dest.exists():
                        content = dest.read_text(encoding="utf-8", errors="replace").strip()
                        if "<Map " in content:
                            reporter.count("alias_with_content")
                        else:
                            reporter.count("alias_empty")
                    else:
                        reporter.count("alias_downloaded")

        alias_tasks = [fetch_alias(url) for url in alias_urls]
        for coro in tqdm(asyncio.as_completed(alias_tasks), total=len(alias_tasks), desc="Alias"):
            await coro


def main():
    parser = argparse.ArgumentParser(description="Step 2: Download HTML, images, alias.xml")
    parser.add_argument("--phase",         required=True)
    parser.add_argument("--config",        default="config/settings.yaml")
    parser.add_argument("--dry-run",       action="store_true")
    parser.add_argument("--force-refresh", action="store_true",
                        help="Re-download files that are already cached")
    args = parser.parse_args()

    settings = load_settings(args.config)
    manifest = load_manifest(args.phase, settings)

    from datetime import datetime
    logs_dir = Path(settings.get("logs_dir", "logs"))
    run_dir  = logs_dir / args.phase / datetime.now().strftime("%Y%m%d-%H%M%S")
    reporter = Reporter(run_dir, "02_download", dry_run=args.dry_run)

    reporter.info(f"=== Step 2: Download | phase={args.phase} "
                  f"dry_run={args.dry_run} force_refresh={args.force_refresh} ===")
    reporter.info(f"Manifest: {len(manifest)} entries")

    asyncio.run(download_phase(manifest, settings, reporter, args.dry_run, args.force_refresh))

    report = reporter.finish()
    return 0 if report["error_count"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
