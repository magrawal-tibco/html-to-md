"""
07_restructure_ebx_addon.py — Restructure EBX addon output directories.

Transforms the existing version-first layout into an addon-first layout,
writing a *separate* copy without touching the original.

Old layout (mirrors source URL):
  output/pub/ebx-addon/<version>/doc/<addon>/<content>
  output/pub/ebx-addon/<version>/<addon>/Java_API/

New layout (addon-first, doc/ removed):
  output/pub/ebx-addon-reorg/<addon>/<version>/<content>
  output/pub/ebx-addon-reorg/<addon>/<version>/Java_API/

Usage:
  python scripts/07_restructure_ebx_addon.py [--src output/pub/ebx-addon]
                                              [--dst output/pub/ebx-addon-reorg]
                                              [--preflight-only]
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path, PurePosixPath

from tqdm import tqdm

# Matches Markdown links: [text](url)
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")


def discover_doc_addon_roots(src: Path) -> list[tuple[str, str, Path]]:
    """Return [(version, addon, addon_doc_path)] for all src/<ver>/doc/<addon>/ dirs."""
    results = []
    for version_dir in sorted(src.iterdir()):
        if not version_dir.is_dir():
            continue
        doc_dir = version_dir / "doc"
        if not doc_dir.is_dir():
            continue
        for addon_dir in sorted(doc_dir.iterdir()):
            if addon_dir.is_dir():
                results.append((version_dir.name, addon_dir.name, addon_dir))
    return results


def build_path_mapping(src: Path, dst: Path) -> dict[Path, Path]:
    """
    Return {old_abs_path: new_abs_path} for every file to be restructured.

    Two source patterns:
      src/<ver>/doc/<addon>/<rest>        → dst/<addon>/<ver>/<rest>
      src/<ver>/<addon>/Java_API/<rest>   → dst/<addon>/<ver>/Java_API/<rest>
    """
    mapping: dict[Path, Path] = {}

    for path in src.rglob("*"):
        if not path.is_file():
            continue
        parts = path.relative_to(src).parts

        if len(parts) >= 4 and parts[1] == "doc":
            # <ver>/doc/<addon>/<rest...>  (includes doc/<addon>/Java_API/)
            ver, addon = parts[0], parts[2]
            rest = Path(*parts[3:])
            new_rel = Path(addon) / ver / rest

        else:
            # Version-level aggregate files (e.g. doc/_toc.json) — skip
            continue

        mapping[path] = dst / new_rel

    return mapping


def preflight_scan(addon_roots: list[tuple[str, str, Path]], src: Path) -> list[dict]:
    """
    Scan .md files for cross-addon relative links.
    A link is cross-addon if it resolves outside the current addon's doc root
    but still within the ebx-addon src directory.
    Returns a list of finding dicts.
    """
    cross_links: list[dict] = []
    src_resolved = src.resolve()

    for _ver, _addon, addon_root in addon_roots:
        addon_root_resolved = addon_root.resolve()
        for md_file in addon_root.rglob("*.md"):
            try:
                content = md_file.read_text(encoding="utf-8")
            except Exception:
                continue

            for m in _MD_LINK_RE.finditer(content):
                url = m.group(2)
                # Skip absolute URLs, anchors, mailto, data URIs
                if url.startswith(("http", "#", "mailto:", "data:")):
                    continue
                url_clean = url.split("#")[0]
                if not url_clean:
                    continue
                resolved = (md_file.parent / url_clean).resolve()
                # Is it outside this addon's root?
                try:
                    resolved.relative_to(addon_root_resolved)
                    continue  # within addon root — fine
                except ValueError:
                    pass
                # Is it still within the overall src (i.e., another addon)?
                try:
                    resolved.relative_to(src_resolved)
                    cross_links.append({
                        "file": str(md_file.relative_to(src)),
                        "link": url,
                        "resolved": str(resolved),
                    })
                except ValueError:
                    pass  # outside ebx-addon src entirely — external link, ignore

    return cross_links


def patch_toc_json(toc_path: Path, old_root: str, new_root: str) -> bool:
    """Update the 'root' field in a _toc.json. Returns True if patched."""
    try:
        data = json.loads(toc_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    current_root = data.get("root", "")
    # Normalise separators for comparison
    if current_root.replace("\\", "/").rstrip("/") == old_root.rstrip("/"):
        data["root"] = new_root
        toc_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Restructure EBX addon output: version-first → addon-first"
    )
    parser.add_argument("--src", default="output/pub/ebx-addon",
                        help="Source directory (default: output/pub/ebx-addon)")
    parser.add_argument("--dst", default="output/pub/ebx-addon-reorg",
                        help="Destination directory (default: output/pub/ebx-addon-reorg)")
    parser.add_argument("--preflight-only", action="store_true",
                        help="Run pre-flight scan only — no files written")
    args = parser.parse_args()

    src = Path(args.src)
    dst = Path(args.dst)

    if not src.exists():
        print(f"Error: source not found: {src}", file=sys.stderr)
        return 1

    print(f"Source : {src.resolve()}")
    print(f"Dest   : {dst.resolve()}")
    print()

    # ── Phase 0: pre-flight cross-addon link scan ────────────────────────────
    print("=== Phase 0: Pre-flight scan ===")
    addon_roots = discover_doc_addon_roots(src)
    print(f"  {len(addon_roots)} addon-version combinations found")

    cross_links = preflight_scan(addon_roots, src)

    if cross_links:
        print(f"\n  *** WARNING: {len(cross_links)} cross-addon link(s) found ***")
        for cl in cross_links[:30]:
            print(f"    {cl['file']}  →  {cl['link']}")
        if len(cross_links) > 30:
            print(f"    ... and {len(cross_links) - 30} more")
        print()
        print("  Cross-addon links will remain as-is in the copy (relative paths")
        print("  will be broken). Review them before using the restructured output.")
    else:
        print("  No cross-addon links found. All relative paths are preserved.")

    if args.preflight_only:
        print("\n(--preflight-only: stopping before copy)")
        return 0

    # ── Phase 1: build path mapping ──────────────────────────────────────────
    print("\n=== Phase 1: Building path mapping ===")
    mapping = build_path_mapping(src, dst)
    print(f"  {len(mapping)} files mapped")

    # ── Phase 2: copy files ──────────────────────────────────────────────────
    print("\n=== Phase 2: Copying files ===")
    errors = 0
    for old_path, new_path in tqdm(mapping.items(), desc="Copying", unit="file"):
        try:
            new_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old_path, new_path)
        except Exception as exc:
            print(f"\n  Error: {old_path} → {exc}")
            errors += 1

    print(f"  Copied {len(mapping) - errors} files ({errors} errors)")

    # ── Phase 3: cross-addon link rewriting (only if needed) ─────────────────
    if cross_links:
        print("\n=== Phase 3: Cross-addon link rewriting ===")
        print("  Skipped — cross-addon links detected but rewriting not implemented.")
        print("  See Phase 0 output for the affected files.")

    # ── Phase 4: patch _toc.json root fields ─────────────────────────────────
    print("\n=== Phase 4: Patching _toc.json root fields ===")

    # Derive path prefixes relative to "output/" for the root field in _toc.json
    output_dir = Path("output")
    try:
        src_prefix = src.resolve().relative_to(output_dir.resolve()).as_posix()
    except ValueError:
        src_prefix = src.as_posix().lstrip("./")
    try:
        dst_prefix = dst.resolve().relative_to(output_dir.resolve()).as_posix()
    except ValueError:
        dst_prefix = dst.as_posix().lstrip("./")

    patched = 0
    for ver, addon, _addon_root in addon_roots:
        old_root = f"{src_prefix}/{ver}/doc/{addon}/"
        new_root = f"{dst_prefix}/{addon}/{ver}/"
        toc_file = dst / addon / ver / "_toc.json"
        if toc_file.exists():
            if patch_toc_json(toc_file, old_root, new_root):
                patched += 1

    print(f"  Patched {patched} / {len(addon_roots)} _toc.json files")

    # ── Phase 5: report ───────────────────────────────────────────────────────
    print("\n=== Done ===")
    total = len(mapping)
    print(f"  Files copied : {total - errors}")
    print(f"  Errors       : {errors}")
    if cross_links:
        print(f"  Cross-addon  : {len(cross_links)} links need manual review")
    print(f"  Output       : {dst.resolve()}")

    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
