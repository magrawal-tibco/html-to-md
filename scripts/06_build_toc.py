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
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import yaml
from bs4 import BeautifulSoup
from markdownify import markdownify as md
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


def version_html_root(output_path: str, version_format: str = "") -> str:
    """
    Extract the version-level root from an output path.

    - MadCap: pub/foo/1.0/doc/html/Admin/file.md → pub/foo/1.0/doc/html/
    - EBX main: pub/ebx/6.2.3/doc/html/en/admin/file.md → pub/ebx/6.2.3/doc/html/en/
    - EBX addon: pub/ebx-addon/6.2.3/doc/adix/guide/file.md → pub/ebx-addon/6.2.3/doc/adix/
    """
    # Normalize to forward slashes (required on Windows)
    output_path = output_path.replace("\\", "/")
    html_marker = "/doc/html/"
    idx = output_path.find(html_marker)
    if idx != -1:
        after_html = output_path[idx + len(html_marker):]
        # EBX main only: next segment is a 2-char lowercase language code (en, fr, de, ...)
        # Guard on format so MadCap products with short folder names are unaffected.
        if version_format == "ebx":
            m = re.match(r'^([a-z]{2})/', after_html)
            if m:
                return output_path[: idx + len(html_marker)] + m.group(1) + "/"
        return output_path[: idx + len(html_marker)]
    # EBX addon / other non-html structures: parent.parent gives module root.
    # If parent.parent ends in a generic directory name ("doc", "html"), step
    # back only one level so the module directory is the version root.
    pp   = PurePosixPath(output_path)
    root = pp.parent.parent
    if root.name in ("doc", "html"):
        root = pp.parent
    return root.as_posix() + "/"


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
            label = f"{name} {version}".strip()
            if label:
                return label
    return ""


def _parse_ebx_nav(ul_el, version_root: str, version_entries: list[dict]) -> list[dict]:
    """Recursively parse EBX nav <ul> into TOC tree nodes, resolving hrefs to output paths."""
    from bs4 import NavigableString
    vr = version_root.replace("\\", "/").rstrip("/")
    lookup: dict[str, str] = {}
    for entry in version_entries:
        posix = Path(entry["output_path"]).as_posix()
        stem = posix.rsplit(".", 1)[0]
        lookup[stem] = entry["output_path"]

    def parse_ul(ul) -> list[dict]:
        nodes = []
        for li in ul.find_all("li", recursive=False):
            title = ""
            href  = ""

            span = li.find("span", recursive=False)
            a_direct = li.find("a", recursive=False)
            if span and span.find("a"):
                # 6.x style: <li><span><a href="...">Title</a></span>
                a = span.find("a")
                title = a.get_text(strip=True)
                href  = a.get("href", "").split("?")[0].split("#")[0]
            elif a_direct:
                # 4.x style: <li><a href="..."><span>Title</span></a>
                title = a_direct.get_text(strip=True)
                href  = a_direct.get("href", "").split("?")[0].split("#")[0]
            elif span:
                # Section root with no link
                title = span.get_text(strip=True)
            else:
                # Structure node: <li>Section Title<ul>...</ul></li>
                direct = next(
                    (s for s in li.children if isinstance(s, NavigableString)), ""
                )
                title = str(direct).strip()

            if not title:
                continue

            file_path = None
            if href:
                resolved = f"{vr}/{href}".replace("//", "/")
                stem = resolved.rsplit(".", 1)[0] if "." in resolved else resolved
                file_path = lookup.get(stem)

            child_ul = li.find("ul", recursive=False)
            children = parse_ul(child_ul) if child_ul else []
            nodes.append({"title": title, "file": file_path, "children": children})
        return nodes

    return parse_ul(ul_el)


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

    # EBX: parse nav tree from the index.html frameset within the version root
    if cache_dir is not None:
        for ebx_idx in [
            cache_dir / version_root.rstrip("/") / "index.html",
        ]:
            if ebx_idx.exists():
                try:
                    from bs4 import BeautifulSoup as _BS
                    idx_soup = _BS(ebx_idx.read_bytes(), "html.parser")
                    nav_ul = idx_soup.select_one("div#ebx_NavigationPagesList > ul")
                    if nav_ul:
                        tree = _parse_ebx_nav(nav_ul, version_root, version_entries)
                        if tree:
                            version_label = _version_label_from_entries(version_entries, output_dir)
                            reporter.count("toc_from_ebx_index")
                            return {
                                "version":  version_label,
                                "root":     version_root,
                                "tree":     tree,
                                "_orphans": [],
                                "_source":  "ebx_index",
                            }
                except Exception as exc:
                    reporter.warning(f"EBX index.html parse failed for {version_root}: {exc} — falling back to breadcrumbs")
                break

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


