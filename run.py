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


STEPS = [
    (1, "scripts/01_build_manifest.py", "Build Manifest"),
    (2, "scripts/02_download.py",       "Download HTML + Images + alias.xml"),
    (3, "scripts/03_convert.py",        "Convert HTML → Markdown"),
    (4, "scripts/04_build_csh_maps.py", "Build CSH Maps"),
    (5, "scripts/05_postprocess.py",    "Postprocess Links + Tokens"),
    (6, "scripts/06_build_toc.py",      "Build TOC JSON"),
]


def load_settings(config_path: str) -> dict:
    return yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))


def run_step(
    step_num: int,
    script: str,
    label: str,
    phase: str,
    config: str,
    dry_run: bool,
    force_rerun: bool,
    force_refresh: bool,
    ignore_registry: bool,
) -> tuple[int, float]:
    """Run a single pipeline step as a subprocess. Returns (exit_code, duration_seconds)."""
    cmd = [sys.executable, script, f"--phase={phase}", f"--config={config}"]
    if dry_run:
        cmd.append("--dry-run")
    if force_rerun:
        cmd.append("--force-rerun")
    # --force-refresh is only used by Step 2
    if force_refresh and "02_download" in script:
        cmd.append("--force-refresh")
    # --ignore-registry is only used by Step 1
    if ignore_registry and "01_build_manifest" in script:
        cmd.append("--ignore-registry")

    print(f"\n{'='*60}")
    print(f"  Step {step_num}: {label}")
    print(f"  Command: {' '.join(cmd)}")
    print(f"{'='*60}")

    start = time.time()
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    result = subprocess.run(cmd, text=True, env=env)
    elapsed = round(time.time() - start, 1)

    status = "OK" if result.returncode == 0 else f"FAILED (exit {result.returncode})"
    print(f"\n  Step {step_num} {status} in {elapsed}s")
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


def main():
    parser = argparse.ArgumentParser(
        description="TIBCO Docs HTML→Markdown pipeline orchestrator"
    )
    parser.add_argument("--phase",        required=True,
                        help="Phase name, e.g. phase_01")
    parser.add_argument("--config",       default="config/settings.yaml",
                        help="Path to settings.yaml")
    parser.add_argument("--from-step",    type=int, default=1, metavar="N",
                        help="Start from step N (1-6, default: 1)")
    parser.add_argument("--to-step",      type=int, default=6, metavar="N",
                        help="Stop after step N (1-6, default: 6)")
    parser.add_argument("--dry-run",      action="store_true",
                        help="Parse and plan but write no files")
    parser.add_argument("--force-rerun",  action="store_true",
                        help="Re-process URLs already marked done in checkpoint DB")
    parser.add_argument("--force-refresh", action="store_true",
                        help="Re-download cached files (Step 2 only)")
    parser.add_argument("--ignore-registry", action="store_true",
                        help="Include versions already in converted_versions.json (Step 1 only)")
    args = parser.parse_args()

    settings  = load_settings(args.config)
    logs_dir  = Path(settings.get("logs_dir", "logs"))

    print(f"\nTIBCO Docs Converter")
    print(f"  Phase:     {args.phase}")
    print(f"  Steps:     {args.from_step} -> {args.to_step}")
    print(f"  Dry run:   {args.dry_run}")
    print(f"  Config:    {args.config}")

    steps_run = []
    for step_num, script, label in STEPS:
        if step_num < args.from_step or step_num > args.to_step:
            continue

        exit_code, elapsed = run_step(
            step_num, script, label,
            args.phase, args.config,
            args.dry_run, args.force_rerun, args.force_refresh,
            args.ignore_registry,
        )
        steps_run.append((step_num, script, label, exit_code, elapsed))

        if exit_code != 0:
            print(f"\nStep {step_num} failed — stopping pipeline.")
            print(f"To resume from this step: python run.py --phase {args.phase} --from-step {step_num}")
            break

    print_summary(args.phase, steps_run, logs_dir, args.dry_run)

    all_ok = all(exit_code == 0 for _, _, _, exit_code, _ in steps_run)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
