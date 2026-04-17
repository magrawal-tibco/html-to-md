"""
07_generate_report.py — Step 7: Generate per-phase CSV report and update consolidated log.

Reads all manifest files produced by steps 1-6 for the current phase and writes:
  - logs/{phase}/{timestamp}/phase_report.csv  — per-run snapshot (all versions this phase)
  - manifests/conversion_log.csv               — persistent log appended every run

CSV columns:
  phase, run_date, product_name, product_version, doc_name,
  status (madcap|dita|no_html), version_sitemap, public_url,
  topics_in_sitemap, topics_converted, csh_id_count, pdf_count,
  zip_status (extracted|missing|na), toc_source (toc_js|breadcrumbs|none)

Usage:
  python scripts/07_generate_report.py --phase phase_01 [--config config/settings.yaml]
"""

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.lib.reporter import Reporter


# Maps internal dict key → CSV column header (human-readable)
COLUMN_HEADERS: dict[str, str] = {
    "phase":             "Phase",
    "run_date":          "Run Date",
    "product_name":      "Product Name",
    "product_version":   "Version",
    "doc_name":          "Document Name",
    "status":            "Status",
    "version_sitemap":   "Version Sitemap URL",
    "public_url":        "Public URL",
    "topics_in_sitemap": "Topics in Sitemap",
    "topics_converted":  "Topics Converted",
    "csh_id_count":      "CSH ID Count",
    "pdf_count":         "PDFs Found",
    "zip_status":        "ZIP Status",
    "toc_source":        "TOC Source",
    "phase_total_seconds": "Phase Total Time (s)",
}

LOG_COLUMNS     = list(COLUMN_HEADERS.keys())       # internal key order
DISPLAY_HEADERS = list(COLUMN_HEADERS.values())     # friendly header order


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def _alias_to_html_root(alias_xml_url: str) -> str:
    """e.g. https://.../pub/foo/1.0/doc/html/Data/Alias.xml → pub/foo/1.0/doc/html/"""
    path = urlparse(alias_xml_url).path
    return PurePosixPath(path).parent.parent.as_posix().lstrip("/") + "/"


def _alias_to_public_url(alias_xml_url: str) -> str:
    """Derive the product-version docs landing page URL."""
    parsed = urlparse(alias_xml_url)
    parts  = [p for p in parsed.path.split("/") if p]
    # parts: ['pub', 'slug', 'version', 'doc', 'html', 'Data', 'Alias.xml']
    if len(parts) >= 3:
        pub_path = "/" + "/".join(parts[:3]) + "/"
    else:
        pub_path = parsed.path
    return f"{parsed.scheme}://{parsed.netloc}{pub_path}"


def _count_md(output_dir: Path, html_root: str) -> int:
    d = output_dir / html_root.rstrip("/")
    return len(list(d.glob("**/*.md"))) if d.exists() else 0


def _count_csh_ids(output_dir: Path, html_root: str) -> int:
    p = output_dir / html_root.rstrip("/") / "csh_map.json"
    if not p.exists():
        return 0
    try:
        return len(_load_json(p, {}))
    except Exception:
        return 0


def _count_pdfs(cache_dir: Path, html_root: str) -> int:
    d = cache_dir / html_root.rstrip("/")
    return len(list(d.glob("**/*.pdf"))) if d.exists() else 0


def _toc_source(output_dir: Path, html_root: str) -> str:
    p = output_dir / html_root.rstrip("/") / "_toc.json"
    if not p.exists():
        return "none"
    try:
        return _load_json(p, {}).get("_source", "breadcrumbs")
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Record collection
# ---------------------------------------------------------------------------

