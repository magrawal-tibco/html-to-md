# Table Audit Script — Context

## Purpose

`scripts/audit_tables.py` audits all `<table>` elements in Flare-generated HTML source
files and classifies them by structural complexity. Designed to plan a phased Markdown
migration and triage which tables require manual attention.

## Usage

```bash
# By phase — reads manifests/manifest_<NAME>.json to get exactly the pipeline's file list
python scripts/audit_tables.py --phase bw_plugins_poc

# By directory — recursively scans any cache subtree
python scripts/audit_tables.py --src cache/pub/activematrix_businessworks

# Both combined — deduplicates automatically; useful for cross-phase sweeps
python scripts/audit_tables.py --src cache/pub/foo --phase my_phase --out audit-output/combined
```

Output is always written to `audit-output/<name>/`:
- `tables.csv` — one row per table
- `summary.md` — counts, category breakdown, top-10 most complex tables

**Idempotent:** output is overwritten on each run. Re-run periodically to track migration progress.

## CSV columns

| Column | Description |
|---|---|
| `file_path` | Relative path from cache root |
| `table_index` | 1-based position within the file |
| `row_count` | Number of `<tr>` rows |
| `col_count` | Maximum columns across all rows |
| `categories` | Pipe-separated matched categories (see below) |
| `high_risk` | `"HIGH_RISK — confirmed silent content loss"` or empty |
| `near_heading` | Nearest preceding h1–h4 text (for human context) |
| `other_block_tags` | Tags triggering `OTHER_BLOCK_ELEMENTS`, comma-separated |
| `content_preview` | First 120 chars of the table's text content |

## Classification categories

A table can match multiple categories simultaneously.

| Category | Trigger | Notes |
|---|---|---|
| `SIMPLE` | No other categories match | Plain text cells — direct GFM pipe-table conversion |
| `FAKE_LIST` | `div.*_inner` or `AutoNumber` class pattern | AutoNumber fake lists — already handled by preprocessor's `fake_list_tables` pass; excluded from all other categories |
| `MULTI_PARAGRAPH` | Cell has >1 `<p>`, or `<p>` + other block sibling | Harder to fit in a single GFM cell |
| `NOTE_OR_ADMONITION` | Cell contains `<div class="note*">` | **HIGH RISK** — see below |
| `LIST_IN_CELL` | Cell contains `<ul>`, `<ol>`, or `<dl>` | Downstream parser preserves these (confirmed safe) |
| `MERGED_CELLS` | Any cell has `colspan > 1` or `rowspan > 1` | Not supported in GFM pipe tables |
| `NESTED_TABLE` | A `<table>` inside any cell | Must flatten or pass through as raw HTML |
| `WIDE` | More than 5 columns | GFM tables become unwieldy |
| `IMAGE_IN_CELL` | Cell contains `<img>` | |
| `CODE_IN_CELL` | Cell contains `<pre>` or `<code>` | |
| `OTHER_BLOCK_ELEMENTS` | Cell has a direct `<h1>`–`<h6>`, `<blockquote>`, `<figure>`, `<aside>`, `<details>`, or `<summary>` child | Unusual structural elements — review the tag inventory in the summary |

## HIGH RISK — NOTE_OR_ADMONITION

Pipeline: source `<div class="note*">` → preprocessor → `<blockquote>` → AEM/DITA →
**SILENTLY DROPPED** (confirmed in testing).

These tables must be addressed before the migration can be considered complete. Options:
1. Extract the admonition content from the table cell into a separate block before/after the table
2. Flatten the table into prose
3. Request a fix to the downstream AEM parser

## Admonition class names (discovered in source)

From inspection of 342 BW source files. Update `_NOTE_CONTAINER_RE` in the script
if new variants are found.

**Container classes** (what the script checks — outer element):
`note`, `noteImportant`, `noteCaution`, `noteTip`, `noteWarning`, `noteNote`

**Child label classes** (NOT checked — inside the container):
`noteHead`, `noteHeadInTable`

**Icon classes** (NOT checked — `<img>` inside the container):
`IconNote`, `IconTip`, `IconWarning`

Pattern matched: `^note([A-Z]|$)` — class exactly `"note"` or `"note"` + uppercase suffix.

## Fake-list table exclusion

MadCap Flare's AutoNumber feature renders bulleted/numbered lists as tables:
```html
<table><tr>
  <td><div class="Bullet_inner">•</div></td>
  <td><div class="Bullet_outer">Content here...</div></td>
</tr></table>
```

The preprocessor's `fake_list_tables` pass already converts these to `<ul>`/`<ol>` —
they don't need migration attention. Detected by presence of `*_inner` or `*_outer`
div class names. These are counted separately and excluded from all other categories.

## File filtering

Files skipped during collection:
- `Default.htm`, `Default_CSH.htm`, `Home.htm`, `index.htm`, `wwhsec.htm` — Flare shell pages
- `GUID-*.htm` — SDL Trisoft DITA WebHelp files (different format, separate pipeline)

## Sample results (bw_plugins_poc phase, 350 real tables)

| Category | Count | % |
|---|---|---|
| SIMPLE | 216 | 61.7% |
| MULTI_PARAGRAPH | 64 | 18.3% |
| NOTE_OR_ADMONITION | 63 | 18.0% |
| CODE_IN_CELL | 57 | 16.3% |
| LIST_IN_CELL | 46 | 13.1% |
| IMAGE_IN_CELL | 23 | 6.6% |
| MERGED_CELLS | 9 | 2.6% |
