"""
create_ebx_export.py — Package the EBX converter scripts for the EBX team.

Creates a zip archive containing only the files needed to run the EBX
documentation conversion pipeline. Runtime directories (cache/, output/,
logs/, .venv/) and unrelated scripts are excluded.

Usage:
  python scripts/create_ebx_export.py
  python scripts/create_ebx_export.py --output ebx-converter-v2.zip
"""

import argparse
import sys
import zipfile
from pathlib import Path

# ── File list ─────────────────────────────────────────────────────────────────

# Individual files to include (paths relative to repo root)
EXPORT_FILES = [
    "run.py",
    "requirements.txt",
    "README-EBX.md",
    "config/settings.yaml",
    "config/phases/ebx.yaml",
    "config/phases/ebx-addon-62x.yaml",
    "config/zip_urls_template.txt",
    "scripts/01_build_manifest.py",
    "scripts/02a_download_zip.py",
    "scripts/02_download.py",
    "scripts/03_convert.py",
    "scripts/04_build_csh_maps.py",
    "scripts/05_postprocess.py",
    "scripts/06_build_toc.py",
    "scripts/07_generate_report.py",
    "scripts/ebx_addon_restructure.py",
    "scripts/08_restructure_ebx.py",
]

# Directories to include recursively (.py files only, no __pycache__)
EXPORT_DIRS = [
    "scripts/lib",
    "scripts/pdf",
    "scripts/webworks",
    "scripts/dita",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def collect_dir(root: Path, rel_dir: str) -> list[Path]:
    """Return all .py files under rel_dir, excluding __pycache__."""
    target = root / rel_dir
    if not target.is_dir():
        print(f"  WARNING: directory not found, skipping: {rel_dir}", file=sys.stderr)
        return []
    return [
        p for p in sorted(target.rglob("*.py"))
        if "__pycache__" not in p.parts
    ]


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a self-contained EBX converter zip for the EBX team"
    )
    parser.add_argument(
        "--output", default="ebx-converter.zip",
        help="Output zip filename (default: ebx-converter.zip)",
    )
    args = parser.parse_args()

    root = Path(__file__).parent.parent.resolve()
    out_path = Path(args.output)
    if not out_path.is_absolute():
        out_path = Path.cwd() / out_path

    # Collect all files
    paths: list[tuple[Path, str]] = []  # (abs_path, arc_name)

    for rel in EXPORT_FILES:
        abs_path = root / rel
        if not abs_path.exists():
            print(f"  WARNING: file not found, skipping: {rel}", file=sys.stderr)
            continue
        paths.append((abs_path, rel))

    for rel_dir in EXPORT_DIRS:
        for abs_path in collect_dir(root, rel_dir):
            arc_name = abs_path.relative_to(root).as_posix()
            paths.append((abs_path, arc_name))

    if not paths:
        print("ERROR: no files found to include.", file=sys.stderr)
        return 1

    # Write zip
    print(f"Writing {out_path.name} ...")
    total_bytes = 0
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for abs_path, arc_name in sorted(paths, key=lambda x: x[1]):
            zf.write(abs_path, arc_name)
            size = abs_path.stat().st_size
            total_bytes += size
            print(f"  + {arc_name}  ({size:,} B)")

    zip_size_kb = out_path.stat().st_size // 1024
    print(
        f"\nDone — {len(paths)} files, "
        f"{total_bytes / 1024:.0f} KB uncompressed -> "
        f"{zip_size_kb:,} KB zip: {out_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
