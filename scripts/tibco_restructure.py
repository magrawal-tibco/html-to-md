"""
tibco_restructure.py — Restructure TIBCO product output directories.

Transforms the existing version-first layout (with doc/html/ wrapper) into a
language-first, per-product staging layout suitable for upload to individual
GitHub repos.

Old layout (mirrors source URL):
  output/pub/<product>/<version>/doc/html/<content>

New layout (language-first, doc/html removed):
  output/<product>/en-us/<product>/online-help/<version>/<content>

Phase 5 copies PDF/doc assets from cache into three named folders:
  user-guides/        — user-guide PDFs
  release-information/— relnotes PDF + readme TXT
  reference-documents/— vpat, license, and all other doc/ files

Phase 6 copies API reference folders (c/, java/, golang/, tibdg/) from cache
into <product>-resources/en-us/<product>/api-references/<subdir>/<version>/
and rewrites links in .md files to point to the new location.

Phase 7 downloads archived version ZIPs (is_archived=True in tibco_versions.csv,
not yet converted) into <product>-resources/en-us/<product>/archives/<version>/.

Original source is left untouched. EBX-family products are excluded (already
reorganized by scripts 07 and 08).

Usage:
  python scripts/tibco_restructure.py [--src output/pub]
                                       [--dst output]
                                       [--cache cache/pub]
                                       [--products bwpluginas bwplugincassandra ...]
                                       [--lang en-us]
                                       [--preflight-only]
                                       [--dry-run]
                                       [--skip-assets]
"""

import argparse
import csv
import io
import json
import re
import shutil
import sys
import urllib.request
from pathlib import Path

from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent))
try:
    from lib.asset_copy import (
        SLUG_MAPPINGS_FILE,
        _USER_GUIDE_EXCLUDE_SLUGS,
        copy_asset_folder,
        copy_release_info_folder,
        copy_reference_docs_folder,
        discover_asset_versions,
        load_slug_mappings,
        save_slug_mappings,
        write_index_md,
        write_toc_yml,
    )
    _ASSET_COPY_AVAILABLE = True
except ImportError:
    _ASSET_COPY_AVAILABLE = False

# Products to skip — already reorganized by scripts 07 / 08
SKIP_PRODUCTS = {"ebx", "ebx-addon", "ebx-addon-reorg", "ebx-reorg"}

# Subdirectory names under doc/ that are API references (copied verbatim)
API_REF_SUBDIRS = {"c", "java", "golang", "tibdg"}

# Matches Markdown links: [text](url)
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")

# Matches relative links into API ref subdirs.
# Covers both:
#   api/c/index.html          (relative, same dir, with api/ prefix)
#   ../../c/index.html        (relative traversal, no api/ prefix)
_API_REF_LINK_RE = re.compile(
    r"\[([^\]]*)\]\((?:(?:\.\.\/)*api\/|(?:\.\.\/)+)("
    + "|".join(sorted(API_REF_SUBDIRS))
    + r")/([^)]+)\)"
)


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
                    version_suffix = " " + version
                    if name.endswith(version_suffix):
                        name = name[: -len(version_suffix)].rstrip()
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


def discover_versions(src: Path, product: str) -> list[tuple[str, Path | None]]:
    """Return [(version, html_dir_or_None)] for each version under src/<product>/."""
    results = []
    product_dir = src / product
    if not product_dir.is_dir():
        return results
    for version_dir in sorted(product_dir.iterdir()):
        if not version_dir.is_dir():
            continue
        html_dir = version_dir / "doc" / "html"
        results.append((
            version_dir.name,
            html_dir if html_dir.is_dir() else None,
        ))
    return results


def build_path_mapping(
    src: Path,
    dst: Path,
    products: list[str],
    lang: str,
) -> dict[Path, Path]:
    """Build {old_path: new_path} for all webhelp files across all products.

    src/<P>/<V>/doc/html/<rest> → dst/<P>/<lang>/<P>/online-help/<V-dashed>/<rest>
    """
    mapping: dict[Path, Path] = {}
    for product in products:
        for version, html_dir in discover_versions(src, product):
            if html_dir:
                for f in html_dir.rglob("*"):
                    if f.is_file():
                        rel = f.relative_to(html_dir)
                        mapping[f] = (dst / product / lang / product
                                      / "online-help" / version.replace(".", "-") / rel)
    return mapping


