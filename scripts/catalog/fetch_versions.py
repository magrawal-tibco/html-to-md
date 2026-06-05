"""
scripts/catalog/fetch_versions.py — Fetch all product versions and archived ZIP URLs
from docs.tibco.com and write a CSV.

For each product the script collects:
  • Active versions  — from /api/products/<versioned-slug> siblings where isArchive=False
  • Archived versions — from /api/products/archive/<slug> ("Other Versions")

Output CSV columns:
  product_name     — human-readable product name
  product_slug     — parent product slug (e.g. tibco-businessevents-enterprise-edition)
  version          — version string (e.g. 6.4.0)
  doc_url          — product version page on docs.tibco.com
  is_archived      — True if this version appears under "Other Versions"
  zip_url          — direct ZIP download URL (archived versions only; empty for current)
  ga_date          — GA release date string (archived versions only)

Usage:
  python scripts/catalog/fetch_versions.py
  python scripts/catalog/fetch_versions.py --out versions.csv
  python scripts/catalog/fetch_versions.py --product tibco-businessevents-enterprise-edition
  python scripts/catalog/fetch_versions.py --concurrency 30
"""

import argparse
import csv
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

A_TO_Z_API      = "https://docs.tibco.com/api/a_to_z"
ARCHIVE_API     = "https://docs.tibco.com/api/products/archive/{slug}"
PRODUCTS_API    = "https://docs.tibco.com/api/products/{slug}"
DOC_URL_BASE    = "https://docs.tibco.com/products/{slug}"
ZIP_BASE        = "https://docs.tibco.com"
DEFAULT_OUT     = "tibco_versions.csv"
USER_AGENT      = "tibco-catalog-fetcher/1.0"

