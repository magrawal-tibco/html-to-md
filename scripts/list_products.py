"""
list_products.py — Enumerate all products on docs.tibco.com and write a CSV
for Business Unit classification.

Uses the same API that powers the A-Z products page (/api/a_to_z).
With --versions, also fetches version breakdown for each product:
  - active_count / active_version_urls  — versions shown in the main dropdown
  - archive_count / archive_zip_urls    — "Other Versions" with ZIP download URLs

Output CSV columns (basic):
  product_slug, product_name, product_page_url, version_count, bu

With --versions, adds:
  active_count, active_version_urls, archive_count, archive_zip_urls

Usage:
  python scripts/list_products.py
  python scripts/list_products.py --out products.csv --versions
  python scripts/list_products.py --concurrency 30
"""

import argparse
import csv
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts.lib.sitemap_parser import build_http_client

A_TO_Z_API   = "https://docs.tibco.com/api/a_to_z"
ARCHIVE_API  = "https://docs.tibco.com/api/products/archive/{slug}"
PRODUCTS_API = "https://docs.tibco.com/api/products/{slug}"
ZIP_BASE     = "https://docs.tibco.com"

BASIC_FIELDS   = ["product_slug", "product_name", "product_page_url", "version_count", "bu"]
VERSION_FIELDS = ["active_count", "active_version_urls", "archive_count", "archive_zip_urls"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fetch_json(client: httpx.Client, url: str) -> dict | None:
    try:
        resp = client.get(url)
        if "json" in resp.headers.get("content-type", ""):
            return resp.json()
    except Exception:
        pass
    return None


def fetch_version_details(client: httpx.Client, slug: str) -> dict:
    """
    Fetch version breakdown for one product using the products API.

    Strategy:
    1. Archive API → archived version_nos + ZIP URLs + one archived versioned slug
    2. If no archived slug, fall back to /api/products/<parent-slug> for a versioned slug
    3. /api/products/<versioned-slug> → siblings → derive active versions
    """
    result = {
        "active_count":        0,
        "active_version_urls": "",
        "archive_count":       0,
        "archive_zip_urls":    "",
    }

    # ── Step 1: Archive API → archived versions + ZIP URLs ────────────────────
    archived_zips: dict[str, str] = {}   # version_no → zip_url
    archived_slug: str | None = None     # versioned slug extracted from first child's zipPath

    data = _fetch_json(client, ARCHIVE_API.format(slug=slug))
    if data:
        for child in data.get("result", {}).get("product", {}).get("children", []):
            ver  = child.get("version_no", "")
            path = child.get("zipPath", "")
            if ver:
                archived_zips[ver] = (ZIP_BASE + path) if path else ""
                if path and archived_slug is None:
                    fname = path.rstrip("/").split("/")[-1]
                    if fname.endswith("_documentation.zip"):
                        archived_slug = fname[: -len("_documentation.zip")]

    result["archive_count"]    = len(archived_zips)
    result["archive_zip_urls"] = " | ".join(url for url in archived_zips.values() if url)

    # ── Step 2: Find any versioned slug ───────────────────────────────────────
    versioned_slug = archived_slug

    if versioned_slug is None:
        # No archives → ask the parent-slug API (it often resolves to the single active version)
        p_data = _fetch_json(client, PRODUCTS_API.format(slug=slug))
        if p_data:
            product = p_data.get("result", {}).get("product", {})
            if not product.get("isParentProduct") and product.get("version_no"):
                versioned_slug = product.get("slug")

    if versioned_slug is None:
        return result

    # ── Step 3: Siblings of a versioned product → active versions ─────────────
    v_data = _fetch_json(client, PRODUCTS_API.format(slug=versioned_slug))
    if not v_data:
        return result
    vp = v_data.get("result", {}).get("product", {})

    active: dict[str, str] = {}
    if not vp.get("isArchive") and vp.get("version_no") and vp.get("slug"):
        active[vp["version_no"]] = f"{ZIP_BASE}/products/{vp['slug']}"
    for sib in vp.get("siblings", []):
        if not sib.get("isArchive") and sib.get("version_no") and sib.get("slug"):
            active[sib["version_no"]] = f"{ZIP_BASE}/products/{sib['slug']}"

    result["active_count"]        = len(active)
    result["active_version_urls"] = " | ".join(active.values())
    return result


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="List all docs.tibco.com products and write a CSV for BU classification."
    )
    parser.add_argument("--out",         default="products.csv", metavar="PATH",
                        help="Output CSV path (default: products.csv)")
    parser.add_argument("--config",      default="config/settings.yaml",
                        help="settings.yaml path (default: config/settings.yaml)")
    parser.add_argument("--versions",    action="store_true",
                        help="Also fetch version breakdown (active/archive counts and URLs)")
    parser.add_argument("--concurrency", type=int, default=20, metavar="N",
                        help="Parallel fetches when --versions is used (default: 20)")
    args = parser.parse_args()

    settings = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    client   = build_http_client(settings)

    # Step 1: fetch product list
    print(f"Fetching product list: {A_TO_Z_API}")
    resp = client.get(A_TO_Z_API)
    resp.raise_for_status()
    raw = resp.json()["result"]["products"]

    products = []
    for p in raw:
        slug = p.get("slug", "")
        name = p.get("name", "").strip()
        if not slug or not name:
            continue
        products.append({
            "product_slug":        slug,
            "product_name":        name,
            "product_page_url":    f"https://docs.tibco.com/products/{slug}",
            "version_count":       p.get("versionCount", ""),
            "bu":                  "",
            "active_count":        "",
            "active_version_urls": "",
            "archive_count":       "",
            "archive_zip_urls":    "",
        })

    print(f"Found {len(products)} products\n")

    # Step 2 (optional): fetch version breakdown per product
    if args.versions:
        print(f"Fetching version details (concurrency={args.concurrency})...")
        start = time.time()

        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {
                pool.submit(fetch_version_details, client, p["product_slug"]): i
                for i, p in enumerate(products)
            }
            done = 0
            for future in as_completed(futures):
                idx  = futures[future]
                info = future.result()
                products[idx].update(info)
                done += 1
                p = products[idx]
                print(f"  [{done:>3}/{len(products)}]  {p['product_name']}"
                      f"  (active={info['active_count']}, archived={info['archive_count']})")

        elapsed = round(time.time() - start, 1)
        print(f"\nDone in {elapsed}s")

    products.sort(key=lambda r: (r["product_name"] or r["product_slug"]).lower())

    fieldnames = BASIC_FIELDS + (VERSION_FIELDS if args.versions else [])
    out_path = Path(args.out)
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(products)

    print(f"CSV written to: {out_path.resolve()}")
    print(f"  Rows:    {len(products)}")
    print(f"  Columns: {', '.join(fieldnames)}")


if __name__ == "__main__":
    sys.exit(main())
