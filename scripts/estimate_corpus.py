"""
estimate_corpus.py — Crawl the 3-level TIBCO sitemap hierarchy and estimate
the total conversion workload.

Reads L1 (master sitemapindex) → fetches all L2 (product sitemapindexes) →
fetches all L3 (version urlsets) → counts filtered HTML pages per version.

Outputs:
  - Console summary table (products × versions × page counts + time estimates)
  - CSV file: estimate_corpus.csv
  - JSON file: estimate_corpus.json

Usage:
  python scripts/estimate_corpus.py [--sitemap URL_or_path] [--config settings.yaml]
                                    [--concurrency 40] [--out estimate_corpus]

Rates measured from phase_03 (21,777 pages, 17 versions):
  Download  : 12.4 pages/sec  (httpx async, concurrency=20, 0.5s delay)
  Convert   : 16.8 pages/sec  (BeautifulSoup + markdownify)
  Postprocess:  64  pages/sec  (regex link rewriting)
  TOC       :  72  pages/sec  (frontmatter reads)
  Images    :   0.8 images/page (estimated from phase_03 ratio)
"""

import argparse
import asyncio
import csv
import json
import re
import sys
import time
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import httpx
import yaml
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Namespace maps ─────────────────────────────────────────────────────────────

_SM_NS  = "http://www.sitemaps.org/schemas/sitemap/0.9"
_SM_NS2 = "http://www.sitemaps.org/schemas/sitemap/0.9/sitemap.xsd"
_COVEO  = "http://www.coveo.com/schemas/metadata"

_NS = {
    "sm":    _SM_NS,
    "sm2":   _SM_NS2,
    "coveo": _COVEO,
}

# ── Measured rates from phase_03 ───────────────────────────────────────────────

RATE_DOWNLOAD_PPS   = 12.4   # pages/sec (I/O bound, network)
RATE_CONVERT_PPS    = 16.8   # pages/sec (CPU, BeautifulSoup + markdownify)
RATE_POSTPROC_PPS   = 64.0   # pages/sec (regex)
RATE_IMAGES_PER_PAGE = 0.80  # estimated images per page
RATE_IMG_DOWNLOAD_PPS = 12.4 # same as pages (same async pool)


def load_settings(config_path: str) -> dict:
    return yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))


def parse_xml(content: bytes) -> ET.Element:
    """Parse XML bytes, stripping BOM if present."""
    if content.startswith(b"\xef\xbb\xbf"):
        content = content[3:]
    return ET.fromstring(content)


def find_locs(root: ET.Element, tag: str) -> list[str]:
    """Find all <loc> values under <tag> elements, trying both namespace variants."""
    locs = []
    for ns_uri in (_SM_NS, _SM_NS2):
        for el in root.iter(f"{{{ns_uri}}}{tag}"):
            loc = el.find(f"{{{ns_uri}}}loc")
            if loc is not None and loc.text:
                locs.append(loc.text.strip())
    return list(dict.fromkeys(locs))  # deduplicate, preserve order


def should_skip(url: str, skip_segments: list[str], skip_filenames: set[str],
                html_extensions: set[str],
                skip_filename_patterns: list[str] | None = None) -> bool:
    """Return True if the URL should be excluded (same logic as 01_build_manifest)."""
    parsed = urlparse(url)
    path   = parsed.path
    filename = PurePosixPath(path).name

    if filename in skip_filenames:
        return True
    ext = PurePosixPath(path).suffix.lower()
    if ext not in html_extensions:
        return True
    for pattern in (skip_filename_patterns or []):
        if re.match(pattern, filename, re.IGNORECASE):
            return True
    for seg in skip_segments:
        if seg in path:
            return True
    return False


def _sitemap_cache_path(url: str, cache_dir: Path) -> Path:
    """Map a sitemap URL to a local cache path under cache_dir/_sitemaps/."""
    parsed = urlparse(url)
    rel    = parsed.path.lstrip("/")
    return cache_dir / "_sitemaps" / rel


