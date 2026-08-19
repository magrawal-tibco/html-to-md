"""
fix_image_alt.py — Strip leading slash from image alt text in converted Markdown output.

EBX source HTML uses the image filename as alt text with a leading "/" (e.g.
alt="/filename.png"). The converter passes this through verbatim, producing:
  ![/filename.png](./resources/pictures/filename.png)

AEM Guides treats a leading-slash alt text as an absolute resource path, causing
the image to appear missing. The src path is correct; only the alt text needs fixing.

This script patches existing output in-place. The root-cause fix in 03_convert.py
prevents recurrence for future conversions.

Usage:
  python scripts/fix_image_alt.py                     # output/ebx + output/ebx-addon
  python scripts/fix_image_alt.py --dirs output/ebx   # specific subtree(s)
  python scripts/fix_image_alt.py --dry-run            # report only, no writes
"""

import argparse
import re
import sys
from pathlib import Path

# Match ![/anything](anything) — captures alt (without slash) and src separately
_ALT_SLASH_RE = re.compile(r"!\[/([^\]]*)\]\(([^)]+)\)")


def patch_file(md_file: Path, dry_run: bool) -> int:
    """Strip leading slash from image alt text in one file. Returns number of replacements."""
    try:
        content = md_file.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        print(f"  [ERROR] {md_file}: {e}", file=sys.stderr)
        return 0

    new_content, n = _ALT_SLASH_RE.subn(r"![\1](\2)", content)
    if n and not dry_run:
        md_file.write_text(new_content, encoding="utf-8")
    return n


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Strip leading slash from image alt text in EBX Markdown output"
    )
    parser.add_argument(
        "--dirs", nargs="+", metavar="DIR",
        help="Directories to scan (default: output/ebx output/ebx-addon)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would change without writing any files"
    )
    args = parser.parse_args()

    dirs = [Path(d) for d in args.dirs] if args.dirs else [Path("output/ebx"), Path("output/ebx-addon")]

    if args.dry_run:
        print("Mode: DRY RUN - no files will be written\n")

    total_files = 0
    total_replacements = 0

    for root in dirs:
        if not root.is_dir():
            print(f"Skipping (not found): {root}")
            continue
        for md_file in sorted(root.rglob("*.md")):
            n = patch_file(md_file, args.dry_run)
            if n:
                action = "would fix" if args.dry_run else "fixed"
                print(f"  [{action} {n}] {md_file}")
                total_files += 1
                total_replacements += n

    print(f"\nSummary: {total_replacements} replacements in {total_files} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
