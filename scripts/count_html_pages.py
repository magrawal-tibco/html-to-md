"""
count_html_pages.py — Count HTML files per non-archived version using local cache
or downloaded ZIPs.

For each non-archived version in tibco_versions.csv:
  1. Call TIBCO products API to resolve pub_slug / product_version / zip_url
  2. If already extracted in cache/pub/<pub_slug>/<version>/: count HTML files
  3. If not: download ZIP → extract → count HTML files
  4. Write a new CSV: tibco_versions_with_counts.csv

Usage:
  python scripts/count_html_pages.py [--input tibco_versions.csv]
                                     [--output tibco_versions_with_counts.csv]
                                     [--config config/settings.yaml]
                                     [--concurrency 10]
                                     [--no-download]     # skip download, only count from cache
"""

import argparse
import asyncio
import csv
import io
import json
import re
import sys
import time
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import httpx
import yaml
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent))

PRODUCTS_API = "https://docs.tibco.com/api/products/{slug}"

_SKIP_FILENAMES = {"Default.htm", "Default_CSH.htm", "Home.htm"}
_SKIP_SEGMENTS = ["/api/javadoc/", "/_globalpages/", "/MicroContent/",
                  "_templates/", "/Skins/", "/Resources/"]
_GUID_RE = re.compile(r"^GUID-[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\.html?$", re.IGNORECASE)
_HTML_EXTS = {".htm", ".html"}


def load_settings(config_path: str) -> dict:
    return yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))


def is_content_html(path: str) -> bool:
    """Return True if path is a content HTML file (not a skin, not a skip segment)."""
    p = PurePosixPath(path)
    name = p.name
    if name in _SKIP_FILENAMES:
        return False
    if p.suffix.lower() not in _HTML_EXTS:
        return False
    if _GUID_RE.match(name):
        return False
    for seg in _SKIP_SEGMENTS:
        if seg in path:
            return False
    return True


def count_html_in_dir(root: Path) -> int:
    """Count content HTML files recursively under root."""
    return sum(
        1 for f in root.rglob("*")
        if f.is_file() and is_content_html(f.as_posix())
    )


def find_extracted_root(cache_dir: Path, pub_slug: str, product_version: str) -> Path | None:
    """
    Return the extracted cache root directory for a version, or None if not found.
    Tries:
      cache/pub/<pub_slug>/<product_version>/
      (version dirs may have dash/underscore variants)
    """
    base = cache_dir / "pub" / pub_slug
    if not base.exists():
        return None
    # Exact match first
    exact = base / product_version
    if exact.exists():
        return exact
    # Fuzzy match: normalize separators
    norm = product_version.replace(".", "-").lower()
    for d in base.iterdir():
        if d.is_dir() and d.name.lower().replace(".", "-").startswith(norm):
            return d
    return None


