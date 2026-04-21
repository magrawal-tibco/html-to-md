"""
scripts/webworks/build_toc.py — Build _toc.json from WebWorks toc.xml.

WebWorks ePublisher stores a structured TOC in wwhdata/xml/toc.xml per guide.
Node format: <i t="Title" l="N"> where l is a 0-based index into the entries
listed in wwhdata/files.htm. Anchors are expressed as "N#anchor_id".

Output: one _toc.json per guide directory, matching the format produced by
scripts/06_build_toc.py for MadCap pages.

Usage:
  python scripts/webworks/build_toc.py --phase bw
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import yaml
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.reporter import Reporter
from webworks.utils import discover_webworks_versions, read_books_htm, read_files_index


def _resolve_l(l_val: str, files_index: list[tuple[str, str]]
               ) -> tuple[str, str, str]:
    """
    Resolve toc.xml l="N" or l="N#anchor" to (href, title, anchor).
    l is 0-based into files_index.
    """
    if "#" in l_val:
        idx_str, anchor = l_val.split("#", 1)
    else:
        idx_str, anchor = l_val, ""
    try:
        idx = int(idx_str)
    except ValueError:
        return "", "", ""
    if 0 <= idx < len(files_index):
        href, title = files_index[idx]
        return href, title, anchor
    return "", "", ""


def _build_node(i_tag, files_index: list, output_dir: Path, guide_dir: Path,
                cache_dir: Path) -> dict | None:
    l_val = i_tag.get("l", "")
    title = i_tag.get("t", "")
    href, _, anchor = _resolve_l(l_val, files_index)

    if href:
        htm_path = guide_dir / href
        rel_to_cache = htm_path.relative_to(cache_dir)
        md_path = output_dir / rel_to_cache.with_suffix(".md")
        file_str = md_path.as_posix().replace("\\", "/")
    else:
        file_str = ""

    children = []
    for child in i_tag.find_all("i", recursive=False):
        child_node = _build_node(child, files_index, output_dir, guide_dir, cache_dir)
        if child_node:
            children.append(child_node)

    node = {"title": title, "file": file_str, "anchor": anchor}
    if children:
        node["children"] = children
    return node


def _build_guide_toc(guide_dir: Path, output_dir: Path, cache_dir: Path,
                     version: str) -> dict:
    toc_xml_path = guide_dir / "wwhdata" / "xml" / "toc.xml"
    files_index = read_files_index(guide_dir)

    rel_guide = guide_dir.relative_to(cache_dir)
    root_str = (output_dir / rel_guide).as_posix().replace("\\", "/") + "/"

    if not toc_xml_path.exists():
        return {
            "version": version, "root": root_str,
            "tree": [], "_orphans": [], "_source": "webworks_toc_xml",
        }

    soup = BeautifulSoup(
        toc_xml_path.read_text(encoding="utf-8", errors="replace"), "xml"
    )
    tree = []
    for i_tag in soup.find("WebWorksHelpTOC").find_all("i", recursive=False):
        node = _build_node(i_tag, files_index, output_dir, guide_dir, cache_dir)
        if node:
            tree.append(node)

    return {
        "version": version,
        "root": root_str,
        "tree": tree,
        "_orphans": [],
        "_source": "webworks_toc_xml",
    }


def main():
    parser = argparse.ArgumentParser(description="Build WebWorks _toc.json files")
    parser.add_argument("--phase",   required=True)
    parser.add_argument("--config",  default="config/settings.yaml")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings  = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    cache_dir  = Path(settings.get("cache_dir", "cache"))
    output_dir = Path(settings.get("output_dir", "output"))
    logs_dir   = Path(settings.get("logs_dir", "logs"))

    run_dir  = logs_dir / args.phase / datetime.now().strftime("%Y%m%d-%H%M%S")
    reporter = Reporter(run_dir, "webworks_toc", dry_run=args.dry_run)
    reporter.info(f"=== WebWorks TOC | phase={args.phase} ===")

    for version_html_root, product_slug, version in discover_webworks_versions(cache_dir):
        reporter.info(f"TOC: {product_slug} {version}")
        guide_dirs = read_books_htm(version_html_root)

        for guide_dir in guide_dirs:
            if not guide_dir.exists():
                continue
            toc = _build_guide_toc(guide_dir, output_dir, cache_dir, version)

            rel_guide = guide_dir.relative_to(cache_dir)
            out_toc = output_dir / rel_guide / "_toc.json"

            if not args.dry_run:
                out_toc.parent.mkdir(parents=True, exist_ok=True)
                out_toc.write_text(
                    json.dumps(toc, indent=2, ensure_ascii=False), encoding="utf-8"
                )
            reporter.count("toc_written")
            reporter.info(f"  Wrote: {out_toc} ({len(toc['tree'])} top-level nodes)")

    reporter.finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
