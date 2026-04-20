"""
run.py — Pipeline orchestrator for TIBCO Docs HTML → Markdown converter.

Runs all 6 steps in sequence for a given phase. Each step is a separate script
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


def load_settings(config_path: str) -> dict:
    return yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))


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
    total_seconds: float | None = None,
) -> tuple[int, float]:
    """Run a single pipeline step as a subprocess. Returns (exit_code, duration_seconds)."""
    cmd = [sys.executable, script, f"--phase={phase}", f"--config={config}"]
    if dry_run:
        cmd.append("--dry-run")
    if force_rerun:
        cmd.append("--force-rerun")
    # --force-refresh is only used by Step 2
    if force_refresh and "02_download.py" in script:
        cmd.append("--force-refresh")
    # --ignore-registry is only used by Step 1
    if ignore_registry and "01_build_manifest" in script:
        cmd.append("--ignore-registry")
    # --total-seconds is only used by Step 7
    if total_seconds is not None and "07_generate_report" in script:
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


def has_dita_versions(phase: str, settings: dict) -> bool:
    """Return True if the phase manifest contains any DITA versions."""
    manifests_dir = Path(settings.get("manifests_dir", "manifests"))
    dita_path = manifests_dir / f"dita_versions_{phase}.json"
    if not dita_path.exists():
        return False
    try:
        data = json.loads(dita_path.read_text(encoding="utf-8"))
        return bool(data)
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


def main():
    parser = argparse.ArgumentParser(
        description="TIBCO Docs HTML→Markdown pipeline orchestrator"
    )
    parser.add_argument("--phase",        required=True,
                        help="Phase name, e.g. phase_01")
    parser.add_argument("--config",       default="config/settings.yaml",
                        help="Path to settings.yaml")
    parser.add_argument("--from-step",    type=int, default=1, metavar="N",
                        help="Start from step N (1-7, default: 1)")
    parser.add_argument("--to-step",      type=int, default=7, metavar="N",
                        help="Stop after step N (1-7, default: 7)")
    parser.add_argument("--dry-run",      action="store_true",
                        help="Parse and plan but write no files")
    parser.add_argument("--force-rerun",  action="store_true",
                        help="Re-process URLs already marked done in checkpoint DB")
    parser.add_argument("--force-refresh", action="store_true",
                        help="Re-download cached files (Step 2 only)")
    parser.add_argument("--ignore-registry", action="store_true",
                        help="Include versions already in converted_versions.json (Step 1 only)")
    parser.add_argument("--skip-dita",    action="store_true",
                        help="Skip the DITA sub-pipeline even if DITA versions are present")
    parser.add_argument("--skip-pdf",     action="store_true",
                        help="Skip the PDF release notes sub-pipeline")
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
            total_seconds=accumulated_seconds if "07_generate_report" in script else None,
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

    return 0 if (dita_ok and pdf_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