def collect_records(phase: str, settings: dict, run_date: str, total_seconds: float = 0.0) -> list[dict]:
    manifests_dir = Path(settings.get("manifests_dir", "manifests"))
    output_dir    = Path(settings.get("output_dir",   "output"))
    cache_dir     = Path(settings.get("cache_dir",    "cache"))

    manifest       = _load_json(manifests_dir / f"manifest_{phase}.json",       [])
    dita_versions  = _load_json(manifests_dir / f"dita_versions_{phase}.json",  [])
    empty_versions = _load_json(manifests_dir / f"empty_versions_{phase}.json", [])
    zip_registry   = _load_json(manifests_dir / f"zip_registry_{phase}.json",   {})
    zip_missing    = _load_json(manifests_dir / f"zip_missing_{phase}.json",     {})

    records: list[dict] = []

    # ── MadCap versions ─────────────────────────────────────────────────────
    by_version: dict[str, list[dict]] = defaultdict(list)
    for entry in manifest:
        by_version[entry.get("version_sitemap", "")].append(entry)

    for vs, entries in by_version.items():
        rep       = entries[0]
        alias_url = rep.get("alias_xml_url", "")
        html_root = _alias_to_html_root(alias_url) if alias_url else ""
        public_url = _alias_to_public_url(alias_url) if alias_url else ""
        zip_status = ("extracted" if vs in zip_registry else
                      "missing"   if vs in zip_missing  else "na")
        records.append({
            "phase":               phase,
            "run_date":            run_date,
            "product_name":        rep.get("product_name", ""),
            "product_version":     rep.get("product_version", ""),
            "doc_name":            rep.get("doc_name", ""),
            "status":              "madcap",
            "version_sitemap":     vs,
            "public_url":          public_url,
            "topics_in_sitemap":   len(entries),
            "topics_converted":    _count_md(output_dir, html_root),
            "csh_id_count":        _count_csh_ids(output_dir, html_root),
            "pdf_count":           _count_pdfs(cache_dir, html_root),
            "zip_status":          zip_status,
            "toc_source":          _toc_source(output_dir, html_root),
            "phase_total_seconds": total_seconds,
        })

    # ── DITA versions ────────────────────────────────────────────────────────
    for v in dita_versions:
        records.append({
            "phase":               phase,
            "run_date":            run_date,
            "product_name":        v.get("product_name", ""),
            "product_version":     v.get("product_version", ""),
            "doc_name":            "",
            "status":              "dita",
            "version_sitemap":     v.get("version_sitemap", ""),
            "public_url":          "",
            "topics_in_sitemap":   v.get("page_count", 0),
            "topics_converted":    0,
            "csh_id_count":        0,
            "pdf_count":           0,
            "zip_status":          "na",
            "toc_source":          "none",
            "phase_total_seconds": total_seconds,
        })

    # ── Empty versions (no HTML) ─────────────────────────────────────────────
    for v in empty_versions:
        records.append({
            "phase":               phase,
            "run_date":            run_date,
            "product_name":        v.get("product_name", ""),
            "product_version":     v.get("product_version", ""),
            "doc_name":            "",
            "status":              "no_html",
            "version_sitemap":     v.get("version_sitemap", ""),
            "public_url":          "",
            "topics_in_sitemap":   v.get("raw_page_count", 0),
            "topics_converted":    0,
            "csh_id_count":        0,
            "pdf_count":           0,
            "zip_status":          "na",
            "toc_source":          "none",
            "phase_total_seconds": total_seconds,
        })

    return records


# ---------------------------------------------------------------------------
# CSV writing
# ---------------------------------------------------------------------------

def _write_csv(path: Path, records: list[dict], append: bool = False):
    mode = "a" if append else "w"
    write_header = not (append and path.exists() and path.stat().st_size > 0)
    # Remap internal keys to display headers for each row
    display_rows = [
        {COLUMN_HEADERS[k]: v for k, v in rec.items() if k in COLUMN_HEADERS}
        for rec in records
    ]
    with open(path, mode, newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=DISPLAY_HEADERS, extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerows(display_rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Step 7: Generate conversion report")
    parser.add_argument("--phase",          required=True)
    parser.add_argument("--config",         default="config/settings.yaml")
    parser.add_argument("--dry-run",        action="store_true")
    parser.add_argument("--force-rerun",    action="store_true")  # orchestrator compat
    parser.add_argument("--total-seconds",  type=float, default=0.0,
                        help="Total pipeline elapsed time in seconds (passed by run.py)")
    args = parser.parse_args()

    settings = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))

    run_date = datetime.now().isoformat(timespec="seconds")
    logs_dir = Path(settings.get("logs_dir", "logs"))
    run_dir  = logs_dir / args.phase / datetime.now().strftime("%Y%m%d-%H%M%S")
    reporter = Reporter(run_dir, "07_report", dry_run=args.dry_run)

    reporter.info(f"=== Step 7: Generate Report | phase={args.phase} dry_run={args.dry_run} ===")

    records = collect_records(args.phase, settings, run_date, args.total_seconds)
    reporter.info(f"Collected {len(records)} version record(s)")

    by_status: dict[str, int] = {}
    for r in records:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    for status, count in sorted(by_status.items()):
        reporter.info(f"  {status}: {count}")

    total_topics    = sum(r["topics_converted"]  for r in records)
    total_csh       = sum(r["csh_id_count"]      for r in records)
    total_pdfs      = sum(r["pdf_count"]          for r in records)
    reporter.info(f"  topics_converted={total_topics}  csh_ids={total_csh}  pdfs={total_pdfs}")

    if not args.dry_run and records:
        manifests_dir = Path(settings.get("manifests_dir", "manifests"))
        manifests_dir.mkdir(parents=True, exist_ok=True)

        phase_csv = run_dir / "phase_report.csv"
        _write_csv(phase_csv, records, append=False)
        reporter.info(f"Phase report → {phase_csv}")

        consolidated = manifests_dir / "conversion_log.csv"
        _write_csv(consolidated, records, append=True)
        reporter.info(f"Consolidated log → {consolidated}  (+{len(records)} rows)")

    elif args.dry_run:
        reporter.info("Dry run — no files written")
    else:
        reporter.info("No records to write")

    reporter.count("versions_reported", len(records))
    report = reporter.finish()
    return 0 if report["error_count"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
