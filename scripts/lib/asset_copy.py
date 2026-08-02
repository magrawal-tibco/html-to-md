"""
asset_copy.py — Shared utilities for copying PDF/doc assets and generating listing pages.

Used by:
  scripts/08_restructure_ebx.py   (EBX-specific restructure)
  scripts/09_copy_assets.py       (generic product asset copy)
"""

import re
import shutil
from pathlib import Path

import fitz  # PyMuPDF
import yaml

SLUG_MAPPINGS_FILE = Path("config/pdf_slug_mappings.yaml")

# PDF slugs that are "release documents" — listed in doc/index.md, excluded from pdf/index.md.
RELEASE_DOC_SLUGS = {"relnotes", "license", "vpat"}

# Anchors on the version number to handle product codes with underscores (e.g. dsp_gridserver).
# Captures everything after the version as the slug.
_VERSION_ANCHOR_RE = re.compile(r"_\d+[\d.]*_(.+)$")
# Fallback for filenames without a version segment — take last underscore-separated token.
_LAST_TOKEN_RE = re.compile(r"_([^_]+)$")


def load_slug_mappings(path: Path = SLUG_MAPPINGS_FILE) -> dict[str, str]:
    if path.is_file():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return {k: (v or "") for k, v in data.items()}
    return {}


def save_slug_mappings(mappings: dict[str, str], path: Path = SLUG_MAPPINGS_FILE) -> None:
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
        # Use yaml.dump for the value to safely quote strings with special characters
        label_yaml = yaml.dump(label, allow_unicode=True, default_flow_style=True).strip()
        lines.append(f"{slug}: {label_yaml}\n")
    path.write_text("".join(lines), encoding="utf-8")


def extract_slug(stem: str) -> str | None:
    """Extract the guide-type slug from a TIB_* filename stem.

    Handles product codes with underscores (e.g. TIB_dsp_gridserver_7.2.0_admin-guide)
    by anchoring on the version number, not on a fixed number of underscore segments.
    """
    m = _VERSION_ANCHOR_RE.search(stem)
    if m:
        return m.group(1)
    m = _LAST_TOKEN_RE.search(stem)
    if m:
        return m.group(1)
    return None


def read_pdf_title(pdf_path: Path) -> str | None:
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
    """Resolve a human-readable display name for a PDF/doc asset file.

    Resolution order:
      1. PDF Title metadata → strip "<product_name> <product_version> " prefix
      2. Slug mapping lookup
      3. Title-case the slug (replace _ and - with spaces)
      4. Raw filename as last resort

    Updates slug_mappings in-place with newly discovered or unknown entries.
    """
    stem = filepath.stem
    ext = filepath.suffix.lower()
    slug = extract_slug(stem)

    # 1. PDF metadata
    if ext == ".pdf" and slug:
        title = read_pdf_title(filepath)
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

    # 3. Title-case fallback (handle both _ and - separators)
    if slug:
        label = re.sub(r"[-_]+", " ", slug).title()
        return f"{product_name} {product_version} {label}"

    # 4. Raw filename
    return filepath.name


def write_index_md(
    dest_dir: Path,
    subfolder: str,
    product_name: str,
    product_version: str,
    files: list[Path],
    slug_mappings: dict[str, str],
    extra_files: list[tuple[Path, str]] | None = None,
) -> None:
    """Write index.md for a pdf/ or doc/ asset folder.

    extra_files: list of (source_path, link_href) for cross-folder entries — used to
    include relnotes/license/vpat links in doc/index.md pointing into pdf/<ver>/.
    """
    doc_type_label = "PDF Downloads" if subfolder == "pdf" else "Release Documents"
    title = f"{product_name} {product_version} {doc_type_label}"

    # Serialize each frontmatter value via yaml.dump to safely quote special characters
    # (colons, brackets, quotes, etc. in product names/versions would break raw f-string YAML)
    def _ys(v: str) -> str:
        return yaml.dump(v, allow_unicode=True, default_flow_style=True).strip()

    lines = [
        "---\n",
        f"title: {_ys(title)}\n",
        f"product_name: {_ys(product_name)}\n",
        f"product_version: {_ys(product_version)}\n",
        f"doc_type: {_ys(subfolder)}\n",
        "---\n",
        "\n",
        f"# {title}\n",
        "\n",
    ]
    for f in sorted(files):
        display = resolve_display_name(f, product_name, product_version, slug_mappings)
        lines.append(f"- [{display}]({f.name})\n")
    for f, link_href in sorted(extra_files or [], key=lambda x: x[0].name):
        display = resolve_display_name(f, product_name, product_version, slug_mappings)
        lines.append(f"- [{display}]({link_href})\n")

    (dest_dir / "index.md").write_text("".join(lines), encoding="utf-8")


def write_toc_yml(dest_dir: Path, subfolder: str, product_name: str, product_version: str) -> None:
    doc_type_label = "PDF Downloads" if subfolder == "pdf" else "Release Documents"
    title = f"{product_name} {product_version} {doc_type_label}"
    # Use yaml.dump for string values to safely handle special characters
    # (product names may contain ®, colons, or other YAML-significant characters)
    def _ys(v: str) -> str:
        return yaml.dump(v, allow_unicode=True, default_flow_style=True).strip()

    content = (
        f"docs_list_title: {_ys(title)}\n"
        "docs:\n"
        f"- title: {_ys(doc_type_label)}\n"
        "  url: index.md\n"
    )
    (dest_dir / "toc.yml").write_text(content, encoding="utf-8")


def copy_asset_folder(
    cache_doc_dir: Path,
    subfolder: str,
    dest_base: Path,
    version_dashed: str,
    product_name: str,
    product_version: str,
    slug_mappings: dict[str, str],
    exclude_slugs: set[str] | None = None,
    extra_files: list[tuple[Path, str]] | None = None,
) -> int:
    """Copy pdf/ or doc/ assets from cache to dest_base/<subfolder>/<version_dashed>/.

    Generates index.md and toc.yml alongside the copied files.
    Returns the number of asset files copied (excluding generated files).

    exclude_slugs: slugs to omit from the index.md listing (files are still copied).
    extra_files: (source_path, link_href) pairs to append to index.md — used to list
                 release-doc PDFs in doc/index.md with cross-folder relative links.
    """
    src_dir = cache_doc_dir / subfolder
    if not src_dir.is_dir():
        return 0

    files = [f for f in sorted(src_dir.iterdir()) if f.is_file()]
    if not files:
        return 0

    dest_dir = dest_base / subfolder / version_dashed
    dest_dir.mkdir(parents=True, exist_ok=True)

    for f in files:
        shutil.copy2(f, dest_dir / f.name)

    list_files = [f for f in files
                  if extract_slug(f.stem) not in (exclude_slugs or set())]
    write_index_md(dest_dir, subfolder, product_name, product_version,
                   list_files, slug_mappings, extra_files)
    write_toc_yml(dest_dir, subfolder, product_name, product_version)

    return len(files)


def discover_asset_versions(cache_src: Path) -> list[tuple[str, str]]:
    """Return [(version, version_dashed)] for cache versions that have pdf/ or doc/ assets."""
    results = []
    for ver_dir in sorted(cache_src.iterdir()):
        if not ver_dir.is_dir():
            continue
        doc_dir = ver_dir / "doc"
        if (doc_dir / "pdf").is_dir() or (doc_dir / "doc").is_dir():
            results.append((ver_dir.name, ver_dir.name.replace(".", "-")))
    return results
