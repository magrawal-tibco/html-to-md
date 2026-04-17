"""
toc_parser.py — Parse MadCap Flare WebHelp2 TOC JavaScript files.

MadCap publishes two files per doc set:
  _HTML_Doc_Set.js       — tree of integer node IDs (hierarchy + sequence)
  _HTML_Doc_Set_Chunk*.js — maps URL paths to {id, title} (data per node)

These files use AMD define({...}) wrappers with unquoted JS keys — not valid
JSON — so they require custom parsing.

Exports used by:
  scripts/06_build_toc.py  — authoritative TOC reconstruction
  scripts/compare_toc.py   — comparison / diagnostic
"""

import json
import re
from pathlib import Path


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_balanced(s: str, start: int, open_ch: str = "[", close_ch: str = "]") -> str:
    """Extract a balanced bracket substring starting at position `start`."""
    depth = 0
    for i in range(start, len(s)):
        if s[i] == open_ch:
            depth += 1
        elif s[i] == close_ch:
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return s[start:]


def _norm_url(url: str) -> str:
    """Normalise a URL for comparison: lowercase, forward slashes, no leading slash."""
    return url.replace("\\", "/").lstrip("/").lower()


# ---------------------------------------------------------------------------
# Public parsing functions
# ---------------------------------------------------------------------------

def parse_chunk_files(toc_js_dir: Path) -> dict[int, dict]:
    """
    Parse all *Chunk*.js files in toc_js_dir.
    Returns id_to_page: {node_id: {"url": str, "title": str}}

    Format: define({'/path/to/file.htm':{i:[N],t:['Title'],b:['']}, ...})
    """
    id_to_page: dict[int, dict] = {}
    pattern = re.compile(
        r"'(/[^']+\.htm)'\s*:\s*\{i:\[(\d+)\],t:\['(.*?)'\]",
        re.DOTALL,
    )
    for chunk_file in sorted(toc_js_dir.glob("*Chunk*.js")):
        content = chunk_file.read_text(encoding="utf-8", errors="replace")
        for url, node_id_str, title in pattern.findall(content):
            node_id = int(node_id_str)
            title = title.replace("\\'", "'")
            id_to_page[node_id] = {"url": url, "title": title}
    return id_to_page


def parse_toc_tree(toc_js_dir: Path) -> list:
    """
    Parse the main _HTML_Doc_Set.js (non-chunk) file.
    Returns the root-level children list: [{i, c, n?}, ...]

    Format: define({..., tree:{n:[{i:N,c:M,n:[...]}, ...]}, ...})
    """
    candidates = [
        f for f in toc_js_dir.glob("*.js")
        if "chunk" not in f.name.lower() and "Chunk" not in f.name
    ]
    if not candidates:
        raise FileNotFoundError(f"No main TOC JS file found in {toc_js_dir}")
    toc_file = candidates[0]
    content = toc_file.read_text(encoding="utf-8", errors="replace")

    marker = "tree:{n:["
    idx = content.find(marker)
    if idx == -1:
        raise ValueError(f"Could not find 'tree:{{n:[' in {toc_file}")
    arr_start = idx + len(marker) - 1  # position of the opening [

    tree_arr_str = _extract_balanced(content, arr_start)

    # Quote single-letter keys i, c, n before JSON parsing
    tree_json = re.sub(r'([{,\[])([icn]):', r'\1"\2":', tree_arr_str)
    return json.loads(tree_json)


# ---------------------------------------------------------------------------
# Flattening (used by compare_toc.py)
# ---------------------------------------------------------------------------

def flatten_madcap(nodes: list, id_to_page: dict, depth: int = 0) -> list:
    """
    DFS-walk the parsed MadCap tree and produce a flat ordered list.
    Returns [{"title", "url", "norm_url", "depth", "section_only"}, ...]
    Section-only nodes are those present in the tree but absent from the chunk.
    """
    result = []
    for node in nodes:
        node_id  = node["i"]
        children = node.get("n", [])
        page = id_to_page.get(node_id)
        if page:
            url   = page["url"]
            title = page["title"]
            section_only = False
        else:
            url   = None
            title = f"<section:{node_id}>"
            section_only = True

        result.append({
            "title":        title,
            "url":          url,
            "norm_url":     _norm_url(url) if url else None,
            "depth":        depth,
            "section_only": section_only,
        })
        if children:
            result.extend(flatten_madcap(children, id_to_page, depth + 1))
    return result


# ---------------------------------------------------------------------------
# _toc.json tree building (used by 06_build_toc.py)
# ---------------------------------------------------------------------------

def _build_node(
    node: dict,
    id_to_page: dict,
    url_to_output: dict[str, str],
) -> dict | None:
    """
    Recursively convert a MadCap tree node to a _toc.json node.
    Returns None only for section-only nodes with no resolvable children.
    url_to_output: normalised relative .htm URL → output_path (.md)
    """
    node_id  = node["i"]
    children = node.get("n", [])

    child_nodes = []
    for child in children:
        result = _build_node(child, id_to_page, url_to_output)
        if result is not None:
            child_nodes.append(result)

    page = id_to_page.get(node_id)
    if page:
        title      = page["title"]
        rel_url    = page["url"].lstrip("/")          # e.g. Administration/file.htm
        output_path = url_to_output.get(rel_url.lower())
    else:
        # Section-only node — no .htm file
        title       = None   # caller fills in from section header heuristic
        output_path = None

    # Section with no children and no file → prune
    if output_path is None and not child_nodes:
        return None

    if page and title:
        return {"title": title, "file": output_path, "children": child_nodes}
    else:
        # Section node: title unknown from chunk — use first child's prefix or leave as id
        sec_title = f"Section {node_id}"
        return {"title": sec_title, "file": None, "children": child_nodes}


def build_toc_tree_from_js(
    toc_js_dir: Path,
    version_root: str,
    version_entries: list[dict],
) -> tuple[list, list]:
    """
    Build a _toc.json-compatible tree from MadCap TOC JS files.

    Returns (tree, orphans) where:
      tree    — list of root-level _toc.json nodes
      orphans — list of output_paths not found in MadCap tree
    """
    id_to_page  = parse_chunk_files(toc_js_dir)
    tree_nodes  = parse_toc_tree(toc_js_dir)

    # Build url → output_path lookup from version_entries
    root_fwd = version_root.replace("\\", "/").rstrip("/") + "/"
    url_to_output: dict[str, str] = {}
    for entry in version_entries:
        op_fwd = entry["output_path"].replace("\\", "/")
        rel    = op_fwd[len(root_fwd):] if op_fwd.startswith(root_fwd) else op_fwd
        htm    = Path(rel).with_suffix(".htm").as_posix()
        url_to_output[htm.lower()] = entry["output_path"]

    # Build tree
    tree = []
    found_output_paths: set[str] = set()

    def _collect_found(nodes: list):
        for n in nodes:
            if n.get("file"):
                found_output_paths.add(n["file"])
            _collect_found(n.get("children", []))

    for node in tree_nodes:
        built = _build_node(node, id_to_page, url_to_output)
        if built is not None:
            tree.append(built)

    _collect_found(tree)

    # Orphans: entries from version_entries not placed in the tree
    all_output_paths = {e["output_path"] for e in version_entries}
    orphan_paths = sorted(all_output_paths - found_output_paths)

    return tree, orphan_paths
