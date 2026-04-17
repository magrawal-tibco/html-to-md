"""
compare_toc.py — Compare Step 6 reconstructed _toc.json against authoritative MadCap TOC JS files.

Usage:
  python scripts/compare_toc.py \
    --toc-js-dir "c:/github/businessevents-enterprise-edition-userdocs/Output/Mayur_Agrawal/html/Data/Tocs" \
    --toc-json   "output/pub/businessevents-enterprise/6.4.0/doc/html/_toc.json"
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.lib.toc_parser import (
    parse_chunk_files,
    parse_toc_tree,
    flatten_madcap,
    _norm_url,
)


# ---------------------------------------------------------------------------
# Flattening (Step 6 side)
# ---------------------------------------------------------------------------

def flatten_step6(tree: list, root: str, depth: int = 0) -> list:
    """
    DFS-walk a Step 6 _toc.json tree and produce a flat ordered list.
    Converts .md file paths → relative .htm URLs for cross-comparison.
    """
    root_norm = root.replace("\\", "/").rstrip("/") + "/"
    result = []

    def _walk(nodes, depth):
        for node in nodes:
            file_raw = node.get("file")
            if file_raw:
                file_fwd = file_raw.replace("\\", "/")
                # Strip the version root prefix to get relative path
                rel = file_fwd[len(root_norm):] if file_fwd.startswith(root_norm) else file_fwd
                # Convert .md → .htm
                htm = Path(rel).with_suffix(".htm").as_posix()
                url = "/" + htm
            else:
                url = None

            result.append({
                "title": node.get("title", ""),
                "url": url,
                "norm_url": _norm_url(url) if url else None,
                "depth": depth,
                "section_only": url is None,
            })
            children = node.get("children", [])
            if children:
                _walk(children, depth + 1)

    _walk(tree, depth)
    return result


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def compare(madcap_flat: list, step6_flat: list) -> dict:
    """Compare two flat DFS lists. Returns a results dict."""
    madcap_pages   = [n for n in madcap_flat  if not n["section_only"]]
    step6_pages    = [n for n in step6_flat   if not n["section_only"]]
    madcap_section = [n for n in madcap_flat  if n["section_only"]]

    madcap_urls = {n["norm_url"]: i for i, n in enumerate(madcap_pages)}
    step6_urls  = {n["norm_url"]: i for i, n in enumerate(step6_pages)}

    matched_urls = set(madcap_urls) & set(step6_urls)
    missing      = [n for n in madcap_pages if n["norm_url"] not in step6_urls]
    extra        = [n for n in step6_pages  if n["norm_url"] not in madcap_urls]

    # Depth accuracy — nodes at correct nesting depth
    wrong_depth = []
    for n in madcap_pages:
        nu = n["norm_url"]
        if nu not in step6_urls:
            continue
        s6_node = step6_pages[step6_urls[nu]]
        if n["depth"] != s6_node["depth"]:
            wrong_depth.append({
                "url": n["url"],
                "title": n["title"],
                "expected_depth": n["depth"],
                "got_depth": s6_node["depth"],
            })

    # Sequence accuracy — % of matched pairs in correct relative order
    matched_sorted = sorted(matched_urls, key=lambda u: madcap_urls[u])
    madcap_rank = {u: i for i, u in enumerate(matched_sorted)}
    step6_rank  = {u: step6_urls[u] for u in matched_sorted}

    # Convert step6 positions to rank within matched set
    step6_matched_order = sorted(matched_urls, key=lambda u: step6_urls[u])
    step6_rank = {u: i for i, u in enumerate(step6_matched_order)}

    concordant = discordant = 0
    matched_list = list(matched_urls)
    for i in range(len(matched_list)):
        for j in range(i + 1, len(matched_list)):
            a, b = matched_list[i], matched_list[j]
            m_order = madcap_rank[a] < madcap_rank[b]
            s_order = step6_rank[a]  < step6_rank[b]
            if m_order == s_order:
                concordant += 1
            else:
                discordant += 1

    total_pairs = concordant + discordant
    seq_accuracy = concordant / total_pairs if total_pairs else 1.0

    # Worst sequence mismatches (largest position delta)
    deltas = []
    for u in matched_urls:
        delta = abs(madcap_rank[u] - step6_rank[u])
        if delta > 0:
            node = madcap_pages[madcap_urls[u]]
            deltas.append({"url": node["url"], "title": node["title"],
                           "madcap_pos": madcap_rank[u], "step6_pos": step6_rank[u],
                           "delta": delta})
    deltas.sort(key=lambda x: -x["delta"])

    return {
        "madcap_total":    len(madcap_flat),
        "madcap_pages":    len(madcap_pages),
        "madcap_sections": len(madcap_section),
        "step6_total":     len(step6_flat),
        "step6_pages":     len(step6_pages),
        "matched":         len(matched_urls),
        "missing":         missing,
        "extra":           extra,
        "wrong_depth":     wrong_depth,
        "seq_accuracy":    seq_accuracy,
        "seq_pairs":       total_pairs,
        "worst_seq":       deltas[:15],
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_report(r: dict, madcap_version: str, step6_version: str):
    p = print
    p("\n=== TOC Comparison Report ===")
    p(f"Authoritative (MadCap):  {r['madcap_total']:>5} nodes  "
      f"({r['madcap_pages']} pages + {r['madcap_sections']} section-only)")
    p(f"Step 6 reconstructed:    {r['step6_total']:>5} nodes  ({r['step6_pages']} pages)")
    p()
    matched_pct = r["matched"] / r["madcap_pages"] * 100 if r["madcap_pages"] else 0
    p(f"Matched pages:           {r['matched']:>5} / {r['madcap_pages']}  ({matched_pct:.1f}%)")
    p(f"Missing from Step 6:     {len(r['missing']):>5}")
    p(f"Extra in Step 6:         {len(r['extra']):>5}  (orphan/inferred)")
    p()
    depth_ok = r["step6_pages"] - len(r["wrong_depth"])
    depth_pct = depth_ok / r["step6_pages"] * 100 if r["step6_pages"] else 0
    p(f"Depth accuracy:          {depth_pct:>5.1f}%  ({len(r['wrong_depth'])} nodes at wrong depth)")
    p(f"Sequence accuracy:       {r['seq_accuracy']*100:>5.1f}%  "
      f"(pairs in correct relative order, n={r['seq_pairs']})")

    if r["missing"]:
        p(f"\n--- Missing from Step 6 (first 20 of {len(r['missing'])}) ---")
        for n in r["missing"][:20]:
            p(f"  {n['url']:<60}  \"{n['title']}\"")

    if r["extra"]:
        p(f"\n--- Extra in Step 6 (first 20 of {len(r['extra'])}) ---")
        for n in r["extra"][:20]:
            p(f"  {n['url']:<60}  \"{n['title']}\"")

    if r["wrong_depth"]:
        p(f"\n--- Wrong nesting depth (first 20 of {len(r['wrong_depth'])}) ---")
        for n in r["wrong_depth"][:20]:
            p(f"  depth expected={n['expected_depth']} got={n['got_depth']}  "
              f"{n['url']}  \"{n['title']}\"")

    if r["worst_seq"]:
        p(f"\n--- Largest sequence mismatches (top {len(r['worst_seq'])}) ---")
        p(f"  {'MadCap':>8}  {'Step6':>8}  {'Delta':>6}  URL")
        for x in r["worst_seq"]:
            p(f"  {x['madcap_pos']:>8}  {x['step6_pos']:>8}  {x['delta']:>6}  {x['url']}")

    p()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Compare Step 6 _toc.json vs MadCap TOC JS")
    parser.add_argument("--toc-js-dir", required=True,
                        help="Directory containing _HTML_Doc_Set.js and chunk files")
    parser.add_argument("--toc-json", required=True,
                        help="Path to Step 6 _toc.json")
    args = parser.parse_args()

    toc_js_dir = Path(args.toc_js_dir)
    toc_json_path = Path(args.toc_json)

    if not toc_js_dir.exists():
        print(f"ERROR: --toc-js-dir not found: {toc_js_dir}", file=sys.stderr)
        return 1
    if not toc_json_path.exists():
        print(f"ERROR: --toc-json not found: {toc_json_path}", file=sys.stderr)
        return 1

    print("Parsing MadCap chunk files...")
    id_to_page = parse_chunk_files(toc_js_dir)
    print(f"  {len(id_to_page)} pages indexed")

    print("Parsing MadCap tree...")
    tree_nodes = parse_toc_tree(toc_js_dir)
    madcap_flat = flatten_madcap(tree_nodes, id_to_page)
    print(f"  {len(madcap_flat)} total nodes flattened")

    print("Loading Step 6 _toc.json...")
    toc_data = json.loads(toc_json_path.read_text(encoding="utf-8"))
    step6_version = toc_data.get("version", str(toc_json_path))
    root = toc_data.get("root", "")
    step6_flat = flatten_step6(toc_data.get("tree", []), root)
    print(f"  {len(step6_flat)} total nodes flattened")

    print("Comparing...")
    results = compare(madcap_flat, step6_flat)
    print_report(results, madcap_version="MadCap", step6_version=step6_version)

    return 0


if __name__ == "__main__":
    sys.exit(main())
