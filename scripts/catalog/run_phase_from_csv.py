"""
run_phase_from_csv.py — Generate a phase YAML from the `phase` column in tibco_versions.csv,
then immediately invoke run.py to start the conversion pipeline.

Filters rows where the `phase` column matches <phase_name>, writes a phase YAML to
config/phases/<phase_name>.yaml, and runs: python run.py --phase <phase_name> [flags].

Usage:
  python scripts/catalog/run_phase_from_csv.py <phase_name>
  python scripts/catalog/run_phase_from_csv.py <phase_name> --dry-run
  python scripts/catalog/run_phase_from_csv.py <phase_name> --csv path/to/other.csv
  python scripts/catalog/run_phase_from_csv.py <phase_name> --out-yaml config/phases/custom.yaml
  python scripts/catalog/run_phase_from_csv.py <phase_name> --from-step 3 --force-rerun
"""
from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from pathlib import Path

DEFAULT_CSV = "tibco_versions.csv"
PHASES_DIR = Path("config/phases")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a phase YAML from the `phase` column in tibco_versions.csv, "
                    "then run the conversion pipeline."
    )
    parser.add_argument("phase_name",
                        help="Value to match in the `phase` column (case-sensitive)")
    parser.add_argument("--csv", default=DEFAULT_CSV, metavar="PATH",
                        help=f"Source CSV (default: {DEFAULT_CSV})")
    parser.add_argument("--out-yaml", default=None, metavar="PATH",
                        help="Output YAML path (default: config/phases/<phase_name>.yaml)")

    # Flags forwarded verbatim to run.py
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the YAML and the run.py command without executing either")
    parser.add_argument("--from-step", metavar="N",
                        help="Pass --from-step N to run.py")
    parser.add_argument("--to-step", metavar="N",
                        help="Pass --to-step N to run.py")
    parser.add_argument("--force-rerun", action="store_true",
                        help="Pass --force-rerun to run.py")
    parser.add_argument("--force-refresh", action="store_true",
                        help="Pass --force-refresh to run.py")
    parser.add_argument("--ignore-registry", action="store_true",
                        help="Pass --ignore-registry to run.py")
    parser.add_argument("--delta", action="store_true",
                        help="Pass --delta to run.py")
    parser.add_argument("--skip-dita", action="store_true",
                        help="Pass --skip-dita to run.py")
    parser.add_argument("--skip-pdf", action="store_true",
                        help="Pass --skip-pdf to run.py")
    parser.add_argument("--skip-webworks", action="store_true",
                        help="Pass --skip-webworks to run.py")
    parser.add_argument("--skip-restructure", action="store_true",
                        help="Pass --skip-restructure to run.py")
    parser.add_argument("--config", default="config/settings.yaml", metavar="PATH",
                        help="Pass --config to run.py (default: config/settings.yaml)")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        sys.exit(f"[error] CSV not found: {csv_path}")

    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if "phase" not in (reader.fieldnames or []):
            sys.exit(
                "[error] The CSV has no `phase` column. "
                "Add a `phase` column and fill in the phase name for the rows you want to include."
            )
        rows = [r for r in reader if r.get("phase", "").strip() == args.phase_name]

    if not rows:
        sys.exit(
            f"[error] No rows found with phase='{args.phase_name}' in {csv_path}.\n"
            f"Make sure the `phase` column is filled in for the versions you want."
        )

    doc_urls = [r["doc_url"].strip() for r in rows if r.get("doc_url", "").strip()]
    if not doc_urls:
        sys.exit(f"[error] Matched {len(rows)} row(s) but none have a `doc_url` value.")

    # Build YAML content
    lines = [
        f'name: "{args.phase_name}"',
        "products:",
    ]
    for url in doc_urls:
        lines.append(f"  - {url}")
    yaml_content = "\n".join(lines) + "\n"

    out_path = Path(args.out_yaml) if args.out_yaml else PHASES_DIR / f"{args.phase_name}.yaml"

    # Build run.py command
    run_cmd = [sys.executable, "run.py", f"--phase={args.phase_name}",
               f"--config={args.config}"]
    if args.from_step:
        run_cmd.append(f"--from-step={args.from_step}")
    if args.to_step:
        run_cmd.append(f"--to-step={args.to_step}")
    if args.force_rerun:
        run_cmd.append("--force-rerun")
    if args.force_refresh:
        run_cmd.append("--force-refresh")
    if args.ignore_registry:
        run_cmd.append("--ignore-registry")
    if args.delta:
        run_cmd.append("--delta")
    if args.skip_dita:
        run_cmd.append("--skip-dita")
    if args.skip_pdf:
        run_cmd.append("--skip-pdf")
    if args.skip_webworks:
        run_cmd.append("--skip-webworks")
    if args.skip_restructure:
        run_cmd.append("--skip-restructure")

    if args.dry_run:
        print(yaml_content)
        print(f"# {len(doc_urls)} URL(s) from {len(rows)} matched row(s) in {csv_path.name}")
        print(f"\n# Would write: {out_path}")
        print(f"# Would run:   {' '.join(run_cmd)}")
        return

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        print(f"[warn] Overwriting existing phase file: {out_path}")
    out_path.write_text(yaml_content, encoding="utf-8")
    print(f"[done] {len(doc_urls)} URL(s) written to {out_path}")

    print(f"\nStarting pipeline: {' '.join(run_cmd)}\n")
    result = subprocess.run(run_cmd)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
