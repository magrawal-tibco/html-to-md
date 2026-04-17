"""
06_build_toc.py — Step 6: Reconstruct TOC tree from toc_path breadcrumbs.

For each product version, reads the toc_path frontmatter field from all .md files
and reconstructs a hierarchical TOC tree. The manifest URL order is used as the
page sort order within each node.

Output: output/<version-html-root>/_toc.json per version

Usage:
  python scripts/06_build_toc.py --phase phase_01 [--config config/settings.yaml] [--dry-run]
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

import yaml
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.lib.reporter import Reporter
from scripts.lib.toc_parser import build_toc_tree_from_js


def load_settings(config_path: str) -> dict:
    return yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))


def load_manifest(phase: str, settings: dict) -> list[dict]:
    manifests_dir = Path(settings.get("manifests_dir", "manifests"))
    path = manifests_dir / f"manifest_{phase}.json"
    if not path.exists():
        raise FileNotFoundError(f"Manifest not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def read_frontmatter(md_path: Path) -> dict:
    """Read YAML frontmatter from a .md file. Returns {} on failure."""
    try:
        content = md_path.read_text(encoding="utf-8")
    except Exception:
        return {}
    if not content.startswith("---"):
        return {}
    end = content.find("\n---\n", 3)
    if end == -1:
        return {}
    try:
        return yaml.safe_load(content[3:end]) or {}
    except yaml.YAMLError:
        return {}


def version_html_root(output_path: str) -> str:
    """
    Extract the /doc/html/ root from an output path.
    e.g. pub/foo/1.0/doc/html/Admin/file.md → pub/foo/1.0/doc/html/
    """
    marker = "/doc/html/"
    idx = output_path.find(marker)
    if idx != -1:
        return output_path[: idx + len(marker)]
    # Fallback: use parent of parent directory
    return str(Path(output_path).parent.parent) + "/"


def insert_into_tree(tree: dict, segments: list[str], page_entry: dict):
    """
    Recursively insert a page into the TOC tree.

    tree structure:
    {
      "title": "...",
      "file": "..." or None,
      "children": [ ... ]
    }
    """
    if not segments:
        return

    title = segments[0]
    rest  = segments[1:]

    # Find existing child with this title
    child = None
    for c in tree["children"]:
        if c["title"] == title:
            child = c
            break

    if child is None:
        child = {"title": title, "file": None, "children": []}
        tree["children"].append(child)

    if not rest:
        # This is the leaf — assign file
        child["file"] = page_entry["output_path"]
    else:
        insert_into_tree(child, rest, page_entry)


def _version_label_from_entries(version_entries: list[dict], output_dir: Path) -> str:
    """Read product name + version from the first available .md frontmatter."""
    for entry in version_entries:
        md_path = output_dir / entry["output_path"]
        if md_path.exists():
            fm = read_frontmatter(md_path)
            name    = fm.get("product_name", "")
            version = fm.get("product_version", "")
            label   = f"{name} {version}".strip()
            if label:
                return label
    return ""


def build_version_toc(
    version_entries: list[dict],
    output_dir: Path,
    version_root: str,
    reporter: Reporter,
    cache_dir: Path | None = None,
) -> dict:
    """
    Build TOC tree for one product version.
    Prefers authoritative MadCap TOC JS files from cache when available;
    falls back to breadcrumb reconstruction.
    Returns the toc dict (not yet written to disk).
    """
    # Prefer TOC JS files extracted from the documentation ZIP
    if cache_dir is not None:
        toc_js_dir = cache_dir / version_root.rstrip("/") / "Data" / "Tocs"
        if toc_js_dir.exists() and any(toc_js_dir.glob("*.js")):
            try:
                tree, orphan_paths = build_toc_tree_from_js(
                    toc_js_dir, version_root, version_entries
                )
                version_label = _version_label_from_entries(version_entries, output_dir)
                reporter.count("toc_entries", len(version_entries) - len(orphan_paths))
                reporter.count("toc_orphans", len(orphan_paths))
                reporter.count("toc_from_js")
                return {
                    "version":  version_label,
                    "root":     version_root,
                    "tree":     tree,
                    "_orphans": orphan_paths,
                    "_source":  "toc_js",
                }
            except Exception as exc:
                reporter.warning(f"TOC JS parse failed for {version_root}: {exc} — falling back to breadcrumbs")

    tree_root = {"title": "root", "file": None, "children": []}
    orphans   = []
    no_toc    = 0

    # First pass: collect the most common toc_path segments per directory so we
    # can infer a section for pages that have no toc_path of their own.
    from collections import Counter
    dir_toc_paths: dict[str, Counter] = defaultdict(Counter)
    for entry in version_entries:
        md_path = output_dir / entry["output_path"]
        if not md_path.exists():
            continue
        fm = read_frontmatter(md_path)
        toc_path = fm.get("toc_path", "")
        segs = [s.strip() for s in toc_path.split("|") if s.strip()]
        if segs:
            directory = str(Path(entry["output_path"]).parent)
            dir_toc_paths[directory]["|".join(segs)] += 1

    # Majority toc_path prefix per directory (drop the last segment — that will
    # be the page title we append ourselves).
    dir_fallback: dict[str, list[str]] = {}
    for directory, counter in dir_toc_paths.items():
        best = counter.most_common(1)[0][0]
        dir_fallback[directory] = [s.strip() for s in best.split("|") if s.strip()]

    for entry in version_entries:
        md_path = output_dir / entry["output_path"]
        if not md_path.exists():
            continue

        fm = read_frontmatter(md_path)
        toc_path = fm.get("toc_path", "")
        # Normalize whitespace — <title> tags sometimes contain embedded newlines.
        page_title = " ".join(fm.get("title", "").split())

        segments = [s.strip() for s in toc_path.split("|") if s.strip()]

        if not segments:
            # No toc_path. Try to infer section from the majority toc_path of
            # other pages in the same directory, then append this page's title.
            directory = str(Path(entry["output_path"]).parent)
            inferred = dir_fallback.get(directory, [])
            if inferred and page_title:
                segments = inferred + [page_title]
            elif page_title:
                # No directory peers with a toc_path — flat top-level entry.
                segments = [page_title]
            else:
                orphans.append(entry["output_path"])
                no_toc += 1
                continue
        else:
            # Append the page title as the leaf segment so that multiple pages
            # under the same toc_path section don't overwrite each other.
            if page_title:
                segments = segments + [page_title]

        insert_into_tree(tree_root, segments, entry)

    reporter.count("toc_entries", len(version_entries) - no_toc)
    reporter.count("toc_orphans", len(orphans))
    reporter.count("toc_from_breadcrumbs")

    return {
        "version":  _version_label_from_entries(version_entries, output_dir),
        "root":     version_root,
        "tree":     tree_root["children"],
        "_orphans": orphans,
        "_source":  "breadcrumbs",
    }


def collect_versions(manifest: list[dict]) -> dict[str, list[dict]]:
    """Group manifest entries by version_html_root."""
    versions: dict[str, list[dict]] = defaultdict(list)
    for entry in manifest:
        root = version_html_root(entry["output_path"])
        versions[root].append(entry)
    return dict(versions)


def main():
    parser = argparse.ArgumentParser(description="Step 6: Build TOC JSON per version")
    parser.add_argument("--phase",   required=True)
    parser.add_argument("--config",  default="config/settings.yaml")
    parser.add_argument("--dry-run",     action="store_true")
    parser.add_argument("--force-rerun", action="store_true", help="Accepted for orchestrator compat")
    args = parser.parse_args()

    settings   = load_settings(args.config)
    manifest   = load_manifest(args.phase, settings)
    output_dir = Path(settings.get("output_dir", "output"))
    cache_dir  = Path(settings.get("cache_dir", "cache"))

    from datetime import datetime
    logs_dir = Path(settings.get("logs_dir", "logs"))
    run_dir  = logs_dir / args.phase / datetime.now().strftime("%Y%m%d-%H%M%S")
    reporter = Reporter(run_dir, "06_toc", dry_run=args.dry_run)

    reporter.info(f"=== Step 6: Build TOC | phase={args.phase} dry_run={args.dry_run} ===")

    versions = collect_versions(manifest)
    reporter.info(f"Building TOC for {len(versions)} version(s)")

    for version_root, entries in tqdm(versions.items(), desc="Versions"):
        toc = build_version_toc(entries, output_dir, version_root, reporter, cache_dir)

        toc_path = output_dir / version_root / "_toc.json"

        if not args.dry_run:
            toc_path.parent.mkdir(parents=True, exist_ok=True)
            toc_path.write_text(
                json.dumps(toc, indent=2, ensure_ascii=False), encoding="utf-8"
            )

        reporter.count("toc_files_written")
        reporter.info(
            f"  {version_root} → {len(toc['tree'])} top-level nodes, "
            f"{len(toc['_orphans'])} orphans"
        )

    report = reporter.finish()
    return 0 if report["error_count"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
