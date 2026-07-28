"""
download_zips.py — Download ZIP files from a plain URL list.

Reads one ZIP URL per line from a text file (blank lines and lines starting
with '#' are ignored) and downloads each to the cache/zip directory.

  cache/zip/<url-path>   — downloaded ZIPs land here

If --product-slug is supplied, a second archive phase runs after downloading:
  - Copies each ZIP to output/archives/<product-slug>/archive/
  - Writes index.md (version-sorted, highest first) and toc.yml
  - Incremental: existing ZIPs in the archive folder are never overwritten;
    index.md is rebuilt from the full on-disk set each run

Already-downloaded ZIPs are skipped unless --force-download is set.

Usage:
  python scripts/download_zips.py urls.txt
         [--config config/settings.yaml]
         [--zip-cache-dir cache/zip]
         [--product-slug ebx]
         [--product-name "TIBCO EBX®"]
         [--archives-dir output/archives]
         [--dry-run]
         [--force-download]
"""

import argparse
import re
import shutil
import sys
import time
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import httpx
import yaml
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.lib.reporter import Reporter


# ── Regex constants ────────────────────────────────────────────────────────────

# Matches a run of dash-separated integers in a filename (the version segment).
# e.g. "tibco-ebx-6-0-17_documentation.zip" → "6-0-17"
_VERSION_IN_FILENAME_RE = re.compile(r'-(\d+(?:-\d+)+)[_.]')

# Matches bullet links in an existing index.md: - [display text](file.zip)
_INDEX_LINK_RE = re.compile(r'^\s*-\s+\[.*?\]\((.+?\.zip)\)\s*$')


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_settings(config_path: str) -> dict:
    return yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))


def read_url_list(path: Path) -> list[str]:
    """Read one URL per line; skip blanks and # comments."""
    urls = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            urls.append(line)
    return urls


def _download_zip(
    client: httpx.Client,
    zip_url: str,
    zip_path: Path,
    reporter: Reporter,
) -> tuple[bool, str]:
    """Stream-download a ZIP. Returns (success, reason_on_failure)."""
    try:
        with client.stream("GET", zip_url) as resp:
            if resp.status_code == 404:
                return False, "http_404"
            resp.raise_for_status()
            zip_path.parent.mkdir(parents=True, exist_ok=True)
            total = int(resp.headers.get("content-length", 0))
            with (
                open(zip_path, "wb") as fh,
                tqdm(
                    total=total or None,
                    unit="B",
                    unit_scale=True,
                    unit_divisor=1024,
                    desc=zip_path.name,
                    leave=False,
                ) as bar,
            ):
                for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                    fh.write(chunk)
                    bar.update(len(chunk))
        return True, ""
    except httpx.HTTPStatusError as e:
        return False, f"http_{e.response.status_code}"
    except Exception as e:
        return False, f"error_{type(e).__name__}"


# ── Archive helpers ────────────────────────────────────────────────────────────

def extract_version(filename: str) -> tuple[tuple[int, ...], str]:
    """Extract a version tuple (for sorting) and dotted string (for display) from a filename.

    e.g. "tibco-ebx-6-0-17_documentation.zip" → ((6, 0, 17), "6.0.17")
    Returns ((0,), "0") when no version segment is found.
    """
    m = _VERSION_IN_FILENAME_RE.search(filename)
    if m:
        parts = tuple(int(x) for x in m.group(1).split('-'))
        return parts, '.'.join(str(x) for x in parts)
    return (0,), '0'


def parse_existing_index(index_path: Path) -> set[str]:
    """Return the set of ZIP filenames already listed in an index.md bullet list."""
    if not index_path.exists():
        return set()
    listed = set()
    for line in index_path.read_text(encoding='utf-8').splitlines():
        m = _INDEX_LINK_RE.match(line)
        if m:
            listed.add(m.group(1))
    return listed


def write_archive_index(archive_dir: Path, product_name: str,
                        entries: list[tuple]) -> None:
    """Write index.md with a bullet list of ZIP links sorted highest version first.

    entries: list of (version_tuple, dotted_version_str, filename)
    """
    title = f"{product_name} Archived Versions"
    lines = [
        "---\n",
        f"doc_name: Archived Versions\n",
        f"product_name: {product_name}\n",
        f"title: {title}\n",
        "---\n\n",
        f"# Archived Versions\n",
    ]
    for _ver_tuple, dotted_ver, filename in entries:
        display = f"{product_name} {dotted_ver}"
        lines.append(f"- [{display}]({filename})\n")
    (archive_dir / "index.md").write_text("".join(lines), encoding="utf-8")


def write_archive_toc(archive_dir: Path, product_name: str) -> None:
    """Write toc.yml with a single entry pointing to index.md."""
    content = (
        f"docs_list_title: {product_name}\n"
        "docs:\n"
        "  - title: Archived Versions\n"
        "    url: index.md\n"
    )
    (archive_dir / "toc.yml").write_text(content, encoding="utf-8")