FIELDS = [
    "product_name", "product_slug",
    "version", "doc_url",
    "is_archived", "zip_url", "ga_date",
]


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json, */*"},
        timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10),
        follow_redirects=True,
    )


def _fetch_json(client: httpx.Client, url: str) -> dict | None:
    try:
        r = client.get(url)
        r.raise_for_status()
        if "json" in r.headers.get("content-type", ""):
            return r.json()
    except Exception:
        pass
    return None


# ── Per-product fetch ─────────────────────────────────────────────────────────

def fetch_product_versions(client: httpx.Client, product: dict) -> list[dict]:
    """
    Fetch all version rows for one product using the products API.

    Strategy:
    1. Archive API → archived version_nos, ZIP URLs, GA dates + one archived versioned slug
    2. If no archived slug, fall back to /api/products/<parent-slug> for a versioned slug
    3. /api/products/<versioned-slug> → siblings → active versions (isArchive=False)
    """
    name = product["name"]
    slug = product["slug"]   # parent product slug from a_to_z
    rows: list[dict] = []

    # ── 1. Archived versions from archive API ─────────────────────────────────
    archived: dict[str, dict] = {}   # version_no → {zip_url, ga_date}
    archived_versioned_slug: str | None = None

    arch_data = _fetch_json(client, ARCHIVE_API.format(slug=slug))
    if arch_data:
        for child in arch_data.get("result", {}).get("product", {}).get("children", []):
            ver  = child.get("version_no", "")
            path = child.get("zipPath", "")
            date = child.get("GA_date", "")
            if ver:
                archived[ver] = {
                    "zip_url": (ZIP_BASE + path) if path else "",
                    "ga_date": date,
                }
                if path and archived_versioned_slug is None:
                    fname = path.rstrip("/").split("/")[-1]
                    if fname.endswith("_documentation.zip"):
                        archived_versioned_slug = fname[: -len("_documentation.zip")]

    # ── 2. Find a versioned slug for the siblings API ─────────────────────────
    versioned_slug = archived_versioned_slug

    if versioned_slug is None:
        # No archives → ask the parent-slug API
        p_data = _fetch_json(client, PRODUCTS_API.format(slug=slug))
        if p_data:
            product_data = p_data.get("result", {}).get("product", {})
            if not product_data.get("isParentProduct") and product_data.get("version_no"):
                versioned_slug = product_data.get("slug")

    if versioned_slug is None:
        # No versioned slug found at all — emit archived-only rows and return
        for ver, arch_info in archived.items():
            rows.append({
                "product_name": name,
                "product_slug": slug,
                "version":      ver,
                "doc_url":      "",
                "is_archived":  True,
                "zip_url":      arch_info["zip_url"],
                "ga_date":      arch_info["ga_date"],
            })
        return rows

    # ── 3. Siblings of a versioned product → active versions ──────────────────
    v_data = _fetch_json(client, PRODUCTS_API.format(slug=versioned_slug))
    if not v_data:
        return rows
    vp = v_data.get("result", {}).get("product", {})

    # Collect all version entries: current product + all siblings
    all_entries: list[dict] = []
    if vp.get("version_no") and vp.get("slug"):
        all_entries.append({
            "version_no": vp["version_no"],
            "slug":       vp["slug"],
            "isArchive":  bool(vp.get("isArchive")),
        })
    for sib in vp.get("siblings", []):
        if sib.get("version_no") and sib.get("slug"):
            all_entries.append({
                "version_no": sib["version_no"],
                "slug":       sib["slug"],
                "isArchive":  bool(sib.get("isArchive")),
            })

    seen_versions: set[str] = set()
    for entry in all_entries:
        ver = entry["version_no"]
        if ver in seen_versions:
            continue
        seen_versions.add(ver)

        is_archived = entry["isArchive"]
        arch_info   = archived.get(ver, {})
        doc_url     = DOC_URL_BASE.format(slug=entry["slug"])

        rows.append({
            "product_name": name,
            "product_slug": slug,
            "version":      ver,
            "doc_url":      doc_url,
            "is_archived":  is_archived,
            "zip_url":      arch_info.get("zip_url", ""),
            "ga_date":      arch_info.get("ga_date", ""),
        })

    # Include archived versions from archive API not yet covered by siblings
    for ver, arch_info in archived.items():
        if ver not in seen_versions:
            rows.append({
                "product_name": name,
                "product_slug": slug,
                "version":      ver,
                "doc_url":      "",
                "is_archived":  True,
                "zip_url":      arch_info["zip_url"],
                "ga_date":      arch_info["ga_date"],
            })

    # Sort by version (newest first)
    def _ver_key(row):
        parts = row["version"].split(".")
        return tuple(int(p) if p.isdigit() else p for p in parts)

    rows.sort(key=_ver_key, reverse=True)
    return rows


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch all product versions and archive ZIP URLs from docs.tibco.com"
    )
    parser.add_argument("--out",         default=DEFAULT_OUT, metavar="PATH",
                        help=f"Output CSV path (default: {DEFAULT_OUT})")
    parser.add_argument("--product",     default=None, metavar="SLUG",
                        help="Limit to a single product slug (for testing)")
    parser.add_argument("--concurrency", type=int, default=10, metavar="N",
                        help="Parallel product fetches (default: 10)")
    args = parser.parse_args()

    client = _client()

    # Step 1: get product list
    print(f"Fetching product list from {A_TO_Z_API}")
    data = _fetch_json(client, A_TO_Z_API)
    if not data:
        print("ERROR: could not fetch product list", file=sys.stderr)
        return 1

    products = data["result"]["products"]
    if args.product:
        products = [p for p in products if p["slug"] == args.product]
        if not products:
            print(f"ERROR: product '{args.product}' not found in a_to_z", file=sys.stderr)
            return 1

    print(f"Found {len(products)} products to process\n")

    # Step 2: fetch versions for all products in parallel
    all_rows: list[dict] = []
    start = time.time()

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(fetch_product_versions, client, p): p for p in products}
        done = 0
        for future in as_completed(futures):
            product = futures[future]
            done += 1
            try:
                rows = future.result()
                all_rows.extend(rows)
                archived_count = sum(1 for r in rows if r["is_archived"])
                print(f"  [{done:>3}/{len(products)}]  {product['name']}"
                      f"  ({len(rows)} versions, {archived_count} archived)")
            except Exception as exc:
                print(f"  [{done:>3}/{len(products)}]  {product['name']}  ERROR: {exc}")

    elapsed = round(time.time() - start, 1)
    print(f"\nDone in {elapsed}s — {len(all_rows)} total version rows")

    # Sort by product name then version (newest first)
    all_rows.sort(key=lambda r: (
        r["product_name"].lower(),
        tuple(-(int(p) if p.isdigit() else 0) for p in r["version"].split(".")),
    ))

    # Write CSV
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"\nCSV written: {out_path.resolve()}")
    print(f"  Rows:    {len(all_rows)}")
    print(f"  Columns: {', '.join(FIELDS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