async def fetch_bytes(client: httpx.AsyncClient, url: str,
                      semaphore: asyncio.Semaphore,
                      cache_dir: Path | None = None) -> bytes | None:
    """Fetch URL, return bytes or None on failure. Caches to disk if cache_dir given."""
    if cache_dir is not None:
        cached = _sitemap_cache_path(url, cache_dir)
        if cached.exists():
            return cached.read_bytes()

    async with semaphore:
        try:
            r = await client.get(url, timeout=30)
            r.raise_for_status()
            data = r.content
        except Exception:
            return None

    if cache_dir is not None and data:
        cached = _sitemap_cache_path(url, cache_dir)
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(data)

    return data


async def fetch_l2(client: httpx.AsyncClient, l2_url: str,
                   semaphore: asyncio.Semaphore,
                   cache_dir: Path | None = None) -> list[str]:
    """Fetch an L2 product sitemapindex and return list of L3 version URLs."""
    data = await fetch_bytes(client, l2_url, semaphore, cache_dir)
    if not data:
        return []
    try:
        root = parse_xml(data)
        return find_locs(root, "sitemap")
    except ET.ParseError:
        return []


async def fetch_l3(client: httpx.AsyncClient, l3_url: str,
                   semaphore: asyncio.Semaphore,
                   skip_segments: list[str], skip_filenames: set[str],
                   html_extensions: set[str],
                   skip_filename_patterns: list[str] | None = None,
                   cache_dir: Path | None = None) -> dict:
    """
    Fetch an L3 version urlset and count accepted HTML pages.
    Returns dict with keys: l3_url, page_count, product_name, version, doc_names.
    """
    data = await fetch_bytes(client, l3_url, semaphore, cache_dir)
    if not data:
        return {"l3_url": l3_url, "page_count": 0, "product_name": "", "version": "", "doc_names": []}

    try:
        root = parse_xml(data)
    except ET.ParseError:
        return {"l3_url": l3_url, "page_count": 0, "product_name": "", "version": "", "doc_names": []}

    page_count   = 0
    product_name = ""
    version      = ""
    doc_names    = set()

    for ns_uri in (_SM_NS, _SM_NS2):
        for url_el in root.iter(f"{{{ns_uri}}}url"):
            loc_el = url_el.find(f"{{{ns_uri}}}loc")
            if loc_el is None or not loc_el.text:
                continue
            loc = loc_el.text.strip()
            if should_skip(loc, skip_segments, skip_filenames, html_extensions, skip_filename_patterns):
                continue
            page_count += 1

            # Extract metadata from coveo namespace (first URL only)
            if not product_name:
                meta = url_el.find(f"{{{_COVEO}}}metadata")
                if meta is not None:
                    for child in meta:
                        local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                        val   = (child.text or "").strip()
                        if local == "name" and not product_name:
                            product_name = val
                        elif local == "productversion" and not version:
                            version = val
                        elif local == "d_name" and val:
                            doc_names.add(val)

    return {
        "l3_url":       l3_url,
        "page_count":   page_count,
        "product_name": product_name,
        "version":      version,
        "doc_names":    sorted(doc_names),
    }


def read_l1_local(path: str) -> list[str]:
    """Read L2 URLs from a local L1 sitemapindex file."""
    root = ET.parse(path).getroot()
    return find_locs(root, "sitemap")


