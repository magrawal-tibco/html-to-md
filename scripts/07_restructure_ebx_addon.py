"""
07_restructure_ebx_addon.py — Restructure EBX addon output directories.

Transforms the existing version-first layout into an addon-first layout,
writing a *separate* copy without touching the original.

Old layout (mirrors source URL):
  output/pub/ebx-addon/<version>/doc/<addon>/<content>
  output/pub/ebx-addon/<version>/doc/<addon>/Java_API/

New webhelp layout (addon-first, Java_API excluded):
  <dst>/en-us/ebx-addon/<addon>/<ver-dashed>/<content>

New javadocs layout (separate tree for separate repo):
  <javadocs-dst>/en-us/ebx-addons/<addon>/javadocs/<ver-dashed>/<Java_API content>

Usage:
  python scripts/07_restructure_ebx_addon.py [--src output/pub/ebx-addon]
                                              [--dst output/ebx-addon]
                                              [--javadocs-dst output/ebx-addon-javadocs]
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
    Return {old_abs_path: new_abs_path} for webhelp files (Java_API excluded).

    Source pattern:
      src/<ver>/doc/<addon>/<rest>  → dst/en-us/ebx-addon/<addon>/<ver-dashed>/<rest>

    Java_API/ subdirectories are excluded — handled by build_javadocs_mapping().
    """
    mapping: dict[Path, Path] = {}

    for path in src.rglob("*"):
        if not path.is_file():
            continue
        parts = path.relative_to(src).parts

        if len(parts) >= 4 and parts[1] == "doc":
            ver, addon = parts[0], parts[2]
            rest = Path(*parts[3:])
            if rest.parts[0] == "Java_API":
                continue  # Java API is handled separately by build_javadocs_mapping()
            new_rel = Path("en-us") / "ebx-addon" / addon / ver.replace(".", "-") / rest
        else:
            # Version-level aggregate files (e.g. doc/_toc.json) — skip
            continue

        mapping[path] = dst / new_rel

    return mapping


