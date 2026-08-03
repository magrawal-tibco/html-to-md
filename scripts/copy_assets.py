"""
09_copy_assets.py — Copy PDF/doc assets from cache to output for any product.

For each version under --cache-src that contains doc/pdf/ or doc/doc/ subfolders:
  - Copies files to <dst>/<lang>/<product-slug>/<subfolder>/<version-dashed>/
  - Generates index.md (listing page with hyperlinks and resolved display names)
  - Generates toc.yml pointing to index.md
  - Updates config/pdf_slug_mappings.yaml with newly discovered slugs

Usage:
  python scripts/09_copy_assets.py \\
    --cache-src cache/pub/dsp_gridserver \\
    --dst       output/dsp_gridserver \\
    --product-slug dsp_gridserver \\
    --product-name "TIBCO DataSynapse GridServer® Manager" \\
    [--lang en-us]
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib.asset_copy import (
    SLUG_MAPPINGS_FILE,
    copy_asset_folder,
    discover_asset_versions,
    load_slug_mappings,
    save_slug_mappings,
)


def _product_name_from_manifest(cache_src: Path, version: str) -> str | None:
    """Try to look up product_name for a given version from any manifest_*.json file."""
    manifests_dir = Path("manifests")
    if not manifests_dir.is_dir():
        return None
    slug = cache_src.name  # e.g. dsp_gridserver
    for mf in manifests_dir.glob("manifest_*.json"):
        try:
            entries = json.loads(mf.read_text(encoding="utf-8"))
            for e in entries:
                if e.get("product_version") == version and slug in (e.get("version_sitemap") or ""):
                    name = e.get("product_name", "").strip()
                    if name:
                        return name
        except Exception:
            continue
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Copy PDF/doc assets from cache to output and generate listing pages"
    )
    parser.add_argument("--cache-src", required=True,
                        help="Cache dir with version subfolders (e.g. cache/pub/dsp_gridserver)")
    parser.add_argument("--dst", required=True,
                        help="Output root dir (e.g. output/dsp_gridserver)")
    parser.add_argument("--product-slug", required=True,
                        help="Product slug used in output path (e.g. dsp_gridserver)")
    parser.add_argument("--product-name",
                        help="Product display name override (auto-detected from manifest if omitted)")
    parser.add_argument("--lang", default="en-us",
                        help="Language code for output path (default: en-us)")
    args = parser.parse_args()

    cache_src = Path(args.cache_src)
    dst = Path(args.dst)
    dest_base = dst / args.lang / args.product_slug

    if not cache_src.is_dir():
        print(f"Error: cache source not found: {cache_src}", file=sys.stderr)
        return 1

    slug_mappings = load_slug_mappings(SLUG_MAPPINGS_FILE)
    asset_versions = discover_asset_versions(cache_src)

    if not asset_versions:
        print(f"No versions with pdf/ or doc/ assets found under {cache_src}")
        return 0

    print(f"Cache  : {cache_src.resolve()}")
    print(f"Output : {dest_base.resolve()}")
    print(f"Versions: {[v for v, _ in asset_versions]}")
    print()

    total_files = 0
    copy_errors = 0
    for version, version_dashed in asset_versions:
        product_name = args.product_name or _product_name_from_manifest(cache_src, version)
        if not product_name:
            print(f"  WARNING: could not resolve product name for {version} — "
                  f"pass --product-name explicitly")
            product_name = args.product_slug  # last-resort fallback

        cache_doc_dir = cache_src / version / "doc"

        try:
            n_pdf = copy_asset_folder(
                cache_doc_dir, "pdf", dest_base, version_dashed,
                product_name, version, slug_mappings
            )
            n_doc = copy_asset_folder(
                cache_doc_dir, "doc", dest_base, version_dashed,
                product_name, version, slug_mappings
            )
        except Exception as exc:
            print(f"  ERROR: {version}: {exc}", file=sys.stderr)
            copy_errors += 1
            continue

        total = n_pdf + n_doc
        total_files += total
        print(f"  {version}: {n_pdf} PDF files, {n_doc} doc files copied")

    try:
        save_slug_mappings(slug_mappings, SLUG_MAPPINGS_FILE)
    except Exception as exc:
        print(f"ERROR: could not save slug mappings: {exc}", file=sys.stderr)
        copy_errors += 1

    needs_review = [k for k, v in slug_mappings.items() if not v]
    print(f"\nTotal asset files copied : {total_files}")
    print(f"Slug mappings            : {len(slug_mappings)} total")
    if needs_review:
        print(f"Slugs needing review     : {', '.join(needs_review)}")
        print(f"  -> Edit config/pdf_slug_mappings.yaml and re-run to apply corrections")

    return 0 if copy_errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