async def crawl(l1_source: str, settings: dict, concurrency: int,
                cache_dir: Path | None = None) -> list[dict]:
    """
    Full 3-level crawl. Returns list of per-version dicts.
    l1_source: local file path or http(s) URL.
    cache_dir: if set, L2/L3 XML responses are cached here and reused on re-runs.
    """
    skip_segments           = settings.get("skip_path_segments", [])
    skip_filenames          = set(settings.get("skip_filenames", []))
    html_extensions         = set(settings.get("html_extensions", [".htm", ".html"]))
    skip_filename_patterns  = settings.get("skip_filename_patterns", [])
    user_agent              = settings.get("http", {}).get("user_agent", "tibco-docs-estimator/1.0")

    # ── L1: get all L2 URLs ───────────────────────────────────────────────────
    if l1_source.startswith("http"):
        async with httpx.AsyncClient(headers={"User-Agent": user_agent},
                                     follow_redirects=True) as client:
            data = await client.get(l1_source, timeout=30)
            root = parse_xml(data.content)
            l2_urls = find_locs(root, "sitemap")
    else:
        l2_urls = read_l1_local(l1_source)

    print(f"L1: {len(l2_urls)} product sitemaps found")

    # Count how many L2/L3 are already cached to give user feedback
    if cache_dir:
        cached_l2 = sum(1 for u in l2_urls if _sitemap_cache_path(u, cache_dir).exists())
        print(f"Sitemap cache: {cached_l2}/{len(l2_urls)} L2 sitemaps already cached")

    semaphore = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(
        headers={"User-Agent": user_agent},
        follow_redirects=True,
        timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10),
    ) as client:

        # ── L2: fetch all product sitemaps → collect L3 URLs ─────────────────
        print(f"Fetching {len(l2_urls)} L2 product sitemaps (concurrency={concurrency})…")
        l2_tasks = [fetch_l2(client, u, semaphore, cache_dir) for u in l2_urls]
        l3_url_lists = []
        for coro in tqdm(asyncio.as_completed(l2_tasks), total=len(l2_tasks), desc="L2 sitemaps"):
            l3_url_lists.append(await coro)

        all_l3_urls = [u for lst in l3_url_lists for u in lst]
        print(f"L2 done: {len(all_l3_urls)} version sitemaps found")

        # ── L3: fetch all version sitemaps → count pages ──────────────────────
        print(f"Fetching {len(all_l3_urls)} L3 version sitemaps…")
        l3_tasks = [
            fetch_l3(client, u, semaphore, skip_segments, skip_filenames,
                     html_extensions, skip_filename_patterns, cache_dir)
            for u in all_l3_urls
        ]
        results = []
        for coro in tqdm(asyncio.as_completed(l3_tasks), total=len(l3_tasks), desc="L3 versions"):
            results.append(await coro)

    return results


