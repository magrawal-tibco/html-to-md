"""
run.py — DITA pipeline orchestrator.

Reads zip_registry to identify file_dita and sdl_dita versions, then runs:
  Step 1: Build GUID rename maps (sdl_dita only)
  Step 2: Convert HTML → Markdown (both types)
  Step 3: Build CSH maps from head.js (both types)
  Step 4: Build TOC JSON from body.js / suitehelp_topic_list.html (both types)

Usage:
  python scripts/dita/run.py --phase phase_01
         [--config config/dita_settings.yaml]
         [--from-step 1] [--to-step 4]
         [--dry-run] [--force-rerun]
"""

import argparse
import subprocess
import sys
from pathlib import Path


def run_step(script: str, args: list[str], step_num: int, step_name: str) -> int:
    cmd = [sys.executable, script] + args
    print(f"\n{'='*60}")
    print(f"DITA Step {step_num}: {step_name}")
    print(f"  {' '.join(cmd)}")
    print(f"{'='*60}")
    result = subprocess.run(cmd)
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description="DITA pipeline orchestrator (Steps 1–4)")
    parser.add_argument("--phase",       required=True, help="Phase name, e.g. phase_01")
    parser.add_argument("--config",      default="config/dita_settings.yaml")
    parser.add_argument("--from-step",   type=int, default=1, metavar="N")
    parser.add_argument("--to-step",     type=int, default=4, metavar="N")
    parser.add_argument("--dry-run",     action="store_true")
    parser.add_argument("--force-rerun", action="store_true")
    args = parser.parse_args()

    scripts_dir = Path(__file__).parent
    base_args = ["--phase", args.phase, "--config", args.config]
    if args.dry_run:
        base_args.append("--dry-run")

    steps = {
        1: ("01_rename_guids.py", "Rename GUIDs (sdl_dita)"),
        2: ("02_convert.py",      "Convert HTML → Markdown"),
        3: ("03_build_csh_maps.py", "Build CSH maps"),
        4: ("04_build_toc.py",    "Build TOC JSON"),
    }

    for step_num in range(args.from_step, args.to_step + 1):
        if step_num not in steps:
            print(f"Unknown step {step_num} — skipping")
            continue

        script_name, step_name = steps[step_num]
        script_path = str(scripts_dir / script_name)

        step_args = list(base_args)
        if args.force_rerun and step_num in (1, 2):
            step_args.append("--force-rerun")

        rc = run_step(script_path, step_args, step_num, step_name)
        if rc != 0:
            print(f"\nStep {step_num} failed with exit code {rc}. Stopping.")
            return rc

    print("\nDITA pipeline complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
