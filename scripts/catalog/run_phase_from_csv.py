"""
run_phase_from_csv.py — Generate a phase YAML from the `phase` column in tibco_versions.csv.

Filters rows where the `phase` column matches <phase_name> and writes a phase YAML file to
config/phases/<phase_name>.yaml, ready to pass to run.py.

Usage:
  python scripts/catalog/run_phase_from_csv.py <phase_name>
  python scripts/catalog/run_phase_from_csv.py <phase_name> --dry-run
  python scripts/catalog/run_phase_from_csv.py <phase_name> --csv path/to/other.csv
  python scripts/catalog/run_phase_from_csv.py <phase_name> --out-yaml config/phases/custom.yaml
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

DEFAULT_CSV = "tibco_versions.csv"
PHASES_DIR = Path("config/phases")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a phase YAML from the `phase` column in tibco_versions.csv."
    )
    parser.add_argument("phase_name",
                        help="Value to match in the `phase` column (case-sensitive)")
    parser.add_argument("--csv", default=DEFAULT_CSV, metavar="PATH",
                        help=f"Source CSV (default: {DEFAULT_CSV})")
    parser.add_argument("--out-yaml", default=None, metavar="PATH",
                        help="Output YAML path (default: config/phases/<phase_name>.yaml)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the YAML to stdout without writing a file")
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

    if args.dry_run:
        print(yaml_content)
        print(f"# {len(doc_urls)} URL(s) from {len(rows)} matched row(s) in {csv_path.name}")
        return

    out_path = Path(args.out_yaml) if args.out_yaml else PHASES_DIR / f"{args.phase_name}.yaml"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if out_path.exists():
        print(f"[warn] Overwriting existing phase file: {out_path}")

    out_path.write_text(yaml_content, encoding="utf-8")

    print(f"[done] {len(doc_urls)} URL(s) written to {out_path}")
    print(f"\nNext step:")
    print(f"  python run.py --phase {args.phase_name}")


if __name__ == "__main__":
    main()
