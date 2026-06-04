"""
scripts/catalog/fetch_versions.py — Fetch all product versions and archived ZIP URLs
from docs.tibco.com and write a CSV.

For each product the script collects:
  • Current versions  — from the product's L2 sitemap (these appear in the
                        main version dropdown on the product page).
  • Archived versions — from /api/products/archive/<slug> (the "Other Versions"
                        section), which also includes a ZIP download path.

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
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import httpx

A_TO_Z_API      = "https://docs.tibco.com/api/a_to_z"
ARCHIVE_API     = "https://docs.tibco.com/api/products/archive/{slug}"
L2_SITEMAP_BASE = "https://docs.tibco.com/ftp_portal/coveo/tibco-{slug}.xml"
DOC_URL_BASE    = "https://docs.tibco.com/products/{version_slug}"
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


def _fetch_xml(client: httpx.Client, url: str) -> ET.Element | None:
    try:
        r = client.get(url)
        r.raise_for_status()
        return ET.fromstring(r.content)
    except Exception:
        return None


# ── Sitemap helpers ───────────────────────────────────────────────────────────

def _ns_uri(root: ET.Element) -> str:
    return root.tag.split("}")[0].lstrip("{") if "}" in root.tag else ""


def _get_locs(root: ET.Element, child_tag: str) -> list[str]:
    ns = _ns_uri(root)
    t_child = f"{{{ns}}}{child_tag}" if ns else child_tag
    t_loc   = f"{{{ns}}}loc"         if ns else "loc"
    return [
        child.find(t_loc).text.strip()
        for child in root.findall(t_child)
        if child.find(t_loc) is not None and child.find(t_loc).text
    ]


def _version_from_l3(l3_url: str, parent_slug: str) -> str:
    """Extract dotted version string from an L3 sitemap filename."""
    stem = Path(urlparse(l3_url).path).stem   # e.g. tibco-foo-6-4-0
    # Strip "tibco-" prefix + parent slug (without tibco- prefix)
    slug_no_prefix = parent_slug[len("tibco-"):] if parent_slug.startswith("tibco-") else parent_slug
    prefix = f"tibco-{slug_no_prefix}-"
    if stem.startswith(prefix):
        return stem[len(prefix):].replace("-", ".")
    m = re.search(r"(\d[\d-]*)$", stem)
    return m.group(1).replace("-", ".") if m else stem


def _version_doc_url(parent_slug: str, version: str) -> str:
    ver_dash = version.replace(".", "-")
    # The slug without "tibco-" prefix for the no-tibco part
    slug_bare = parent_slug[len("tibco-"):] if parent_slug.startswith("tibco-") else parent_slug
    version_slug = f"tibco-{slug_bare}-{ver_dash}"
    return DOC_URL_BASE.format(version_slug=version_slug)


# ── Per-product fetch ─────────────────────────────────────────────────────────

def fetch_product_versions(client: httpx.Client, product: dict) -> list[dict]:
    """
    Fetch all version rows for one product.
    Returns a list of row dicts (one per version).
    """
    name  = product["name"]
    slug  = product["slug"]   # e.g. tibco-businessevents-enterprise-edition
    rows: list[dict] = []

    # ── 1. Current versions from L2 sitemap ──────────────────────────────────
    slug_no_tibco = slug[len("tibco-"):] if slug.startswith("tibco-") else slug
    l2_url = L2_SITEMAP_BASE.format(slug=slug_no_tibco)
    l2_root = _fetch_xml(client, l2_url)

    l3_urls: list[str] = []
    if l2_root is not None:
        local_tag = l2_root.tag.split("}")[-1] if "}" in l2_root.tag else l2_root.tag
        if local_tag == "sitemapindex":
            l3_urls = _get_locs(l2_root, "sitemap")
        else:
            # L2 is itself the urlset (single-version product)
            l3_urls = [l2_url]

    sitemap_versions: dict[str, str] = {}   # version → doc_url
    for l3_url in l3_urls:
        ver = _version_from_l3(l3_url, slug)
        doc_url = _version_doc_url(slug, ver)
        sitemap_versions[ver] = doc_url

    # ── 2. Archived versions from archive API ─────────────────────────────────
    arch_data = _fetch_json(client, ARCHIVE_API.format(slug=slug))
    archived: dict[str, dict] = {}   # version_no → {zip_url, ga_date}
    if arch_data:
        product_data = arch_data.get("result", {}).get("product", {})
        for child in product_data.get("children", []):
            ver  = child.get("version_no", "")
            path = child.get("zipPath", "")
            date = child.get("GA_date", "")
            if ver:
                archived[ver] = {
                    "zip_url": (ZIP_BASE + path) if path else "",
                    "ga_date": date,
                }

    # ── 3. Merge: sitemap versions as primary, archived adds zip info ─────────
    # Versions from sitemap (current dropdown)
    for ver, doc_url in sitemap_versions.items():
        arch_info = archived.get(ver, {})
        rows.append({
            "product_name": name,
            "product_slug": slug,
            "version":      ver,
            "doc_url":      doc_url,
            "is_archived":  bool(arch_info),
            "zip_url":      arch_info.get("zip_url", ""),
            "ga_date":      arch_info.get("ga_date", ""),
        })

    # Archived versions not in the sitemap (very old versions)
    sitemap_ver_set = set(sitemap_versions.keys())
    for ver, arch_info in archived.items():
        if ver not in sitemap_ver_set:
            rows.append({
                "product_name": name,
                "product_slug": slug,
                "version":      ver,
                "doc_url":      _version_doc_url(slug, ver),
                "is_archived":  True,
                "zip_url":      arch_info.get("zip_url", ""),
                "ga_date":      arch_info.get("ga_date", ""),
            })

    # Sort by version (newest first) — natural sort on version parts
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
