"""
scripts/catalog/fetch_products.py — Fetch the TIBCO product catalog and write a CSV.

The A-Z products page (https://docs.tibco.com/a_z_products) is JavaScript-rendered.
This script uses the underlying data source: https://docs.tibco.com/sitemap.xml,
which is a flat urlset of all product page URLs with human-readable <name> tags —
exactly what the A-Z page displays.

Default run (fast — 1 HTTP request):
  Columns: product_name, product_slug, product_page_url

With --versions (parallel L2 fetches — one per product):
  Adds: version_count, oldest_version, latest_version, all_versions, l2_sitemap_url

Usage:
  python scripts/catalog/fetch_products.py
  python scripts/catalog/fetch_products.py --out products.csv
  python scripts/catalog/fetch_products.py --versions
  python scripts/catalog/fetch_products.py --versions --concurrency 30
"""

import argparse
import csv
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import httpx

MASTER_SITEMAP   = "https://docs.tibco.com/sitemap.xml"
L2_SITEMAP_BASE  = "https://docs.tibco.com/ftp_portal/coveo/tibco-{slug}.xml"
DEFAULT_OUT      = "tibco_products.csv"
USER_AGENT       = "tibco-catalog-fetcher/1.0"

BASIC_FIELDS   = ["product_name", "product_slug", "product_page_url"]
VERSION_FIELDS = ["version_count", "oldest_version", "latest_version",
                  "all_versions", "l2_sitemap_url"]


# ── XML helpers ──────────────────────────────────────────────────────────────

def _http_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10),
        follow_redirects=True,
    )


def _fetch_xml(client: httpx.Client, url: str) -> ET.Element:
    resp = client.get(url)
    resp.raise_for_status()
    return ET.fromstring(resp.content)


def _ns_uri(root: ET.Element) -> str:
    return root.tag.split("}")[0].lstrip("{") if "}" in root.tag else ""


def _tag(ns: str, local: str) -> str:
    return f"{{{ns}}}{local}" if ns else local


def _is_sitemapindex(root: ET.Element) -> bool:
    return (root.tag.split("}")[-1] if "}" in root.tag else root.tag) == "sitemapindex"


def _get_locs(root: ET.Element, child_tag: str) -> list[str]:
    ns = _ns_uri(root)
    locs = []
    for child in root.findall(_tag(ns, child_tag)):
        loc_el = child.find(_tag(ns, "loc"))
        if loc_el is not None and loc_el.text:
            locs.append(loc_el.text.strip())
    return locs


# ── Step 1: parse sitemap.xml ────────────────────────────────────────────────

def parse_master_sitemap(client: httpx.Client) -> list[dict]:
    """
    Fetch sitemap.xml and return one dict per product page entry.
    Filters to entries under /products/ (excludes /videos, /faq, /a_z_products, etc.)
    """
    print(f"Fetching: {MASTER_SITEMAP}")
    root = _fetch_xml(client, MASTER_SITEMAP)
    ns   = _ns_uri(root)

    products = []
    for url_el in root.findall(_tag(ns, "url")):
        loc_el  = url_el.find(_tag(ns, "loc"))
        name_el = url_el.find(_tag(ns, "name"))
        if loc_el is None or not loc_el.text:
            continue

        page_url = loc_el.text.strip()
        if "/products/" not in page_url:
            continue

        slug = page_url.split("/products/", 1)[-1].strip("/")
        if not slug or slug == "search":
            continue

        name = name_el.text.strip() if name_el is not None and name_el.text else ""
        # Clean encoding artifacts (e.g. ® rendered as garbled bytes in some contexts)
        name = name.encode("utf-8", "replace").decode("utf-8")

        products.append({
            "product_name":     name,
            "product_slug":     slug,
            "product_page_url": page_url,
        })

    return products


# ── Step 2 (optional): fetch L2 sitemaps for version info ────────────────────

def _version_from_l3_url(l3_url: str, slug: str) -> str:
    """Extract a dotted version string from an L3 sitemap filename."""
    stem = Path(urlparse(l3_url).path).stem   # e.g. tibco-businessevents-6-4-0
    prefix = f"tibco-{slug}-"
    if stem.startswith(prefix):
        return stem[len(prefix):].replace("-", ".")
    # Fallback: last run of digits-and-dashes at end of stem
    import re
    m = re.search(r"(\d[\d-]*)$", stem)
    return m.group(1).replace("-", ".") if m else stem


def fetch_version_info(client: httpx.Client, slug: str) -> dict:
    """Try to fetch the L2 sitemapindex for a product and return version metadata."""
    l2_url = L2_SITEMAP_BASE.format(slug=slug)
    result = {
        "version_count":   "",
        "oldest_version":  "",
        "latest_version":  "",
        "all_versions":    "",
        "l2_sitemap_url":  "",
    }
    try:
        root = _fetch_xml(client, l2_url)
    except Exception:
        return result   # Product has no L2 sitemap (e.g. ibi, legacy pages)

    result["l2_sitemap_url"] = l2_url

    if _is_sitemapindex(root):
        l3_urls = _get_locs(root, "sitemap")
    else:
        l3_urls = _get_locs(root, "url")   # Single-version product

    if not l3_urls:
        return result

    versions = [_version_from_l3_url(u, slug) for u in l3_urls]
    result["version_count"]  = len(l3_urls)
    result["oldest_version"] = versions[0]
    result["latest_version"] = versions[-1]
    result["all_versions"]   = " | ".join(versions)
    return result


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch the TIBCO docs product catalog from sitemap.xml"
    )
    parser.add_argument("--out",         default=DEFAULT_OUT, metavar="PATH",
                        help=f"Output CSV path (default: {DEFAULT_OUT})")
    parser.add_argument("--versions",    action="store_true",
                        help="Also fetch L2 sitemaps for version count and version list")
    parser.add_argument("--concurrency", type=int, default=20, metavar="N",
                        help="Parallel fetches when --versions is used (default: 20)")
    args = parser.parse_args()

    client = _http_client()

    # Step 1: basic product list from master sitemap (1 HTTP request)
    products = parse_master_sitemap(client)
    print(f"Found {len(products)} products")

    # Step 2 (optional): fetch L2 sitemaps in parallel for version info
    if args.versions:
        print(f"\nFetching version info (concurrency={args.concurrency})...")
        start = time.time()

        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {
                pool.submit(fetch_version_info, client, p["product_slug"]): i
                for i, p in enumerate(products)
            }
            done = 0
            for future in as_completed(futures):
                idx = futures[future]
                info = future.result()
                products[idx].update(info)
                done += 1
                p = products[idx]
                vc = info["version_count"]
                vc_str = f"{vc} versions" if vc else "no L2 sitemap"
                print(f"  [{done:>3}/{len(products)}]  {p['product_name']}  ({vc_str})")

        elapsed = round(time.time() - start, 1)
        with_versions = sum(1 for p in products if p.get("version_count"))
        print(f"\nDone in {elapsed}s — {with_versions}/{len(products)} products have version sitemaps")

    # Sort alphabetically by product name
    products.sort(key=lambda p: p["product_name"].lower())

    # Write CSV
    fieldnames = BASIC_FIELDS + (VERSION_FIELDS if args.versions else [])
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(products)

    print(f"\nCSV written: {out_path.resolve()}")
    print(f"  Rows:    {len(products)}")
    print(f"  Columns: {', '.join(fieldnames)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