def build_javadocs_mapping(src: Path, dst: Path) -> dict[Path, Path]:
    """
    Return {old_abs_path: new_abs_path} for Java_API content.

    Source pattern:
      src/<ver>/doc/<addon>/Java_API/<rest>
        → dst/en-us/ebx-addons/<addon>/javadocs/<ver-dashed>/<rest>
    """
    mapping: dict[Path, Path] = {}

    for path in src.rglob("*"):
        if not path.is_file():
            continue
        parts = path.relative_to(src).parts

        # <ver>/doc/<addon>/Java_API/<rest...>
        if len(parts) >= 5 and parts[1] == "doc" and parts[3] == "Java_API":
            ver, addon = parts[0], parts[2]
            rest = Path(*parts[4:])
            new_rel = (
                Path("en-us") / "ebx-addons" / addon
                / "javadocs" / ver.replace(".", "-") / rest
            )
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
    parser.add_argument("--dst", default="output/ebx-addon",
                        help="Webhelp destination (default: output/ebx-addon)")
    parser.add_argument("--javadocs-dst", default=None,
                        help="Javadocs destination (default: same as --dst)")
    parser.add_argument("--preflight-only", action="store_true",
                        help="Run pre-flight scan only — no files written")
    args = parser.parse_args()

    src = Path(args.src)
    dst = Path(args.dst)
    javadocs_dst = Path(args.javadocs_dst) if args.javadocs_dst else dst

    if not src.exists():
        print(f"Error: source not found: {src}", file=sys.stderr)
        return 1

    print(f"Source       : {src.resolve()}")
    print(f"Webhelp dest : {dst.resolve()}")
    print(f"Javadocs dest: {javadocs_dst.resolve()}")
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

    # ── Phase 1: build webhelp path mapping ──────────────────────────────────
    print("\n=== Phase 1: Building webhelp path mapping ===")
    mapping = build_path_mapping(src, dst)
    print(f"  {len(mapping)} webhelp files mapped (Java_API excluded)")

    # ── Phase 2: build javadocs path mapping ─────────────────────────────────
    print("\n=== Phase 2: Building javadocs path mapping ===")
    javadocs_mapping = build_javadocs_mapping(src, javadocs_dst)
    print(f"  {len(javadocs_mapping)} javadoc files mapped")
    # Show unique addon/version pairs
    addon_ver_pairs = {
        (p.relative_to(javadocs_dst).parts[2], p.relative_to(javadocs_dst).parts[4])
        for p in javadocs_mapping.values()
        if len(p.relative_to(javadocs_dst).parts) >= 5
    }
    for addon, ver in sorted(addon_ver_pairs):
        print(f"    {addon}  {ver}")

    # ── Phase 3: copy webhelp files ───────────────────────────────────────────
    print("\n=== Phase 3: Copying webhelp files ===")
    errors = 0
    for old_path, new_path in tqdm(mapping.items(), desc="Webhelp", unit="file"):
        try:
            new_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old_path, new_path)
        except Exception as exc:
            print(f"\n  Error: {old_path} → {exc}")
            errors += 1
    print(f"  Copied {len(mapping) - errors} files ({errors} errors)")

    # ── Phase 4: copy javadocs files ──────────────────────────────────────────
    print("\n=== Phase 4: Copying javadocs files ===")
    jd_errors = 0
    for old_path, new_path in tqdm(javadocs_mapping.items(), desc="Javadocs", unit="file"):
        try:
            new_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old_path, new_path)
        except Exception as exc:
            print(f"\n  Error: {old_path} → {exc}")
            jd_errors += 1
    print(f"  Copied {len(javadocs_mapping) - jd_errors} files ({jd_errors} errors)")

    # ── Phase 5: remove Java_API remnants from webhelp tree ───────────────────
    print("\n=== Phase 5: Removing Java_API from webhelp tree ===")
    removed_dirs = 0
    for java_dir in sorted((dst / "en-us" / "ebx-addon").rglob("Java_API")):
        if java_dir.is_dir():
            shutil.rmtree(java_dir)
            removed_dirs += 1
    print(f"  Removed {removed_dirs} Java_API director{'ies' if removed_dirs != 1 else 'y'}")

    # ── Phase 6: cross-addon link rewriting (only if needed) ──────────────────
    if cross_links:
        print("\n=== Phase 6: Cross-addon link rewriting ===")
        print("  Skipped — cross-addon links detected but rewriting not implemented.")
        print("  See Phase 0 output for the affected files.")

    # ── Phase 7: patch _toc.json root fields ──────────────────────────────────
    print("\n=== Phase 7: Patching _toc.json root fields ===")

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
        folder_ver = ver.replace(".", "-")
        old_root = f"{src_prefix}/{ver}/doc/{addon}/"
        new_root = f"{dst_prefix}/en-us/ebx-addon/{addon}/{folder_ver}/"
        toc_file = dst / "en-us" / "ebx-addon" / addon / folder_ver / "_toc.json"
        if toc_file.exists():
            if patch_toc_json(toc_file, old_root, new_root):
                patched += 1

    print(f"  Patched {patched} / {len(addon_roots)} _toc.json files")

    # ── Phase 8: rewrite EBX-main javadoc URLs → addon-specific URLs ──────────
    # Step 5 converts relative Java_API/ links to the EBX main javadoc URL.
    # After restructuring, replace every occurrence in each addon/version tree
    # with the correct addon-specific URL and strip MadCap popup-link duplicates.
    print("\n=== Phase 8: Patching Java API URLs ===")

    _EBX_MAIN_JAVADOC_PREFIX = "https://stg-docs.onebx.com/us/en/ebx/resources/javadocs/"
    _ADDON_JAVADOC_TMPL      = "https://stg-docs.onebx.com/us/en/ebx-addons/resources/{addon}/javadocs/{ver}/"
    _POPUP_LINK_RE           = re.compile(r"\[open JavaAPI in popup\]\([^)]+\)")

    patched_java = 0
    for ver, addon, _addon_root in addon_roots:
        folder_ver = ver.replace(".", "-")
        addon_dir  = dst / "en-us" / "ebx-addon" / addon / folder_ver
        old_prefix = f"{_EBX_MAIN_JAVADOC_PREFIX}{folder_ver}/"
        new_url    = _ADDON_JAVADOC_TMPL.format(addon=addon, ver=folder_ver)
        old_url_re = re.compile(re.escape(old_prefix) + r'[^)\s"]*')

        for md_file in addon_dir.rglob("*.md"):
            text = md_file.read_text(encoding="utf-8")
            if old_prefix not in text:
                continue
            text = old_url_re.sub(new_url, text)
            text = _POPUP_LINK_RE.sub("", text)
            md_file.write_text(text, encoding="utf-8")
            patched_java += 1

    print(f"  Patched {patched_java} files")

    # ── Summary ───────────────────────────────────────────────────────────────
    total_errors = errors + jd_errors
    print("\n=== Done ===")
    print(f"  Webhelp files copied : {len(mapping) - errors}")
    print(f"  Javadocs files copied: {len(javadocs_mapping) - jd_errors}")
    print(f"  Errors               : {total_errors}")
    if cross_links:
        print(f"  Cross-addon          : {len(cross_links)} links need manual review")
    print(f"  Webhelp output       : {dst.resolve()}")
    print(f"  Javadocs output      : {javadocs_dst.resolve()}")

    return 0 if total_errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
