"""
fetch_eos.py — Enrich tibco_versions.csv with a `retired` boolean column.

Data source: TIBCO End of Support page
  https://support.tibco.com/support-home/aboutsupport/end_of_support_information

The page is a JS-rendered SPA. This script tries Playwright first to automate
the "Export to Excel" download. If Playwright is unavailable or fails, it falls
back to prompting the user to download the file manually.

Usage:
  python scripts/catalog/fetch_eos.py [--eos-file PATH] [--csv PATH] [--out PATH]
                                       [--no-playwright] [--dry-run] [--show-columns]
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

_CLEAN_RE = re.compile("[®™�\N{REPLACEMENT CHARACTER}]")
_WS_RE = re.compile(r"\s+")
_TIBCO_PREFIX = re.compile(r"^tibco\s+", re.IGNORECASE)


def normalize(text: str) -> str:
    """Lowercase, strip trademark symbols, collapse whitespace, drop 'TIBCO ' prefix."""
    text = _CLEAN_RE.sub("", text)
    text = _WS_RE.sub(" ", text).strip().lower()
    text = _TIBCO_PREFIX.sub("", text)
    return text


# ---------------------------------------------------------------------------
# Playwright download (best-effort)
# ---------------------------------------------------------------------------

EOS_URL = "https://support.tibco.com/support-home/aboutsupport/end_of_support_information"


def download_eos_excel(dest_dir: Path) -> Path:
    from playwright.sync_api import sync_playwright  # type: ignore[import]

    print(f"[playwright] Navigating to {EOS_URL} …")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()
        page.goto(EOS_URL, timeout=60_000)

        print("[playwright] Waiting for table to render …")
        page.wait_for_selector("table", timeout=60_000)

        # Try exact text first, then a looser regex match
        export_btn = page.get_by_text("Export to Excel", exact=False)
        if export_btn.count() == 0:
            export_btn = page.locator("button, a").filter(has_text=re.compile(r"export", re.I))

        print("[playwright] Clicking Export button …")
        with page.expect_download(timeout=60_000) as dl_info:
            export_btn.first.click()

        download = dl_info.value
        dest = dest_dir / (download.suggested_filename or "eos_export.xlsx")
        download.save_as(dest)
        browser.close()

    print(f"[playwright] Saved to: {dest}")
    return dest


# ---------------------------------------------------------------------------
# Manual fallback
# ---------------------------------------------------------------------------

def prompt_manual_download() -> Path:
    print()
    print("Please download the EOS Excel file manually:")
    print(f"  1. Open: {EOS_URL}")
    print("  2. Click 'Export to Excel'")
    print("  3. Enter the path to the downloaded file below.")
    print()
    path_str = input("Path to downloaded .xlsx file: ").strip().strip('"').strip("'")
    return Path(path_str)


def get_eos_file(dest_dir: Path, no_playwright: bool) -> Path:
    if not no_playwright:
        try:
            return download_eos_excel(dest_dir)
        except ImportError:
            print("[warn] playwright not installed. Run: pip install playwright && playwright install chromium")
            print("[info] Falling back to manual download.")
        except Exception as e:
            print(f"[warn] Playwright download failed: {e}")
            print("[info] Falling back to manual download.")
    return prompt_manual_download()


# ---------------------------------------------------------------------------
# Parse the EOS Excel
# ---------------------------------------------------------------------------

def _load_rows_from_file(path: Path) -> tuple[list[str], list[list[str]]]:
    """Return (header, data_rows) from a .csv or .xlsx file."""
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            all_rows = [row for row in reader if any(cell.strip() for cell in row)]
        if not all_rows:
            sys.exit(f"[error] EOS file is empty: {path}")
        header = [c.strip() for c in all_rows[0]]
        data = [[c.strip() for c in row] for row in all_rows[1:]]
        return header, data
    elif suffix in (".xlsx", ".xls"):
        try:
            import openpyxl  # type: ignore[import]
        except ImportError:
            sys.exit("[error] openpyxl is required for .xlsx files. Run: pip install openpyxl")
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        raw = list(ws.iter_rows(values_only=True))
        wb.close()
        if not raw:
            sys.exit(f"[error] EOS file is empty: {path}")
        header = [str(c).strip() if c is not None else "" for c in raw[0]]
        data = [
            [str(c).strip() if c is not None else "" for c in row]
            for row in raw[1:]
        ]
        return header, data
    else:
        sys.exit(f"[error] Unsupported file type: {suffix}. Expected .csv or .xlsx")


def load_eos_file(path: Path, show_columns: bool = False) -> dict[tuple[str, str], str]:
    """
    Returns a lookup dict: (normalized_product_name, normalized_version) → release_status.
    Accepts both .csv and .xlsx files.
    """
    header, data = _load_rows_from_file(path)

    if show_columns:
        print(f"EOS file columns ({path.name}):")
        for i, col in enumerate(header):
            print(f"  [{i}] {col!r}")
        if data:
            print(f"\nSample row: {data[0]}")
        return {}

    # Locate the columns we need (case-insensitive, partial match)
    def find_col(candidates: list[str]) -> int:
        for candidate in candidates:
            for i, h in enumerate(header):
                if candidate.lower() in h.lower():
                    return i
        return -1

    col_product = find_col(["product name", "product"])
    col_version = find_col(["version"])
    col_status  = find_col(["release status", "status"])

    missing = []
    if col_product < 0: missing.append("Product Name")
    if col_version < 0: missing.append("Version")
    if col_status  < 0: missing.append("Release Status")
    if missing:
        print(f"[error] Could not locate columns: {missing}")
        print("Run with --show-columns to inspect the file's actual column names.")
        sys.exit(1)

    print(f"[info] Column mapping: product={header[col_product]!r}  "
          f"version={header[col_version]!r}  status={header[col_status]!r}")

    lookup: dict[tuple[str, str], str] = {}
    for row in data:
        # Pad short rows
        product = row[col_product] if col_product < len(row) else ""
        version = row[col_version] if col_version < len(row) else ""
        status  = row[col_status]  if col_status  < len(row) else ""
        if not product and not version:
            continue
        key = (normalize(product), normalize(version))
        lookup[key] = status

    print(f"[info] Loaded {len(lookup)} EOS entries from {path.name}")
    return lookup


# ---------------------------------------------------------------------------
# Enrich CSV
# ---------------------------------------------------------------------------

def enrich_csv(
    csv_path: Path,
    out_path: Path,
    eos_lookup: dict[tuple[str, str], str],
    dry_run: bool,
) -> None:
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        original_fields = list(reader.fieldnames or [])
        rows = list(reader)

    new_cols = ["retired"]
    all_fields = original_fields + [c for c in new_cols if c not in original_fields]

    matched = 0
    unmatched: list[str] = []

    for row in rows:
        key = (normalize(row.get("product_name", "")), normalize(row.get("version", "")))
        status = eos_lookup.get(key, "")
        if status:
            matched += 1
            row["retired"] = "True" if status.lower() == "retired" else "False"
        else:
            unmatched.append(f"{row.get('product_name','')} {row.get('version','')}")
            row["retired"] = "False"

    total = len(rows)
    print(f"\n[result] {total} CSV rows | {matched} matched | {total - matched} unmatched")
    if unmatched:
        sample = unmatched[:10]
        print(f"[warn] Unmatched rows (first {len(sample)}):")
        for s in sample:
            print(f"  - {s}")
        if len(unmatched) > 10:
            print(f"  … and {len(unmatched) - 10} more")

    if dry_run:
        print("[dry-run] No files written.")
        return

    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=all_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"[done] Written to: {out_path}")


# ---------------------------------------------------------------------------
# Default CSV path helper
# ---------------------------------------------------------------------------

def find_default_csv() -> Path:
    catalog_dir = Path("manifests/catalog")
    if catalog_dir.is_dir():
        candidates = sorted(catalog_dir.glob("tibco_versions_*.csv"), reverse=True)
        if candidates:
            return candidates[0]
    fallback = Path("tibco_versions.csv")
    if fallback.exists():
        return fallback
    sys.exit("[error] Could not find tibco_versions.csv. Use --csv to specify the path.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add a 'retired' column to tibco_versions.csv from the TIBCO EOS page."
    )
    parser.add_argument("--eos-file", metavar="PATH",
                        help="Path to a pre-downloaded EOS .xlsx file (skips Playwright).")
    parser.add_argument("--no-playwright", action="store_true",
                        help="Skip Playwright; prompt for manual file path instead.")
    parser.add_argument("--csv", metavar="PATH",
                        help="Input tibco_versions.csv (default: latest in manifests/catalog/).")
    parser.add_argument("--out", metavar="PATH",
                        help="Output CSV path (default: overwrite --csv in-place).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and match but do not write output.")
    parser.add_argument("--show-columns", action="store_true",
                        help="Print EOS Excel column names and exit.")
    args = parser.parse_args()

    csv_path = Path(args.csv) if args.csv else find_default_csv()
    if not csv_path.exists():
        sys.exit(f"[error] CSV not found: {csv_path}")

    out_path = Path(args.out) if args.out else csv_path

    # Resolve the EOS Excel file
    if args.eos_file:
        eos_path = Path(args.eos_file)
        if not eos_path.exists():
            sys.exit(f"[error] EOS file not found: {eos_path}")
    else:
        with tempfile.TemporaryDirectory() as tmp:
            eos_path = get_eos_file(Path(tmp), no_playwright=args.no_playwright)
            # Copy out of temp dir so we can use it after context exits
            import shutil
            permanent = Path("manifests/catalog") / eos_path.name
            permanent.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(eos_path, permanent)
            eos_path = permanent
            print(f"[info] EOS file saved to: {eos_path}")

    eos_lookup = load_eos_file(eos_path, show_columns=args.show_columns)
    if args.show_columns:
        return

    enrich_csv(csv_path, out_path, eos_lookup, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
