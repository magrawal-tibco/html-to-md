"""
scripts/webworks/build_csh_maps.py — Build CSH maps for WebWorks ePublisher docs.

CTX files live at: <version_html_root>/ctx/<guide_name><numeric_id>.htm
Each contains a JS redirect: ctx = "..?context=<guide>&topic=<topic_id>";

Topic IDs are resolved to .htm filenames via: wwhdata/xml/files.xml per guide.

Outputs per guide:
  output/.../csh_map.json  — {csh_id: {md_path, topic_name}}
Also injects csh_ids + csh_names into frontmatter of the relevant .md files.

Usage:
  python scripts/webworks/build_csh_maps.py --phase bw
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
from webworks.utils import discover_webworks_versions

_CTX_RE = re.compile(
    r'ctx\s*=\s*"[^"]*\?context=([^&"]+)&topic=([^;"]+)"',
    re.IGNORECASE,
)



def _read_topic_map(guide_dir: Path) -> dict[str, str]:
    """Return {topic_id: href} from wwhdata/xml/files.xml."""
    files_xml = guide_dir / "wwhdata" / "xml" / "files.xml"
    if not files_xml.exists():
        return {}
    soup = BeautifulSoup(
        files_xml.read_text(encoding="utf-8", errors="replace"), "xml"
    )
    return {
        t["name"]: t["href"]
        for t in soup.find_all("Topic")
        if t.get("name") and t.get("href")
    }


def _parse_ctx_files(ctx_dir: Path) -> list[tuple[int, str, str]]:
    """Return [(csh_id, context_name, topic_id), ...] from ctx/*.htm files."""
    results = []
    for f in sorted(ctx_dir.glob("*.htm")):
        # Numeric suffix in filename = CSH ID
        m = re.search(r"(\d+)\.htm$", f.name, re.IGNORECASE)
        if not m:
            continue
        csh_id = int(m.group(1))
        raw = f.read_text(encoding="utf-8", errors="replace")
        match = _CTX_RE.search(raw)
        if match:
            context = match.group(1).strip()
            topic   = match.group(2).strip()
            results.append((csh_id, context, topic))
    return results


def _inject_frontmatter(md_path: Path, csh_id: int, topic_name: str) -> bool:
    """Inject csh_ids and csh_names into an existing .md file's frontmatter."""
    if not md_path.exists():
        return False
    text = md_path.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---"):
        return False

    end = text.find("---", 3)
    if end == -1:
        return False

    fm_text = text[3:end]
    try:
        fm = yaml.safe_load(fm_text) or {}
    except Exception:
        return False

    ids   = fm.get("csh_ids", [])
    names = fm.get("csh_names", [])

    if csh_id not in ids:
        ids.append(csh_id)
    if topic_name not in names:
        names.append(topic_name)

    fm["csh_ids"]   = ids
    fm["csh_names"] = names

    new_fm = "---\n" + yaml.dump(fm, allow_unicode=True, sort_keys=False) + "---\n"
    md_path.write_text(new_fm + text[end + 3:], encoding="utf-8")
    return True


def main():
    parser = argparse.ArgumentParser(description="Build WebWorks CSH maps")
    parser.add_argument("--phase",       required=True)
    parser.add_argument("--config",      default="config/settings.yaml")
    parser.add_argument("--dry-run",     action="store_true")
    parser.add_argument("--force-rerun", action="store_true")
    args = parser.parse_args()

    settings   = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    cache_dir  = Path(settings.get("cache_dir", "cache"))
    output_dir = Path(settings.get("output_dir", "output"))
    logs_dir   = Path(settings.get("logs_dir", "logs"))

    run_dir  = logs_dir / args.phase / datetime.now().strftime("%Y%m%d-%H%M%S")
    reporter = Reporter(run_dir, "webworks_csh", dry_run=args.dry_run)
    reporter.info(f"=== WebWorks CSH Maps | phase={args.phase} ===")

    for version_html_root, product_slug, version in discover_webworks_versions(cache_dir):
        reporter.info(f"CSH: {product_slug} {version}")
        ctx_dir = version_html_root / "ctx"
        if not ctx_dir.exists():
            reporter.info(f"  No ctx/ dir — skipping.")
            continue

        ctx_entries = _parse_ctx_files(ctx_dir)
        if not ctx_entries:
            reporter.info(f"  No ctx entries found.")
            continue

        # Build topic map per guide
        guide_topic_maps: dict[str, dict[str, str]] = {}
        for guide_dir in version_html_root.iterdir():
            if not guide_dir.is_dir():
                continue
            tm = _read_topic_map(guide_dir)
            if tm:
                guide_topic_maps[guide_dir.name] = tm

        # Resolve each ctx entry to an output .md path
        csh_map: dict[int, dict] = {}
        for csh_id, context, topic_id in ctx_entries:
            topic_map = guide_topic_maps.get(context, {})
            href = topic_map.get(topic_id, "")
            if not href:
                reporter.count("csh_unresolved")
                continue

            # href may be "admin.4.02.htm" or "admin.4.02.htm#1674616"
            if "#" in href:
                htm_rel, anchor = href.split("#", 1)
            else:
                htm_rel, anchor = href, ""

            guide_dir = version_html_root / context
            htm_path  = guide_dir / htm_rel
            rel_to_cache = htm_path.relative_to(cache_dir)
            md_path = output_dir / rel_to_cache.with_suffix(".md")

            csh_map[csh_id] = {
                "md_path":    md_path.as_posix(),
                "anchor":     anchor,
                "topic_name": topic_id,
            }

            if not args.dry_run:
                _inject_frontmatter(md_path, csh_id, topic_id)
                reporter.count("csh_injected")

        # Write csh_map.json alongside the guide dirs
        rel_version = version_html_root.relative_to(cache_dir)
        out_csh = output_dir / rel_version / "csh_map.json"

        if not args.dry_run and csh_map:
            out_csh.parent.mkdir(parents=True, exist_ok=True)
            serializable = {str(k): v for k, v in sorted(csh_map.items())}
            out_csh.write_text(
                json.dumps(serializable, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            reporter.info(f"  Wrote csh_map.json: {len(csh_map)} entries")
            reporter.count("csh_map_written")

    reporter.finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
