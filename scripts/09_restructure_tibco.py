"""
09_restructure_tibco.py — Restructure TIBCO product output directories.

Transforms the existing version-first layout (with doc/html/ wrapper) into a
language-first, per-product staging layout suitable for upload to individual
GitHub repos.

Old layout (mirrors source URL):
  output/pub/<product>/<version>/doc/html/<content>
  output/pub/<product>/<version>/doc/relnotes/<content>

New layout (language-first, doc/html removed):
  output/<product>/en-us/<product>/webhelp/<version>/<content>
  output/<product>/en-us/<product>/relnotes/<version>/<content>

Phase 5 copies PDF/doc assets from cache/pub/<product>/<version>/doc/pdf/ and
doc/doc/ into the new layout alongside the webhelp output, generating index.md
and toc.yml for each version folder.

Original source is left untouched. EBX-family products are excluded (already
reorganized by scripts 07 and 08).

Usage:
  python scripts/09_restructure_tibco.py [--src output/pub]
                                          [--dst output]
                                          [--cache cache/pub]
                                          [--products bwpluginas bwplugincassandra ...]
                                          [--lang en-us]
                                          [--preflight-only]
                                          [--dry-run]
                                          [--skip-assets]
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent))
try:
    from lib.asset_copy import (
        SLUG_MAPPINGS_FILE,
        copy_asset_folder,
        discover_asset_versions,
        load_slug_mappings,
        save_slug_mappings,
    )
    _ASSET_COPY_AVAILABLE = True
except ImportError:
    _ASSET_COPY_AVAILABLE = False

# Products to skip — already reorganized by scripts 07 / 08
SKIP_PRODUCTS = {"ebx", "ebx-addon", "ebx-addon-reorg", "ebx-reorg"}

# Matches Markdown links: [text](url)
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")


def _lookup_product_name(product_slug: str, version: str) -> str | None:
    """Search manifest_*.json files for a product_name matching product_slug + version."""
    manifests_dir = Path("manifests")
    if not manifests_dir.is_dir():
        return None
    for mf in manifests_dir.glob("manifest_*.json"):
        try:
            entries = json.loads(mf.read_text(encoding="utf-8"))
            for e in entries:
                if (e.get("product_version") == version
                        and product_slug in (e.get("output_path") or e.get("url") or "")):
                    name = e.get("product_name", "").strip()
                    if name:
                        return name
        except Exception:
            continue
    return None


def discover_products(src: Path, filter_products: list[str]) -> list[str]:
    """Return sorted product names found in src/, excluding SKIP_PRODUCTS."""
    products = []
    for p in sorted(src.iterdir()):
        if not p.is_dir() or p.name in SKIP_PRODUCTS:
            continue
        if filter_products and p.name not in filter_products:
            continue
        products.append(p.name)
    return products


def discover_versions(src: Path, product: str) -> list[tuple[str, Path, Path | None]]:
    """
    Return [(version, html_dir_or_None, relnotes_dir_or_None)] for each version.
    """
    results = []
    product_dir = src / product
    if not product_dir.is_dir():
        return results
    for version_dir in sorted(product_dir.iterdir()):
        if not version_dir.is_dir():
            continue
        html_dir = version_dir / "doc" / "html"
        relnotes_dir = version_dir / "doc" / "relnotes"
        results.append((
            version_dir.name,
            html_dir if html_dir.is_dir() else None,
            relnotes_dir if relnotes_dir.is_dir() else None,
        ))
    return results


def build_path_mapping(
    src: Path,
    dst: Path,
    products: list[str],
    lang: str,
) -> dict[Path, Path]:
    """
    Build {old_path: new_path} for all files across all products.

    webhelp:  src/<P>/<V>/doc/html/<rest>     → dst/<P>/<lang>/<P>/webhelp/<V>/<rest>
    relnotes: src/<P>/<V>/doc/relnotes/<rest>  → dst/<P>/<lang>/<P>/relnotes/<V>/<rest>
    """
    mapping: dict[Path, Path] = {}
    for product in products:
        for version, html_dir, relnotes_dir in discover_versions(src, product):
            if html_dir:
                for f in html_dir.rglob("*"):
                    if f.is_file():
                        rel = f.relative_to(html_dir)
                        mapping[f] = dst / product / lang / product / "webhelp" / version.replace(".", "-") / rel

            if relnotes_dir:
                for f in relnotes_dir.rglob("*"):
                    if f.is_file():
                        rel = f.relative_to(relnotes_dir)
                        mapping[f] = dst / product / lang / product / "relnotes" / version.replace(".", "-") / rel

    return mapping


def preflight_scan(src: Path, products: list[str]) -> list[dict]:
    """
    Scan .md files for cross-product or cross-version relative links.
    A link is flagged if it resolves outside the current doc/html/ version root.
    """
    cross_links: list[dict] = []
    src_resolved = src.resolve()

    for product in products:
        for version, html_dir, _ in discover_versions(src, product):
            if not html_dir:
                continue
            html_root = html_dir.resolve()
            for md_file in html_dir.rglob("*.md"):
                try:
                    content = md_file.read_text(encoding="utf-8")
                except Exception:
                    continue
                for m in _MD_LINK_RE.finditer(content):
                    url = m.group(2)
                    if url.startswith(("http", "#", "mailto:", "data:")):
                        continue
                    url_clean = url.split("#")[0]
                    if not url_clean:
                        continue
                    resolved = (md_file.parent / url_clean).resolve()
                    try:
                        resolved.relative_to(html_root)
                        continue  # within this version's html root — fine
                    except ValueError:
                        pass
                    try:
                        resolved.relative_to(src_resolved)
                        cross_links.append({
                            "product": product,
                            "version": version,
                            "file": str(md_file.relative_to(src)),
                            "link": url,
                        })
                    except ValueError:
                        pass  # outside src entirely — not a concern

    return cross_links


def patch_toc_json(toc_path: Path, old_root: str, new_root: str) -> bool:
    """Update the 'root' field in a _toc.json. Returns True if patched."""
    try:
        data = json.loads(toc_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    current_root = data.get("root", "").replace("\\", "/").rstrip("/")
    if current_root == old_root.rstrip("/"):
        data["root"] = new_root
        toc_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Restructure TIBCO product output: version-first → language-first per-product staging"
    )
    parser.add_argument("--src", default="output/pub",
                        help="Source base directory (default: output/pub)")
    parser.add_argument("--dst", default="output",
                        help="Destination base directory (default: output)")
    parser.add_argument("--cache", default="cache/pub",
                        help="Cache root for PDF/doc assets (default: cache/pub)")
    parser.add_argument("--products", nargs="+", metavar="PRODUCT",
                        help="Restrict to specific product(s); default = all non-EBX products")
    parser.add_argument("--lang", default="en-us",
                        help="Language code to use in new path (default: en-us)")
    parser.add_argument("--preflight-only", action="store_true",
                        help="Run pre-flight scan only — no files written")
    parser.add_argument("--dry-run", action="store_true",
                        help="Build mapping and report counts, but do not copy files")
    parser.add_argument("--skip-assets", action="store_true",
                        help="Skip Phase 5 PDF/doc asset copy")
    args = parser.parse_args()

    src = Path(args.src)
    dst = Path(args.dst)

    if not src.exists():
        print(f"Error: source not found: {src}", file=sys.stderr)
        return 1

    print(f"Source : {src.resolve()}")
    print(f"Dest   : {dst.resolve()}")
    print(f"Lang   : {args.lang}")
    if args.dry_run:
        print("Mode   : DRY RUN (no files written)")
    print()

    # ── Phase 0: discover & preflight ────────────────────────────────────────
    print("=== Phase 0: Discovery & pre-flight scan ===")
    products = discover_products(src, args.products or [])
    if not products:
        print("  No products found (check --src and --products).", file=sys.stderr)
        return 1

    print(f"  Products to restructure: {len(products)}")
    for product in products:
        versions = discover_versions(src, product)
        webhelp_files = sum(
            sum(1 for _ in v[1].rglob("*") if v[1] and _.is_file()) if v[1] else 0
            for v in versions
        )
        relnotes_files = sum(
            sum(1 for _ in v[2].rglob("*") if v[2] and _.is_file()) if v[2] else 0
            for v in versions
        )
        ver_names = [v[0] for v in versions]
        print(f"    {product:<30} versions={ver_names}  webhelp={webhelp_files}  relnotes={relnotes_files}")

    cross_links = preflight_scan(src, products)
    if cross_links:
        print(f"\n  *** WARNING: {len(cross_links)} cross-product/version link(s) found ***")
        for cl in cross_links[:20]:
            print(f"    {cl['file']}  ->  {cl['link']}")
        if len(cross_links) > 20:
            print(f"    ... and {len(cross_links) - 20} more")
        print("  These relative links may be broken in the restructured copy.")
    else:
        print("  No cross-product relative links found. All relative paths are preserved.")

    if args.preflight_only:
        print("\n(--preflight-only: stopping before copy)")
        return 0

    # ── Phase 1: build path mapping ───────────────────────────────────────────
    print("\n=== Phase 1: Building path mapping ===")
    mapping = build_path_mapping(src, dst, products, args.lang)
    print(f"  {len(mapping)} files mapped")

    if args.dry_run:
        print("\n(--dry-run: skipping copy and patch)")
        print(f"\n=== Done (dry run) ===")
        print(f"  Would copy : {len(mapping)} files")
        return 0

    # ── Phase 2: copy files ───────────────────────────────────────────────────
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

    # ── Phase 3: cross-product link rewriting (informational only) ────────────
    if cross_links:
        print("\n=== Phase 3: Cross-product link rewriting ===")
        print("  Skipped -- cross-product links detected but auto-rewriting not implemented.")
        print("  Review Phase 0 output for affected files.")

    # ── Phase 4: patch _toc.json root fields ─────────────────────────────────
    print("\n=== Phase 4: Patching _toc.json root fields ===")

    output_dir = Path("output")
    try:
        src_prefix = src.resolve().relative_to(output_dir.resolve()).as_posix()
    except ValueError:
        src_prefix = src.as_posix().lstrip("./")

    patched = 0
    toc_candidates = 0
    for product in products:
        for version, html_dir, _ in discover_versions(src, product):
            if not html_dir:
                continue
            folder_ver = version.replace(".", "-")
            toc_file = dst / product / args.lang / product / "webhelp" / folder_ver / "_toc.json"
            if toc_file.exists():
                toc_candidates += 1
                old_root = f"{src_prefix}/{product}/{version}/doc/html/"
                new_root = f"{product}/{args.lang}/{product}/webhelp/{folder_ver}/"
                if patch_toc_json(toc_file, old_root, new_root):
                    patched += 1

    print(f"  Patched {patched} / {toc_candidates} _toc.json files")

    # ── Phase 5: copy PDF/doc assets ─────────────────────────────────────────
    total_asset_files = 0
    if args.skip_assets:
        print("\n=== Phase 5: PDF/doc asset copy skipped (--skip-assets) ===")
    elif not _ASSET_COPY_AVAILABLE:
        print("\n=== Phase 5: PDF/doc asset copy skipped (lib.asset_copy not available) ===")
    else:
        print("\n=== Phase 5: Copying PDF/doc assets ===")
        cache_root = Path(args.cache)
        if not cache_root.exists():
            print(f"  Cache root not found: {cache_root} — skipping")
        else:
            slug_mappings = load_slug_mappings(SLUG_MAPPINGS_FILE)
            for product in products:
                cache_src = cache_root / product
                if not cache_src.is_dir():
                    continue
                asset_versions = discover_asset_versions(cache_src)
                if not asset_versions:
                    continue
                dest_base = dst / product / args.lang / product
                product_total = 0
                for version, version_dashed in asset_versions:
                    product_name = _lookup_product_name(product, version) or product
                    cache_doc_dir = cache_src / version / "doc"
                    n_pdf = copy_asset_folder(
                        cache_doc_dir, "pdf", dest_base, version_dashed,
                        product_name, version, slug_mappings,
                    )
                    n_doc = copy_asset_folder(
                        cache_doc_dir, "doc", dest_base, version_dashed,
                        product_name, version, slug_mappings,
                    )
                    product_total += n_pdf + n_doc
                if product_total:
                    print(f"  {product}: {product_total} asset files copied")
                total_asset_files += product_total
            save_slug_mappings(slug_mappings, SLUG_MAPPINGS_FILE)
            print(f"  Total: {total_asset_files} asset files copied")

    # ── Phase 6: report ───────────────────────────────────────────────────────
    print("\n=== Done ===")
    print(f"  Products  : {len(products)}")
    print(f"  Files     : {len(mapping) - errors} copied, {errors} errors")
    print(f"  _toc.json : {patched} patched")
    if total_asset_files:
        print(f"  Assets    : {total_asset_files} PDF/doc files copied")
    if cross_links:
        print(f"  Cross-link: {len(cross_links)} links need manual review")
    print(f"  Output    : {dst.resolve()}")

    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
