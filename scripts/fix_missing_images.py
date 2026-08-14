"""
fix_missing_images.py — Find and heal broken image references in restructured output.

Scans .md files under output/<product>/en-us/<product>/online-help/ for image
references that resolve to missing files. For each missing image, looks up the
corresponding file in cache/pub/<product>/<version>/doc/html/ and copies it to
the expected location.

Reports:
  - Images found in cache and copied (healed)
  - Images not found in cache (unresolvable — needs manual attention)

Usage:
  python scripts/fix_missing_images.py --products as bwce
  python scripts/fix_missing_images.py                        # all non-EBX products
  python scripts/fix_missing_images.py --dry-run --products as
  python scripts/fix_missing_images.py --file output/as/en-us/as/online-help/4-10-0/Admin/page.md
"""

import argparse
import re
import shutil
import sys
from pathlib import Path

_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)#\s]+)")

SKIP_PRODUCTS = {"ebx", "ebx-addon", "ebx-addon-reorg", "ebx-reorg"}


def _version_dashed_to_dotted(version_dashed: str) -> str:
    """Convert '4-10-0' back to '4.10.0' for cache path lookup."""
    return version_dashed.replace("-", ".")


def scan_file(md_file: Path) -> list[tuple[str, Path]]:
    """Return list of (raw_src, resolved_abs_path) for every missing local image in md_file."""
    missing = []
    try:
        content = md_file.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return missing
    for m in _IMG_RE.finditer(content):
        src = m.group(2).strip()
        if not src or src.startswith(("http://", "https://", "data:")):
            continue
        resolved = (md_file.parent / src).resolve()
        if not resolved.exists():
            missing.append((src, resolved))
    return missing


def cache_lookup(
    missing_abs: Path,
    online_help_root: Path,
    cache_version_html_root: Path,
) -> Path | None:
    """Try to find a missing image in the cache by mirroring the relative path.

    online_help_root:       output/<product>/en-us/<product>/online-help/<ver>/
    cache_version_html_root: cache/pub/<product>/<ver>/doc/html/
    missing_abs:             absolute path where the image should be (may not exist)
    """
    try:
        rel = missing_abs.relative_to(online_help_root)
    except ValueError:
        return None
    candidate = cache_version_html_root / rel
    return candidate if candidate.exists() else None


def process_product(
    product: str,
    dst: Path,
    cache: Path,
    lang: str,
    dry_run: bool,
) -> tuple[int, int, int]:
    """Scan all online-help versions for a product and heal missing images.

    Returns (n_healed, n_missing, n_files_scanned).
    """
    online_help_base = dst / product / lang / product / "online-help"
    if not online_help_base.is_dir():
        return 0, 0, 0

    n_healed = 0
    n_missing = 0
    n_files = 0

    for version_dir in sorted(online_help_base.iterdir()):
        if not version_dir.is_dir():
            continue
        version_dashed = version_dir.name
        version_dotted = _version_dashed_to_dotted(version_dashed)
        cache_html_root = cache / product / version_dotted / "doc" / "html"

        for md_file in sorted(version_dir.rglob("*.md")):
            n_files += 1
            for src, missing_abs in scan_file(md_file):
                cache_src = cache_lookup(missing_abs, version_dir, cache_html_root)
                if cache_src:
                    if not dry_run:
                        missing_abs.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(cache_src, missing_abs)
                    action = "would copy" if dry_run else "copied"
                    print(f"  [{action}] {md_file.relative_to(dst)}: {src}")
                    print(f"           <- {cache_src.relative_to(cache.parent)}")
                    n_healed += 1
                else:
                    print(f"  [NOT FOUND] {md_file.relative_to(dst)}: {src}")
                    print(f"              looked in {cache_html_root.relative_to(cache.parent) if cache_html_root.exists() else cache_html_root}")
                    n_missing += 1

    return n_healed, n_missing, n_files