def preflight_scan(src: Path, products: list[str]) -> list[dict]:
    """Scan .md files for cross-product or cross-version relative links."""
    cross_links: list[dict] = []
    src_resolved = src.resolve()

    for product in products:
        for version, html_dir in discover_versions(src, product):
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
                        continue
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
                        pass

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


def rewrite_api_ref_links(
    md_file: Path,
    product: str,
    version_dashed: str,
    dst: Path,
    lang: str,
) -> int:
    """Rewrite relative API ref links in a .md file to point to <product>-resources location.

    Handles two link patterns (matched by _API_REF_LINK_RE):
      [text](api/c/index.html)       →  [text](../../../../../../<product>-resources/...)
      [text](../../c/index.html)     →  [text](../../../../../../<product>-resources/...)

    Returns number of replacements made.
    """
    try:
        content = md_file.read_text(encoding="utf-8")
    except Exception:
        return 0

    # Depth from dst/<product>/<lang>/<product>/online-help/<version-dashed>/ back to dst/ = 5
    prefix = "../../../../../"
    resources_product = f"{product}-resources"

    def replace_link(m: re.Match) -> str:
        text = m.group(1)
        subdir = m.group(2)
        rest = m.group(3)
        new_url = (f"{prefix}{resources_product}/{lang}/{product}"
                   f"/api-references/{subdir}/{version_dashed}/{rest}")
        return f"[{text}]({new_url})"

    new_content, count = _API_REF_LINK_RE.subn(replace_link, content)
    if count:
        md_file.write_text(new_content, encoding="utf-8")
    return count


_PUB_SLUG_RE = re.compile(r"/pub/([^/]+)/")


def _extract_pub_slug_from_manifests(product: str) -> str | None:
    """Extract the pub slug for a product from any manifest that covers it.

    Looks for entries whose zip_url contains /pub/<slug>/ where the URL path
    also contains the product name. Returns the first slug found, or None if
    no manifest has a zip_url for this product.
    """
    manifests_dir = Path("manifests")
    if not manifests_dir.is_dir():
        return None
    for mf in sorted(manifests_dir.glob("manifest_*.json")):
        try:
            entries = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            continue
        for e in entries:
            zip_url = (e.get("zip_url") or "").strip()
            if not zip_url:
                continue
            # Confirm this entry belongs to the product (its output path or url contains it)
            context = (e.get("output_path") or e.get("url") or "")
            if f"/{product}/" not in context and not context.startswith(f"{product}/"):
                continue
            m = _PUB_SLUG_RE.search(zip_url)
            if m:
                return m.group(1)
    return None


def _load_archived_versions(pub_slug: str, converted_urls: set[str]) -> list[dict]:
    """Read tibco_versions.csv and return archived versions not yet converted.

    pub_slug: the /pub/<slug>/ segment from zip_urls for this product — derived
    from manifests (for converted products) or the cache dir name (fallback).

    Anchored match (/pub/<slug>/ or /pub/<slug>_) avoids false substring matches.

    Returns list of {version, version_dashed, zip_url, zip_filename, product_name}.
    """
    csv_path = Path("tibco_versions.csv")
    if not csv_path.is_file():
        return []

    slug_pattern = f"/pub/{pub_slug}/"
    slug_pattern_alt = f"/pub/{pub_slug}_"

    results = []
    with csv_path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            doc_url = (row.get("doc_url") or "").strip()
            is_archived = (row.get("is_archived") or "").strip().lower() in ("true", "1", "yes")
            zip_url = (row.get("zip_url") or "").strip()

            if not is_archived or not zip_url:
                continue
            if doc_url in converted_urls:
                continue
            if slug_pattern not in zip_url and slug_pattern_alt not in zip_url:
                continue

            # Strip BOM from first column if present
            product_name = (
                row.get("product_name")
                or row.get("﻿product_name")
                or pub_slug
            ).strip()
            version = (row.get("version") or "").strip()
            zip_filename = zip_url.rstrip("/").split("/")[-1]
            results.append({
                "version": version,
                "version_dashed": version.replace(".", "-"),
                "zip_url": zip_url,
                "zip_filename": zip_filename,
                "product_name": product_name,
            })

    return results


