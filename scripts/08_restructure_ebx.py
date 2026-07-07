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


def build_path_mapping(src: Path, dst: Path) -> dict[Path, Path]:
    """
    Return {old_abs_path: new_abs_path} for every file to restructure.

    Webhelp:  src/<ver>/doc/html/<lang>/<rest> → dst/<lang-norm>/ebx/webhelp/<ver-dashed>/<rest>
    Relnotes: src/<ver>/doc/relnotes/<file>    → dst/en-us/ebx/relnotes/<ver-dashed>/<file>
    Addon:    src/<ver>/doc/<addon>/<rest>      → dst/en-us/ebx-addon/<addon>/<ver-dashed>/<rest>
              where <addon> is one of the known EBX add-on module names (not html/relnotes/java)
    """
    _ADDON_MODULES = {"adix", "common", "dama", "daqa", "dint", "dmdv", "dpra", "dqid", "mame", "moda", "tese"}
    mapping: dict[Path, Path] = {}

    for path in src.rglob("*"):
        if not path.is_file():
            continue
        parts = path.relative_to(src).parts

        # Webhelp: <ver>/doc/html/<lang>/<rest…>  — at least 5 parts
        if len(parts) >= 5 and parts[1] == "doc" and parts[2] == "html" and parts[3] != "_toc.json":
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

    # ── Phase 4: report ───────────────────────────────────────────────────────
    print("\n=== Done ===")
    print(f"  Files copied : {len(mapping) - errors}")
    print(f"  Errors       : {errors}")
    if cross_links:
        print(f"  Cross-lang   : {len(cross_links)} links need manual review")
    print(f"  Output       : {dst.resolve()}")

    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
