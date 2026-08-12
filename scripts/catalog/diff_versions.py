"""
scripts/catalog/diff_versions.py — Diff two tibco_versions.csv snapshots.

Reports versions added, removed, or changed between a baseline snapshot and a
newer fetch. Use after re-running fetch_versions.py to find releases published
since the last conversion run.

Rows are keyed on (product_slug, version).

Usage:
  python scripts/catalog/diff_versions.py manifests/catalog/tibco_versions_2026-08-12.csv tibco_versions.csv
  python scripts/catalog/diff_versions.py OLD.csv NEW.csv --out delta.csv
  python scripts/catalog/diff_versions.py OLD.csv NEW.csv --added-only
"""

import argparse
import csv
import sys
from pathlib import Path

FIELDS = [
    "change", "product_name", "product_slug",
    "version", "doc_url", "is_archived", "zip_url", "ga_date", "detail",
]


def load(path: Path) -> dict[tuple[str, str], dict]:
    with path.open(encoding="utf-8-sig") as f:
        return {(r["product_slug"], r["version"]): r for r in csv.DictReader(f)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Diff two tibco_versions.csv snapshots")
    parser.add_argument("old", type=Path, help="Baseline snapshot CSV")
    parser.add_argument("new", type=Path, help="Newer snapshot CSV")
    parser.add_argument("--out", type=Path, default=None, metavar="PATH",
                        help="Write delta rows to CSV (default: print only)")
    parser.add_argument("--added-only", action="store_true",
                        help="Report only added versions")
    args = parser.parse_args()

    for p in (args.old, args.new):
        if not p.exists():
            print(f"ERROR: not found: {p}", file=sys.stderr)
            return 1

    old, new = load(args.old), load(args.new)
    added   = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(k for k in set(old) & set(new) if old[k] != new[k])

    rows: list[dict] = []

    for k in added:
        rows.append({"change": "added", "detail": "", **{f: new[k][f] for f in new[k]}})

    if not args.added_only:
        for k in removed:
            rows.append({"change": "removed", "detail": "", **{f: old[k][f] for f in old[k]}})
        for k in changed:
            diffs = [f for f in new[k] if old[k][f] != new[k][f]]
            detail = "; ".join(f"{f}: {old[k][f] or '(empty)'} -> {new[k][f] or '(empty)'}"
                               for f in diffs)
            rows.append({"change": "changed", "detail": detail, **{f: new[k][f] for f in new[k]}})

    print(f"Baseline: {args.old}  ({len(old)} versions, {len({k[0] for k in old})} products)")
    print(f"Current:  {args.new}  ({len(new)} versions, {len({k[0] for k in new})} products)")
    print(f"\nAdded:   {len(added)}")
    for k in added:
        flag = "archived" if new[k]["is_archived"] == "True" else "active"
        print(f"  + {new[k]['product_name']}  {k[1]}  ({flag})")
    if not args.added_only:
        print(f"\nRemoved: {len(removed)}")
        for k in removed:
            print(f"  - {old[k]['product_name']}  {k[1]}")
        print(f"\nChanged: {len(changed)}")
        for k in changed:
            diffs = [f for f in new[k] if old[k][f] != new[k][f]]
            print(f"  ~ {new[k]['product_name']}  {k[1]}  [{', '.join(diffs)}]")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nDelta CSV written: {args.out.resolve()}  ({len(rows)} rows)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
