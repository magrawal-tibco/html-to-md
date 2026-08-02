#!/usr/bin/env python3
"""
Copy all PDFs from cache/pub/ebx-addon/<version>/pdf/ to the en-us-onebx-ebx-addons
repo and generate index.md + toc.yml per version.

Source:      cache/pub/ebx-addon/<version>/pdf/
Destination: C:/github/ebx/en-us-onebx-ebx-addons/en-us/ebx-addon/pdf/<version-dashed>/
"""
import argparse
import re
import shutil
from pathlib import Path

DEFAULT_CACHE_SRC = Path("cache/pub/ebx-addon")
DEFAULT_DEST = Path(r"C:\github\ebx\en-us-onebx-ebx-addons\en-us\ebx-addon\pdf")

PRODUCT_NAME = "TIBCO EBX Add-ons"

ADDON_NAMES = {
    "common": "TIBCO EBX Addon",
    "adix":   "TIBCO EBX Data Exchange Add-on (Legacy)",
    "dama":   "TIBCO EBX Digital Asset Manager Add-on",
    "daqa":   "TIBCO EBX Match and Merge Add-on (Legacy)",
    "dint":   "TIBCO EBX Data Exchange Add-on (New)",
    "dmdv":   "TIBCO EBX Data Model and Data Visualization Add-on",
    "dpra":   "TIBCO EBX Insight Add-on (New)",
    "dqid":   "TIBCO EBX Insight Add-on (Legacy)",
    "gram":   "TIBCO EBX Graph View Add-on",
    "hmfh":   "TIBCO EBX Add-on for Oracle Hyperion EPM",
    "igov":   "TIBCO EBX Information Governance Add-on",
    "mame":   "TIBCO EBX Match and Merge Add-on (New)",
    "moda":   "TIBCO EBX GO Add-on",
    "mtrn":   "TIBCO EBX Activity Monitoring Add-on",
    "rpfl":   "TIBCO EBX Rules Portfolio Add-on",
    "tese":   "TIBCO EBX Information Search Add-on",
    "addon":  "TIBCO EBX Add-ons",
}

SLUG_LABELS = {
    "license":                        "License Agreement",
    "relnotes":                       "Release Notes",
    "versioning_and_packaging_guide": "Versioning and Packaging Guide",
    "vpat":                           "VPAT (Accessibility)",
    "readme":                         "Readme",
    "admin-guide":                    "Administration Guide",
    "dev-guide":                      "Developer Guide",
    "installation":                   "Installation Guide",
    "user-guide":                     "User Guide",
    "upgrade-guide":                  "Upgrade Guide",
    "security-guide":                 "Security Guide",
}

VERSION_RE = re.compile(r"^\d+\.\d+")


def version_dashed(version: str) -> str:
    return version.replace(".", "-")


def parse_pdf_filename(stem: str):
    """
    Parse a PDF stem like TIB_ebx-adix_2.7.12_relnotes into
    (addon_code, guide_slug_or_None).

    Returns None if the filename does not match the expected TIB_ebx-* pattern.
    """
    parts = stem.split("_")
    # parts[0] = "TIB", parts[1] = "ebx-<addon>", parts[2] = version, parts[3:] = slug tokens
    if len(parts) < 3 or parts[0] != "TIB" or not parts[1].startswith("ebx-"):
        return None

    addon_code = parts[1][4:]  # strip "ebx-" prefix

    # Find the index of the version token (first token starting with a digit)
    version_idx = None
    for i, p in enumerate(parts):
        if VERSION_RE.match(p):
            version_idx = i
            break
    if version_idx is None:
        return None

    slug_tokens = parts[version_idx + 1:]
    guide_slug = "_".join(slug_tokens) if slug_tokens else None

    return addon_code, guide_slug


