"""
scripts/webworks/run.py — WebWorks ePublisher sub-pipeline orchestrator.

Runs three steps in sequence:
  1. convert.py      — HTML content → Markdown
  2. build_toc.py    — toc.xml → _toc.json
  3. build_csh_maps.py — ctx/*.htm → csh_map.json + frontmatter injection

Usage:
  python scripts/webworks/run.py --phase bw
  python scripts/webworks/run.py --phase bw --dry-run
  python scripts/webworks/run.py --phase bw --force-rerun
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# (name, script, supports_force_rerun)
STEPS = [
    ("convert",   "scripts/webworks/convert.py",       True),
    ("toc",       "scripts/webworks/build_toc.py",      False),
    ("csh",       "scripts/webworks/build_csh_maps.py", True),
]

LABELS = {
    "convert": "Convert HTML to Markdown",
    "toc":     "Build TOC JSON",
    "csh":     "Build CSH Maps",
}


def run_step(script: str, label: str, phase: str, config: str,
             dry_run: bool, force_rerun: bool, supports_force_rerun: bool) -> tuple[int, float]:
    cmd = [sys.executable, script, f"--phase={phase}", f"--config={config}"]
    if dry_run:
        cmd.append("--dry-run")
    if force_rerun and supports_force_rerun:
        cmd.append("--force-rerun")

    print(f"\n{'='*60}")
    print(f"  WebWorks: {label}")
    print(f"  Command: {' '.join(cmd)}")
    print(f"{'='*60}")

    start = time.time()
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    result = subprocess.run(cmd, text=True, env=env)
    elapsed = round(time.time() - start, 1)

    status = "OK" if result.returncode == 0 else f"FAILED (exit {result.returncode})"
    print(f"\n  {label} {status} in {elapsed}s")
    return result.returncode, elapsed


def main():
    parser = argparse.ArgumentParser(description="WebWorks sub-pipeline orchestrator")
    parser.add_argument("--phase",       required=True)
    parser.add_argument("--config",      default="config/settings.yaml")
    parser.add_argument("--dry-run",     action="store_true")
    parser.add_argument("--force-rerun", action="store_true")
    args = parser.parse_args()

    print(f"\nWebWorks Sub-pipeline | phase={args.phase}")

    all_ok = True
    for name, script, supports_force_rerun in STEPS:
        label = LABELS[name]
        rc, elapsed = run_step(
            script, label, args.phase, args.config,
            args.dry_run, args.force_rerun, supports_force_rerun,
        )
        if rc != 0:
            print(f"\nWebWorks step '{name}' failed — stopping.")
            all_ok = False
            break

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