def resolve_version_via_api(client: httpx.Client, doc_url: str) -> dict | None:
    """Call TIBCO products API to get pub_slug, product_version, zip_url."""
    versioned_slug = urlparse(doc_url).path.rstrip("/").split("/")[-1]
    try:
        resp = client.get(PRODUCTS_API.format(slug=versioned_slug), timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None
    product = data.get("result", {}).get("product", {})
    folder_path = product.get("folder_path", "")
    if not folder_path or "/" not in folder_path:
        return None
    pub_slug, product_version = folder_path.split("/", 1)
    zip_url = f"https://docs.tibco.com/pub/{folder_path}/{versioned_slug}_documentation.zip"
    return {
        "versioned_slug": versioned_slug,
        "pub_slug":        pub_slug,
        "product_version": product_version,
        "zip_url":         zip_url,
    }


def download_and_count(client: httpx.Client, zip_url: str,
                       cache_dir: Path, pub_slug: str, product_version: str) -> int | None:
    """
    Download ZIP to a temp path, extract to cache, count HTML files.
    Returns page count, or None on failure.
    """
    tmp_zip = cache_dir / "pub" / pub_slug / product_version / "_tmp_docs.zip"
    tmp_zip.parent.mkdir(parents=True, exist_ok=True)

    # Download
    try:
        with client.stream("GET", zip_url, timeout=120) as resp:
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            with open(tmp_zip, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                    f.write(chunk)
    except Exception:
        tmp_zip.unlink(missing_ok=True)
        return None

    # Extract
    try:
        if not zipfile.is_zipfile(tmp_zip):
            tmp_zip.unlink(missing_ok=True)
            return None

        extract_root = cache_dir / "pub" / pub_slug / product_version
        with zipfile.ZipFile(tmp_zip, "r") as zf:
            top_dirs = {
                m.filename.replace("\\", "/").split("/")[0]
                for m in zf.infolist()
                if "/" in m.filename.replace("\\", "/")
            }
            strip = (top_dirs.pop() + "/") if len(top_dirs) == 1 else ""
            for member in zf.infolist():
                rel = member.filename.replace("\\", "/")
                if strip and rel.startswith(strip):
                    rel = rel[len(strip):]
                if not rel or rel.endswith("/"):
                    continue
                dest = extract_root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(zf.read(member.filename))
    except Exception:
        return None
    finally:
        tmp_zip.unlink(missing_ok=True)

    return count_html_in_dir(extract_root)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Count HTML pages per non-archived version from cache or ZIPs"
    )
    parser.add_argument("--input", default="tibco_versions.csv")
    parser.add_argument("--output", default="tibco_versions_with_counts.csv")
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument("--concurrency", type=int, default=10,
                        help="Max parallel API / download requests (default: 10)")
    parser.add_argument("--no-download", action="store_true",
                        help="Only count from local cache; skip downloading missing ZIPs")
    args = parser.parse_args()

    settings = load_settings(args.config)
    cache_dir = Path(settings.get("cache_dir", "cache"))
    delay = settings.get("http", {}).get("delay_seconds", 0.2)
    user_agent = settings.get("http", {}).get("user_agent", "tibco-docs-counter/1.0")

    # Read input CSV
    rows = list(csv.DictReader(open(args.input, encoding="utf-8-sig")))
    non_archived = [r for r in rows if r["is_archived"].strip().upper() == "FALSE"]
    archived = [r for r in rows if r["is_archived"].strip().upper() != "FALSE"]
    print(f"Input rows: {len(rows)}  |  Non-archived: {len(non_archived)}  |  Archived: {len(archived)}")

    client = httpx.Client(
        headers={"User-Agent": user_agent},
        follow_redirects=True,
    )

    results: list[dict] = []
    cache_hits = 0
    api_resolved = 0
    downloaded = 0
    not_found = 0
    errors = 0

    print(f"\nProcessing {len(non_archived)} non-archived versions...")

    for row in tqdm(non_archived, desc="Versions", unit="ver"):
        doc_url = row["doc_url"].strip()
        html_count = ""

        # Step 1: resolve via API
        info = resolve_version_via_api(client, doc_url)
        time.sleep(delay)

        if not info:
            errors += 1
            results.append({**row, "html_file_count": ""})
            continue

        pub_slug = info["pub_slug"]
        product_version = info["product_version"]

        # Step 2: check local cache
        root = find_extracted_root(cache_dir, pub_slug, product_version)
        if root:
            html_count = count_html_in_dir(root)
            cache_hits += 1
            api_resolved += 1
        else:
            api_resolved += 1
            if args.no_download:
                html_count = ""
            else:
                # Step 3: download ZIP
                count = download_and_count(client, info["zip_url"], cache_dir,
                                           pub_slug, product_version)
                if count is None:
                    not_found += 1
                    html_count = ""
                else:
                    downloaded += 1
                    html_count = count

        results.append({**row, "html_file_count": html_count})

    client.close()

    # Append archived rows (no count)
    for row in archived:
        results.append({**row, "html_file_count": ""})

    # Write output CSV — preserve original column order + new column
    fieldnames = list(rows[0].keys()) + ["html_file_count"]
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(results)

    print(f"\n=== Summary ===")
    print(f"  API resolved     : {api_resolved}")
    print(f"  From cache       : {cache_hits}")
    print(f"  Downloaded ZIP   : {downloaded}")
    print(f"  ZIP not found    : {not_found}")
    print(f"  API errors       : {errors}")
    print(f"  Output           : {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
