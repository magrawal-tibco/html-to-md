"""
scripts/catalog/fetch_products.py — Fetch the TIBCO product catalog and write a CSV.

Uses the same REST API that powers the A-Z products page:
  https://docs.tibco.com/api/a_to_z

This returns the definitive product list with proper names, slugs, and version counts.

Output CSV columns:
  product_name      — human-readable product name
  product_slug      — slug used in product page URLs
  product_page_url  — https://docs.tibco.com/products/<slug>
  version_count     — number of versions available

Usage:
  python scripts/catalog/fetch_products.py
  python scripts/catalog/fetch_products.py --out products.csv
"""

import argparse
import csv
import sys
from pathlib import Path

import httpx

A_TO_Z_API   = "https://docs.tibco.com/api/a_to_z"
DEFAULT_OUT  = "tibco_products.csv"
USER_AGENT   = "tibco-catalog-fetcher/1.0"

FIELDS = ["product_name", "product_slug", "product_page_url", "version_count"]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch the TIBCO docs product catalog from /api/a_to_z"
    )
    parser.add_argument("--out", default=DEFAULT_OUT, metavar="PATH",
                        help=f"Output CSV path (default: {DEFAULT_OUT})")
    args = parser.parse_args()

    client = httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10),
        follow_redirects=True,
    )

    print(f"Fetching: {A_TO_Z_API}")
    resp = client.get(A_TO_Z_API)
    resp.raise_for_status()
    data = resp.json()

    if not data.get("result", {}).get("success"):
        print(f"ERROR: API returned failure: {data}", file=sys.stderr)
        return 1

    raw = data["result"]["products"]

    products = []
    for p in raw:
        slug = p.get("slug", "")
        name = p.get("name", "").strip()
        if not slug or not name:
            continue
        products.append({
            "product_name":     name,
            "product_slug":     slug,
            "product_page_url": f"https://docs.tibco.com/products/{slug}",
            "version_count":    p.get("versionCount", ""),
        })

    products.sort(key=lambda p: p["product_name"].lower())
    print(f"Found {len(products)} products")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(products)

    print(f"CSV written: {out_path.resolve()}")
    print(f"  Rows:    {len(products)}")
    print(f"  Columns: {', '.join(FIELDS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
