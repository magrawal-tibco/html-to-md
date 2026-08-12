"""
scripts/catalog/fetch_versions.py — Fetch all product versions and archived ZIP URLs
from docs.tibco.com and write a CSV.

For each product the script collects:
  • Active versions  — from /api/products/<versioned-slug> siblings where isArchive=False
  • Archived versions — from /api/products/archive/<slug> ("Other Versions")

Output CSV columns:
  product_name     — human-readable product name
  product_slug     — parent product slug (e.g. tibco-businessevents-enterprise-edition)
  category         — "Products & Solutions" grouping (e.g. Integration, Visual
                     Analytics); "Unassigned" when the product has no grouping
  version          — version string (e.g. 6.4.0)
  doc_url          — product version page on docs.tibco.com
  is_archived      — True if this version appears under "Other Versions"
  zip_url          — direct ZIP download URL (archived versions only; empty for current)
  ga_date          — GA release date string (archived versions only)

A second CSV (<out>_errors.csv) records any endpoint that could not be resolved,
so a run that silently lost data is distinguishable from one that found none.
See "Failure classification" below — this matters when diffing two snapshots,
because a dropped zip_url looks identical whether the product was un-archived
or the archive endpoint simply failed.

Usage:
  python scripts/catalog/fetch_versions.py
  python scripts/catalog/fetch_versions.py --out versions.csv
  python scripts/catalog/fetch_versions.py --product tibco-businessevents-enterprise-edition
  python scripts/catalog/fetch_versions.py --concurrency 30
  python scripts/catalog/fetch_versions.py --strict      # exit 2 if any endpoint failed

Failure classification (see _fetch_json):
  The API wraps every JSON response in {"result": {"success": bool, "error": str|null, ...}}.
  That envelope — not the HTTP status — is the reliable signal.

  definitive absent  → 200 with a text/html body. The API serves the SPA shell for
                       slugs it cannot resolve. Returns None; caller treats as "no data".
  indeterminate      → transport error, non-JSON error response, malformed JSON, or
                       success=false (e.g. HTTP 500 {"success": false, "error": ...},
                       which is what an unknown slug yields on the archive endpoint).
                       Retried; raises FetchError once retries are exhausted.
  success            → success=true. children:[] here is authoritative — the product
                       genuinely has no archived versions.
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

DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 1.5
UNASSIGNED      = "Unassigned"

FIELDS = [
    "product_name", "product_slug", "category",
    "version", "doc_url",
    "is_archived", "zip_url", "ga_date",
]

ERROR_FIELDS = ["product_name", "product_slug", "endpoint", "url", "error", "impact"]


class FetchError(Exception):
    """An endpoint could not be resolved to a definite answer."""


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json, */*"},
        timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10),
        follow_redirects=True,
    )


def _fetch_json(
    client: httpx.Client,
    url: str,
    *,
    retries: int = DEFAULT_RETRIES,
    backoff: float = DEFAULT_BACKOFF,
) -> dict | None:
    """
    Fetch a JSON API response.

    Returns the parsed payload on success, or None when the endpoint definitively
    has no data (200 text/html — the SPA shell served for unresolvable slugs).
    Raises FetchError when the outcome is indeterminate after `retries` attempts.

    Never conflate the two: returning None on a timeout would make a failed run
    look like an empty one, which is invisible in the output CSV.
    """
    last = "no attempt made"

    for attempt in range(retries):
        try:
            r = client.get(url)
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"
        else:
            if "json" not in r.headers.get("content-type", ""):
                if r.status_code == 200:
                    return None          # SPA shell — slug does not resolve
                last = f"HTTP {r.status_code}, content-type {r.headers.get('content-type') or '(none)'}"
            else:
                try:
                    data = r.json()
                except ValueError as exc:
                    last = f"HTTP {r.status_code}, malformed JSON: {exc}"
                else:
                    result = data.get("result")
                    if isinstance(result, dict) and result.get("success"):
                        return data
                    api_err = result.get("error") if isinstance(result, dict) else None
                    last = f"HTTP {r.status_code}, success=false, error={api_err!r}"

        if attempt < retries - 1:
            time.sleep(backoff ** attempt)

    raise FetchError(last)


# ── Category ─────────────────────────────────────────────────────────────────

def _product_category(product: dict) -> str:
    """
    Category ("Products & Solutions" grouping) for an a_to_z product entry.

    The docs.tibco.com /product/categories page renders this client-side, so the
    page HTML cannot be fetched directly — but the same data is already on every
    a_to_z product as `Groups`. Verified against a browser-saved copy of that
    page: 380 grouped products vs the page's 378, zero disagreements, so the API
    is a strict superset.

    No product currently has more than one group; if that changes they are joined
    with "; " rather than silently dropping one.
    """
    names = sorted(
        g["name"].strip()
        for g in (product.get("Groups") or [])
        if isinstance(g, dict) and (g.get("name") or "").strip()
    )
    return "; ".join(names) if names else UNASSIGNED


# ── Per-product fetch ─────────────────────────────────────────────────────────

def fetch_product_versions(
    client: httpx.Client, product: dict, retries: int = DEFAULT_RETRIES,
) -> tuple[list[dict], list[dict]]:
    """
    Fetch all version rows for one product using the products API.

    Strategy:
    1. Archive API → archived version_nos, ZIP URLs, GA dates + one archived versioned slug
    2. If no archived slug, fall back to /api/products/<parent-slug> for a versioned slug
    3. /api/products/<versioned-slug> → siblings → active versions (isArchive=False)

    Returns (rows, errors). Rows derived from an endpoint that failed are still
    emitted where they come from a different endpoint that succeeded, but every
    failure is recorded so the caller can flag the affected fields as unreliable.
    """
    name     = product["name"]
    slug     = product["slug"]   # parent product slug from a_to_z
    category = _product_category(product)
    rows: list[dict] = []
    errors: list[dict] = []

    def _record(endpoint: str, url: str, exc: Exception, impact: str) -> None:
        errors.append({
            "product_name": name, "product_slug": slug,
            "endpoint": endpoint, "url": url, "error": str(exc), "impact": impact,
        })

    # ── 1. Archived versions from archive API ─────────────────────────────────
    archived: dict[str, dict] = {}   # version_no → {zip_url, ga_date}
    archived_versioned_slug: str | None = None

    arch_url = ARCHIVE_API.format(slug=slug)
    try:
        arch_data = _fetch_json(client, arch_url, retries=retries)
    except FetchError as exc:
        arch_data = None
        _record("archive", arch_url, exc,
                "zip_url/ga_date missing and archived versions may be absent")

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
        p_url = PRODUCTS_API.format(slug=slug)
        try:
            p_data = _fetch_json(client, p_url, retries=retries)
        except FetchError as exc:
            p_data = None
            _record("product", p_url, exc, "no versioned slug found; active versions missing")
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
                "category":     category,
                "version":      ver,
                "doc_url":      "",
                "is_archived":  True,
                "zip_url":      arch_info["zip_url"],
                "ga_date":      arch_info["ga_date"],
            })
        return rows, errors

    # ── 3. Siblings of a versioned product → active versions ──────────────────
    v_url = PRODUCTS_API.format(slug=versioned_slug)
    try:
        v_data = _fetch_json(client, v_url, retries=retries)
    except FetchError as exc:
        v_data = None
        _record("siblings", v_url, exc, "active (non-archived) versions missing")
    if not v_data:
        return rows, errors
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
            "category":     category,
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
                "category":     category,
                "version":      ver,
                "doc_url":      "",
                "is_archived":  True,
                "zip_url":      arch_info["zip_url"],
                "ga_date":      arch_info["ga_date"],
            })

    # Sort by version (newest first). Tag each part with a type rank so a numeric
    # part is never compared against a string one (e.g. "1.0.0" vs "1.0.0-beta"),
    # which would raise TypeError and drop the whole product from the CSV.
    def _ver_key(row):
        return tuple(
            (0, int(p), "") if p.isdigit() else (1, 0, p)
            for p in row["version"].split(".")
        )

    rows.sort(key=_ver_key, reverse=True)
    return rows, errors


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
    parser.add_argument("--retries",     type=int, default=DEFAULT_RETRIES, metavar="N",
                        help=f"Attempts per endpoint before giving up (default: {DEFAULT_RETRIES})")
    parser.add_argument("--strict",      action="store_true",
                        help="Exit 2 if any endpoint failed (CSV is still written)")
    args = parser.parse_args()

    client = _client()

    # Step 1: get product list
    print(f"Fetching product list from {A_TO_Z_API}")
    try:
        data = _fetch_json(client, A_TO_Z_API, retries=args.retries)
    except FetchError as exc:
        print(f"ERROR: could not fetch product list — {exc}", file=sys.stderr)
        return 1
    if not data:
        print(f"ERROR: {A_TO_Z_API} returned no JSON payload", file=sys.stderr)
        return 1

    products = data["result"]["products"]

    # a_to_z can list one slug twice under different spellings of the name (e.g.
    # "Spotfire Application" and "Spotfire™ Application"), which would duplicate
    # every version of that product. Keep one entry per slug, preferring the one
    # carrying Groups — the duplicates are not equivalent, and the bare-name entry
    # is the one missing its category. Name is only a tie-break, so the pick stays
    # stable across runs regardless of API ordering.
    def _entry_rank(e: dict) -> tuple:
        return (-len(e.get("Groups") or []), e["name"])

    by_slug: dict[str, list[dict]] = {}
    for p in products:
        by_slug.setdefault(p["slug"], []).append(p)
    dup_slugs = {s: v for s, v in by_slug.items() if len(v) > 1}
    if dup_slugs:
        print(f"Note: {len(dup_slugs)} slug(s) listed more than once in a_to_z — deduplicating:")
        for s, entries in sorted(dup_slugs.items()):
            kept = min(entries, key=_entry_rank)
            print(f"  {s}: {len(entries)} entries, keeping {kept['name']!r} "
                  f"(category: {_product_category(kept)})")
    products = [min(v, key=_entry_rank) for v in by_slug.values()]

    if args.product:
        products = [p for p in products if p["slug"] == args.product]
        if not products:
            print(f"ERROR: product '{args.product}' not found in a_to_z", file=sys.stderr)
            return 1

    print(f"Found {len(products)} products to process\n")

    # Step 2: fetch versions for all products in parallel
    all_rows: list[dict] = []
    all_errors: list[dict] = []
    start = time.time()

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(fetch_product_versions, client, p, args.retries): p
            for p in products
        }
        done = 0
        for future in as_completed(futures):
            product = futures[future]
            done += 1
            try:
                rows, errors = future.result()
            except Exception as exc:
                # Unexpected bug rather than a fetch failure — surface it as an error
                # row so the product cannot vanish from the CSV unnoticed.
                print(f"  [{done:>3}/{len(products)}]  {product['name']}  ERROR: {exc}")
                all_errors.append({
                    "product_name": product["name"], "product_slug": product["slug"],
                    "endpoint": "unhandled", "url": "",
                    "error": f"{type(exc).__name__}: {exc}",
                    "impact": "product omitted entirely",
                })
                continue

            all_rows.extend(rows)
            all_errors.extend(errors)
            archived_count = sum(1 for r in rows if r["is_archived"])
            suffix = f"  !! {len(errors)} endpoint error(s)" if errors else ""
            print(f"  [{done:>3}/{len(products)}]  {product['name']}"
                  f"  ({len(rows)} versions, {archived_count} archived){suffix}")

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

    cat_products: dict[str, set[str]] = {}
    for r in all_rows:
        cat_products.setdefault(r["category"], set()).add(r["product_slug"])
    print(f"\nCategories ({len(cat_products)}), by product count:")
    for cat, slugs in sorted(cat_products.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        n_rows = sum(1 for r in all_rows if r["category"] == cat)
        print(f"  {cat:34} {len(slugs):>4} products  {n_rows:>5} versions")

    # Error report — written only when there is something to report, and removed
    # otherwise so a stale file from a previous run cannot be mistaken for current.
    err_path = out_path.with_name(f"{out_path.stem}_errors{out_path.suffix}")
    if all_errors:
        with err_path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=ERROR_FIELDS)
            writer.writeheader()
            writer.writerows(all_errors)

        affected = sorted({e["product_slug"] for e in all_errors})
        print(f"\n{'!' * 72}")
        print(f"WARNING: {len(all_errors)} endpoint failure(s) across {len(affected)} product(s).")
        print("Data for these products is incomplete — do NOT treat a missing zip_url")
        print("or absent version as a real catalog change when diffing snapshots.")
        for e in all_errors[:15]:
            print(f"  {e['product_slug']}  [{e['endpoint']}]  {e['error']}")
        if len(all_errors) > 15:
            print(f"  ... and {len(all_errors) - 15} more")
        print(f"Error report: {err_path.resolve()}")
        print("!" * 72)
        if args.strict:
            return 2
    else:
        err_path.unlink(missing_ok=True)
        print("\nAll endpoints resolved cleanly — no failures to report.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
