"""
list_converted.py — View and manage the converted-versions registry.

The registry (manifests/converted_versions.json) tracks every product version
that has been successfully converted. This script lets you:

  - List all converted versions (default)
  - Filter by product name keyword or phase
  - Remove a version entry so it will be re-processed on the next pipeline run
  - Mark a version as obsolete (adds a flag; it is still skipped by the pipeline
    unless --force-rerun is used, but shows up highlighted in the list)
  - Update tibco_versions.csv with enriched conversion data (--update-csv)

Usage:
  python scripts/list_converted.py                        # list all
  python scripts/list_converted.py --filter ebx           # keyword filter
  python scripts/list_converted.py --phase activespaces_ee
  python scripts/list_converted.py --remove <version_url>
  python scripts/list_converted.py --remove-all --phase gridserver_711
  python scripts/list_converted.py --mark-obsolete <version_url>
  python scripts/list_converted.py --mark-obsolete-all --filter "GridServer"
  python scripts/list_converted.py --update-csv           # enrich tibco_versions.csv
  python scripts/list_converted.py --update-csv --csv tibco_versions.csv --output out.csv
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path

REGISTRY_PATH = Path("manifests/converted_versions.json")
CONVERSION_LOG_PATH = Path("manifests/conversion_log.csv")
TIBCO_VERSIONS_CSV = Path("tibco_versions.csv")
OUTPUT_DIR = Path("output")

# ANSI colours (disabled automatically on non-TTY)
_USE_COLOR = sys.stdout.isatty()
_RED    = "\033[91m" if _USE_COLOR else ""
_YELLOW = "\033[93m" if _USE_COLOR else ""
_GREEN  = "\033[92m" if _USE_COLOR else ""
_CYAN   = "\033[96m" if _USE_COLOR else ""
_BOLD   = "\033[1m"  if _USE_COLOR else ""
_RESET  = "\033[0m"  if _USE_COLOR else ""


# ---------------------------------------------------------------------------
# Registry I/O
# ---------------------------------------------------------------------------

def load_registry(path: Path | None = None) -> dict:
    p = path or REGISTRY_PATH
    if not p.exists():
        print(f"Registry not found: {p}", file=sys.stderr)
        sys.exit(1)
    return json.loads(p.read_text(encoding="utf-8"))


def save_registry(registry: dict, path: Path | None = None) -> None:
    p = path or REGISTRY_PATH
    p.write_text(
        json.dumps(dict(sorted(registry.items())), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def matches(url: str, entry: dict, keyword: str, phase: str) -> bool:
    if keyword:
        haystack = (entry.get("product_name", "") + " " + url).lower()
        if keyword.lower() not in haystack:
            return False
    if phase:
        if entry.get("phase", "").lower() != phase.lower():
            return False
    return True


def apply_filter(registry: dict, keyword: str, phase: str) -> dict:
    return {url: e for url, e in registry.items() if matches(url, e, keyword, phase)}


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def _group_key(entry: dict) -> tuple:
    """Sort/group key: (phase, product_name, version)."""
    return (
        entry.get("phase", ""),
        entry.get("product_name", ""),
        entry.get("product_version", ""),
    )


def print_list(registry: dict) -> None:
    if not registry:
        print("No matching entries.")
        return

    # Group by phase
    by_phase: dict[str, list[tuple[str, dict]]] = {}
    for url, entry in registry.items():
        ph = entry.get("phase", "(unknown)")
        by_phase.setdefault(ph, []).append((url, entry))

    total = 0
    for phase in sorted(by_phase):
        entries = sorted(by_phase[phase], key=lambda x: _group_key(x[1]))
        print(f"\n{_BOLD}Phase: {phase}{_RESET}  ({len(entries)} versions)")
        print(f"  {'Product':<55} {'Ver':<10} {'Pages':>6}  {'Converted':<12}  {'Flags'}")
        print(f"  {'-'*55} {'-'*10} {'-'*6}  {'-'*12}  {'-'*8}")
        for url, e in entries:
            name    = e.get("product_name", "?")
            ver     = e.get("product_version", "?")
            pages   = e.get("page_count", "?")
            date    = (e.get("converted_at") or "")[:10]
            obsolete = e.get("obsolete", False)

            if obsolete:
                color = _YELLOW
                flag  = "OBSOLETE"
            else:
                color = _GREEN
                flag  = ""

            name_display = (name[:52] + "...") if len(name) > 55 else name
            print(f"  {color}{name_display:<55} {ver:<10} {str(pages):>6}  {date:<12}  {flag}{_RESET}")
        total += len(entries)

    print(f"\n{_BOLD}Total: {total} versions{_RESET}")


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

def remove_entries(registry: dict, to_remove: dict, dry_run: bool, reg_path: Path) -> int:
    if not to_remove:
        print("No matching entries to remove.")
        return 0
    print(f"{'[DRY RUN] ' if dry_run else ''}Removing {len(to_remove)} entry/entries:")
    for url, e in sorted(to_remove.items(), key=lambda x: _group_key(x[1])):
        print(f"  {_RED}- {e.get('product_name')} {e.get('product_version')}{_RESET}")
        print(f"    {url}")
        if not dry_run:
            del registry[url]
    if not dry_run:
        save_registry(registry, reg_path)
        print("\nRegistry updated. These versions will be re-processed on the next pipeline run.")
    return len(to_remove)


def mark_obsolete_entries(registry: dict, to_mark: dict, dry_run: bool, reg_path: Path) -> int:
    if not to_mark:
        print("No matching entries to mark.")
        return 0
    print(f"{'[DRY RUN] ' if dry_run else ''}Marking {len(to_mark)} entry/entries as obsolete:")
    for url, e in sorted(to_mark.items(), key=lambda x: _group_key(x[1])):
        print(f"  {_YELLOW}~ {e.get('product_name')} {e.get('product_version')}{_RESET}")
        if not dry_run:
            registry[url]["obsolete"] = True
    if not dry_run:
        save_registry(registry, reg_path)
        print("\nMarked as obsolete. The pipeline still skips these unless you remove them or use --force-rerun.")
    return len(to_mark)


# ---------------------------------------------------------------------------
# CSV enrichment (--update-csv)
# ---------------------------------------------------------------------------

# Business-unit prefixes to strip when deriving product slug for repo names.
# Symbols (®™) are stripped before prefix matching.
_BU_PREFIXES = [
    ("TIBCO DataSynapse ", "datasynapse"),
    ("DataSynapse ", "datasynapse"),
    ("TIBCO IBI ", "ibi"),
    ("IBI ", "ibi"),
    ("TIBCO EBX ", "tibco"),
    ("EBX ", "tibco"),
    ("TIBCO ", "tibco"),
]

_CLEAN_RE = re.compile(r"[®™]")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _derive_repo_names(product_name: str) -> tuple[str, str]:
    """Return (repo_name, resources_repo_name) for a product.

    Pattern: en-us-<bu>-<product-slug>
    Example: "TIBCO ActiveSpaces® Enterprise Edition" → "en-us-tibco-activespaces-enterprise-edition"
    """
    # Strip trademark symbols before prefix matching
    clean = _CLEAN_RE.sub("", product_name)

    bu = "tibco"
    remainder = clean
    matched_prefix = ""
    for prefix, bu_tag in _BU_PREFIXES:
        if clean.startswith(prefix):
            bu = bu_tag
            matched_prefix = prefix
            remainder = clean[len(prefix):]
            break

    # Strip trailing version suffix (e.g. " 4.10.0" or standalone "4.10.0")
    remainder = re.sub(r"(\s+|^)\d+(\.\d+)+\s*$", "", remainder).strip()

    # If the remainder is empty after version strip (e.g. "TIBCO EBX® 6.2.3"),
    # the product name is the last word of the matched prefix.
    if not remainder and matched_prefix:
        remainder = matched_prefix.strip().split()[-1]

    # Lowercase, collapse non-alphanumeric to hyphens
    slug = _NON_ALNUM_RE.sub("-", remainder.lower()).strip("-")

    repo = f"en-us-{bu}-{slug}"
    resources_repo = f"en-us-{bu}-{slug}-resources"
    return repo, resources_repo


def _load_conversion_log(log_path: Path) -> dict[str, dict]:
    """Return dict keyed by 'Version Sitemap URL' → row dict from conversion_log.csv."""
    if not log_path.exists():
        return {}
    result: dict[str, dict] = {}
    with log_path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            url = row.get("Version Sitemap URL", "").strip()
            if url:
                # Keep the last row for a URL (most recent run wins)
                result[url] = {k.strip(): v.strip() for k, v in row.items()}
    return result


def _count_api_ref_files(output_dir: Path, product_slug: str) -> int:
    """Count files under output/<product-slug>-resources/en-us/*/api-references/."""
    resources_dir = output_dir / f"{product_slug}-resources"
    if not resources_dir.exists():
        return 0
    total = 0
    for api_dir in resources_dir.rglob("api-references"):
        if api_dir.is_dir():
            total += sum(1 for _ in api_dir.rglob("*") if _.is_file())
    return total


def _product_slug_from_name(product_name: str) -> str:
    """Derive a filesystem product slug from the product name for output path lookup."""
    clean = _CLEAN_RE.sub("", product_name)
    remainder = clean
    for prefix, _ in _BU_PREFIXES:
        if clean.startswith(prefix):
            remainder = clean[len(prefix):]
            break
    remainder = re.sub(r"\s+\d+(\.\d+)+\s*$", "", remainder)
    return _NON_ALNUM_RE.sub("-", remainder.lower()).strip("-")


def update_csv(
    registry: dict,
    log_path: Path,
    csv_path: Path,
    output_path: Path,
    output_dir: Path,
    dry_run: bool,
) -> None:
    """Enrich tibco_versions.csv with conversion data and write result."""
    if not csv_path.exists():
        print(f"CSV not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    log = _load_conversion_log(log_path)

    # Read existing CSV — preserve original rows and column order
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        original_fields = reader.fieldnames or []
        rows = [row for row in reader]

    # New columns to add (skip if already present)
    new_cols = [
        "topics_converted",
        "csh_ids_count",
        "api_ref_files",
        "pdf_files",
        "help_type",
        "conversion_date",
        "repo_name",
        "resources_repo_name",
    ]
    all_fields = list(original_fields) + [c for c in new_cols if c not in original_fields]

    # Build index: doc_url → registry entry + log row
    updated = 0
    for row in rows:
        doc_url = row.get("doc_url", "").strip()
        if not doc_url:
            continue

        reg_entry = registry.get(doc_url)
        log_row = log.get(doc_url)

        if reg_entry is None and log_row is None:
            # Not converted — leave new columns blank (preserve any existing values)
            for col in new_cols:
                if col not in row:
                    row[col] = ""
            continue

        # topics_converted: prefer log (most recent run), fall back to registry page_count
        if log_row:
            row["topics_converted"] = log_row.get("Topics Converted", "")
            row["csh_ids_count"]    = log_row.get("CSH ID Count", "")
            row["pdf_files"]        = log_row.get("PDFs Found", "")
            row["help_type"]        = log_row.get("Status", "")
        else:
            row["topics_converted"] = str(reg_entry.get("page_count", "")) if reg_entry else ""
            row["csh_ids_count"]    = ""
            row["pdf_files"]        = ""
            row["help_type"]        = ""

        # conversion_date from registry
        if reg_entry:
            raw_date = reg_entry.get("converted_at", "")
            row["conversion_date"] = raw_date[:10] if raw_date else ""
        else:
            row["conversion_date"] = ""

        # api_ref_files: count from output directory
        product_name = (reg_entry or {}).get("product_name", row.get("product_name", "")).strip()
        product_slug = _product_slug_from_name(product_name) if product_name else ""
        row["api_ref_files"] = str(_count_api_ref_files(output_dir, product_slug)) if product_slug else ""

        # repo names derived from product_name
        if product_name:
            repo, res_repo = _derive_repo_names(product_name)
            row["repo_name"]           = repo
            row["resources_repo_name"] = res_repo
        else:
            row["repo_name"]           = ""
            row["resources_repo_name"] = ""

        updated += 1

    if dry_run:
        print(f"[DRY RUN] Would update {updated} rows in {output_path}")
        print(f"  New columns: {', '.join(new_cols)}")
        return

    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=all_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"Updated {updated} rows -> {output_path}  ({len(all_fields)} columns)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="View and manage the converted-versions registry",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/list_converted.py
  python scripts/list_converted.py --filter ebx
  python scripts/list_converted.py --phase activespaces_ee
  python scripts/list_converted.py --remove https://docs.tibco.com/products/tibco-ebx-6-2-3
  python scripts/list_converted.py --remove-all --phase gridserver_711
  python scripts/list_converted.py --mark-obsolete https://docs.tibco.com/products/tibco-ebx-6-2-3
  python scripts/list_converted.py --mark-obsolete-all --filter "GridServer"
  python scripts/list_converted.py --remove-all --filter "GridServer" --dry-run
  python scripts/list_converted.py --update-csv
  python scripts/list_converted.py --update-csv --csv tibco_versions.csv --output enriched.csv
""")

    parser.add_argument("--filter", metavar="KEYWORD",
        help="Case-insensitive keyword to filter by product name or URL")
    parser.add_argument("--phase", metavar="PHASE",
        help="Filter by phase name (exact match, case-insensitive)")

    action = parser.add_mutually_exclusive_group()
    action.add_argument("--remove", metavar="URL",
        help="Remove a single entry by its version_sitemap URL")
    action.add_argument("--remove-all", action="store_true",
        help="Remove all entries matching --filter / --phase")
    action.add_argument("--mark-obsolete", metavar="URL",
        help="Mark a single entry as obsolete by its version_sitemap URL")
    action.add_argument("--mark-obsolete-all", action="store_true",
        help="Mark all entries matching --filter / --phase as obsolete")
    action.add_argument("--update-csv", action="store_true",
        help="Enrich tibco_versions.csv with conversion data (topics, CSH IDs, repo names, etc.)")

    parser.add_argument("--dry-run", action="store_true",
        help="Show what would change without writing to disk")
    parser.add_argument("--registry", default=str(REGISTRY_PATH), metavar="PATH",
        help=f"Path to registry JSON (default: {REGISTRY_PATH})")
    parser.add_argument("--log", default=str(CONVERSION_LOG_PATH), metavar="PATH",
        help=f"Path to conversion_log.csv (default: {CONVERSION_LOG_PATH})")
    parser.add_argument("--csv", default=str(TIBCO_VERSIONS_CSV), metavar="PATH",
        help=f"Input tibco_versions.csv (default: {TIBCO_VERSIONS_CSV})")
    parser.add_argument("--output", default=None, metavar="PATH",
        help="Output CSV path (default: overwrites --csv in-place)")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR), metavar="PATH",
        help=f"Output directory to scan for API reference files (default: {OUTPUT_DIR})")

    args = parser.parse_args()

    reg_path = Path(args.registry)
    registry = load_registry(reg_path)

    # --- Single-URL actions ---
    if args.remove:
        url = args.remove
        if url not in registry:
            print(f"Not found in registry: {url}", file=sys.stderr)
            return 1
        return 0 if remove_entries(registry, {url: registry[url]}, args.dry_run, reg_path) else 1

    if args.mark_obsolete:
        url = args.mark_obsolete
        if url not in registry:
            print(f"Not found in registry: {url}", file=sys.stderr)
            return 1
        return 0 if mark_obsolete_entries(registry, {url: registry[url]}, args.dry_run, reg_path) else 1

    # --- Bulk actions (require --filter or --phase to avoid accidents) ---
    if args.remove_all or args.mark_obsolete_all:
        if not args.filter and not args.phase:
            print("Error: --remove-all and --mark-obsolete-all require --filter or --phase to avoid accidental bulk changes.",
                  file=sys.stderr)
            return 1
        subset = apply_filter(registry, args.filter or "", args.phase or "")
        if args.remove_all:
            remove_entries(registry, subset, args.dry_run, reg_path)
        else:
            mark_obsolete_entries(registry, subset, args.dry_run, reg_path)
        return 0

    # --- CSV enrichment ---
    if args.update_csv:
        csv_path = Path(args.csv)
        output_path = Path(args.output) if args.output else csv_path
        update_csv(
            registry=registry,
            log_path=Path(args.log),
            csv_path=csv_path,
            output_path=output_path,
            output_dir=Path(args.output_dir),
            dry_run=args.dry_run,
        )
        return 0

    # --- Default: list ---
    subset = apply_filter(registry, args.filter or "", args.phase or "")
    print_list(subset)
    return 0


if __name__ == "__main__":
    sys.exit(main())