def run_archive_phase(
    zip_paths: list[Path],
    archive_dir: Path,
    product_name: str,
    reporter: Reporter,
    dry_run: bool,
) -> None:
    """Copy ZIPs to archive_dir and write/update index.md + toc.yml.

    Only copies ZIPs that do not already exist in archive_dir (incremental).
    index.md is rebuilt from the complete on-disk set after copying.
    """
    reporter.info(f"\n=== Archive phase → {archive_dir} ===")

    if not dry_run:
        archive_dir.mkdir(parents=True, exist_ok=True)

    new_count = 0
    for zip_path in zip_paths:
        dest = archive_dir / zip_path.name
        if dry_run:
            reporter.info(f"  [dry-run] Would archive → {dest}")
        elif not dest.exists():
            shutil.copy2(zip_path, dest)
            new_count += 1
            reporter.info(f"  Archived: {dest}")

    # Rebuild index from everything now on disk in the archive folder
    if dry_run:
        all_names = [p.name for p in zip_paths]
    else:
        all_names = [z.name for z in sorted(archive_dir.glob("*.zip"))]

    entries = []
    for name in all_names:
        ver_tuple, dotted = extract_version(name)
        entries.append((ver_tuple, dotted, name))
    entries.sort(key=lambda e: e[0], reverse=True)  # highest version first

    if not dry_run:
        write_archive_index(archive_dir, product_name, entries)
        write_archive_toc(archive_dir, product_name)
        reporter.info(
            f"Archive: {len(entries)} ZIP(s) total, {new_count} new → {archive_dir}"
        )
    else:
        reporter.info(
            f"[dry-run] Archive would have {len(entries)} ZIP(s) in {archive_dir}"
        )


# ── Main logic ────────────────────────────────────────────────────────────────

def process_urls(
    urls: list[str],
    settings: dict,
    reporter: Reporter,
    zip_cache_dir: Path,
    dry_run: bool,
    force_download: bool,
) -> list[Path]:
    http_cfg = settings.get("http", {})
    delay    = http_cfg.get("delay_seconds", 0.5)

    client = httpx.Client(
        headers={"User-Agent": http_cfg.get("user_agent", "tibco-docs-converter/1.0")},
        timeout=httpx.Timeout(connect=http_cfg.get("timeout_connect", 10),
                              read=600, write=10, pool=10),
        follow_redirects=True,
    )

    counts = {"skipped": 0, "downloaded": 0, "failed": 0}
    available_zips: list[Path] = []

    with client:
        for zip_url in tqdm(urls, desc="ZIPs", unit="zip"):
            zip_url_path = urlparse(zip_url).path.lstrip("/")
            zip_path     = zip_cache_dir / zip_url_path

            reporter.info(f"  {zip_url}")

            if not force_download and zip_path.exists():
                size_kb = zip_path.stat().st_size // 1024
                reporter.info(f"    -> Already downloaded ({size_kb:,} KB) — skipping")
                counts["skipped"] += 1
                available_zips.append(zip_path)
                continue

            if dry_run:
                reporter.info(f"    [dry-run] Would download → {zip_path}")
                available_zips.append(zip_path)
                continue

            ok, reason = _download_zip(client, zip_url, zip_path, reporter)
            if not ok:
                reporter.info(f"    -> Failed: {reason}")
                counts["failed"] += 1
                time.sleep(delay)
                continue

            size_kb = zip_path.stat().st_size // 1024
            reporter.info(f"    Downloaded: {size_kb:,} KB → {zip_path}")
            counts["downloaded"] += 1
            available_zips.append(zip_path)
            time.sleep(delay)

    reporter.info(
        f"\nDownload — downloaded: {counts['downloaded']}  "
        f"skipped: {counts['skipped']}  "
        f"failed: {counts['failed']}"
    )

    return available_zips


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download ZIP files listed in a text file"
    )
    parser.add_argument(
        "url_file",
        help="Text file with one ZIP URL per line (# comments and blank lines ignored)",
    )
    parser.add_argument("--config",        default="config/settings.yaml")
    parser.add_argument("--zip-cache-dir", default=None,
                        help="Where to save ZIPs (default: from settings, usually cache/zip)")
    parser.add_argument("--product-slug",  default=None,
                        help="Product folder name under archives/. Enables archive phase.")
    parser.add_argument("--product-name",  default=None,
                        help="Human-readable product name for index.md and toc.yml. "
                             "Defaults to title-casing the product slug.")
    parser.add_argument("--archives-dir",  default="output/archives",
                        help="Root for archive output (default: output/archives)")
    parser.add_argument("--dry-run",       action="store_true",
                        help="Print what would be downloaded/archived without writing files")
    parser.add_argument("--force-download", action="store_true",
                        help="Re-download even if the ZIP already exists in cache")
    args = parser.parse_args()

    url_file = Path(args.url_file)
    if not url_file.exists():
        print(f"Error: URL file not found: {url_file}", file=sys.stderr)
        return 1

    settings      = load_settings(args.config)
    zip_cache_dir = Path(args.zip_cache_dir or
                         settings.get("zip", {}).get("zip_cache_dir", "cache/zip"))

    urls = read_url_list(url_file)
    if not urls:
        print(f"No URLs found in {url_file}", file=sys.stderr)
        return 1

    # Resolve product display name
    product_name = args.product_name or ""
    if args.product_slug and not product_name:
        product_name = re.sub(r'[-_]+', ' ', args.product_slug).title()

    from datetime import datetime
    logs_dir = Path(settings.get("logs_dir", "logs"))
    run_dir  = logs_dir / "download_zips" / datetime.now().strftime("%Y%m%d-%H%M%S")
    reporter = Reporter(run_dir, "download_zips", dry_run=args.dry_run)

    reporter.info(
        f"=== download_zips | {len(urls)} URL(s) | "
        f"dry_run={args.dry_run} force_download={args.force_download} ==="
    )
    reporter.info(f"ZIP cache: {zip_cache_dir.resolve()}")
    reporter.info(f"URL file : {url_file.resolve()}")
    if args.product_slug:
        reporter.info(f"Product  : {product_name} (slug={args.product_slug})")
        reporter.info(f"Archives : {args.archives_dir}")

    available_zips = process_urls(urls, settings, reporter, zip_cache_dir,
                                  dry_run=args.dry_run, force_download=args.force_download)

    if args.product_slug:
        archive_dir = Path(args.archives_dir) / args.product_slug / "archive"
        run_archive_phase(available_zips, archive_dir, product_name, reporter, args.dry_run)

    reporter.finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
