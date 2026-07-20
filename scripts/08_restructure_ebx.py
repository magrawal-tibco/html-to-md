"""
08_restructure_ebx.py — Restructure EBX main output to match AEM Guides layout.

Transforms the version-first, URL-mirroring layout into a language-first,
product-slug/doc-type layout matching the structure used by dsp-logv and
other AEM-ready products.

Old layout (mirrors source URL):
  output/pub/ebx/<version>/doc/html/<lang>/<content>
  output/pub/ebx/<version>/doc/relnotes/relnotes.md

New layout (language-first, product-slug grouped):
  output/ebx/<lang>/ebx/webhelp/<version>/<content>
  output/ebx/<lang>/ebx/webhelp/<version>/_toc.json   (root path updated)
  output/ebx/<lang>/ebx/webhelp/<version>/toc.yml
  output/ebx/en-us/ebx/relnotes/<version>/relnotes.md

Language normalisation:
  en  → en-us
  fr  → fr-fr
  ja  → ja-jp

Version normalisation:  5.9.26 → 5-9-26  (dots → dashes)

Original source is left untouched.

Usage:
  python scripts/08_restructure_ebx.py [--src output/pub/ebx]
                                        [--dst output/ebx]
                                        [--preflight-only]
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

from tqdm import tqdm

# Matches Markdown links: [text](url)
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")

# Language normalisation map
LANG_MAP: dict[str, str] = {
    "en": "en-us",
    "fr": "fr-fr",
    "ja": "ja-jp",
}

PRODUCT_SLUG = "ebx"
PRODUCT_NAME = "TIBCO EBX®"
SLUG_MAPPINGS_FILE = Path("config/pdf_slug_mappings.yaml")

# Parses TIB_<prod>_<version>_<slug> (standard) or TIB_<prod>_<slug> (no version)
_SLUG_RE = re.compile(r"^TIB_[^_]+_\d[\d.]+_(.+)$")
_SLUG_NOVERSION_RE = re.compile(r"^TIB_[^_]+_(.+)$")


def load_slug_mappings(path: Path) -> dict[str, str]:
    if path.is_file():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return {k: (v or "") for k, v in data.items()}
    return {}


def save_slug_mappings(path: Path, mappings: dict[str, str]) -> None:
    # Preserve any existing comments by writing a fresh file with a header comment.
    lines = [
        "# PDF / doc filename slug → guide label (without product name or version prefix).\n",
        "# Display name is constructed as: \"<product_name> <version> <label>\"\n",
        "#\n",
        "# Auto-populated from PDF Title metadata during conversion.\n",
        "# Manual corrections here take precedence and apply to all future runs.\n",
        "# Slugs with an empty value (\"\") were discovered but not yet resolved — fill them in.\n",
        "\n",
    ]
    for slug in sorted(mappings):
        label = mappings[slug]
        lines.append(f'{slug}: "{label}"\n')
    path.write_text("".join(lines), encoding="utf-8")


def _extract_slug(stem: str) -> str | None:
    m = _SLUG_RE.match(stem)
    if m:
        return m.group(1)
    m = _SLUG_NOVERSION_RE.match(stem)
    if m:
        return m.group(1)
    return None


def _read_pdf_title(pdf_path: Path) -> str | None:
    try:
        doc = fitz.open(str(pdf_path))
        meta = doc.metadata
        doc.close()
        return (meta.get("title") or "").strip() or None
    except Exception:
        return None


def resolve_display_name(
    filepath: Path,
    product_name: str,
    product_version: str,
    slug_mappings: dict[str, str],
) -> str:
    """
    Resolve a human-readable display name for a PDF/doc asset file.

    Resolution order:
      1. PDF Title metadata → strip "<product_name> <product_version> " prefix → label
      2. Slug mapping lookup
      3. Title-case the slug (underscores → spaces)
      4. Raw filename as last resort

    Updates slug_mappings in-place with newly discovered entries.
    """
    stem = filepath.stem
    ext = filepath.suffix.lower()
    slug = _extract_slug(stem)

    # 1. PDF metadata
    if ext == ".pdf" and slug:
        title = _read_pdf_title(filepath)
        if title:
            prefix = f"{product_name} {product_version} "
            if title.startswith(prefix):
                label = title[len(prefix):]
                if slug not in slug_mappings:
                    slug_mappings[slug] = label
                return f"{product_name} {product_version} {label}"

    # 2. Slug mapping
    if slug and slug_mappings.get(slug):
        return f"{product_name} {product_version} {slug_mappings[slug]}"

    # Flag unknown slug for manual review
    if slug and slug not in slug_mappings:
        slug_mappings[slug] = ""

    # 3. Title-case fallback
    if slug:
        label = slug.replace("_", " ").title()
        return f"{product_name} {product_version} {label}"

    # 4. Raw filename
    return filepath.name


def _write_index_md(
    dest_dir: Path,
    subfolder: str,
    product_name: str,
    product_version: str,
    files: list[Path],
    slug_mappings: dict[str, str],
) -> None:
    doc_type_label = "PDF Downloads" if subfolder == "pdf" else "Additional Documents"
    title = f"{product_name} {product_version} {doc_type_label}"

    lines = [
        "---\n",
        f'title: "{title}"\n',
        f'product_name: "{product_name}"\n',
        f'product_version: "{product_version}"\n',
        f'doc_type: "{subfolder}"\n',
        "---\n",
        "\n",
        f"# {title}\n",
        "\n",
    ]
    for f in sorted(files):
        display = resolve_display_name(f, product_name, product_version, slug_mappings)
        lines.append(f"- [{display}]({f.name})\n")

    (dest_dir / "index.md").write_text("".join(lines), encoding="utf-8")


def _write_toc_yml(dest_dir: Path, subfolder: str, product_name: str, product_version: str) -> None:
    doc_type_label = "PDF Downloads" if subfolder == "pdf" else "Additional Documents"
    title = f"{product_name} {product_version} {doc_type_label}"
    content = (
        f"docs_list_title: {title}\n"
        "docs:\n"
        f"- title: {doc_type_label}\n"
        "  url: index.md\n"
    )
    (dest_dir / "toc.yml").write_text(content, encoding="utf-8")


def copy_asset_folder(
    cache_doc_dir: Path,
    subfolder: str,
    dst: Path,
    version_dashed: str,
    product_name: str,
    product_version: str,
    slug_mappings: dict[str, str],
) -> int:
    """Copy pdf/ or doc/ assets from cache to output, generate index.md and toc.yml.
    Returns number of files copied."""
    src_dir = cache_doc_dir / subfolder
    if not src_dir.is_dir():
        return 0

    files = [f for f in sorted(src_dir.iterdir()) if f.is_file()]
    if not files:
        return 0

    dest_dir = dst / "en-us" / PRODUCT_SLUG / subfolder / version_dashed
    dest_dir.mkdir(parents=True, exist_ok=True)

    for f in files:
        shutil.copy2(f, dest_dir / f.name)

    _write_index_md(dest_dir, subfolder, product_name, product_version, files, slug_mappings)
    _write_toc_yml(dest_dir, subfolder, product_name, product_version)

    return len(files)


def discover_asset_versions(cache_src: Path) -> list[tuple[str, str]]:
    """Return [(version, version_dashed)] for cache versions with pdf/ or doc/ assets."""
    results = []
    for ver_dir in sorted(cache_src.iterdir()):
        if not ver_dir.is_dir():
            continue
        doc_dir = ver_dir / "doc"
        if (doc_dir / "pdf").is_dir() or (doc_dir / "doc").is_dir():
            results.append((ver_dir.name, ver_dir.name.replace(".", "-")))
    return results


def _norm_lang(lang: str) -> str:
    return LANG_MAP.get(lang, lang)


def discover_lang_roots(src: Path) -> list[tuple[str, str, Path]]:
    """Return [(version, lang, lang_root)] for all src/<ver>/doc/html/<lang>/ dirs."""
    results = []
    for version_dir in sorted(src.iterdir()):
        if not version_dir.is_dir():
            continue
        html_dir = version_dir / "doc" / "html"
        if not html_dir.is_dir():
            continue
        for lang_dir in sorted(html_dir.iterdir()):
            if lang_dir.is_dir():
                results.append((version_dir.name, lang_dir.name, lang_dir))
    return results


def discover_relnotes(src: Path) -> list[tuple[str, Path]]:
    """Return [(version, relnotes_file)] for src/<ver>/doc/relnotes/relnotes.md."""
    results = []
    for version_dir in sorted(src.iterdir()):
        if not version_dir.is_dir():
            continue
        relnotes = version_dir / "doc" / "relnotes" / "relnotes.md"
        if relnotes.is_file():
            results.append((version_dir.name, relnotes))
    return results


def build_path_mapping(src: Path, dst: Path,
                       exclude_java_api: bool = False) -> dict[Path, Path]:
    """
    Return {old_abs_path: new_abs_path} for every file to restructure.

    Webhelp:  src/<ver>/doc/html/<lang>/<rest> → dst/<lang-norm>/ebx/webhelp/<ver-dashed>/<rest>
    Relnotes: src/<ver>/doc/relnotes/<file>    → dst/en-us/ebx/relnotes/<ver-dashed>/<file>
    Addon:    src/<ver>/doc/<addon>/<rest>      → dst/en-us/ebx-addon/<addon>/<ver-dashed>/<rest>
              where <addon> is one of the known EBX add-on module names (not html/relnotes/java)

    Java_API/ subdirectories are included by default (copied as-is, no markdown conversion).
    Pass exclude_java_api=True to omit them entirely.
    """
    _ADDON_MODULES = {"adix", "common", "dama", "daqa", "dint", "dmdv", "dpra", "dqid", "mame", "moda", "tese"}
    mapping: dict[Path, Path] = {}

    for path in src.rglob("*"):
        if not path.is_file():
            continue
        parts = path.relative_to(src).parts

        # Webhelp: <ver>/doc/html/<lang>/<rest…>  — at least 5 parts
        if len(parts) >= 5 and parts[1] == "doc" and parts[2] == "html" and parts[3] != "_toc.json":
            if exclude_java_api and len(parts) >= 6 and parts[4] == "Java_API":
                continue
            ver, lang = parts[0], parts[3]
            if not lang.startswith("_") and (Path(src / ver / "doc" / "html" / lang)).is_dir():
                lang_norm = _norm_lang(lang)
                ver_dashed = ver.replace(".", "-")
                rest = Path(*parts[4:])
                new_rel = Path(lang_norm) / PRODUCT_SLUG / "webhelp" / ver_dashed / rest
                mapping[path] = dst / new_rel

        # Relnotes: <ver>/doc/relnotes/<file>
        elif len(parts) >= 4 and parts[1] == "doc" and parts[2] == "relnotes":
            ver = parts[0]
            ver_dashed = ver.replace(".", "-")
            rest = Path(*parts[3:])
            new_rel = Path("en-us") / PRODUCT_SLUG / "relnotes" / ver_dashed / rest
            mapping[path] = dst / new_rel

        # Addon WebWorks: <ver>/doc/<addon>/<rest…>  — addon modules only
        elif len(parts) >= 4 and parts[1] == "doc" and parts[2] in _ADDON_MODULES:
            if exclude_java_api and len(parts) >= 5 and parts[3] == "Java_API":
                continue
            ver, addon = parts[0], parts[2]
            ver_dashed = ver.replace(".", "-")
            rest = Path(*parts[3:])
            new_rel = Path("en-us") / "ebx-addon" / addon / ver_dashed / rest
            mapping[path] = dst / new_rel

    return mapping


def preflight_scan(lang_roots: list[tuple[str, str, Path]], src: Path) -> list[dict]:
    """
    Scan .md files for cross-language or cross-version relative links.
    Returns a list of finding dicts.
    """
    cross_links: list[dict] = []
    src_resolved = src.resolve()

    for _ver, _lang, lang_root in lang_roots:
        lang_root_resolved = lang_root.resolve()
        for md_file in lang_root.rglob("*.md"):
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
                    resolved.relative_to(lang_root_resolved)
                    continue  # within lang root — fine
                except ValueError:
                    pass
                try:
                    resolved.relative_to(src_resolved)
                    cross_links.append({
                        "file": str(md_file.relative_to(src)),
                        "link": url,
                        "resolved": str(resolved),
                    })
                except ValueError:
                    pass

    return cross_links


def _rewrite_toc_file_path(file_path: str, old_prefix: str, new_prefix: str) -> str:
    """Rewrite a single file path in a _toc.json tree node."""
    if not file_path:
        return file_path
    normalised = file_path.replace("\\", "/")
    old_norm = old_prefix.replace("\\", "/").rstrip("/") + "/"
    if normalised.startswith(old_norm):
        rest = normalised[len(old_norm):]
        return (new_prefix.rstrip("/") + "/" + rest).replace("/", "\\")
    return file_path


def _walk_tree(node, old_prefix: str, new_prefix: str) -> None:
    """Recursively rewrite 'file' fields in a _toc.json tree."""
    if isinstance(node, dict):
        if "file" in node and node["file"]:
            node["file"] = _rewrite_toc_file_path(node["file"], old_prefix, new_prefix)
        for child in node.get("children", []):
            _walk_tree(child, old_prefix, new_prefix)
    elif isinstance(node, list):
        for item in node:
            _walk_tree(item, old_prefix, new_prefix)


def patch_toc_json(toc_path: Path, old_root: str, new_root: str,
                   old_file_prefix: str, new_file_prefix: str) -> bool:
    """Update root and all file paths in a _toc.json. Returns True if patched."""
    try:
        data = json.loads(toc_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    current_root = data.get("root", "").replace("\\", "/").rstrip("/")
    if current_root != old_root.rstrip("/"):
        return False
    data["root"] = new_root
    if "tree" in data:
        _walk_tree(data["tree"], old_file_prefix, new_file_prefix)
    toc_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Restructure EBX output: version-first → language-first AEM layout"
    )
    parser.add_argument("--src", default="output/pub/ebx",
                        help="Source directory (default: output/pub/ebx)")
    parser.add_argument("--dst", default="output/ebx",
                        help="Destination directory (default: output/ebx)")
    parser.add_argument("--cache-src", default="cache/pub/ebx",
                        help="Cache directory with raw EBX archives (default: cache/pub/ebx)")
    parser.add_argument("--preflight-only", action="store_true",
                        help="Run pre-flight scan only — no files written")
    parser.add_argument("--exclude-java-api", action="store_true",
                        help="Omit Java_API/ subdirectories from output (default: include as-is)")
    args = parser.parse_args()

    src = Path(args.src)
    dst = Path(args.dst)
    cache_src = Path(args.cache_src)

    if not src.exists():
        print(f"Error: source not found: {src}", file=sys.stderr)
        return 1

    print(f"Source : {src.resolve()}")
    print(f"Dest   : {dst.resolve()}")
    print()

    # ── Phase 0: pre-flight cross-language link scan ─────────────────────────
    print("=== Phase 0: Pre-flight scan ===")
    lang_roots = discover_lang_roots(src)
    relnotes = discover_relnotes(src)
    print(f"  {len(lang_roots)} version-language combinations found")
    print(f"  {len(relnotes)} version relnotes files found")

    cross_links = preflight_scan(lang_roots, src)

    if cross_links:
        print(f"\n  *** WARNING: {len(cross_links)} cross-language/version link(s) found ***")
        for cl in cross_links[:30]:
            print(f"    {cl['file']}  →  {cl['link']}")
        if len(cross_links) > 30:
            print(f"    ... and {len(cross_links) - 30} more")
        print()
        print("  These relative links will be broken in the restructured copy.")
    else:
        print("  No cross-language links found. All relative paths are preserved.")

    if args.preflight_only:
        print("\n(--preflight-only: stopping before copy)")
        return 0

    # ── Phase 1: build path mapping ──────────────────────────────────────────
    print("\n=== Phase 1: Building path mapping ===")
    mapping = build_path_mapping(src, dst, exclude_java_api=args.exclude_java_api)
    java_api_note = " (Java_API excluded)" if args.exclude_java_api else " (Java_API included)"
    print(f"  {len(mapping)} files mapped{java_api_note}")

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

    # ── Phase 3: patch _toc.json root and file paths ─────────────────────────
    print("\n=== Phase 3: Patching _toc.json root and file paths ===")

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
    for ver, lang, _lang_root in lang_roots:
        lang_norm = _norm_lang(lang)
        ver_dashed = ver.replace(".", "-")

        old_root = f"{src_prefix}/{ver}/doc/html/{lang}/"
        new_root = f"{dst_prefix}/{lang_norm}/{PRODUCT_SLUG}/webhelp/{ver_dashed}/"

        # File paths in the JSON tree are prefixed with src_prefix/{ver}/doc/html/{lang}/
        old_file_prefix = f"{src_prefix}/{ver}/doc/html/{lang}"
        new_file_prefix = f"{dst_prefix}/{lang_norm}/{PRODUCT_SLUG}/webhelp/{ver_dashed}"

        toc_file = dst / lang_norm / PRODUCT_SLUG / "webhelp" / ver_dashed / "_toc.json"
        if toc_file.exists():
            if patch_toc_json(toc_file, old_root, new_root, old_file_prefix, new_file_prefix):
                patched += 1

    print(f"  Patched {patched} / {len(lang_roots)} _toc.json files")

    # ── Phase 4: copy PDF and doc assets ─────────────────────────────────────
    print("\n=== Phase 4: Copying PDF and doc assets ===")
    slug_mappings = load_slug_mappings(SLUG_MAPPINGS_FILE)
    asset_total = 0

    if not cache_src.is_dir():
        print(f"  WARNING: cache source not found ({cache_src}) — skipping asset copy")
    else:
        asset_versions = discover_asset_versions(cache_src)
        print(f"  {len(asset_versions)} versions with PDF/doc assets found")
        for version, version_dashed in tqdm(asset_versions, desc="Asset versions", unit="ver"):
            cache_doc_dir = cache_src / version / "doc"
            asset_total += copy_asset_folder(
                cache_doc_dir, "pdf", dst, version_dashed, PRODUCT_NAME, version, slug_mappings
            )
            asset_total += copy_asset_folder(
                cache_doc_dir, "doc", dst, version_dashed, PRODUCT_NAME, version, slug_mappings
            )

        save_slug_mappings(SLUG_MAPPINGS_FILE, slug_mappings)
        needs_review = sum(1 for v in slug_mappings.values() if not v)
        print(f"  Asset files copied : {asset_total}")
        print(f"  Slug mappings      : {len(slug_mappings)} total, {needs_review} needing review")
        if needs_review:
            unfilled = [k for k, v in slug_mappings.items() if not v]
            print(f"  Review slugs       : {', '.join(unfilled)}")

    # ── Phase 5: report ───────────────────────────────────────────────────────
    print("\n=== Done ===")
    print(f"  Files copied : {len(mapping) - errors}")
    print(f"  Assets copied: {asset_total}")
    print(f"  Errors       : {errors}")
    if cross_links:
        print(f"  Cross-lang   : {len(cross_links)} links need manual review")
    print(f"  Output       : {dst.resolve()}")

    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
