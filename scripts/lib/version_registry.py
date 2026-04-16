"""
version_registry.py — Track which product versions have been fully converted.

The registry is stored at manifests/converted_versions.json and committed to git
so the conversion history persists across machines and re-clones.

Registry format:
  {
    "<version_sitemap_url>": {
      "product_name":    "TIBCO BusinessEvents® Enterprise Edition",
      "product_version": "6.4.0",
      "doc_name":        "Administration Guide",
      "phase":           "phase_03",
      "converted_at":    "2026-04-16T00:45:36",
      "page_count":      1388
    },
    ...
  }

The key is the L3 version sitemap URL — unique per product version and
available on every manifest entry as the "version_sitemap" field.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

REGISTRY_FILENAME = "converted_versions.json"


def registry_path(manifests_dir: Path) -> Path:
    return manifests_dir / REGISTRY_FILENAME


def load_registry(manifests_dir: Path) -> dict:
    """Load the registry, returning an empty dict if it doesn't exist yet."""
    path = registry_path(manifests_dir)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_registry(registry: dict, manifests_dir: Path) -> None:
    """Write the registry to disk, sorted by key for stable diffs."""
    path = registry_path(manifests_dir)
    sorted_registry = dict(sorted(registry.items()))
    path.write_text(
        json.dumps(sorted_registry, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def record_converted_versions(
    manifest: list[dict],
    version_errors: dict[str, int],
    phase: str,
    manifests_dir: Path,
    dry_run: bool = False,
) -> list[str]:
    """
    For every version_sitemap that completed with zero errors, write an entry
    into the registry. Returns the list of newly registered version_sitemap URLs.

    version_errors: {version_sitemap_url: error_count}
    """
    # Group manifest entries by version_sitemap to count pages and collect metadata
    versions: dict[str, dict] = {}
    for entry in manifest:
        vs = entry.get("version_sitemap", "")
        if not vs:
            continue
        if vs not in versions:
            versions[vs] = {
                "product_name":    entry.get("product_name", ""),
                "product_version": entry.get("product_version", ""),
                "doc_name":        entry.get("doc_name", ""),
                "page_count":      0,
            }
        versions[vs]["page_count"] += 1

    registry = load_registry(manifests_dir)
    newly_registered: list[str] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for vs, meta in versions.items():
        errors = version_errors.get(vs, 0)
        if errors > 0:
            continue  # version had failures — do not register
        registry[vs] = {
            "product_name":    meta["product_name"],
            "product_version": meta["product_version"],
            "doc_name":        meta["doc_name"],
            "phase":           phase,
            "converted_at":    now,
            "page_count":      meta["page_count"],
        }
        newly_registered.append(vs)

    if not dry_run and newly_registered:
        save_registry(registry, manifests_dir)

    return newly_registered


def filter_manifest_by_registry(
    manifest: list[dict],
    registry: dict,
) -> tuple[list[dict], list[str]]:
    """
    Remove entries whose version_sitemap is already in the registry.

    Returns:
        kept    — entries to include in the manifest
        skipped — version_sitemap URLs that were skipped
    """
    skipped_versions: set[str] = set()
    kept: list[dict] = []

    for entry in manifest:
        vs = entry.get("version_sitemap", "")
        if vs in registry:
            skipped_versions.add(vs)
        else:
            kept.append(entry)

    return kept, sorted(skipped_versions)
