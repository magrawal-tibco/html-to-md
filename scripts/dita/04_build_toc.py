"""
04_build_toc.py — DITA Step 4: Build TOC JSON from body.js or suitehelp_topic_list.html.

Works for both file_dita and sdl_dita formats.

file_dita:
  - Parse body.js: extract suitehelp.toc JSON string → parse as HTML nav
  - Walk <li class="leaf|closed|open"> nodes under <nav>
  - Map href (%APPROOT% stripped) → output .md path

sdl_dita:
  - Parse suitehelp_topic_list.html nested <ul>/<li>/<a> structure
  - Walk hierarchy; resolve GUID hrefs → slug filenames via guid_rename_map
  - Map slug filename → output .md path

Fallback (both): if a topic .md file is not in the TOC tree, collect toc_path
breadcrumbs from frontmatter and insert into the tree as _orphans.

Output: output/{version_root}/_toc.json

Usage:
  python scripts/dita/04_build_toc.py --phase phase_01
         [--config config/dita_settings.yaml] [--dry-run]
"""

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import yaml
from bs4 import BeautifulSoup
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.lib.reporter import Reporter


def load_settings(config_path: str) -> dict:
    return yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))


def load_manifest(phase: str, settings: dict) -> list[dict]:
    manifests_dir = Path(settings.get("manifests_dir", "manifests"))
    path = manifests_dir / f"manifest_{phase}.json"
    if not path.exists():
        raise FileNotFoundError(f"Manifest not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_zip_registry(phase: str, settings: dict) -> dict:
    manifests_dir = Path(settings.get("manifests_dir", "manifests"))
    path = manifests_dir / f"zip_registry_{phase}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_guid_rename_map(phase: str, version_sitemap: str, settings: dict) -> dict | None:
    manifests_dir = Path(settings.get("manifests_dir", "manifests"))
    key = urlparse(version_sitemap).path.strip("/").replace("/", "_")
    map_path = manifests_dir / f"guid_rename_map_{phase}_{key}.json"
    if not map_path.exists():
        return None
    return json.loads(map_path.read_text(encoding="utf-8"))


# ── file_dita: parse body.js ──────────────────────────────────────────────────

def _extract_toc_html(body_js_text: str) -> str | None:
    """Extract the suitehelp.toc HTML string from body.js."""
    m = re.search(r'suitehelp\.toc\s*=\s*\{', body_js_text)
    if not m:
        return None
    start = m.end() - 1
    depth = 0
    i = start
    in_string = False
    escape_next = False
    while i < len(body_js_text):
        ch = body_js_text[i]
        if escape_next:
            escape_next = False
        elif ch == '\\' and in_string:
            escape_next = True
        elif ch == '"' and not escape_next:
            in_string = not in_string
        elif not in_string:
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    break
        i += 1
    obj_str = body_js_text[start:i + 1]
    try:
        obj = json.loads(obj_str)
        return obj.get("toc", "")
    except (json.JSONDecodeError, AttributeError):
        return None


def _walk_body_js_nav(ul_tag, html_root: str, output_dir: Path) -> list[dict]:
    """Recursively walk the <nav><ul> from suitehelp.toc HTML."""
    nodes = []
    for li in ul_tag.find_all("li", recursive=False):
        a = li.find("a", recursive=False)
        if not a:
            continue
        title = a.get_text(strip=True)
        href  = a.get("href", "")
        # Strip %APPROOT% prefix
        href = href.replace("%APPROOT%", "")
        # Build output .md path
        md_rel = re.sub(r"\.html?$", ".md", href)
        md_path = str(output_dir / html_root.rstrip("/") / md_rel).replace("\\", "/")
        md_path_rel = str(
            (output_dir / html_root.rstrip("/") / md_rel).relative_to(output_dir)
        ).replace("\\", "/")
        child_ul = li.find("ul", recursive=False)
        node: dict = {"title": title, "file": md_path_rel}
        if child_ul:
            children = _walk_body_js_nav(child_ul, html_root, output_dir)
            if children:
                node["children"] = children
        nodes.append(node)
    return nodes


def build_toc_from_body_js(
    body_js_path: Path,
    html_root: str,
    output_dir: Path,
) -> tuple[list[dict], str]:
    """
    Parse body.js and return (toc_nodes, source_label).
    source_label is "body_js" on success or "empty" if no toc found.
    """
    try:
        text = body_js_path.read_text(encoding="utf-8")
    except Exception:
        return [], "read_error"

    toc_html = _extract_toc_html(text)
    if not toc_html:
        return [], "no_toc_in_body_js"

    toc_html = toc_html.replace("%APPROOT%", "")
    soup = BeautifulSoup(toc_html, "html.parser")
    nav  = soup.find("nav")
    if not nav:
        return [], "no_nav_element"
    root_ul = nav.find("ul", recursive=False)
    if not root_ul:
        return [], "no_root_ul"

    nodes = _walk_body_js_nav(root_ul, html_root, output_dir)
    return nodes, "body_js"


# ── sdl_dita: parse suitehelp_topic_list.html ────────────────────────────────

def _walk_topic_list(
    ul_tag,
    html_root: str,
    rename_map: dict,
    output_dir: Path,
) -> list[dict]:
    """Recursively walk the suitehelp_topic_list.html nested <ul>/<li>/<a>."""
    nodes = []
    for li in ul_tag.find_all("li", recursive=False):
        a = li.find("a", recursive=False)
        if not a:
            continue
        title        = a.get_text(strip=True)
        guid_href    = a.get("href", "")
        slug_href    = rename_map.get("topics", {}).get(guid_href, guid_href)
        md_filename  = re.sub(r"\.html?$", ".md", slug_href)
        md_path_rel  = str(
            Path(html_root.rstrip("/")) / md_filename
        ).replace("\\", "/")
        child_ul = li.find("ul", recursive=False)
        node: dict = {"title": title, "file": md_path_rel}
        if child_ul:
            children = _walk_topic_list(child_ul, html_root, rename_map, output_dir)
            if children:
                node["children"] = children
        nodes.append(node)
    return nodes


def build_toc_from_topic_list(
    topic_list_path: Path,
    html_root: str,
    rename_map: dict,
    output_dir: Path,
) -> tuple[list[dict], str]:
    try:
        soup = BeautifulSoup(topic_list_path.read_bytes(), "html.parser")
    except Exception:
        return [], "read_error"

    root_ul = soup.find("ul")
    if not root_ul:
        return [], "no_root_ul"

    nodes = _walk_topic_list(root_ul, html_root, rename_map, output_dir)
    return nodes, "topic_list"


# ── Fallback: build from frontmatter toc_path breadcrumbs ────────────────────

def _read_frontmatter(md_path: Path) -> dict:
    try:
        text = md_path.read_text(encoding="utf-8")
    except Exception:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---\n", 3)
    if end == -1:
        return {}
    try:
        return yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError:
        return {}


def build_toc_from_breadcrumbs(version_md_dir: Path, output_dir: Path) -> list[dict]:
    """
    Fallback: collect toc_path from all .md frontmatter files and build a flat tree.
    Pages with empty toc_path go into _orphans.
    """
    tree: dict[str, dict] = {}
    orphans: list[dict] = []

    for md_path in sorted(version_md_dir.rglob("*.md")):
        if md_path.name.startswith("_"):
            continue
        fm = _read_frontmatter(md_path)
        title    = fm.get("title", md_path.stem)
        toc_path = fm.get("toc_path", "")
        rel      = str(md_path.relative_to(output_dir)).replace("\\", "/")

        if not toc_path:
            orphans.append({"title": title, "file": rel})
            continue

        parts = [p.strip() for p in toc_path.split("|") if p.strip()]
        current = tree
        for part in parts[:-1]:
            if part not in current:
                current[part] = {"title": part, "children": {}}
            current = current[part]["children"]
        leaf_key = parts[-1] if parts else title
        current[leaf_key] = {"title": leaf_key, "file": rel}

    def _dictree_to_list(d: dict) -> list[dict]:
        nodes = []
        for node in d.values():
            item: dict = {"title": node["title"]}
            if "file" in node:
                item["file"] = node["file"]
            if "children" in node and node["children"]:
                item["children"] = _dictree_to_list(node["children"])
            nodes.append(item)
        return nodes

    nodes = _dictree_to_list(tree)
    if orphans:
        nodes.append({"title": "_orphans", "children": orphans})
    return nodes


# ── Main per-version processor ────────────────────────────────────────────────

def process_version(
    version_sitemap: str,
    fmt: str,
    settings: dict,
    zip_registry: dict,
    rename_map: dict | None,
    reporter: Reporter,
    dry_run: bool,
) -> None:
    cache_dir  = Path(settings.get("cache_dir", "cache"))
    output_dir = Path(settings.get("output_dir", "output"))

    reg_entry = zip_registry.get(version_sitemap, {})
    html_root = reg_entry.get("html_root", "")
    if not html_root:
        reporter.fail(version_sitemap, "html_root not in zip_registry")
        return

    version_out_dir = output_dir / html_root.rstrip("/")
    toc_json_path   = version_out_dir / "_toc.json"

    if fmt == "file_dita":
        body_js_path = cache_dir / html_root.rstrip("/") / "static" / "body.js"
        if not body_js_path.exists():
            reporter.info(f"  {version_sitemap}: body.js not found — using breadcrumb fallback")
            nodes, source = build_toc_from_breadcrumbs(version_out_dir, output_dir), "breadcrumbs"
        else:
            nodes, source = build_toc_from_body_js(body_js_path, html_root, output_dir)
            if not nodes:
                reporter.info(f"  {version_sitemap}: body.js TOC empty ({source}) — using breadcrumbs")
                nodes = build_toc_from_breadcrumbs(version_out_dir, output_dir)
                source = "breadcrumbs"

    else:  # sdl_dita
        topic_list_path = cache_dir / html_root.rstrip("/") / "suitehelp_topic_list.html"
        if not topic_list_path.exists() or rename_map is None:
            reporter.info(
                f"  {version_sitemap}: suitehelp_topic_list.html or rename map missing"
                " — using breadcrumb fallback"
            )
            nodes = build_toc_from_breadcrumbs(version_out_dir, output_dir)
            source = "breadcrumbs"
        else:
            nodes, source = build_toc_from_topic_list(
                topic_list_path, html_root, rename_map, output_dir
            )
            if not nodes:
                reporter.info(f"  {version_sitemap}: topic list TOC empty — using breadcrumbs")
                nodes = build_toc_from_breadcrumbs(version_out_dir, output_dir)
                source = "breadcrumbs"

    toc_doc = {
        "_source": source,
        "toc":     nodes,
    }

    if not dry_run:
        version_out_dir.mkdir(parents=True, exist_ok=True)
        toc_json_path.write_text(
            json.dumps(toc_doc, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        reporter.info(
            f"  {version_sitemap}: _toc.json written "
            f"({len(nodes)} top-level nodes, source={source})"
        )
    else:
        reporter.info(
            f"  [dry-run] {version_sitemap}: would write _toc.json "
            f"({len(nodes)} top-level nodes, source={source})"
        )

    reporter.count("versions_processed")


def main():
    parser = argparse.ArgumentParser(description="DITA Step 4: Build TOC JSON")
    parser.add_argument("--phase",   required=True)
    parser.add_argument("--config",  default="config/dita_settings.yaml")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings     = load_settings(args.config)
    manifest     = load_manifest(args.phase, settings)
    zip_registry = load_zip_registry(args.phase, settings)

    from datetime import datetime
    logs_dir = Path(settings.get("logs_dir", "logs"))
    run_dir  = logs_dir / args.phase / datetime.now().strftime("%Y%m%d-%H%M%S")
    reporter = Reporter(run_dir, "dita_04_toc", dry_run=args.dry_run)

    reporter.info(
        f"=== DITA Step 4: Build TOC | phase={args.phase} dry_run={args.dry_run} ==="
    )

    dita_versions: dict[str, tuple[str, dict]] = {}
    for entry in manifest:
        vs = entry.get("version_sitemap", "")
        if not vs or vs in dita_versions:
            continue
        fmt = zip_registry.get(vs, {}).get("format", "")
        if fmt in ("file_dita", "sdl_dita"):
            dita_versions[vs] = (fmt, entry)
        elif entry.get("version_format") == "sdl_dita":
            dita_versions[vs] = ("sdl_dita", entry)

    reporter.info(f"DITA versions to process: {len(dita_versions)}")

    for version_sitemap, (fmt, _entry) in tqdm(dita_versions.items(), desc="TOC"):
        rename_map = None
        if fmt == "sdl_dita":
            rename_map = load_guid_rename_map(args.phase, version_sitemap, settings)

        process_version(
            version_sitemap, fmt, settings, zip_registry, rename_map, reporter, args.dry_run
        )

    report = reporter.finish()
    return 0 if report["error_count"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
