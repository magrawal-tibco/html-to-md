"""
list_products.py — One-off utility: enumerate all products on docs.tibco.com and
write a CSV for Business Unit classification.

Fetches:
  1. Master sitemapindex  → all L2 product sitemap URLs
  2. Each L2 sitemap      → list of L3 version sitemaps (version count)
  3. First L3 per product → coveo:metadata for real product name + latest version string

Output CSV columns:
  product_slug, product_name, l2_sitemap_url, version_count, latest_version, bu

The 'bu' column is intentionally empty — fill it in and feed it back as phase YAML inputs.

Usage:
  python scripts/list_products.py [--out products.csv] [--config config/settings.yaml]
  python scripts/list_products.py --concurrency 30   # faster, more parallel fetches
"""

import argparse
import csv
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.lib.sitemap_parser import build_http_client, _fetch_xml, _is_sitemapindex, _get_locs, _parse_urlset

MASTER_SITEMAP = "https://docs.tibco.com/sitemap.xml"


def slug_from_url(url: str) -> str:
    """Extract product slug from L2 URL filename, stripping 'tibco-' prefix."""
    filename = Path(urlparse(url).path).stem   # e.g. tibco-businessevents-enterprise-edition
    if filename.startswith("tibco-"):
        return filename[len("tibco-"):]
    return filename


def name_from_slug(slug: str) -> str:
    """Derive a readable fallback name from a product slug."""
    return slug.replace("-", " ").title()


def fetch_l2_info(client, l2_url: str) -> dict:
    """
    Fetch one L2 product sitemapindex and, if it has versions, peek at the first
    L3 to get the real product name. Returns a dict with product info.
    """
    slug = slug_from_url(l2_url)
    result = {
        "product_slug":    slug,
        "product_name":    "",
        "name_source":     "",
        "l2_sitemap_url":  l2_url,
        "version_count":   0,
        "latest_version":  "",
        "bu":              "",
    }

    try:
        root = _fetch_xml(client, l2_url)
    except Exception:
        # Can't reach L2 — fall through to slug-based name at the bottom
        root = None

    if root is None:
        result["product_name"] = name_from_slug(slug)
        result["name_source"]  = "slug"
        return result

    if _is_sitemapindex(root):
        version_urls = _get_locs(root, "sitemap")
        result["version_count"] = len(version_urls)
        if not version_urls:
            return result
        # Latest version is typically last in the sitemap
        result["latest_version"] = Path(urlparse(version_urls[-1]).path).stem
        first_l3_url = version_urls[-1]
    else:
        # Single-version product — the L2 IS the urlset
        version_urls = [l2_url]
        result["version_count"] = 1
        first_l3_url = l2_url

    # Peek at the latest L3 to get product name from coveo:metadata
    try:
        v_root = _fetch_xml(client, first_l3_url)
        entries = _parse_urlset(v_root)
        if entries and entries[0].product_name:
            result["product_name"] = entries[0].product_name
            result["name_source"]  = "coveo"
            if not result["latest_version"] and entries[0].product_version:
                result["latest_version"] = entries[0].product_version
    except Exception:
        pass  # Fall through to slug-based name

    # Fallback: derive readable name from slug when coveo metadata is unavailable
    if not result["product_name"]:
        result["product_name"] = name_from_slug(result["product_slug"])
        result["name_source"]  = "slug"

    return result


def main():
    parser = argparse.ArgumentParser(
        description="List all docs.tibco.com products and write a CSV for BU classification."
    )
    parser.add_argument("--out",         default="products.csv", metavar="PATH",
                        help="Output CSV path (default: products.csv)")
    parser.add_argument("--config",      default="config/settings.yaml",
                        help="settings.yaml path (default: config/settings.yaml)")
    parser.add_argument("--concurrency", type=int, default=20, metavar="N",
                        help="Parallel HTTP fetches (default: 20)")
    parser.add_argument("--sitemap",     default=None, metavar="PATH",
                        help="Local master sitemap XML file (e.g. 'sample sitemaps/sitemap.xml'). "
                             "If omitted, fetches from docs.tibco.com.")
    args = parser.parse_args()

    settings = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    client   = build_http_client(settings)

    # --- Step 1: Parse master sitemapindex ---
    import xml.etree.ElementTree as ET
    if args.sitemap:
        sitemap_path = Path(args.sitemap)
        print(f"Reading local master sitemap: {sitemap_path}")
        master_root = ET.fromstring(sitemap_path.read_bytes())
    else:
        print(f"Fetching master sitemap: {MASTER_SITEMAP}")
        master_root = _fetch_xml(client, MASTER_SITEMAP)

    l2_urls = _get_locs(master_root, "sitemap")
    print(f"Found {len(l2_urls)} product sitemaps (L2)\n")

    # --- Step 2: Fetch each L2 in parallel ---
    results = []
    errors  = 0
    start   = time.time()

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(fetch_l2_info, client, url): url for url in l2_urls}
        done = 0
        for future in as_completed(futures):
            done += 1
            info = future.result()
            results.append(info)
            status = info["product_name"] if info["product_name"] else info["product_slug"]
            if "ERROR" in status:
                errors += 1
            print(f"  [{done:>3}/{len(l2_urls)}] {status} ({info['version_count']} versions)")

    elapsed = round(time.time() - start, 1)
    print(f"\nFetched {len(results)} products in {elapsed}s ({errors} errors)")

    # Sort by product name, fallback to slug
    results.sort(key=lambda r: (r["product_name"] or r["product_slug"]).lower())

    # --- Step 3: Write CSV ---
    out_path = Path(args.out)
    fieldnames = ["product_slug", "product_name", "name_source", "l2_sitemap_url", "version_count", "latest_version", "bu"]

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    print(f"CSV written to: {out_path.resolve()}")
    print(f"  Rows: {len(results)}")
    print(f"\nNext steps:")
    print(f"  1. Open {out_path} in Excel or Sheets")
    print(f"  2. Fill in the 'bu' column for each product")
    print(f"  3. Group by BU to define phase YAML files")


if __name__ == "__main__":
    sys.exit(main())
