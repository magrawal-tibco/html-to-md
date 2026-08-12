# Version Catalog Snapshots

Point-in-time copies of `tibco_versions.csv` (produced by
`scripts/catalog/fetch_versions.py`). Each snapshot records every product
version known to docs.tibco.com on the date in its filename.

Purpose: after a conversion run completes, re-fetch the catalog and diff against
the snapshot taken at the start of that run to find releases published since.

## Snapshots

| Snapshot | Products | Versions | Notes |
|---|---|---|---|
| `tibco_versions_2026-08-12.csv` | 654 | 4527 | Baseline taken before the bulk conversion run |

## Refresh and diff

```bash
# 1. Fetch current catalog
.venv/Scripts/python scripts/catalog/fetch_versions.py --concurrency 20

# 2. Diff against the baseline snapshot
.venv/Scripts/python scripts/catalog/diff_versions.py \
    manifests/catalog/tibco_versions_2026-08-12.csv \
    tibco_versions.csv \
    --out delta.csv

# 3. Snapshot the new state for the next round
cp tibco_versions.csv manifests/catalog/tibco_versions_<YYYY-MM-DD>.csv
```

Rows are keyed on `(product_slug, version)`. `diff_versions.py` reports:

- **added** — version present now, absent in baseline (new releases)
- **removed** — version dropped from the catalog
- **changed** — same version, different `is_archived` / `zip_url` / `ga_date` / `doc_url`

## Caveat on `changed` rows

`fetch_versions.py` swallows HTTP errors (`_fetch_json` returns `None`), so a
transient archive-API failure looks identical to "this product has no archives".
A version flipping `is_archived: True -> False` **and** losing its `zip_url` is
more likely an API blip than a real un-archive. Re-run scoped to that product to
confirm before acting on it:

```bash
.venv/Scripts/python scripts/catalog/fetch_versions.py --product <slug> --out /tmp/check.csv
```
