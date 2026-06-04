"""
list_products.py — Enumerate all products on docs.tibco.com and write a CSV
for Business Unit classification.

Uses the same API that powers the A-Z products page (/api/a_to_z).
Optionally fetches each product's L2 sitemap for version details.

Output CSV columns:
  product_slug, product_name, product_page_url, version_count, latest_version, bu

The 'bu' column is intentionally empty — fill it in and feed it back as phase YAML inputs.

Usage:
  python scripts/list_products.py [--out products.csv] [--config config/settings.yaml]
  python scripts/list_products.py --versions            # also fetch version details
  python scripts/list_products.py --concurrency 30
"""

import argparse
import csv
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.lib.sitemap_parser import build_http_client, _fetch_xml, _is_sitemapindex, _get_locs, _parse_urlset

A_TO_Z_API = "https://docs.tibco.com/api/a_to_z"
L2_SITEMAP_BASE = "https://docs.tibco.com/ftp_portal/coveo/tibco-{slug}.xml"


def _version_from_l3(l3_url: str, slug: str) -> str:
    """Extract dotted version string from an L3 sitemap filename."""
    stem = Path(urlparse(l3_url).path).stem
    slug_bare = slug[len("tibco-"):] if slug.startswith("tibco-") else slug
    prefix = f"tibco-{slug_bare}-"
    if stem.startswith(prefix):
        return stem[len(prefix):].replace("-", ".")
    m = re.search(r"(\d[\d-]*)$", stem)
    return m.group(1).replace("-", ".") if m else stem


def fetch_version_info(client, slug: str) -> dict:
    """Fetch the L2 sitemapindex for a product and return version metadata."""
    slug_bare = slug[len("tibco-"):] if slug.startswith("tibco-") else slug
    l2_url = L2_SITEMAP_BASE.format(slug=slug_bare)
    result = {"version_count": "", "latest_version": ""}
    try:
        root = _fetch_xml(client, l2_url)
    except Exception:
        return result

    if _is_sitemapindex(root):
        l3_urls = _get_locs(root, "sitemap")
    else:
        l3_urls = _get_locs(root, "url")

    if not l3_urls:
        return result

    versions = [_version_from_l3(u, slug) for u in l3_urls]
    result["version_count"]  = len(l3_urls)
    result["latest_version"] = versions[-1] if versions else ""
    return result


def main():
    parser = argparse.ArgumentParser(
        description="List all docs.tibco.com products and write a CSV for BU classification."
    )
    parser.add_argument("--out",         default="products.csv", metavar="PATH",
                        help="Output CSV path (default: products.csv)")
    parser.add_argument("--config",      default="config/settings.yaml",
                        help="settings.yaml path (default: config/settings.yaml)")
    parser.add_argument("--versions",    action="store_true",
                        help="Also fetch L2 sitemaps to populate version_count and latest_version")
    parser.add_argument("--concurrency", type=int, default=20, metavar="N",
                        help="Parallel HTTP fetches when --versions is used (default: 20)")
    args = parser.parse_args()

    settings = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    client   = build_http_client(settings)

    # Step 1: fetch product list from /api/a_to_z
    print(f"Fetching product list: {A_TO_Z_API}")
    resp = client.get(A_TO_Z_API)
    resp.raise_for_status()
    data = resp.json()
    raw  = data["result"]["products"]

    products = []
    for p in raw:
        slug = p.get("slug", "")
        name = p.get("name", "").strip()
        if not slug or not name:
            continue
        products.append({
            "product_slug":     slug,
            "product_name":     name,
            "product_page_url": f"https://docs.tibco.com/products/{slug}",
            "version_count":    p.get("versionCount", ""),
            "latest_version":   "",
            "bu":               "",
        })

    print(f"Found {len(products)} products\n")

    # Step 2 (optional): fetch L2 sitemaps for latest_version
    if args.versions:
        print(f"Fetching version details (concurrency={args.concurrency})...")
        start = time.time()

        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {
                pool.submit(fetch_version_info, client, p["product_slug"]): i
                for i, p in enumerate(products)
            }
            done = 0
            for future in as_completed(futures):
                idx  = futures[future]
                info = future.result()
                products[idx].update(info)
                done += 1
                p = products[idx]
                vc = info["version_count"]
                print(f"  [{done:>3}/{len(products)}] {p['product_name']}"
                      f"  ({vc} versions)" if vc else
                      f"  [{done:>3}/{len(products)}] {p['product_name']}  (no sitemap)")

        elapsed = round(time.time() - start, 1)
        print(f"\nDone in {elapsed}s")

    # Sort by product name
    products.sort(key=lambda r: (r["product_name"] or r["product_slug"]).lower())

    # Write CSV
    out_path = Path(args.out)
    fieldnames = ["product_slug", "product_name", "product_page_url",
                  "version_count", "latest_version", "bu"]

    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(products)

    print(f"CSV written to: {out_path.resolve()}")
    print(f"  Rows: {len(products)}")
    print(f"\nNext steps:")
    print(f"  1. Open {out_path} in Excel or Sheets")
    print(f"  2. Fill in the 'bu' column for each product")
    print(f"  3. Group by BU to define phase YAML files")


if __name__ == "__main__":
    sys.exit(main())