def fmt_time(seconds: float) -> str:
    """Format seconds as Xh Ym Zs."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    parts = []
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def estimate_times(total_pages: int, total_images: int) -> dict:
    """Return estimated wall-clock times for each pipeline step."""
    return {
        "download_pages":  total_pages  / RATE_DOWNLOAD_PPS,
        "download_images": total_images / RATE_IMG_DOWNLOAD_PPS,
        "convert":         total_pages  / RATE_CONVERT_PPS,
        "postprocess":     total_pages  / RATE_POSTPROC_PPS,
    }


def print_summary(results: list[dict], out_stem: str):
    """Print a grouped summary table and write CSV + JSON outputs."""

    # Group by product
    from collections import defaultdict
    by_product: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        parts = r["l3_url"].split("/")
        key = r["product_name"] or (parts[6] if len(parts) > 6 else r["l3_url"])
        by_product[key].append(r)

    total_pages   = sum(r["page_count"] for r in results)
    total_images  = int(total_pages * RATE_IMAGES_PER_PAGE)
    total_versions = len(results)

    print(f"\n{'='*80}")
    print(f"  TIBCO DOCS CORPUS ESTIMATE")
    print(f"  Products: {len(by_product)}   Versions: {total_versions}   Pages: {total_pages:,}   Est. images: {total_images:,}")
    print(f"{'='*80}")
    print(f"  {'Product':<50} {'Vers':>5} {'Pages':>8}")
    print(f"  {'-'*50} {'-'*5} {'-'*8}")

    rows = []
    for product, versions in sorted(by_product.items(), key=lambda x: -sum(v["page_count"] for v in x[1])):
        prod_pages = sum(v["page_count"] for v in versions)
        print(f"  {product[:50]:<50} {len(versions):>5} {prod_pages:>8,}")
        for v in sorted(versions, key=lambda x: x.get("version", "")):
            rows.append({
                "product_name":  v["product_name"],
                "version":       v["version"],
                "l3_url":        v["l3_url"],
                "page_count":    v["page_count"],
                "est_images":    int(v["page_count"] * RATE_IMAGES_PER_PAGE),
                "doc_names":     "; ".join(v["doc_names"]),
            })

    print(f"  {'-'*50} {'-'*5} {'-'*8}")
    print(f"  {'TOTAL':<50} {total_versions:>5} {total_pages:>8,}")

    # Time estimates
    times = estimate_times(total_pages, total_images)
    total_download = times["download_pages"] + times["download_images"]
    total_process  = times["convert"] + times["postprocess"]
    grand_total    = total_download + total_process

    print(f"\n  TIME ESTIMATES (based on phase_03 measured rates, concurrency=20):")
    print(f"    Download pages  : {fmt_time(times['download_pages'])}  ({RATE_DOWNLOAD_PPS} pages/sec)")
    print(f"    Download images : {fmt_time(times['download_images'])}  ({RATE_IMG_DOWNLOAD_PPS} img/sec, ~{RATE_IMAGES_PER_PAGE} img/page)")
    print(f"    Convert         : {fmt_time(times['convert'])}  ({RATE_CONVERT_PPS} pages/sec)")
    print(f"    Postprocess     : {fmt_time(times['postprocess'])}  ({RATE_POSTPROC_PPS} pages/sec)")
    print(f"    -----------------------------")
    print(f"    Download total  : {fmt_time(total_download)}")
    print(f"    Processing total: {fmt_time(total_process)}")
    print(f"    GRAND TOTAL     : {fmt_time(grand_total)}")
    print(f"{'='*80}\n")

    # Write CSV
    csv_path = f"{out_stem}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["product_name", "version", "page_count", "est_images", "doc_names", "l3_url"])
        w.writeheader()
        w.writerows(rows)
    print(f"  CSV written: {csv_path}")

    # Write JSON
    json_path = f"{out_stem}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "summary": {
                "products":       len(by_product),
                "versions":       total_versions,
                "total_pages":    total_pages,
                "est_images":     total_images,
                "time_estimates": {k: fmt_time(v) for k, v in times.items()},
                "grand_total":    fmt_time(grand_total),
            },
            "versions": rows,
        }, f, indent=2, ensure_ascii=False)
    print(f"  JSON written: {json_path}\n")


def main():
    parser = argparse.ArgumentParser(description="Estimate TIBCO docs corpus size and conversion time")
    parser.add_argument("--sitemap",     default="sample sitemaps/sitemap.xml",
                        help="Path to local L1 sitemapindex XML, or https:// URL")
    parser.add_argument("--config",      default="config/settings.yaml")
    parser.add_argument("--concurrency", type=int, default=40,
                        help="Concurrent HTTP requests for L2/L3 fetching (default: 40)")
    parser.add_argument("--out",         default="estimate_corpus",
                        help="Output file stem for CSV and JSON (default: estimate_corpus)")
    parser.add_argument("--no-cache",    action="store_true",
                        help="Bypass sitemap cache and re-fetch all L2/L3 sitemaps")
    args = parser.parse_args()

    settings  = load_settings(args.config)
    cache_dir = None if args.no_cache else Path(settings.get("cache_dir", "cache"))

    t0 = time.time()
    results = asyncio.run(crawl(args.sitemap, settings, args.concurrency, cache_dir))
    elapsed = time.time() - t0

    print(f"\nCrawl complete in {fmt_time(elapsed)} — {len(results)} versions found")
    print_summary(results, args.out)


if __name__ == "__main__":
    main()
