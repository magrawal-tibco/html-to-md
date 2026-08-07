"""
apply_fragment_rewrite.py — One-shot script to rewrite heading fragment anchors
in .md files from HTML source IDs to Markdown heading slugs.

AEM generates heading anchors from the heading text at publish time (e.g. "Version 6.2.3"
→ "version-6-2-3"). Links in converted Markdown still reference the original HTML source
IDs (e.g. #id1, #azure_topPage). This script rewrites those fragments so navigation works.

It works by scanning .md files for headings that carry <a name="old_id"></a> anchors
(appended by the step-3 converter), builds a per-file mapping of old_id → slug, then
rewrites all fragment references in three passes:
  1. In-page fragments:    [text](#old_id)
  2. Cross-file Markdown:  [text](other.md#old_id)
  3. HTML href fragments:  href="other.md#old_id"

Safe to re-run (idempotent: slugs → slugs with no further change).

Usage:
  python scripts/apply_fragment_rewrite.py --dir output/ebx-addon/en-us/ebx-addon/dama/6-2-3
  python scripts/apply_fragment_rewrite.py --dir output/ebx-addon/en-us/ebx-addon
  python scripts/apply_fragment_rewrite.py --dir output/ebx/en-us/ebx/webhelp [--dry-run]
"""

import argparse
import re
import sys
from pathlib import Path


# ── slug helpers ──────────────────────────────────────────────────────────────

def heading_slug(text: str) -> str:
    """Compute the GFM heading slug from heading text.

    GitHub-Flavored Markdown: strip HTML, lowercase, remove non-word/space/hyphen
    chars (dots, parens, etc.), replace whitespace/underscores with a hyphen.
    Examples: "Version 6.2.3" → "version-623", "New features" → "new-features"
    """
    text = re.sub(r"<[^>]+>", "", text)
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text.strip("-")


_HEADING_ANCHOR_RE = re.compile(
    r'^(#{1,6})\s+(.+?)\s+<a name="([^"]+)"></a>', re.MULTILINE
)
_INPAGE_FRAG_RE = re.compile(r'\[([^\]]*)\]\(#([^)]+)\)')
_CROSSFILE_FRAG_RE = re.compile(r'\[([^\]]*)\]\(([^)#][^)]*?)#([^)]+)\)')
_HTML_HREF_FRAG_RE = re.compile(r'href="([^"#]*?)#([^"]+)"')


# ── map building ──────────────────────────────────────────────────────────────

def build_anchor_slug_map(md_files: list, root: Path) -> dict:
    """Return {abs_path_str: {anchor_id: slug}} for all scanned files."""
    result = {}
    for md_path in md_files:
        try:
            content = md_path.read_text(encoding="utf-8")
        except Exception:
            continue
        file_map = {}
        slug_seen = {}  # base_slug → number of prior occurrences in this file
        for m in _HEADING_ANCHOR_RE.finditer(content):
            anchor_id = m.group(3)
            base = heading_slug(m.group(2))
            n = slug_seen.get(base, 0)
            slug_seen[base] = n + 1
            file_map[anchor_id] = base if n == 0 else f"{base}-{n}"
        if file_map:
            result[str(md_path.resolve())] = file_map
    return result


# ── rewrite ───────────────────────────────────────────────────────────────────

def rewrite_fragments(body: str, md_path: Path, anchor_map: dict) -> tuple:
    """Rewrite fragment anchors in body. Returns (updated_body, replacement_count)."""
    count = 0
    local_map = anchor_map.get(str(md_path.resolve()), {})
    current_dir = md_path.parent

    def _resolve(rel_path: str) -> dict:
        try:
            return anchor_map.get(str((current_dir / rel_path).resolve()), {})
        except Exception:
            return {}

    # 1. In-page
    def _inpage(m: re.Match) -> str:
        nonlocal count
        text, anchor_id = m.group(1), m.group(2)
        slug = local_map.get(anchor_id)
        if slug and slug != anchor_id:
            count += 1
            return f"[{text}](#{slug})"
        return m.group(0)

    body = _INPAGE_FRAG_RE.sub(_inpage, body)

    # 2. Cross-file Markdown
    def _crossfile(m: re.Match) -> str:
        nonlocal count
        text, path_part, anchor_id = m.group(1), m.group(2), m.group(3)
        slug = _resolve(path_part).get(anchor_id)
        if slug and slug != anchor_id:
            count += 1
            return f"[{text}]({path_part}#{slug})"
        return m.group(0)

    body = _CROSSFILE_FRAG_RE.sub(_crossfile, body)

    # 3. HTML href fragments
    def _href_frag(m: re.Match) -> str:
        nonlocal count
        path_part, anchor_id = m.group(1), m.group(2)
        if not path_part:
            slug = local_map.get(anchor_id)
        else:
            slug = _resolve(path_part).get(anchor_id)
        if slug and slug != anchor_id:
            count += 1
            return f'href="{path_part}#{slug}"'
        return m.group(0)

    body = _HTML_HREF_FRAG_RE.sub(_href_frag, body)

    return body, count


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rewrite heading fragment anchors to Markdown slugs in .md files"
    )
    parser.add_argument("--dir", required=True, help="Directory to scan recursively")
    parser.add_argument("--dry-run", action="store_true", help="Report without writing")
    args = parser.parse_args()

    root = Path(args.dir)
    if not root.exists():
        print(f"ERROR: directory not found: {root}", file=sys.stderr)
        return 1

    md_files = sorted(root.rglob("*.md"))
    if not md_files:
        print(f"No .md files found under {root}")
        return 0

    print(f"Scanning {len(md_files)} .md files to build anchor map...")
    anchor_map = build_anchor_slug_map(md_files, root)
    total_anchors = sum(len(v) for v in anchor_map.values())
    print(f"  {len(anchor_map)} files with anchors, {total_anchors} anchor-to-slug mappings\n")

    total_files_changed = 0
    total_replacements = 0

    for md_path in md_files:
        try:
            content = md_path.read_text(encoding="utf-8")
        except Exception as exc:
            print(f"  SKIP {md_path}: {exc}", file=sys.stderr)
            continue

        updated, count = rewrite_fragments(content, md_path, anchor_map)
        if count:
            total_files_changed += 1
            total_replacements += count
            label = "[dry-run] " if args.dry_run else ""
            rel = md_path.relative_to(root)
            print(f"  {label}{rel}  ({count} replacement{'s' if count != 1 else ''})")
            if not args.dry_run:
                md_path.write_text(updated, encoding="utf-8")

    print(
        f"\nDone: {total_files_changed} file{'s' if total_files_changed != 1 else ''} changed, "
        f"{total_replacements} fragment{'s' if total_replacements != 1 else ''} rewritten"
        + (" (dry run - no files written)" if args.dry_run else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