def process_file(
    md_file: Path,
    dst: Path,
    cache: Path,
    lang: str,
    dry_run: bool,
) -> tuple[int, int]:
    """Heal missing images for a single .md file. Returns (n_healed, n_missing)."""
    # Infer product and version from path: dst/<product>/<lang>/<product>/online-help/<ver>/...
    try:
        rel = md_file.relative_to(dst)
        parts = rel.parts
        # parts: product, lang, product, "online-help", version_dashed, ...
        product = parts[0]
        version_dashed = parts[4]
    except (ValueError, IndexError):
        print(f"Cannot infer product/version from path: {md_file}", file=sys.stderr)
        print("Expected: output/<product>/<lang>/<product>/online-help/<version>/...", file=sys.stderr)
        return 0, 0

    version_dotted = _version_dashed_to_dotted(version_dashed)
    online_help_root = dst / product / lang / product / "online-help" / version_dashed
    cache_html_root = cache / product / version_dotted / "doc" / "html"

    n_healed = 0
    n_missing = 0

    for src, missing_abs in scan_file(md_file):
        cache_src = cache_lookup(missing_abs, online_help_root, cache_html_root)
        if cache_src:
            if not dry_run:
                missing_abs.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(cache_src, missing_abs)
            action = "would copy" if dry_run else "copied"
            print(f"  [{action}] {src}")
            print(f"           <- {cache_src}")
            n_healed += 1
        else:
            print(f"  [NOT FOUND] {src}")
            print(f"              looked in {cache_html_root}")
            n_missing += 1

    return n_healed, n_missing


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Find and heal broken image references in restructured TIBCO output"
    )
    parser.add_argument("--dst", default="output",
                        help="Restructured output root (default: output)")
    parser.add_argument("--cache", default="cache/pub",
                        help="Cache root (default: cache/pub)")
    parser.add_argument("--lang", default="en-us",
                        help="Language code (default: en-us)")
    parser.add_argument("--products", nargs="+", metavar="PRODUCT",
                        help="Restrict to specific product(s); default = all non-EBX products")
    parser.add_argument("--file", metavar="PATH",
                        help="Check a single .md file instead of scanning by product")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be copied without writing any files")
    args = parser.parse_args()

    dst = Path(args.dst)
    cache = Path(args.cache)

    if args.dry_run:
        print("Mode: DRY RUN - no files will be written\n")

    # Single-file mode
    if args.file:
        md_file = Path(args.file)
        if not md_file.exists():
            print(f"Error: file not found: {md_file}", file=sys.stderr)
            return 1
        print(f"Scanning: {md_file}\n")
        n_healed, n_missing = process_file(md_file, dst, cache, args.lang, args.dry_run)
        print(f"\nResult: {n_healed} healed, {n_missing} unresolvable")
        return 0 if n_missing == 0 else 1

    # Product scan mode
    if not dst.is_dir():
        print(f"Error: output directory not found: {dst}", file=sys.stderr)
        return 1

    products = []
    for p in sorted(dst.iterdir()):
        if not p.is_dir() or p.name in SKIP_PRODUCTS:
            continue
        if not (p / args.lang / p.name / "online-help").is_dir():
            continue
        if args.products and p.name not in args.products:
            continue
        products.append(p.name)

    if not products:
        print("No products found (check --dst and --products).", file=sys.stderr)
        return 1

    total_healed = 0
    total_missing = 0
    total_files = 0

    for product in products:
        print(f"=== {product} ===")
        n_healed, n_missing, n_files = process_product(
            product, dst, cache, args.lang, args.dry_run
        )
        if n_healed == 0 and n_missing == 0:
            print(f"  No missing images in {n_files} files scanned.")
        else:
            print(f"  {n_files} files scanned — {n_healed} healed, {n_missing} unresolvable")
        total_healed += n_healed
        total_missing += n_missing
        total_files += n_files
        print()

    print("=== Summary ===")
    print(f"  Products : {len(products)}")
    print(f"  Files    : {total_files}")
    action = "would heal" if args.dry_run else "healed"
    print(f"  {action.capitalize()} : {total_healed}")
    print(f"  Unresolvable : {total_missing}")

    return 0 if total_missing == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
