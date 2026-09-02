"""
run.py — Pipeline orchestrator for TIBCO Docs HTML → Markdown converter.

Runs all pipeline steps in sequence for a given phase. Each step is a separate script
invoked as a subprocess so it has its own clean Python environment.

Usage:
  python run.py --phase phase_01
  python run.py --phase phase_01 --from-step 3
  python run.py --phase phase_01 --from-step 1 --to-step 2
  python run.py --phase phase_01 --dry-run
  python run.py --phase phase_01 --force-rerun
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from scripts.lib.io_utils import load_settings

# Force UTF-8 output on Windows consoles
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# Each step: (display_id, sort_key, script, label)
# sort_key is a float so "2a" (1.5) slots between 1 and 2 without renumbering.
# --from-step / --to-step use integer step numbers; 2a is always included when
# the range spans both 1 and 2 (sort_key 1.5 falls between them automatically).
STEPS = [
    (1,    1.0, "scripts/01_build_manifest.py",   "Build Manifest"),
    ("2a", 1.5, "scripts/02a_download_zip.py",    "Download ZIPs + Extract"),
    (2,    2.0, "scripts/02_download.py",          "Download HTML + Images + alias.xml"),
    (3,    3.0, "scripts/03_convert.py",           "Convert HTML → Markdown"),
    (4,    4.0, "scripts/04_build_csh_maps.py",   "Build CSH Maps"),
    (5,    5.0, "scripts/05_postprocess.py",       "Postprocess Links + Tokens"),
    (6,    6.0, "scripts/06_build_toc.py",         "Build TOC JSON"),
    (7,    7.0, "scripts/07_generate_report.py",  "Generate Report"),
]


def _parse_step(value: str) -> float:
    """Convert a step display ID (e.g. '1', '2a', '3') to its sort key for range comparisons."""
    for display_id, sort_key, _, _ in STEPS:
        if str(display_id) == value:
            return sort_key
    try:
        return float(value)
    except ValueError:
        valid = ", ".join(str(s[0]) for s in STEPS)
        raise argparse.ArgumentTypeError(
            f"Invalid step '{value}'. Valid step IDs: {valid}"
        )


def run_step(
    display_id,
    script: str,
    label: str,
    phase: str,
    config: str,
    dry_run: bool,
    force_rerun: bool,
    force_refresh: bool,
    ignore_registry: bool,
    scan_cache: bool = False,
    total_seconds: float | None = None,
    delta: bool = False,
) -> tuple[int, float]:
    """Run a single pipeline step as a subprocess. Returns (exit_code, duration_seconds)."""
    cmd = [sys.executable, script, f"--phase={phase}", f"--config={config}"]
    script_name = Path(script).name
    if dry_run:
        cmd.append("--dry-run")
    if force_rerun and script_name not in {"02_download.py", "01_build_manifest.py"}:
        cmd.append("--force-rerun")
    # --force-refresh is only used by Step 2; treat --force-rerun as equivalent
    if script_name == "02_download.py" and (force_refresh or force_rerun):
        cmd.append("--force-refresh")
    # --ignore-registry is only used by Step 1
    if ignore_registry and script_name == "01_build_manifest.py":
        cmd.append("--ignore-registry")
    # --delta is only used by Step 1
    if delta and script_name == "01_build_manifest.py":
        cmd.append("--delta")
    # --scan-cache is only used by Step 3
    if scan_cache and script_name == "03_convert.py":
        cmd.append("--scan-cache")
    # --total-seconds is only used by Step 7
    if total_seconds is not None and script_name == "07_generate_report.py":
        cmd.append(f"--total-seconds={total_seconds:.1f}")

    print(f"\n{'='*60}")
    print(f"  Step {display_id}: {label}")
    print(f"  Command: {' '.join(cmd)}")
    print(f"{'='*60}")

    start = time.time()
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    result = subprocess.run(cmd, text=True, env=env)
    elapsed = round(time.time() - start, 1)

    status = "OK" if result.returncode == 0 else f"FAILED (exit {result.returncode})"
    print(f"\n  Step {display_id} {status} in {elapsed}s")
    return result.returncode, elapsed


def find_latest_step_report(logs_dir: Path, phase: str, step_name: str) -> dict | None:
    """Find the most recent JSON report for a step to include in the summary."""
    phase_dir = logs_dir / phase
    if not phase_dir.exists():
        return None
    # Walk timestamped run dirs in reverse order
    run_dirs = sorted(phase_dir.iterdir(), reverse=True)
    for run_dir in run_dirs:
        report_path = run_dir / f"{step_name}.json"
        if report_path.exists():
            try:
                return json.loads(report_path.read_text(encoding="utf-8"))
            except Exception:
                pass
    return None


def print_summary(
    phase: str,
    steps_run: list[tuple[int, str, str, int, float]],
    logs_dir: Path,
    dry_run: bool,
):
    """Print a final summary table to stdout."""
    print(f"\n{'='*60}")
    print(f"  PIPELINE SUMMARY — phase={phase}  dry_run={dry_run}")
    print(f"{'='*60}")
    print(f"  {'Step':<6} {'Label':<38} {'Status':<10} {'Time':>6}")
    print(f"  {'-'*6} {'-'*38} {'-'*10} {'-'*6}")

    total_time = 0.0
    all_ok = True
    for step_num, script, label, exit_code, elapsed in steps_run:
        status = "OK" if exit_code == 0 else "FAILED"
        if exit_code != 0:
            all_ok = False
        total_time += elapsed
        print(f"  {step_num:<6} {label:<38} {status:<10} {elapsed:>5.1f}s")

    print(f"  {'-'*6} {'-'*38} {'-'*10} {'-'*6}")
    print(f"  {'TOTAL':<6} {'':<38} {'OK' if all_ok else 'ERRORS':<10} {total_time:>5.1f}s")
    print(f"{'='*60}\n")

    if not all_ok:
        print("  One or more steps failed. Check logs/ for details.")
    else:
        print(f"  All steps completed. Output in: output/")
        print(f"  Logs in: {logs_dir / phase}/")


def has_webworks_versions(phase: str, settings: dict) -> bool:
    """Return True if any version-level wwhelp/books.htm exists in cache.
    Version-level books.htm links to multiple guides (links have >= 2 slashes).
    Per-guide books.htm links only to its own wwhdata/files.htm."""
    from bs4 import BeautifulSoup
    cache_dir = Path(settings.get("cache_dir", "cache"))
    for books_path in cache_dir.glob("**/wwhelp/books.htm"):
        try:
            soup = BeautifulSoup(
                books_path.read_text(encoding="utf-8", errors="replace"), "html.parser"
            )
            links = [
                a["href"]
                for div in soup.find_all("div")
                if (a := div.find("a")) and a.get("href")
            ]
            if any(link.count("/") >= 3 for link in links):
                return True
        except Exception:
            pass
    return False


def run_webworks_pipeline(phase: str, config: str, dry_run: bool, force_rerun: bool) -> tuple[int, float]:
    """Run the WebWorks ePublisher sub-pipeline (scripts/webworks/run.py) as a subprocess."""
    cmd = [sys.executable, "scripts/webworks/run.py", "--phase", phase, "--config", config]
    if dry_run:
        cmd.append("--dry-run")
    if force_rerun:
        cmd.append("--force-rerun")

    print(f"\n{'='*60}")
    print(f"  WebWorks Sub-pipeline")
    print(f"  Command: {' '.join(cmd)}")
    print(f"{'='*60}")

    start = time.time()
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    result = subprocess.run(cmd, text=True, env=env)
    elapsed = round(time.time() - start, 1)

    status = "OK" if result.returncode == 0 else f"FAILED (exit {result.returncode})"
    print(f"\n  WebWorks sub-pipeline {status} in {elapsed}s")
    return result.returncode, elapsed


def get_zip_pub_slugs(phase: str, settings: dict) -> list[str]:
    """Return unique pub_slugs for zip-based (new-format) products in this phase.

    Includes both products with converted HTML pages AND PDF-only products (whose ZIPs
    had no HTML — they still need restructure Phase 5 to copy PDF assets).
    EBX-family products are excluded — they have their own restructure scripts (07/08).
    """
    EBX_SLUGS = {"ebx", "ebx-addon", "ebx-addon-reorg"}
    manifests_dir = Path(settings.get("manifests_dir", "manifests"))
    seen: set[str] = set()
    slugs: list[str] = []

    # Slugs from expanded manifest entries (HTML-bearing products)
    manifest_path = manifests_dir / f"manifest_{phase}.json"
    if manifest_path.exists():
        try:
            for e in json.loads(manifest_path.read_text(encoding="utf-8")):
                slug = e.get("pub_slug", "")
                if slug and slug not in seen and slug not in EBX_SLUGS:
                    seen.add(slug)
                    slugs.append(slug)
        except Exception:
            pass

    # Also include PDF-only products: in zip_registry but absent from manifest
    # (step 2a extracted them but found 0 HTML pages, so manifest has no entries)
    zip_registry_path = manifests_dir / f"zip_registry_{phase}.json"
    if zip_registry_path.exists():
        try:
            registry = json.loads(zip_registry_path.read_text(encoding="utf-8"))
            for info in registry.values():
                html_root = info.get("html_root", "")
                # html_root is "pub/<slug>/<version>/doc/html/" — extract slug
                parts = html_root.strip("/").split("/")
                if len(parts) >= 2 and parts[0] == "pub":
                    slug = parts[1]
                    if slug and slug not in seen and slug not in EBX_SLUGS:
                        seen.add(slug)
                        slugs.append(slug)
        except Exception:
            pass

    return slugs


def warn_previously_extracted(phase: str, settings: dict) -> None:
    """Print a warning for versions that were skipped in step 2a because they were
    already extracted from a prior run. These may be stale if source content changed."""
    manifests_dir = Path(settings.get("manifests_dir", "manifests"))
    zip_registry_path = manifests_dir / f"zip_registry_{phase}.json"
    if not zip_registry_path.exists():
        return
    try:
        registry = json.loads(zip_registry_path.read_text(encoding="utf-8"))
    except Exception:
        return

    stale = [
        (version_url, info)
        for version_url, info in registry.items()
        if info.get("extracted_at") == "previously"
    ]
    if not stale:
        return

    print(f"\n  [!] {len(stale)} version(s) were already extracted from a previous run and skipped:")
    for version_url, info in stale:
        html_root = info.get("html_root", "")
        fmt = info.get("format", "unknown")
        print(f"      {html_root}  (format={fmt})")
    print(f"\n  If source content has changed, re-run with --force-rerun to re-extract.")
    print(f"  To re-run step 2a only:  python run.py --phase {phase} --from-step 2a --to-step 2a --force-rerun")


def run_restructure_pipeline(
    pub_slugs: list[str],
    dry_run: bool,
    phase: str | None = None,
) -> tuple[int, float]:
    """Run tibco_restructure.py for the given product slugs."""
    cmd = [
        sys.executable, "scripts/tibco_restructure.py",
        "--products", *pub_slugs,
    ]
    if phase:
        cmd += ["--phase", phase, "--phase-group", phase]
    if dry_run:
        cmd.append("--dry-run")

    print(f"\n{'='*60}")
    print(f"  Restructure Sub-pipeline  ({', '.join(pub_slugs)})")
    print(f"  Command: {' '.join(cmd)}")
    print(f"{'='*60}")

    start = time.time()
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    result = subprocess.run(cmd, text=True, env=env)
    elapsed = round(time.time() - start, 1)

    status = "OK" if result.returncode == 0 else f"FAILED (exit {result.returncode})"
    print(f"\n  Restructure sub-pipeline {status} in {elapsed}s")
    return result.returncode, elapsed


def has_dita_versions(phase: str, settings: dict) -> bool:
    """Return True if the phase manifest contains any sdl_dita or file_dita versions."""
    manifests_dir = Path(settings.get("manifests_dir", "manifests"))
    manifest_path = manifests_dir / f"manifest_{phase}.json"
    if not manifest_path.exists():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return any(
            e.get("version_format") in ("sdl_dita", "file_dita")
            for e in manifest
        )
    except Exception:
        return False


def run_dita_pipeline(phase: str, config: str, dry_run: bool, force_rerun: bool) -> tuple[int, float]:
    """Run the DITA sub-pipeline (scripts/dita/run.py) as a subprocess."""
    cmd = [sys.executable, "scripts/dita/run.py", "--phase", phase, "--config", config]
    if dry_run:
        cmd.append("--dry-run")
    if force_rerun:
        cmd.append("--force-rerun")

    print(f"\n{'='*60}")
    print(f"  DITA Sub-pipeline")
    print(f"  Command: {' '.join(cmd)}")
    print(f"{'='*60}")

    start = time.time()
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    result = subprocess.run(cmd, text=True, env=env)
    elapsed = round(time.time() - start, 1)

    status = "OK" if result.returncode == 0 else f"FAILED (exit {result.returncode})"
    print(f"\n  DITA sub-pipeline {status} in {elapsed}s")
    return result.returncode, elapsed


def run_pdf_pipeline(phase: str, config: str, dry_run: bool, force_rerun: bool) -> tuple[int, float]:
    """Run the PDF release notes sub-pipeline (scripts/pdf/convert.py) as a subprocess."""
    cmd = [sys.executable, "scripts/pdf/convert.py", "--phase", phase, "--config", config]
    if dry_run:
        cmd.append("--dry-run")
    if force_rerun:
        cmd.append("--force-rerun")

    print(f"\n{'='*60}")
    print(f"  PDF Release Notes Sub-pipeline")
    print(f"  Command: {' '.join(cmd)}")
    print(f"{'='*60}")

    start = time.time()
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    result = subprocess.run(cmd, text=True, env=env)
    elapsed = round(time.time() - start, 1)

    status = "OK" if result.returncode == 0 else f"FAILED (exit {result.returncode})"
    print(f"\n  PDF sub-pipeline {status} in {elapsed}s")
    return result.returncode, elapsed


def write_phase_summary(phase: str, settings: dict) -> None:
    """Write a human-readable conversion status report to manifests/<phase>_summary.txt."""
    manifests_dir = Path(settings.get("manifests_dir", "manifests"))
    logs_dir = Path(settings.get("logs_dir", "logs"))
    summary_path = manifests_dir / f"{phase}_summary.txt"

    # Load manifest (HTML-bearing products)
    manifest_path = manifests_dir / f"manifest_{phase}.json"
    manifest_entries: list[dict] = []
    if manifest_path.exists():
        try:
            manifest_entries = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Load zip registry
    zip_registry_path = manifests_dir / f"zip_registry_{phase}.json"
    registry: dict = {}
    if zip_registry_path.exists():
        try:
            registry = json.loads(zip_registry_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    EBX_SLUGS = {"ebx", "ebx-addon", "ebx-addon-reorg"}
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Group manifest entries by pub_slug+version to count pages
    # Page-level entries have no version_url, so key by slug+version instead.
    html_versions: dict[str, dict] = {}  # "slug@version" -> {slug, pages, product_name, product_version}
    for e in manifest_entries:
        slug = e.get("pub_slug", "")
        if not slug or slug in EBX_SLUGS:
            continue
        version = e.get("product_version", "")
        key = f"{slug}@{version}"
        if key not in html_versions:
            html_versions[key] = {
                "slug": slug,
                "product_name": e.get("product_name", slug),
                "product_version": version,
                "pages": 0,
            }
        html_versions[key]["pages"] += 1

    # Identify PDF-only products (in registry but no manifest entries for that slug)
    manifest_slugs = {v["slug"] for v in html_versions.values()}
    pdf_only_versions: list[dict] = []
    previously_extracted: list[dict] = []

    for version_url, info in registry.items():
        html_root = info.get("html_root", "")
        parts = html_root.strip("/").split("/")
        slug = parts[1] if len(parts) >= 2 and parts[0] == "pub" else ""
        if not slug or slug in EBX_SLUGS:
            continue

        if info.get("extracted_at") == "previously":
            previously_extracted.append({"version_url": version_url, "html_root": html_root,
                                          "format": info.get("format", "unknown"), "slug": slug})
        elif slug not in manifest_slugs:
            # Extracted this run but no HTML pages found → PDF-only
            file_count = info.get("file_count", 0)
            # Count PDFs in cache
            cache_dir = Path(settings.get("cache_dir", "cache"))
            pdf_dir = cache_dir / html_root.replace("doc/html/", "doc/pdf/").strip("/")
            pdf_count = len(list(pdf_dir.glob("*.pdf"))) if pdf_dir.exists() else 0
            pdf_only_versions.append({
                "slug": slug, "version_url": version_url,
                "html_root": html_root, "file_count": file_count, "pdf_count": pdf_count,
            })

    lines = [
        f"Conversion Status Report — phase: {phase}",
        f"Generated: {now}",
        "=" * 60,
        "",
    ]

    # HTML-converted products
    if html_versions:
        lines.append(f"HTML CONVERTED ({len(html_versions)} version(s)):")
        lines.append("-" * 60)
        for _key, v in sorted(html_versions.items(), key=lambda x: x[1]["slug"]):
            lines.append(f"  {v['slug']}  {v['product_version']:>10}  {v['pages']:>5} pages")
        total_pages = sum(v["pages"] for v in html_versions.values())
        lines.append(f"  {'TOTAL':>30}  {total_pages:>5} pages")
        lines.append("")
    else:
        lines.append("HTML CONVERTED: none")
        lines.append("")

    # PDF-only products
    if pdf_only_versions:
        lines.append(f"PDF-ONLY (no HTML pages) ({len(pdf_only_versions)} version(s)):")
        lines.append("-" * 60)
        for v in sorted(pdf_only_versions, key=lambda x: x["slug"]):
            lines.append(f"  {v['slug']}  {v['pdf_count']:>5} PDF(s)  {v['version_url']}")
        lines.append("")
    else:
        lines.append("PDF-ONLY: none")
        lines.append("")

    # Previously extracted (skipped this run)
    if previously_extracted:
        lines.append(f"PREVIOUSLY EXTRACTED — SKIPPED ({len(previously_extracted)} version(s)):")
        lines.append("-" * 60)
        for v in sorted(previously_extracted, key=lambda x: x["slug"]):
            lines.append(f"  {v['slug']}  {v['html_root']}  (format={v['format']})")
        lines.append(f"  To re-extract: python run.py --phase {phase} --from-step 2a --to-step 2a --force-rerun")
        lines.append("")
    else:
        lines.append("PREVIOUSLY EXTRACTED: none")
        lines.append("")

    lines.append("=" * 60)
    lines.append(f"  HTML versions:       {len(html_versions)}")
    lines.append(f"  PDF-only versions:   {len(pdf_only_versions)}")
    lines.append(f"  Skipped (prev run):  {len(previously_extracted)}")
    lines.append(f"  Total versions:      {len(html_versions) + len(pdf_only_versions) + len(previously_extracted)}")
    lines.append("")

    summary_text = "\n".join(lines)
    summary_path.write_text(summary_text, encoding="utf-8")
    print(f"\n  Conversion summary written to: {summary_path}")
    print(summary_text)


def main():
    parser = argparse.ArgumentParser(
        description="TIBCO Docs HTML→Markdown pipeline orchestrator"
    )
    parser.add_argument("--phase",        required=True,
                        help="Phase name, e.g. phase_01")
    parser.add_argument("--config",       default="config/settings.yaml",
                        help="Path to settings.yaml")
    parser.add_argument("--from-step",    type=_parse_step, default=1.0, metavar="N",
                        help="Start from step N (1, 2a, 2-7, default: 1)")
    parser.add_argument("--to-step",      type=_parse_step, default=7.0, metavar="N",
                        help="Stop after step N (1, 2a, 2-7, default: 7)")
    parser.add_argument("--dry-run",      action="store_true",
                        help="Parse and plan but write no files")
    parser.add_argument("--force-rerun",  action="store_true",
                        help="Re-process URLs already marked done in checkpoint DB")
    parser.add_argument("--force-refresh", action="store_true",
                        help="Re-download cached files (Step 2 only)")
    parser.add_argument("--ignore-registry", action="store_true",
                        help="Include versions already in converted_versions.json (Step 1 only)")
    parser.add_argument("--delta",          action="store_true",
                        help="Skip versions whose ZIP Last-Modified is unchanged since last checkpoint (Step 1 only)")
    parser.add_argument("--scan-cache",    action="store_true",
                        help="Drive Step 3 from cached files instead of sitemap manifest (use when ZIP paths differ from sitemap URLs)")
    parser.add_argument("--skip-dita",    action="store_true",
                        help="Skip the DITA sub-pipeline even if DITA versions are present")
    parser.add_argument("--skip-pdf",       action="store_true",
                        help="Skip the PDF release notes sub-pipeline")
    parser.add_argument("--skip-webworks", action="store_true",
                        help="Skip the WebWorks ePublisher sub-pipeline")
    parser.add_argument("--skip-restructure", action="store_true",
                        help="Skip the restructure sub-pipeline (tibco_restructure.py)")
    args = parser.parse_args()

    settings  = load_settings(args.config)
    logs_dir  = Path(settings.get("logs_dir", "logs"))

    print(f"\nTIBCO Docs Converter")
    print(f"  Phase:     {args.phase}")
    print(f"  Steps:     {args.from_step} -> {args.to_step}")
    print(f"  Dry run:   {args.dry_run}")
    print(f"  Config:    {args.config}")

    # ── Main pipeline (steps 1-7) ─────────────────────────────────────────────
    steps_run = []
    accumulated_seconds = 0.0
    main_ok = True
    for display_id, sort_key, script, label in STEPS:
        if sort_key < args.from_step or sort_key > args.to_step:
            continue

        exit_code, elapsed = run_step(
            display_id, script, label,
            args.phase, args.config,
            args.dry_run, args.force_rerun, args.force_refresh,
            args.ignore_registry,
            scan_cache=args.scan_cache,
            total_seconds=accumulated_seconds if "07_generate_report" in script else None,
            delta=args.delta,
        )
        accumulated_seconds += elapsed
        steps_run.append((display_id, script, label, exit_code, elapsed))

        if exit_code != 0:
            resume_step = int(sort_key) if sort_key == int(sort_key) else display_id
            print(f"\nStep {display_id} failed — stopping pipeline.")
            print(f"To resume from this step: python run.py --phase {args.phase} --from-step {resume_step}")
            main_ok = False
            break

    print_summary(args.phase, steps_run, logs_dir, args.dry_run)

    if not main_ok:
        return 1

    # ── DITA sub-pipeline ─────────────────────────────────────────────────────
    dita_ok = True
    if not args.skip_dita and has_dita_versions(args.phase, settings):
        print(f"\nDITA versions detected — running DITA sub-pipeline...")
        dita_rc, dita_elapsed = run_dita_pipeline(
            args.phase, args.config, args.dry_run, args.force_rerun
        )
        dita_ok = (dita_rc == 0)
        if not dita_ok:
            print(f"\nDITA sub-pipeline failed. PDF sub-pipeline will still run.")
    elif not args.skip_dita:
        print(f"\nNo DITA versions found for phase '{args.phase}' — skipping DITA sub-pipeline.")

    # ── PDF release notes sub-pipeline ───────────────────────────────────────
    pdf_ok = True
    if not args.skip_pdf:
        pdf_rc, pdf_elapsed = run_pdf_pipeline(
            args.phase, args.config, args.dry_run, args.force_rerun
        )
        pdf_ok = (pdf_rc == 0)

    # ── WebWorks ePublisher sub-pipeline ─────────────────────────────────────
    webworks_ok = True
    if not args.skip_webworks and has_webworks_versions(args.phase, settings):
        print(f"\nWebWorks versions detected — running WebWorks sub-pipeline...")
        ww_rc, ww_elapsed = run_webworks_pipeline(
            args.phase, args.config, args.dry_run, args.force_rerun
        )
        webworks_ok = (ww_rc == 0)
    elif not args.skip_webworks:
        print(f"\nNo WebWorks versions found for phase '{args.phase}' — skipping WebWorks sub-pipeline.")

    # ── Restructure sub-pipeline ──────────────────────────────────────────────
    restructure_ok = True
    zip_slugs = get_zip_pub_slugs(args.phase, settings)
    if not args.skip_restructure and zip_slugs:
        print(f"\nZip-based products detected — running restructure sub-pipeline...")
        restructure_rc, restructure_elapsed = run_restructure_pipeline(
            zip_slugs, args.dry_run, phase=args.phase
        )
        restructure_ok = (restructure_rc == 0)
        if not restructure_ok:
            print(f"\nRestructure sub-pipeline failed.")
    elif not args.skip_restructure:
        print(f"\nNo zip-based products found for phase '{args.phase}' — skipping restructure.")

    # ── Already-extracted warning ─────────────────────────────────────────────
    warn_previously_extracted(args.phase, settings)

    # ── End-of-run conversion status report ──────────────────────────────────
    if not args.dry_run:
        write_phase_summary(args.phase, settings)

    return 0 if (dita_ok and pdf_ok and webworks_ok and restructure_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
