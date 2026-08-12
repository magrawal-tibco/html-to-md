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
  python scripts/catalog/diff_versions.py OLD.csv NEW.csv --exclude-errors tibco_versions_errors.csv

If the fetch that produced NEW.csv reported endpoint failures, pass its
<out>_errors.csv via --exclude-errors. Products listed there are held out of the
diff and reported separately: their missing rows and dropped zip_urls are fetch
artefacts, not catalog changes.
"""

import argparse
import csv
import sys
from pathlib import Path

FIELDS = [
    "change", "product_name", "product_slug", "category",
    "version", "doc_url", "is_archived", "zip_url", "ga_date", "detail",
]


def load(path: Path) -> tuple[dict[tuple[str, str], dict], list[str]]:
    with path.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = {(r["product_slug"], r["version"]): r for r in reader}
        return rows, list(reader.fieldnames or [])


def main() -> int:
    parser = argparse.ArgumentParser(description="Diff two tibco_versions.csv snapshots")
    parser.add_argument("old", type=Path, help="Baseline snapshot CSV")
    parser.add_argument("new", type=Path, help="Newer snapshot CSV")
    parser.add_argument("--out", type=Path, default=None, metavar="PATH",
                        help="Write delta rows to CSV (default: print only)")
    parser.add_argument("--added-only", action="store_true",
                        help="Report only added versions")
    parser.add_argument("--exclude-errors", type=Path, default=None, metavar="PATH",
                        help="Errors CSV from fetch_versions.py; hold its products "
                             "out of the diff (their data is known-incomplete)")
    args = parser.parse_args()

    for p in (args.old, args.new):
        if not p.exists():
            print(f"ERROR: not found: {p}", file=sys.stderr)
            return 1

    old, old_fields = load(args.old)
    new, new_fields = load(args.new)

    # Compare only columns both files have. A schema change (e.g. adding
    # `category`) would otherwise mark every single row as changed.
    compare_fields = [f for f in new_fields if f in set(old_fields)]
    dropped = sorted(set(old_fields) ^ set(new_fields))
    if dropped:
        print(f"Note: schemas differ; comparing {len(compare_fields)} shared column(s). "
              f"Ignoring: {', '.join(dropped)}\n")

    excluded: set[str] = set()
    if args.exclude_errors:
        if not args.exclude_errors.exists():
            print(f"ERROR: not found: {args.exclude_errors}", file=sys.stderr)
            return 1
        with args.exclude_errors.open(encoding="utf-8-sig") as f:
            excluded = {r["product_slug"] for r in csv.DictReader(f)}
        held = {k for k in (set(old) | set(new)) if k[0] in excluded}
        old = {k: v for k, v in old.items() if k[0] not in excluded}
        new = {k: v for k, v in new.items() if k[0] not in excluded}
        print(f"Excluded {len(excluded)} product(s) with fetch errors "
              f"({len(held)} version rows held out) — data known-incomplete:")
        for slug in sorted(excluded):
            print(f"  ! {slug}")
        print()

    def _differs(k) -> bool:
        return any(old[k].get(f) != new[k].get(f) for f in compare_fields)

    added   = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(k for k in set(old) & set(new) if _differs(k))

    rows: list[dict] = []

    for k in added:
        rows.append({"change": "added", "detail": "", **{f: new[k][f] for f in new[k]}})

    if not args.added_only:
        for k in removed:
            rows.append({"change": "removed", "detail": "", **{f: old[k][f] for f in old[k]}})
        for k in changed:
            diffs = [f for f in compare_fields if old[k].get(f) != new[k].get(f)]
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
            diffs = [f for f in compare_fields if old[k].get(f) != new[k].get(f)]
            print(f"  ~ {new[k]['product_name']}  {k[1]}  [{', '.join(diffs)}]")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", newline="", encoding="utf-8-sig") as f:
            # extrasaction: tolerate snapshots carrying columns this tool predates
            writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nDelta CSV written: {args.out.resolve()}  ({len(rows)} rows)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