def _load_converted_urls() -> set[str]:
    """Return set of doc_url values from all manifest_*.json files."""
    manifests_dir = Path("manifests")
    urls: set[str] = set()
    if not manifests_dir.is_dir():
        return urls
    for mf in manifests_dir.glob("manifest_*.json"):
        try:
            entries = json.loads(mf.read_text(encoding="utf-8"))
            for e in entries:
                doc_url = (e.get("url") or "").strip()
                if doc_url:
                    urls.add(doc_url)
        except Exception:
            continue
    return urls


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
    parser.add_argument("--skip-api-refs", action="store_true",
                        help="Skip Phase 6 API reference copy")
    parser.add_argument("--skip-archives", action="store_true",
                        help="Skip Phase 7 archived ZIP download")
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
            sum(1 for _ in v[1].rglob("*") if _.is_file()) if v[1] else 0
            for v in versions
        )
        ver_names = [v[0] for v in versions]
        print(f"    {product:<30} versions={ver_names}  webhelp={webhelp_files}")

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
        for version, html_dir in discover_versions(src, product):
            if not html_dir:
                continue
            folder_ver = version.replace(".", "-")
            toc_file = dst / product / args.lang / product / "online-help" / folder_ver / "_toc.json"
            if toc_file.exists():
                toc_candidates += 1
                old_root = f"{src_prefix}/{product}/{version}/doc/html/"
                new_root = f"{product}/{args.lang}/{product}/online-help/{folder_ver}/"
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

                    n_user = copy_asset_folder(
                        cache_doc_dir, "pdf", dest_base, version_dashed,
                        product_name, version, slug_mappings,
                        exclude_slugs=_USER_GUIDE_EXCLUDE_SLUGS,
                        dest_subfolder="user-guides",
                        label="User Guides (PDF)",
                    )
                    n_rel = copy_release_info_folder(
                        cache_doc_dir, dest_base, version_dashed,
                        product_name, version, slug_mappings,
                    )
                    n_ref = copy_reference_docs_folder(
                        cache_doc_dir, dest_base, version_dashed,
                        product_name, version, slug_mappings,
                    )
                    product_total += n_user + n_rel + n_ref
                if product_total:
                    print(f"  {product}: {product_total} asset files copied")
                total_asset_files += product_total
            save_slug_mappings(slug_mappings, SLUG_MAPPINGS_FILE)
            print(f"  Total: {total_asset_files} asset files copied")

    # ── Phase 6: copy API reference folders + rewrite links ──────────────────
    total_api_files = 0
    total_api_link_rewrites = 0
    if args.skip_api_refs:
        print("\n=== Phase 6: API reference copy skipped (--skip-api-refs) ===")
    else:
        print("\n=== Phase 6: Copying API reference folders ===")
        cache_root = Path(args.cache)
        if not cache_root.exists():
            print(f"  Cache root not found: {cache_root} — skipping")
        else:
            for product in products:
                cache_src = cache_root / product
                if not cache_src.is_dir():
                    continue
                resources_base = (dst / f"{product}-resources"
                                  / args.lang / product / "api-references")
                product_api_files = 0
                for version_dir in sorted(cache_src.iterdir()):
                    if not version_dir.is_dir():
                        continue
                    version = version_dir.name
                    version_dashed = version.replace(".", "-")
                    doc_dir = version_dir / "doc"
                    for subdir in sorted(API_REF_SUBDIRS):
                        src_api = doc_dir / subdir
                        if not src_api.is_dir():
                            continue
                        dest_api = resources_base / subdir / version_dashed
                        shutil.copytree(src_api, dest_api, dirs_exist_ok=True)
                        count = sum(1 for f in dest_api.rglob("*") if f.is_file())
                        product_api_files += count

                    # Rewrite API ref links and remove the api/ mirror from online-help
                    online_help_dir = (dst / product / args.lang / product
                                       / "online-help" / version_dashed)
                    if online_help_dir.is_dir():
                        for md_file in online_help_dir.rglob("*.md"):
                            rewrites = rewrite_api_ref_links(
                                md_file, product, version_dashed, dst, args.lang
                            )
                            total_api_link_rewrites += rewrites

                        # Remove the api/ mirror folder that the ZIP placed under online-help
                        # (typically under API-Reference/api/ — now served from api-references/)
                        for api_mirror in online_help_dir.rglob("api"):
                            if api_mirror.is_dir():
                                shutil.rmtree(api_mirror)

                if product_api_files:
                    print(f"  {product}: {product_api_files} API reference files copied")
                total_api_files += product_api_files

        if total_api_files:
            print(f"  Total: {total_api_files} API reference files copied")
        if total_api_link_rewrites:
            print(f"  Link rewrites: {total_api_link_rewrites} API reference links updated")

    # ── Phase 7: download archived version ZIPs ───────────────────────────────
    total_archived = 0
    if args.skip_archives:
        print("\n=== Phase 7: Archived ZIP download skipped (--skip-archives) ===")
    else:
        print("\n=== Phase 7: Downloading archived version ZIPs ===")
        converted_urls = _load_converted_urls()
        for product in products:
            # Derive the pub slug from manifests; fall back to the cache dir name
            pub_slug = _extract_pub_slug_from_manifests(product) or product
            if pub_slug != product:
                print(f"  {product}: pub slug = '{pub_slug}' (from manifests)")

            archived = _load_archived_versions(pub_slug, converted_urls)
            if not archived:
                continue

            # All ZIPs go into a single flat archives/ folder — no per-version subdir
            archives_dir = dst / f"{product}-resources" / args.lang / product / "archives"
            archives_dir.mkdir(parents=True, exist_ok=True)

            downloaded_entries: list[dict] = []
            for entry in archived:
                zip_dest = archives_dir / entry["zip_filename"]
                if zip_dest.exists():
                    downloaded_entries.append(entry)
                else:
                    try:
                        print(f"  Downloading {entry['zip_url']} ...")
                        urllib.request.urlretrieve(entry["zip_url"], zip_dest)
                        downloaded_entries.append(entry)
                    except Exception as exc:
                        print(f"  WARNING: failed to download {entry['zip_url']}: {exc}")

            if not downloaded_entries:
                continue

            # Derive product_name from the first successful entry (all share the same product)
            product_name = downloaded_entries[0]["product_name"]
            title = f"{product_name} Archived Documentation"

            import yaml as _yaml

            def _ys(v: str) -> str:
                return _yaml.dump(v, allow_unicode=True, default_flow_style=True).split("\n")[0]

            # Write single index.md listing all downloaded ZIPs
            archive_label = "Archived Documentation"
            index_lines = [
                "---\n",
                f"doc_name: {_ys(archive_label)}\n",
                f"title: {_ys(title)}\n",
                f"product_name: {_ys(product_name)}\n",
                "---\n",
                "\n",
                f"# {title}\n",
                "\n",
            ]
            for entry in sorted(downloaded_entries, key=lambda e: e["version"], reverse=True):
                label = f"{product_name} {entry['version']} Documentation"
                index_lines.append(f"- [{label}]({entry['zip_filename']})\n")
            (archives_dir / "index.md").write_text("".join(index_lines), encoding="utf-8")

            # Write toc.yml
            toc_lines = [
                f"docs_list_title: {_ys(title)}\n",
                "docs:\n",
                f"- title: {_ys(title)}\n",
                "  url: index.md\n",
            ]
            (archives_dir / "toc.yml").write_text("".join(toc_lines), encoding="utf-8")

            print(f"  {product}: {len(downloaded_entries)} archived ZIPs -> archives/")
            total_archived += len(downloaded_entries)

        if not total_archived:
            print("  No archived versions to download.")

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n=== Done ===")
    print(f"  Products  : {len(products)}")
    print(f"  Files     : {len(mapping) - errors} copied, {errors} errors")
    print(f"  _toc.json : {patched} patched")
    if total_asset_files:
        print(f"  Assets    : {total_asset_files} PDF/doc files copied")
    if total_api_files:
        print(f"  API refs  : {total_api_files} files copied, {total_api_link_rewrites} links rewritten")
    if total_archived:
        print(f"  Archives  : {total_archived} version ZIPs downloaded")
    if cross_links:
        print(f"  Cross-link: {len(cross_links)} links need manual review")
    print(f"  Output    : {dst.resolve()}")

    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
