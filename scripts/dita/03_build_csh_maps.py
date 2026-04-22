"""
03_build_csh_maps.py — DITA Step 3: Build CSH maps from head.js.

Works for both file_dita and sdl_dita formats.

For each DITA version:
  1. Read static/head.js
  2. Extract suitehelp.contexts JS object (brace-balanced parser)
  3. Resolve each context value to an output .md path:
     - file_dita: strip #anchor from "path/topic.html#GUID-..." → match .md file
     - sdl_dita:  "GUID-xxx.html" → look up in guid_rename_map → slug.md
  4. Inject csh_names: [...] into matching .md frontmatter
  5. Write csh_map.json per version root (output dir)

Usage:
  python scripts/dita/03_build_csh_maps.py --phase phase_01
         [--config config/dita_settings.yaml] [--dry-run]
"""

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import yaml
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


def _extract_contexts(js_text: str) -> dict[str, str]:
    """
    Extract the suitehelp.contexts JS object from head.js using brace balancing.
    Handles both single-line and multi-line formats.
    """
    m = re.search(r'suitehelp\.contexts\s*=\s*\{', js_text)
    if not m:
        return {}
    start = m.end() - 1  # position of opening {
    depth = 0
    i = start
    in_string = False
    escape_next = False
    while i < len(js_text):
        ch = js_text[i]
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
    obj_str = js_text[start:i + 1]
    try:
        return json.loads(obj_str)
    except json.JSONDecodeError:
        return {}


def _resolve_file_dita_path(value: str, html_root: str, output_dir: Path) -> Path | None:
    """
    Resolve a file_dita CSH value to an output .md path.
    Value format: "path/to/topic.html#GUID-anchor" (anchor optional).
    """
    filename = value.split("#")[0]  # strip anchor
    # filename is relative to html_root
    md_rel = re.sub(r"\.html?$", ".md", filename)
    candidate = output_dir / html_root.rstrip("/") / md_rel
    if candidate.exists():
        return candidate
    return None


def _resolve_sdl_dita_path(
    value: str,
    html_root: str,
    rename_map: dict,
    output_dir: Path,
) -> Path | None:
    """
    Resolve an sdl_dita CSH value to an output .md path.
    Value format: "GUID-xxx.html" (no anchor in sdl_dita contexts).
    """
    guid_filename = value.split("#")[0]
    slug_filename = rename_map.get("topics", {}).get(guid_filename, guid_filename)
    md_filename = re.sub(r"\.html?$", ".md", slug_filename)
    candidate = output_dir / html_root.rstrip("/") / md_filename
    if candidate.exists():
        return candidate
    return None


def _inject_csh_names(md_path: Path, csh_names: list[str], dry_run: bool) -> bool:
    """Add or update csh_names field in the .md file's YAML frontmatter."""
    try:
        text = md_path.read_text(encoding="utf-8")
    except Exception:
        return False

    if not text.startswith("---"):
        return False

    end = text.find("\n---\n", 3)
    if end == -1:
        return False

    fm_text = text[3:end]
    rest    = text[end + 5:]  # content after closing ---

    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        return False

    existing = fm.get("csh_names", [])
    merged = sorted(set(existing) | set(csh_names))
    fm["csh_names"] = merged

    new_fm = "---\n" + yaml.dump(fm, allow_unicode=True, default_flow_style=False) + "---\n\n"
    new_text = new_fm + rest

    if not dry_run:
        md_path.write_text(new_text, encoding="utf-8")
    return True


def process_version(
    version_sitemap: str,
    fmt: str,
    entry: dict,
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
        output_path = entry.get("output_path", "")
        if output_path:
            html_root = str(Path(output_path).parent).replace("\\", "/")
            reporter.warning(f"html_root not in zip_registry for {version_sitemap} — derived from manifest")
    if not html_root:
        reporter.fail(version_sitemap, "html_root not in zip_registry")
        return

    head_js_path = cache_dir / html_root.rstrip("/") / "static" / "head.js"
    if not head_js_path.exists():
        reporter.info(f"  {version_sitemap}: head.js not found at {head_js_path} — skipping")
        reporter.count("versions_no_head_js")
        return

    js_text  = head_js_path.read_text(encoding="utf-8")
    contexts = _extract_contexts(js_text)
    if not contexts:
        reporter.info(f"  {version_sitemap}: suitehelp.contexts is empty — skipping")
        reporter.count("versions_empty_contexts")
        return

    reporter.info(f"  {version_sitemap}: {len(contexts)} context IDs found")

    # Resolve each context to an output .md path and build csh_map
    csh_map: dict[str, str] = {}
    md_to_names: dict[Path, list[str]] = {}

    for ctx_id, value in contexts.items():
        if fmt == "file_dita":
            md_path = _resolve_file_dita_path(value, html_root, output_dir)
        else:
            if rename_map is None:
                continue
            md_path = _resolve_sdl_dita_path(value, html_root, rename_map, output_dir)

        if md_path is None:
            reporter.count("csh_unresolved")
            continue

        rel = str(md_path.relative_to(output_dir)).replace("\\", "/")
        csh_map[ctx_id] = rel
        md_to_names.setdefault(md_path, []).append(ctx_id)
        reporter.count("csh_resolved")

    # Inject csh_names into matching .md files
    injected = 0
    for md_path, names in md_to_names.items():
        if _inject_csh_names(md_path, names, dry_run):
            injected += 1
    reporter.count("csh_injected", injected)

    # Write csh_map.json to the version output dir
    version_out_dir = output_dir / html_root.rstrip("/")
    csh_map_path = version_out_dir / "csh_map.json"
    if not dry_run:
        version_out_dir.mkdir(parents=True, exist_ok=True)
        csh_map_path.write_text(
            json.dumps(csh_map, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        reporter.info(f"    Written: {csh_map_path} ({len(csh_map)} entries)")
    else:
        reporter.info(f"    [dry-run] Would write: {csh_map_path} ({len(csh_map)} entries)")

    reporter.count("versions_processed")


def main():
    parser = argparse.ArgumentParser(description="DITA Step 3: Build CSH maps from head.js")
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
    reporter = Reporter(run_dir, "dita_03_csh", dry_run=args.dry_run)

    reporter.info(
        f"=== DITA Step 3: Build CSH maps | phase={args.phase} dry_run={args.dry_run} ==="
    )

    # Collect one representative manifest entry per DITA version
    dita_versions: dict[str, tuple[str, dict]] = {}  # {vs: (fmt, entry)}
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

    for version_sitemap, (fmt, entry) in tqdm(dita_versions.items(), desc="CSH maps"):
        rename_map = None
        if fmt == "sdl_dita":
            rename_map = load_guid_rename_map(args.phase, version_sitemap, settings)
            if rename_map is None:
                reporter.info(
                    f"  WARNING: No rename map for {version_sitemap} — run DITA step 1 first"
                )

        process_version(
            version_sitemap, fmt, entry, settings, zip_registry,
            rename_map, reporter, args.dry_run
        )

    report = reporter.finish()
    return 0 if report["error_count"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