def node_to_yaml(node: dict, version_root: str) -> dict:
    result: dict = {"title": node["title"]}

    file_path = node.get("file")
    if file_path:
        if file_path.startswith(("http://", "https://")):
            result["url"] = file_path  # external URL — use as-is
        else:
            posix = Path(file_path).as_posix()
            root  = version_root.replace("\\", "/").rstrip("/") + "/"
            rel   = posix[len(root):] if posix.startswith(root) else posix
            result["url"] = rel

    children = node.get("children", [])
    if children:
        result["subfolderlist"] = [node_to_yaml(c, version_root) for c in children]

    return result


def toc_to_yaml(toc: dict) -> dict:
    version_root = toc["root"].replace("\\", "/")
    return {
        "docs_list_title": toc.get("version", ""),
        "docs": [node_to_yaml(n, version_root) for n in toc["tree"]],
    }


def slugify_title(title: str) -> str:
    """Convert a section title to a filesystem-safe ASCII slug."""
    s = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    s = re.sub(r"[^\w\s-]", "", s).strip().lower()
    return re.sub(r"[\s_-]+", "_", s) or "section"


def render_subtree(children: list, version_root: str, indent: int = 0) -> list[str]:
    """
    Render a TOC subtree as a nested markdown unordered list.

    - Nodes with a file become hyperlinks relative to the version root.
    - Nodes without a file (section headings) are plain text items.
    """
    lines = []
    prefix = "  " * indent
    root_prefix = version_root.replace("\\", "/").rstrip("/") + "/"
    for child in children:
        title = child.get("title", "")
        file_path = child.get("file")
        if file_path:
            rel = Path(file_path).as_posix()
            if rel.startswith(root_prefix):
                rel = rel[len(root_prefix):]
            lines.append(f"{prefix}- [{title}]({rel})")
        else:
            lines.append(f"{prefix}- {title}")
        sub = child.get("children", [])
        if sub:
            lines.extend(render_subtree(sub, version_root, indent + 1))
    return lines


def _version_meta(version_entries: list[dict], output_dir: Path) -> dict:
    """Return {product_name, product_version, lang} from the first readable entry."""
    for entry in version_entries:
        md_path = output_dir / entry["output_path"]
        if md_path.exists():
            fm = read_frontmatter(md_path)
            name = fm.get("product_name", "")
            if name:
                return {
                    "product_name": name,
                    "product_version": fm.get("product_version", ""),
                    "lang": fm.get("lang", "en-us"),
                }
    return {}


