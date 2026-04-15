"""
04_build_csh_maps.py — Step 4: Parse alias.xml and build CSH maps.

For each product version in the manifest:
  1. Read the cached alias.xml (downloaded in Step 2)
  2. Parse <Map Name="TOPIC_ID" Link="relative.htm" ResolvedId="1000"/>
  3. Write csh_map.json alongside the version's output files
  4. Inject csh_ids and csh_names into matching .md file frontmatter

Empty alias.xml (<CatapultAliasFile />) and missing files are silently skipped.

Usage:
  python scripts/04_build_csh_maps.py --phase phase_01 [--config config/settings.yaml] [--dry-run]
"""

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

import yaml
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.lib.reporter import Reporter


def load_settings(config_path: str) -> dict:
    return yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))


def load_manifest(phase: str, settings: dict) -> list[dict]:
    manifests_dir = Path(settings.get("manifests_dir", "manifests"))
    path = manifests_dir / f"manifest_{phase}.json"
    if not path.exists():
        raise FileNotFoundError(f"Manifest not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def url_to_cache_path(loc: str, cache_dir: Path) -> Path:
    path = urlparse(loc).path.lstrip("/")
    return cache_dir / path


def parse_alias_xml(xml_path: Path) -> list[dict]:
    """
    Parse a CatapultAliasFile and return list of:
      { "name": str, "resolved_id": int, "link": str }
    Returns empty list for empty or malformed files.
    """
    try:
        content = xml_path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return []

    if not content or "<Map " not in content:
        return []

    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return []

    entries = []
    for map_el in root.iter("Map"):
        name = map_el.get("Name", "").strip()
        link = map_el.get("Link", "").strip()
        resolved_id_str = map_el.get("ResolvedId", "").strip()
        if not name or not link:
            continue
        try:
            resolved_id = int(resolved_id_str)
        except (ValueError, TypeError):
            resolved_id = None
        entries.append({"name": name, "resolved_id": resolved_id, "link": link})
    return entries


def read_frontmatter(md_path: Path) -> tuple[dict, str]:
    """
    Read a .md file and return (frontmatter_dict, body_text).
    If no frontmatter, returns ({}, full_content).
    """
    content = md_path.read_text(encoding="utf-8")
    if not content.startswith("---"):
        return {}, content
    end = content.find("\n---\n", 3)
    if end == -1:
        return {}, content
    fm_text = content[3:end]
    body    = content[end + 5:]
    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        fm = {}
    return fm, body


def write_frontmatter(md_path: Path, fm: dict, body: str):
    """Overwrite a .md file with updated frontmatter + body."""
    fm_text = yaml.dump(fm, allow_unicode=True, default_flow_style=False)
    md_path.write_text(f"---\n{fm_text}---\n\n{body.lstrip()}", encoding="utf-8")


def collect_versions(manifest: list[dict]) -> dict[str, dict]:
    """
    Group manifest entries by alias_xml_url.
    Returns { alias_xml_url: { "alias_xml_url": str, "entries": [manifest_entry] } }
    """
    versions: dict[str, dict] = {}
    for entry in manifest:
        au = entry.get("alias_xml_url")
        if not au:
            continue
        if au not in versions:
            versions[au] = {"alias_xml_url": au, "entries": []}
        versions[au]["entries"].append(entry)
    return versions


def process_version(
    alias_xml_url: str,
    version_entries: list[dict],
    cache_dir: Path,
    output_dir: Path,
    reporter: Reporter,
    dry_run: bool,
):
    """Process one product version: read alias.xml, write csh_map.json, update frontmatter."""
    alias_cache = url_to_cache_path(alias_xml_url, cache_dir)

    if not alias_cache.exists():
        reporter.skip(alias_xml_url, "alias-xml-not-cached")
        reporter.count("versions_no_alias")
        return

    maps = parse_alias_xml(alias_cache)
    if not maps:
        reporter.count("versions_alias_empty")
        return

    reporter.count("versions_with_csh")

    # Build lookup: normalised link → list of {name, resolved_id}
    # alias.xml Link is relative to the /doc/html/ root of the version
    link_to_csh: dict[str, list[dict]] = {}
    for m in maps:
        norm_link = m["link"].replace("\\", "/").lstrip("./")
        if norm_link not in link_to_csh:
            link_to_csh[norm_link] = []
        link_to_csh[norm_link].append({"name": m["name"], "resolved_id": m["resolved_id"]})

    # Build csh_map.json keyed by resolved_id
    csh_map: dict[str, dict] = {}
    for m in maps:
        if m["resolved_id"] is not None:
            csh_map[str(m["resolved_id"])] = {
                "name": m["name"],
                "file": Path(m["link"].replace("\\", "/")).with_suffix(".md").as_posix(),
            }

    # Determine output root for this version from the first entry's output_path
    # e.g. pub/foo/1.0/doc/html/Admin/file.md → pub/foo/1.0/doc/html/
    if not version_entries:
        return
    # Normalise to forward slashes for cross-platform path matching
    sample_output = version_entries[0]["output_path"].replace("\\", "/")
    # Find /doc/html/ segment
    marker = "/doc/html/"
    idx = sample_output.find(marker)
    if idx != -1:
        version_html_root = sample_output[: idx + len(marker)]
    else:
        version_html_root = str(Path(sample_output).parent).replace("\\", "/") + "/"

    csh_map_path = output_dir / version_html_root / "csh_map.json"

    if not dry_run:
        csh_map_path.parent.mkdir(parents=True, exist_ok=True)
        csh_map_path.write_text(
            json.dumps(csh_map, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    reporter.count("csh_maps_written")
    reporter.count("csh_ids_total", len(csh_map))

    # Inject csh_ids / csh_names into matching .md frontmatter
    for entry in version_entries:
        out_path = output_dir / entry["output_path"]
        if not out_path.exists():
            continue

        # Normalise output_path relative to version_html_root to match alias link
        rel_to_html = entry["output_path"][len(version_html_root):]
        # Convert .md → .htm for lookup
        rel_htm = str(Path(rel_to_html).with_suffix(".htm"))

        matched = link_to_csh.get(rel_htm) or link_to_csh.get(rel_htm.lower())
        if not matched:
            continue

        csh_ids   = [m["resolved_id"] for m in matched if m["resolved_id"] is not None]
        csh_names = [m["name"] for m in matched]

        if not csh_ids and not csh_names:
            continue

        if not dry_run:
            fm, body = read_frontmatter(out_path)
            if csh_ids:
                fm["csh_ids"] = csh_ids
            if csh_names:
                fm["csh_names"] = csh_names
            write_frontmatter(out_path, fm, body)

        reporter.count("pages_csh_injected")


def main():
    parser = argparse.ArgumentParser(description="Step 4: Build CSH maps from alias.xml")
    parser.add_argument("--phase",   required=True)
    parser.add_argument("--config",  default="config/settings.yaml")
    parser.add_argument("--dry-run",     action="store_true")
    parser.add_argument("--force-rerun", action="store_true", help="Accepted for orchestrator compat")
    args = parser.parse_args()

    settings   = load_settings(args.config)
    manifest   = load_manifest(args.phase, settings)
    cache_dir  = Path(settings.get("cache_dir", "cache"))
    output_dir = Path(settings.get("output_dir", "output"))

    from datetime import datetime
    logs_dir = Path(settings.get("logs_dir", "logs"))
    run_dir  = logs_dir / args.phase / datetime.now().strftime("%Y%m%d-%H%M%S")
    reporter = Reporter(run_dir, "04_csh", dry_run=args.dry_run)

    reporter.info(f"=== Step 4: Build CSH Maps | phase={args.phase} dry_run={args.dry_run} ===")

    versions = collect_versions(manifest)
    reporter.info(f"Processing {len(versions)} unique version(s)")

    for alias_url, version_data in tqdm(versions.items(), desc="Versions"):
        process_version(
            alias_url,
            version_data["entries"],
            cache_dir,
            output_dir,
            reporter,
            args.dry_run,
        )

    report = reporter.finish()
    return 0 if report["error_count"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
