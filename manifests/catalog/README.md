# Version Catalog Snapshots

Point-in-time copies of `tibco_versions.csv` (produced by
`scripts/catalog/fetch_versions.py`). Each snapshot records every product
version known to docs.tibco.com on the date in its filename.

Purpose: after a conversion run completes, re-fetch the catalog and diff against
the snapshot taken at the start of that run to find releases published since.

## Snapshots

| Snapshot | Products | Versions | Notes |
|---|---|---|---|
| `tibco_versions_2026-08-12.csv` | 659 | 4969 | Baseline taken before the bulk conversion run |

## The `category` column

The "Products & Solutions" grouping shown on
<https://docs.tibco.com/product/categories>, used to batch similar products into
conversion phases. Products with no grouping get **`Unassigned`** — currently 332
of 659, so this is the largest bucket by product count, though not by version
count.

| Category | Products | Versions |
|---|---:|---:|
| Unassigned | 332 | 1560 |
| Integration | 111 | 854 |
| Visual Analytics | 77 | 1075 |
| B2B | 27 | 212 |
| Data Science & Streaming | 24 | 240 |
| Master Data Management | 19 | 183 |
| Messaging | 12 | 215 |
| Event Processing | 11 | 149 |
| Foresight | 11 | 93 |
| Fulfillment Orchestration Suite | 5 | 42 |
| Mainframe | 5 | 44 |
| Data Grid | 4 | 50 |
| Data Virtualization | 4 | 39 |
| DataSynapse | 4 | 28 |
| Monitoring | 3 | 37 |
| Others | 3 | 14 |
| iProcess | 3 | 37 |
| Business Process Management | 2 | 41 |
| EBX | 1 | 54 |
| Social BPM | 1 | 2 |

Source is the `Groups` field already present on every `a_to_z` product — **not**
scraped from the categories page, which renders client-side and returns only an
8 KB shell to a plain GET. Validated against a browser-saved copy of that page:
the API lists 380 grouped products to the page's 378, with zero disagreements,
so it is a strict superset. The two extras are `tibco-messaging` (Messaging) and
`tibco-nimbus-player-desktop-edition` (Social BPM).

No product currently belongs to more than one group. If that changes, groups are
joined with `; ` rather than one being silently dropped.

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
- **changed** — same version, different `is_archived` / `zip_url` / `ga_date` /
  `doc_url` / `category`

Only columns present in *both* files are compared, so adding a column to the
schema does not mark every row as changed. Columns unique to one side are
reported once and ignored.

## Trusting a diff

`fetch_versions.py` distinguishes "this endpoint has no data" from "this endpoint
did not answer". Any endpoint that fails after retries is written to
`tibco_versions_errors.csv` next to the output CSV, and the run prints a warning
block. Use `--strict` to exit 2 when that happens.

**If an errors file was produced, the diff is not trustworthy as-is.** A product
whose archive endpoint failed loses its `zip_url` and `ga_date`, which is
indistinguishable from a genuine un-archive. Hold those products out:

```bash
.venv/Scripts/python scripts/catalog/diff_versions.py OLD.csv NEW.csv \
    --exclude-errors tibco_versions_errors.csv
```

No errors file means every endpoint answered definitively, and the delta can be
taken at face value. A clean run is byte-for-byte reproducible.

## History

The pre-2026-08-12 fetcher swallowed all HTTP exceptions and returned `None`, so
a timeout was indistinguishable from an empty result. Two consequences, both
fixed in the same change:

- **442 rows were silently missing.** Five large Spotfire products
  (`spotfire-server`, `spotfire-application`,
  `spotfire-enterprise-runtime-for-r-server-edition`, `spotfire-desktop`,
  `spotfire-statistics-services`) have version lists big enough to occasionally
  time out. On failure the fetcher emitted zero rows for them without complaint.
  Retries recovered all 442.
- **99 rows were duplicated.** `a_to_z` lists `spotfire-application` twice, under
  `Spotfire Application` and `Spotfire™ Application`, duplicating all of its
  versions. The list is now deduplicated by slug. The two entries are *not*
  equivalent — the bare-name one has no `Groups` — so the entry carrying a
  category wins, with name only as a tie-break. Picking by name alone would have
  left a 99-version product `Unassigned`.

Separately, ten versions across six BW plug-ins plus Product and Service Catalog
5.1.0 flipped `is_archived: True -> False` and lost their `zip_url` in this
snapshot. That was verified against the live API as a **real** catalog change,
not a fetch artefact: the archive endpoint returns
`{"success": true, ..., "children": []}` for those products, which is an
authoritative "no archived versions".
