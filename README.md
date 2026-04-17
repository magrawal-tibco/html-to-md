# TIBCO Docs HTML → Markdown Converter

A Python pipeline that converts TIBCO product documentation (~2000 product versions) from HTML to plain Markdown. The source is MadCap Flare WebHelp2 HTML output published at [docs.tibco.com](https://docs.tibco.com), crawled via a 3-level sitemap hierarchy.

## Overview

The pipeline downloads documentation HTML (or full documentation ZIPs where available), runs a series of BeautifulSoup preprocessing transforms to clean up MadCap-specific markup, then converts to GitHub-Flavored Markdown using [markdownify](https://github.com/matthewwithanm/python-markdownify). Each Markdown file includes YAML frontmatter with metadata (title, TOC path, product version, context-sensitive help IDs).

## Requirements

- Python 3.11+
- Dependencies in `requirements.txt`

```bash
python -m venv .venv
.venv\Scripts\activate       # Windows
source .venv/bin/activate    # Linux/Mac
pip install -r requirements.txt
```

## Quick Start

```bash
# Full pipeline run for a phase
python run.py --phase phase_01

# Resume from step 3 (steps 1–2 already done)
python run.py --phase phase_01 --from-step 3

# Dry run — no files written
python run.py --phase phase_01 --dry-run

# Re-convert already-processed files
python run.py --phase phase_03 --from-step 3 --force-rerun

# Run a single step directly
python scripts/03_convert.py --phase phase_01
```

## Folder Structure

```
html-to-md/
├── run.py                          # Orchestrator (--phase, --from-step, --to-step, --dry-run)
├── requirements.txt
├── config/
│   ├── settings.yaml               # All tunable settings
│   └── phases/
│       ├── phase_template.yaml     # Annotated template — copy to create a new phase
│       ├── phase_01.yaml           # L2 product or L3 version sitemap URLs
│       └── phase_02.yaml
├── scripts/
│   ├── 01_build_manifest.py        # Sitemap crawl → manifests/manifest_<phase>.json
│   ├── 02a_download_zip.py         # Download + extract per-version documentation ZIPs
│   ├── 02_download.py              # HTML + images + alias.xml → cache/ (fallback)
│   ├── 03_convert.py               # HTML → Markdown with preprocessor transforms
│   ├── 04_build_csh_maps.py        # alias.xml → csh_map.json + frontmatter injection
│   ├── 05_postprocess.py           # Rewrite .htm links → .md, strip variable tokens
│   ├── 06_build_toc.py             # Build _toc.json (prefers ZIP TOC JS, falls back to breadcrumbs)
│   ├── 07_generate_report.py       # Write phase_report.csv and update conversion_log.csv
│   ├── compare_toc.py              # Compare _toc.json against authoritative MadCap TOC JS files
│   └── lib/
│       ├── sitemap_parser.py       # 3-level sitemap crawl functions
│       ├── toc_parser.py           # MadCap WebHelp2 TOC JS parsing (shared by steps 6 + compare_toc)
│       ├── preprocessor.py         # 13 BeautifulSoup transform passes
│       ├── table_classifier.py     # Tier 1/2/3 table classification
│       └── reporter.py             # Structured logging + JSON report writing
├── manifests/                      # Generated JSON manifests — committed to git
│   └── conversion_log.csv          # Persistent cross-phase conversion log
├── cache/                          # Downloaded HTML + images — gitignored
├── output/                         # Converted Markdown files — gitignored
└── logs/                           # Per-run logs and reports — gitignored
```

## Pipeline Steps

| Step | Script | Input | Output |
|------|--------|-------|--------|
| 1 | `01_build_manifest.py` | Phase YAML | `manifests/manifest_<phase>.json`, `dita_versions_<phase>.json`, `empty_versions_<phase>.json` |
| 2a | `02a_download_zip.py` | Manifest JSON | `cache/` — full ZIP extracted; `zip_registry_<phase>.json`, `zip_missing_<phase>.json` |
| 2 | `02_download.py` | Manifest + zip_registry | `cache/` — HTML, images, alias.xml (skips versions covered by ZIP) |
| 3 | `03_convert.py` | Manifest + cache/ | `output/**/*.md` + images |
| 4 | `04_build_csh_maps.py` | cache/ alias.xml | `output/.../csh_map.json` + updated frontmatter |
| 5 | `05_postprocess.py` | output/**/*.md | Updated .md files (in-place) |
| 6 | `06_build_toc.py` | cache/ TOC JS + output/**/*.md | `output/.../_toc.json` per version |
| 7 | `07_generate_report.py` | All manifests + output/ | `logs/.../phase_report.csv`, `manifests/conversion_log.csv` |

### ZIP-first download (Step 2a)

TIBCO publishes a documentation ZIP per product version at a predictable URL. Step 2a downloads and extracts these ZIPs, which provides:

- **Authoritative TOC** — `Data/Tocs/*.js` files give exact hierarchy and page order (Step 6 prefers these over breadcrumb reconstruction)
- **Efficiency** — one request per version instead of hundreds of individual page requests
- **Completeness** — all HTML pages, images, and PDFs in one download

Versions where the ZIP is missing or fails are written to `zip_missing_<phase>.json` and fall back to individual page downloading in Step 2.

ZIP settings in `config/settings.yaml`:

```yaml
zip:
  enabled: true
  store_zip: true        # Keep .zip after extraction (false = delete to save disk space)
  zip_cache_dir: "cache/zip"
  min_free_gb: 20        # Skip version if free disk space drops below this
```

## Phase Files

Phase files (`config/phases/<name>.yaml`) define which products or versions to process. Copy `config/phases/phase_template.yaml` to create a new phase.

Two keys are supported and can be combined:

```yaml
name: "Phase 3 — BusinessEvents"

# L2 product sitemaps — all versions discovered automatically
products:
  - https://docs.tibco.com/ftp_portal/coveo/tibco-businessevents-enterprise-edition.xml

# L3 version sitemaps — target a specific version directly
versions:
  - https://docs.tibco.com/ftp_portal/coveo/tibco-businessevents-enterprise-edition-6-4-0.xml
```

| Key | Level | Discovers |
|-----|-------|-----------|
| `products:` | L2 product sitemapindex | All versions under the product automatically |
| `versions:` | L3 version urlset | Exactly the specified version |

Sitemap URL patterns:
- **L2:** `https://docs.tibco.com/ftp_portal/coveo/tibco-<product-slug>.xml`
- **L3:** `https://docs.tibco.com/ftp_portal/coveo/tibco-<product-slug>-<X-Y-Z>.xml`

The master sitemap at `https://docs.tibco.com/sitemap.xml` lists all L2 URLs.

## Sitemap Hierarchy

```
https://docs.tibco.com/sitemap.xml                              (master sitemapindex)
  └─ https://docs.tibco.com/ftp_portal/coveo/tibco-foo.xml     (product sitemapindex, L2)
       └─ https://docs.tibco.com/ftp_portal/coveo/tibco-foo-1-0.xml  (version urlset, L3)
```

L3 urlsets use the `coveo:` namespace extension for metadata (product name, version, doc name).

## TOC Reconstruction (Step 6)

Step 6 builds `_toc.json` per version using the best available source:

1. **MadCap TOC JS** (authoritative) — uses `Data/Tocs/*.js` files from the extracted ZIP when present. These give exact hierarchy depth and page order as authored.
2. **Breadcrumbs** (fallback) — reconstructs the tree from `data-mc-toc-path` attributes in each page's `<html>` tag. Accurate for page membership but flattens deep hierarchies.

The `_toc.json` includes a `"_source"` field (`"toc_js"` or `"breadcrumbs"`) indicating which method was used.

To compare a reconstructed `_toc.json` against the authoritative MadCap TOC JS:

```bash
python scripts/compare_toc.py \
  --toc-js-dir "path/to/Data/Tocs" \
  --toc-json   "output/pub/product/version/doc/html/_toc.json"
```

## Preprocessor Transforms

`scripts/lib/preprocessor.py` applies 13 transforms in order before markdownify runs:

| # | Transform | What it does |
|---|-----------|--------------|
| 1 | `strip_chrome` | Removes nav/UI chrome (toolbar, breadcrumbs, feedback survey, copy buttons) |
| 2 | `fake_list_tables` | `AutoNumber_p_*` table classes → proper `<ul>`/`<ol>` |
| 3 | `callout_divs` | `div.note/warning/caution/tip/important` → `<blockquote>` with bold label |
| 4 | `text_popups` | MCTextPopup inline popups → Note blockquotes; trigger marker removed |
| 5 | `definition_lists` | DITA `div.dl/dlentry/dt/dd` → bold term + unwrapped definition |
| 6 | `task_sections` | DITA task structure (prereq, steps, result, postreq) → semantic HTML |
| 7 | `inline_spans` | MadCap span classes → `<strong>`, `<code>`, `<em>` |
| 8 | `anchor_only_links` | Strips `<a name="...">` anchors with no href |
| 9 | `split_colspan_tables` | Full-width colspan rows → `<h4>` headings + sub-tables |
| 10 | `classify_tables` | 3-tier table classification (see below) |
| 11 | `normalize_whitespace` | Collapses `\r\n\t` in text nodes (browser whitespace rules) |
| 12 | `fix_pre_linebreaks` | Replaces `<br>` inside `<pre>` with actual newlines |
| 13 | `rewrite_image_src` | Makes image paths relative to output .md location |

## Table Classification (3 Tiers)

`scripts/lib/table_classifier.py` classifies each table before conversion:

| Tier | Cell content | Output |
|------|-------------|--------|
| **Tier 1** | Plain text only | GFM pipe table |
| **Tier 2** | Inline HTML (`<strong>`, `<em>`, `<code>`, `<a>`) | GFM pipe table (inline flattened) |
| **Tier 3** | Block content (`<ul>`, `<ol>`, `<pre>`, nested tables, headings) | Raw HTML passthrough |

Tables without a `<thead>` have their first row automatically promoted to a header row so GFM tables render correctly.

## Output Format

### Frontmatter

Every converted topic gets YAML frontmatter:

```yaml
---
title: "Page Title"
source_url: "https://docs.tibco.com/pub/product/version/doc/html/path/file.htm"
lang: "en-us"
topic_type: "concept"            # concept | task | reference
toc_path: "Section|Subsection"  # from data-mc-toc-path attribute
product_name: "TIBCO BusinessEvents® Enterprise Edition"
product_version: "6.4.0"
doc_name: "Administration Guide"
csh_ids: [1000, 1001]           # only present if alias.xml maps this page
csh_names: ["TOPIC_ID"]         # only present if alias.xml maps this page
---
```

### Output Path Structure

Output mirrors the URL path from docs.tibco.com:

```
https://docs.tibco.com/pub/businessevents-enterprise/6.4.0/doc/html/Admin/file.htm
→ output/pub/businessevents-enterprise/6.4.0/doc/html/Admin/file.md
```

Images are copied alongside their referencing Markdown file.

## Reporting (Step 7)

Step 7 runs automatically at the end of every phase and writes:

- **`logs/<phase>/<timestamp>/phase_report.csv`** — snapshot of all versions processed in this run
- **`manifests/conversion_log.csv`** — persistent log appended every run, one row per version

### Conversion Log Columns

| Column | Description |
|--------|-------------|
| Phase | Phase name |
| Run Date | ISO timestamp of the run |
| Product Name | From coveo:metadata |
| Version | Product version string |
| Document Name | Doc set name from coveo:metadata |
| Status | `madcap` / `dita` / `no_html` |
| Version Sitemap URL | L3 sitemap URL |
| Public URL | `https://docs.tibco.com/pub/<slug>/<version>/` |
| Topics in Sitemap | Raw page count from sitemap |
| Topics Converted | Actual `.md` files written to output/ |
| CSH ID Count | Number of context-sensitive help ID mappings |
| PDFs Found | PDF files found in cache for this version |
| ZIP Status | `extracted` / `missing` / `na` |
| TOC Source | `toc_js` (authoritative) / `breadcrumbs` (fallback) / `none` |
| Phase Total Time (s) | Total elapsed seconds for the full pipeline run |

### Version Status Values

| Status | Meaning |
|--------|---------|
| `madcap` | MadCap Flare WebHelp2 output — converted successfully |
| `dita` | SDL Trisoft / DITA WebHelp output (GUID filenames) — skipped, future phase |
| `no_html` | Sitemap had entries but none were accepted HTML pages (PDFs, ZIPs, etc.) |

## Configuration

All settings are in `config/settings.yaml`:

```yaml
base_url: "https://docs.tibco.com"
output_dir: "output"
cache_dir: "cache"

http:
  concurrency: 20
  delay_seconds: 0.5
  max_retries: 3
  timeout_connect: 10
  timeout_read: 30

zip:
  enabled: true
  store_zip: true
  zip_cache_dir: "cache/zip"
  min_free_gb: 20

content_selectors:
  - "div[role='main']#mc-main-content"   # MadCap Flare WebHelp2
  - "div#center article"                 # DITA WebHelp Responsive
  - "article"

chrome_selectors:                        # Elements stripped before conversion
  - p.MCWebHelpFramesetLink
  - div.toolbar
  - div.breadcrumbs
  - a.codeSnippetCopyButton
  # ... etc.

tables:
  passthrough_block_tags:                # Cell content forcing Tier 3 passthrough
    - ul
    - ol
    - pre
    - table
    # ... etc.
```

## Logging & Reports

Each step writes to its own timestamped folder under `logs/<phase>/`:

```
logs/<phase>/<YYYYMMDD-HHMMSS>/
  run.log              # Full verbose log
  errors.log           # Errors only
  skipped.log          # Filtered URLs with reason code
  01_manifest.json     # Step 1 counts and errors
  02a_zip.json         # Step 2a counts (ZIP downloads)
  02_download.json     # Step 2 counts
  03_convert.json      # Step 3 counts
  04_csh.json          # Step 4 counts
  05_postprocess.json  # Step 5 counts
  06_toc.json          # Step 6 counts
  07_report.json       # Step 7 counts
  phase_report.csv     # Per-version report for this run
```

## Known Source Variations

| Product | Variation | Handling |
|---------|-----------|----------|
| BusinessWorks | `AutoNumber_p_*` table classes as fake lists | `fake_list_tables` transform |
| BusinessEvents 6.4.0 | DITA task/concept/reference structure | `task_sections` + `definition_lists` transforms |
| SDL Trisoft / DITA products | GUID-based filenames (`GUID-xxx.html`) | Filtered in Step 1 as `dita`; written to `dita_versions_<phase>.json` |
| Javadoc products | `/api/javadoc/` path segment | Filtered in Step 1 as `non-madcap-html` |
| All products | MadCap variable tokens in TOC path (`[%=System.LinkedHeader%]`) | Stripped in Step 5 |
| All products | Empty or 404 `alias.xml` | Handled silently (not an error) |
| All products | ZIP unavailable (HTTP 404) | Logged to `zip_missing_<phase>.json`; Step 2 falls back to web crawl |