def generate_section_pages(
    nodes: list,
    version_root: str,
    version_dir: Path,
    meta: dict,
    seen_slugs: set | None = None,
    breadcrumb: list[str] | None = None,
    dry_run: bool = False,
) -> int:
    """
    Walk the TOC tree and synthesize a listing page for every node that has
    children but no file (pure structural/grouping headings from MadCap).

    Recurses depth-first (bottom-up) so child sections are assigned files before
    parent sections render their subtree listing.

    Writes _section_<slug>.md files at the version root directory.
    Updates node["file"] in place so the caller can re-emit _toc.json and toc.yml.

    Returns the number of pages generated.
    """
    if seen_slugs is None:
        seen_slugs = set()
    if breadcrumb is None:
        breadcrumb = []

    count = 0
    for node in nodes:
        children = node.get("children", [])
        node_breadcrumb = breadcrumb + [node["title"]]

        # Recurse FIRST (bottom-up) — child sections get files before parent renders them
        if children:
            count += generate_section_pages(
                children, version_root, version_dir, meta,
                seen_slugs, node_breadcrumb, dry_run,
            )

        # Skip nodes that already have a page or have no children at all
        if node.get("file") or not children:
            continue

        # Derive a unique slug for this section
        base = slugify_title(node["title"])
        slug = base
        n = 2
        while slug in seen_slugs:
            slug = f"{base}_{n}"
            n += 1
        seen_slugs.add(slug)

        # Build frontmatter — toc_path = the PARENT breadcrumb (excluding this node's title)
        fm: dict = {
            "title": node["title"],
            "product_name": meta.get("product_name", ""),
            "product_version": meta.get("product_version", ""),
            "lang": meta.get("lang", "en-us"),
        }
        if len(node_breadcrumb) > 1:
            fm["toc_path"] = "|".join(node_breadcrumb[:-1])

        fm_yaml = yaml.dump(fm, default_flow_style=False, allow_unicode=True, sort_keys=False)
        body_lines = render_subtree(children, version_root)
        content = (
            f"---\n{fm_yaml}---\n\n"
            f"# {node['title']}\n\n"
            + "\n".join(body_lines)
            + "\n"
        )

        out_path = version_dir / f"_section_{slug}.md"
        if not dry_run:
            out_path.write_text(content, encoding="utf-8")

        # Update node["file"] using the same path convention as converted pages:
        # version_root (e.g. "pub/ebx/6.2.3/doc/html/en/") + filename
        vr_prefix = version_root.replace("\\", "/").rstrip("/") + "/"
        node["file"] = vr_prefix + f"_section_{slug}.md"
        count += 1

    return count


# Map of TOC node titles to external URL templates.
# {version} is replaced with the product version in dot-to-dash form (e.g. "6-2-3").
# All languages use the same English URL (en/us).
_EXTERNAL_TOC_URLS: dict[str, str] = {
    "Java API": "https://stg-docs.onebx.com/us/en/ebx/resources/javadocs/{version}/",
    "Java API (英語)": "https://stg-docs.onebx.com/us/en/ebx/resources/javadocs/{version}/",
}