def build_title(addon_code: str, guide_slug) -> str:
    product = ADDON_NAMES.get(addon_code, f"TIBCO EBX {addon_code.title()} Add-on")
    if guide_slug is None:
        return f"{product} Documentation"
    label = SLUG_LABELS.get(guide_slug, guide_slug.replace("_", " ").replace("-", " ").title())
    return f"{product} {label}"


def write_index_md(dest_dir: Path, version: str, pdf_entries: list):
    """Write index.md listing all PDFs as bullet links."""
    title = f"{PRODUCT_NAME} {version} PDF Downloads"
    lines = [
        "---",
        f'title: "{title}"',
        f'product_name: "{PRODUCT_NAME}"',
        f'product_version: "{version}"',
        'doc_type: "pdf"',
        "---",
        "",
        "# PDF Downloads",
        "",
    ]
    for filename, display_title in pdf_entries:
        lines.append(f"- [{display_title}]({filename})")
    lines.append("")
    (dest_dir / "index.md").write_text("\n".join(lines), encoding="utf-8")


def write_toc_yml(dest_dir: Path, version: str):
    """Write toc.yml pointing to index.md."""
    content = (
        f"docs_list_title: {PRODUCT_NAME} {version} PDF Downloads\n"
        "docs:\n"
        "- title: PDF Downloads\n"
        "  url: index.md\n"
    )
    (dest_dir / "toc.yml").write_text(content, encoding="utf-8")


def process_version(version: str, cache_src: Path, dest_base: Path, dry_run: bool) -> bool:
    pdf_src = cache_src / version / "pdf"
    if not pdf_src.is_dir():
        pdf_src = cache_src / version / "doc" / "pdf"
    if not pdf_src.is_dir():
        return False

    pdf_files = sorted(f for f in pdf_src.iterdir() if f.suffix.lower() == ".pdf")
    if not pdf_files:
        return False

    ver_dashed = version_dashed(version)
    dest_dir = dest_base / ver_dashed

    print(f"\n[{version}] -> {dest_dir} ({len(pdf_files)} PDFs)")

    pdf_entries = []
    for src in pdf_files:
        parsed = parse_pdf_filename(src.stem)
        if parsed is None:
            print(f"  [SKIP] unrecognised filename: {src.name}")
            continue
        addon_code, guide_slug = parsed
        title = build_title(addon_code, guide_slug)
        pdf_entries.append((src.name, title))
        if dry_run:
            print(f"  [DRY] copy {src.name!r} -> [{title}]")
        else:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest_dir / src.name)
            print(f"  [COPY] {src.name!r} -> {title!r}")

    if not dry_run:
        write_index_md(dest_dir, version, pdf_entries)
        write_toc_yml(dest_dir, version)
        print(f"  [GEN] index.md + toc.yml")
    else:
        print(f"  [DRY] would write index.md + toc.yml with {len(pdf_entries)} entries")

    return True


def main():
    parser = argparse.ArgumentParser(description="Copy EBX addon PDFs and generate index.md + toc.yml per version.")
    parser.add_argument("--cache-src", type=Path, default=DEFAULT_CACHE_SRC,
                        help=f"Root cache dir for ebx-addon (default: {DEFAULT_CACHE_SRC})")
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST,
                        help=f"Destination pdf/ root (default: {DEFAULT_DEST})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would happen without copying files or writing output")
    args = parser.parse_args()

    cache_src = args.cache_src
    if not cache_src.is_dir():
        print(f"ERROR: cache source not found: {cache_src}")
        return

    versions = sorted(d.name for d in cache_src.iterdir() if d.is_dir())
    print(f"Found {len(versions)} version dirs under {cache_src}")
    if args.dry_run:
        print("DRY RUN — no files will be written\n")

    processed = 0
    skipped = 0
    for version in versions:
        ok = process_version(version, cache_src, args.dest, args.dry_run)
        if ok:
            processed += 1
        else:
            skipped += 1

    print(f"\nDone: {processed} versions processed, {skipped} skipped (no pdf/ folder).")


if __name__ == "__main__":
    main()
