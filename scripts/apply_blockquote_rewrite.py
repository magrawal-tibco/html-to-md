"""
apply_blockquote_rewrite.py — One-shot script to replace <blockquote> HTML tags
with <div class="note-inline"> in a specific output directory.

Used to apply the blockquote rewrite to already-restructured output (e.g. after
08_restructure_ebx.py has run) without re-running the full pipeline. The same
transform is baked into step 5 (05_postprocess.py) for future pipeline runs on
pre-restructured output.

Usage:
  python scripts/apply_blockquote_rewrite.py --dir output/ebx-addon/en-us/ebx-addon/dint/6-2-3
  python scripts/apply_blockquote_rewrite.py --dir output/ebx-addon/en-us/ebx-addon
  python scripts/apply_blockquote_rewrite.py --dir output/ebx-addon --dry-run
"""

import argparse
import sys
from pathlib import Path


def rewrite_blockquotes(body: str) -> tuple[str, int]:
    count = body.count("<blockquote>")
    if not count:
        return body, 0
    body = body.replace("<blockquote>", '<div class="note-inline">')
    body = body.replace("</blockquote>", "</div>")
    return body, count


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replace <blockquote> with <div class='note-inline'> in .md files"
    )
    parser.add_argument(
        "--dir",
        required=True,
        help="Directory to scan recursively for .md files",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report changes without writing files",
    )
    args = parser.parse_args()

    root = Path(args.dir)
    if not root.exists():
        print(f"ERROR: directory not found: {root}", file=sys.stderr)
        return 1

    md_files = sorted(root.rglob("*.md"))
    if not md_files:
        print(f"No .md files found under {root}")
        return 0

    total_files_changed = 0
    total_replacements = 0

    for md_path in md_files:
        try:
            content = md_path.read_text(encoding="utf-8")
        except Exception as exc:
            print(f"  SKIP {md_path}: {exc}", file=sys.stderr)
            continue

        updated, count = rewrite_blockquotes(content)
        if count:
            total_files_changed += 1
            total_replacements += count
            label = "[dry-run] " if args.dry_run else ""
            print(f"  {label}{md_path.relative_to(root)}  ({count} replacement{'s' if count != 1 else ''})")
            if not args.dry_run:
                md_path.write_text(updated, encoding="utf-8")

    print(
        f"\nDone: {total_files_changed} file{'s' if total_files_changed != 1 else ''} changed, "
        f"{total_replacements} <blockquote> tag{'s' if total_replacements != 1 else ''} replaced"
        + (" (dry run — no files written)" if args.dry_run else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