def _slugify(title: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-")


def inject_external_urls(nodes: list, version_dashed: str) -> int:
    """
    Walk the TOC tree and assign external URLs to known placeholder nodes
    (nodes that have no file and no children but correspond to off-site content).
    Returns the number of nodes patched.
    """
    count = 0
    for node in nodes:
        if node.get("children"):
            count += inject_external_urls(node["children"], version_dashed)
        if not node.get("file") and not node.get("children"):
            template = _EXTERNAL_TOC_URLS.get(node["title"])
            if template:
                node["file"] = template.format(version=version_dashed)
                count += 1
    return count


def create_external_url_pages(
    nodes: list,
    version_root: str,
    output_dir: Path,
    meta: dict,
    dry_run: bool = False,
) -> int:
    """
    For each TOC node whose file is an external URL (set by inject_external_urls),
    write a synthetic .md that links to it and replace node["file"] with the .md path.

    This allows toc.yml to emit a relative url: instead of an absolute https:// value,
    which is required by AEM Guides.
    Returns the number of pages created.
    """
    count = 0
    vr_prefix = version_root.replace("\\", "/").rstrip("/") + "/"

    for node in nodes:
        if node.get("children"):
            count += create_external_url_pages(
                node["children"], version_root, output_dir, meta, dry_run
            )
        file_val = node.get("file") or ""
        if not file_val.startswith(("http://", "https://")):
            continue

        title      = node["title"]
        page_title = f"{title} Reference"
        filename   = f"{_slugify(page_title)}.md"
        ext_url    = file_val

        product_name    = meta.get("product_name", "")
        product_version = meta.get("product_version", "")
        link_text = (f"{product_name} {product_version} {title}"
                     if product_name else title)

        frontmatter = {
            "title": page_title,
            "source_url": "",
            "lang": meta.get("lang", "en-us"),
            "topic_type": "",
            "toc_path": "",
            "product_name": product_name,
            "product_version": product_version,
        }
        fm_str = yaml.dump(frontmatter, default_flow_style=False,
                           allow_unicode=True, sort_keys=False)
        body = f"# {page_title}\n\n- [{link_text}]({ext_url})\n"
        file_content = f"---\n{fm_str}---\n\n{body}"

        out_path = output_dir / version_root / filename
        if not dry_run:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(file_content, encoding="utf-8")

        node["file"] = vr_prefix + filename
        count += 1

    return count


def collect_versions(manifest: list[dict]) -> dict[str, list[dict]]:
    """Group manifest entries by version_html_root."""
    versions: dict[str, list[dict]] = defaultdict(list)
    for entry in manifest:
        fmt  = entry.get("version_format", "")
        root = version_html_root(entry["output_path"], fmt)
        versions[root].append(entry)
    return dict(versions)


# ---------------------------------------------------------------------------
# What's New generation
# ---------------------------------------------------------------------------

_TOKEN_RE   = re.compile(r"\[%=[\w.\s]+%\]")
_HTM_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")


def _rewrite_whats_new_links(md_text: str) -> str:
    """Rewrite links from _templates/ context to version-root-relative .md paths."""
    def replace(m):
        text, url = m.group(1), m.group(2)
        if url.startswith(("http://", "https://")):
            return m.group(0)
        if url.startswith("../"):
            url = url[3:]  # strip ../ — now version-root-relative
        elif "/" not in url:
            return text    # same-dir link (e.g. Whats-New.htm) — drop href, keep text
        url = re.sub(r"\.html?(\#.*)?$", lambda e: ".md" + (e.group(1) or ""), url)
        return f"[{text}]({url})"
    return _HTM_LINK_RE.sub(replace, md_text)


def generate_whats_new(
    version_root: str,
    cache_dir: Path,
    output_dir: Path,
    meta: dict,
    base_url: str = "https://docs.tibco.com",
    dry_run: bool = False,
) -> dict | None:
    """
    Extract What's New content from _templates/ and write Whats-New.md at the
    version root.  Returns a TOC node dict on success, None if no content found.

    Source preference:
      1. _templates/Whats-New.htm  — full page with detailed descriptions
      2. _templates/Home.htm       — Key New Features dropdown (brief fallback)
      3. Neither found             — returns None, no file written
    """
    templates_dir = cache_dir / version_root / "_templates"
    whats_new_htm = templates_dir / "Whats-New.htm"
    home_htm      = templates_dir / "Home.htm"

    html_text     = None
    source_url    = None
    use_full_page = False

    vr_for_url = version_root.replace("\\", "/").rstrip("/")
    if whats_new_htm.exists():
        html_text     = whats_new_htm.read_text(encoding="utf-8", errors="replace")
        source_url    = f"{base_url}/{vr_for_url}/_templates/Whats-New.htm"
        use_full_page = True
    elif home_htm.exists():
        html_text  = home_htm.read_text(encoding="utf-8", errors="replace")
        source_url = f"{base_url}/{vr_for_url}/_templates/Home.htm"
        use_full_page = False
    else:
        return None

    soup = BeautifulSoup(html_text, "lxml")

    if use_full_page:
        content = soup.find("div", attrs={"role": "main", "id": "mc-main-content"})
        if not content:
            return None
        for sel in ["#prdnm", ".toolbar", ".breadcrumbs", "#feedback-survey",
                    ".MCWebHelpFramesetLink", ".Copyright"]:
            for el in content.select(sel):
                el.decompose()
    else:
        whats_new_div = soup.find("div", class_="dropDownwhats-new")
        if not whats_new_div:
            return None
        body = whats_new_div.find("div", class_="MCDropDownBody")
        if not body:
            return None
        content = soup.new_tag("div")
        h1 = soup.new_tag("h1")
        h1.string = "What's New"
        content.append(h1)
        content.extend(list(body.children))

    md_body = md(str(content), heading_style="ATX", bullets="-")
    md_body = _TOKEN_RE.sub("", md_body)
    md_body = _rewrite_whats_new_links(md_body)
    md_body = md_body.strip()

    if not md_body:
        return None

    frontmatter = {
        "title": "What's New",
        "source_url": source_url,
        "lang": meta.get("lang", "en-us"),
        "topic_type": "",
        "toc_path": "",
        "product_name": meta.get("product_name", ""),
        "product_version": meta.get("product_version", ""),
    }
    fm_str       = yaml.dump(
        frontmatter, default_flow_style=False, allow_unicode=True, sort_keys=False
    )
    file_content = f"---\n{fm_str}---\n\n{md_body}\n"

    out_path = output_dir / version_root / "Whats-New.md"
    if not dry_run:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(file_content, encoding="utf-8")

    # Use the same path convention as section pages: version_root prefix + filename,
    # without the output_dir prefix so node_to_yaml strips it correctly.
    vr_prefix = version_root.replace("\\", "/").rstrip("/") + "/"
    return {"title": "What's New", "file": vr_prefix + "Whats-New.md", "children": []}


def main():
    parser = argparse.ArgumentParser(description="Step 6: Build TOC JSON per version")
    parser.add_argument("--phase",   required=False)
    parser.add_argument("--config",  default="config/settings.yaml")
    parser.add_argument("--dry-run",     action="store_true")
    parser.add_argument("--force-rerun", action="store_true", help="Accepted for orchestrator compat")
    parser.add_argument("--from-json", metavar="PATH", help="Convert a single _toc.json to toc.yml and exit")
    args = parser.parse_args()

    if args.from_json:
        json_path = Path(args.from_json)
        toc = json.loads(json_path.read_text(encoding="utf-8"))
        yml_path = json_path.parent / "toc.yml"
        yml_path.write_text(
            yaml.dump(
                toc_to_yaml(toc),
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        print(f"Written: {yml_path}")
        return 0

    if not args.phase:
        parser.error("--phase is required when --from-json is not specified")

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

        # Generate synthetic index pages for url-less structural TOC nodes.
        # Recurses bottom-up so child sections have files before parents render them.
        meta = _version_meta(entries, output_dir)
        version_dir = output_dir / version_root
        n_generated = generate_section_pages(
            toc["tree"], version_root, version_dir, meta, dry_run=args.dry_run
        )
        if n_generated:
            reporter.info(f"  Generated {n_generated} section index page(s) for {version_root}")
            reporter.count("section_pages_generated", n_generated)

        # Inject external URLs for known off-site placeholder nodes (e.g. Java API).
        version_dashed = meta.get("product_version", "").replace(".", "-")
        n_external = inject_external_urls(toc["tree"], version_dashed)
        if n_external:
            reporter.info(f"  Injected {n_external} external URL(s) for {version_root}")
            reporter.count("external_urls_injected", n_external)

        # Create synthetic .md pages for any external-URL nodes, replacing the URL with
        # a relative path so toc.yml emits a relative url: (required by AEM Guides).
        n_ext_pages = create_external_url_pages(
            toc["tree"], version_root, output_dir, meta, dry_run=args.dry_run
        )
        if n_ext_pages:
            reporter.count("external_url_pages_created", n_ext_pages)
            reporter.info(f"  Created {n_ext_pages} external-URL page(s) for {version_root}")

        # Generate What's New page from _templates/Whats-New.htm (or Home.htm fallback).
        whats_new_node = generate_whats_new(
            version_root, cache_dir, output_dir, meta,
            base_url=settings.get("base_url", "https://docs.tibco.com"),
            dry_run=args.dry_run,
        )
        if whats_new_node:
            toc["tree"].insert(0, whats_new_node)
            reporter.count("whats_new_pages_generated")
            reporter.info(f"  Generated Whats-New.md for {version_root}")

        toc_path = output_dir / version_root / "_toc.json"

        if not args.dry_run:
            toc_path.parent.mkdir(parents=True, exist_ok=True)
            toc_path.write_text(
                json.dumps(toc, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            toc_yml_path = output_dir / version_root / "toc.yml"
            toc_yml_path.write_text(
                yaml.dump(
                    toc_to_yaml(toc),
                    default_flow_style=False,
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
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
