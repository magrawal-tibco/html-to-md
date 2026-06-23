"""
patch_missing_counts.py — Download ZIPs for rows with manually-provided URLs in
tibco_versions_missing_log.csv, count HTML files, and patch tibco_versions_with_counts.csv.

Usage:
  python scripts/patch_missing_counts.py
      [--log tibco_versions_missing_log.csv]
      [--counts tibco_versions_with_counts.csv]
      [--config config/settings.yaml]
"""

import argparse
import csv
import sys
import time
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse
import re

import httpx
import yaml
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent))

_SKIP_FILENAMES = {"Default.htm", "Default_CSH.htm", "Home.htm"}
_SKIP_SEGMENTS = ["/api/javadoc/", "/_globalpages/", "/MicroContent/",
                  "_templates/", "/Skins/", "/Resources/"]
_GUID_RE = re.compile(
    r"^GUID-[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\.html?$",
    re.IGNORECASE,
)
_HTML_EXTS = {".htm", ".html"}


def load_settings(config_path: str) -> dict:
    return yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))


def is_content_html(path: str) -> bool:
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
    return sum(1 for f in root.rglob("*") if f.is_file() and is_content_html(f.as_posix()))


def cache_path_from_zip_url(cache_dir: Path, zip_url: str) -> tuple[str, str, Path]:
    """
    Derive (pub_slug, product_version, extract_root) from a zip_url.

    zip_url format: https://docs.tibco.com/pub/<pub_slug>/<versioned_slug>_documentation.zip
    OR:             https://docs.tibco.com/pub/<pub_slug>/<product_version>/<versioned_slug>_documentation.zip

    We try to extract pub_slug and product_version from the URL path.
    Strategy:
      - path segments after /pub/ are: [pub_slug, ...rest, <slug>_documentation.zip]
      - If rest has 1 element (just the zip filename) → pub_slug=segments[0], version=slugified from filename
      - If rest has 2+ elements → pub_slug=segments[0], product_version=segments[1]
    """
    parsed = urlparse(zip_url)
    parts = [p for p in parsed.path.split("/") if p]
    # parts[0] == 'pub', parts[1] == pub_slug, parts[-1] == filename
    pub_slug = parts[1] if len(parts) > 1 else "unknown"
    filename = parts[-1]  # e.g. tibco-foo-1-2-3_documentation.zip

    if len(parts) >= 4:
        # /pub/<pub_slug>/<product_version>/<filename> — version is explicit
        product_version = parts[2]
    else:
        # /pub/<pub_slug>/<filename> — derive version from filename
        # filename: tibco-activematrix-businessworks-plug-in-for-b2b-1-1-0_documentation.zip
        # versioned slug: tibco-...-1-1-0
        stem = filename.replace("_documentation.zip", "")
        # Extract trailing version-like segment: digits separated by hyphens
        m = re.search(r"((\d+(?:-\d+)+))$", stem)
        product_version = m.group(1).replace("-", ".") if m else stem

    extract_root = cache_dir / "pub" / pub_slug / product_version
    return pub_slug, product_version, extract_root


def download_and_count(client: httpx.Client, zip_url: str, extract_root: Path) -> int | None:
    tmp_zip = extract_root / "_tmp_docs.zip"
    extract_root.mkdir(parents=True, exist_ok=True)

    try:
        with client.stream("GET", zip_url, timeout=120) as resp:
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            # Check content-type — reject HTML pages disguised as ZIPs
            ct = resp.headers.get("content-type", "")
            if "text/html" in ct:
                return None
            with open(tmp_zip, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                    f.write(chunk)
    except Exception as e:
        tmp_zip.unlink(missing_ok=True)
        return None

    try:
        if not zipfile.is_zipfile(tmp_zip):
            tmp_zip.unlink(missing_ok=True)
            return None

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
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", default="tibco_versions_missing_log.csv")
    parser.add_argument("--counts", default="tibco_versions_with_counts.csv")
    parser.add_argument("--config", default="config/settings.yaml")
    args = parser.parse_args()

    settings = load_settings(args.config)
    cache_dir = Path(settings.get("cache_dir", "cache"))
    delay = settings.get("http", {}).get("delay_seconds", 0.2)
    user_agent = settings.get("http", {}).get("user_agent", "tibco-docs-counter/1.0")

    # Load log rows with non-empty zip_url
    log_rows = list(csv.DictReader(open(args.log, encoding="utf-8-sig")))
    to_process = [r for r in log_rows if r.get("zip_url", "").strip()]
    skipped = [r for r in log_rows if not r.get("zip_url", "").strip()]
    print(f"Log rows: {len(log_rows)}  |  With ZIP URL: {len(to_process)}  |  Skipping (no URL): {len(skipped)}")

    # Load counts CSV — index by doc_url
    counts_rows = list(csv.DictReader(open(args.counts, encoding="utf-8-sig")))
    counts_by_url = {r["doc_url"].strip(): r for r in counts_rows}

    client = httpx.Client(headers={"User-Agent": user_agent}, follow_redirects=True)

    success = 0
    not_found = 0
    errors = 0
    already_cached = 0

    for row in tqdm(to_process, desc="Patching", unit="row"):
        zip_url = row["zip_url"].strip()
        doc_url = row["doc_url"].strip()

        pub_slug, product_version, extract_root = cache_path_from_zip_url(cache_dir, zip_url)

        # Check if already extracted
        if extract_root.exists() and any(extract_root.rglob("*.htm")) or \
           extract_root.exists() and any(extract_root.rglob("*.html")):
            count = count_html_in_dir(extract_root)
            already_cached += 1
        else:
            count = download_and_count(client, zip_url, extract_root)
            time.sleep(delay)

        if count is None:
            not_found += 1
            tqdm.write(f"  FAIL  {doc_url}")
        else:
            # Patch counts CSV
            if doc_url in counts_by_url:
                counts_by_url[doc_url]["html_file_count"] = count
                success += 1
                tqdm.write(f"  OK    {count:>6}  {doc_url}")
            else:
                tqdm.write(f"  WARN  doc_url not found in counts CSV: {doc_url}")
                errors += 1

    client.close()

    # Write updated counts CSV
    fieldnames = list(counts_rows[0].keys())
    with open(args.counts, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(counts_rows)

    print(f"\n=== Summary ===")
    print(f"  Patched (new count)  : {success}")
    print(f"  From cache           : {already_cached}")
    print(f"  ZIP failed/not found : {not_found}")
    print(f"  URL not in CSV       : {errors}")
    print(f"  Skipped (no URL)     : {len(skipped)}")
    print(f"  Output               : {args.counts}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
