# TIBCO Docs HTML → Markdown Converter

A Python pipeline that converts TIBCO product documentation — 600K+ pages across 584 products,
1500+ versions, and 4 business units (TIBCO, IBI, DataSynapse, EBX) — from HTML and PDF into
structured, AI-ready Markdown. Source content originates from multiple authoring tools including
MadCap Flare, DocBook, FrameMaker (WebWorks ePublisher), DITA, and Word, published at
[docs.tibco.com](https://docs.tibco.com).

## Overview

The pipeline downloads documentation ZIPs (or individual HTML pages as fallback), applies a
series of format-aware BeautifulSoup preprocessing transforms, and converts each topic to
GitHub-Flavored Markdown using [markdownify](https://github.com/matthewwithanm/python-markdownify).
Separate sub-pipelines handle DITA WebHelp Responsive (SDL Trisoft), WebWorks ePublisher
(legacy FrameMaker), and PDF release notes. Every output file includes YAML frontmatter with
metadata (title, TOC path, product version, context-sensitive help IDs).

## Why This Tool Exists

### The Problem

TIBCO's documentation catalog spans **600,000+ pages** across **584 products**, **1,500+ versions**, and **4 business units** — TIBCO, IBI, DataSynapse, and EBX. Content was authored in a wide range of tools over many years: MadCap Flare, DocBook, FrameMaker (published via WebWorks ePublisher), DITA (SDL Trisoft WebHelp Responsive), and Word. Each tool produces a different HTML or PDF output format, none of which is directly ingestible by Adobe Experience Manager (AEM) Guides — the platform TIBCO uses as its central authoring and publishing hub.

AEM Guides requires structured Markdown with YAML frontmatter, a specific folder layout per language and product version, and machine-readable TOC files (`toc.yml`). Manually converting even a single product version was a multi-day copy-paste exercise. At catalog scale it was not feasible at all: documentation was effectively **trapped in siloed HTML — invisible to AI systems and impossible to reformat consistently**.

### What This Tool Does

The pipeline automates the full conversion lifecycle for all four business units and all supported source formats:

- **Discovers** all product versions from the docs.tibco.com API or sitemap hierarchy — no manual URL lists
- **Downloads** documentation ZIPs (or individual HTML pages as fallback), along with images, alias.xml CSH mappings, and archive assets, to a local cache
- **Converts** each HTML topic to clean Markdown with accurate frontmatter (title, language, TOC path, product name/version, CSH IDs), with format-aware transforms per authoring tool
- **Post-processes** the converted files: rewrites internal `.htm` links to relative `.md` links, strips authoring-tool tokens, normalises heading levels, rewrites external Java API links, and generates synthetic section index pages for TOC grouping nodes that have no source page
- **Reconstructs the TOC** from authoritative MadCap TOC JS files (when available from ZIPs) or from per-page `data-mc-toc-path` breadcrumbs as fallback, emitting `_toc.json` and `toc.yml` compatible with AEM Guides
- **Runs sub-pipelines** automatically for DITA WebHelp (SDL Trisoft), WebWorks ePublisher (legacy FrameMaker), and PDF release notes within the same orchestrated run
- **Restructures** EBX output into the language-first folder layout required by AEM Guides, and copies PDF/doc assets alongside with generated index pages

### Key Benefits

- **Zero manual work per version** — adding a new product or version requires only one URL in a phase YAML; the pipeline handles everything else
- **Format-agnostic** — a single orchestrator handles MadCap Flare, DocBook, DITA, WebWorks ePublisher, and PDF source formats through dedicated sub-pipelines
- **Idempotent and resumable** — each step is independently re-runnable; a SQLite progress database checkpoints completed URLs so interrupted runs pick up where they left off
- **Auditable output** — every converted page carries frontmatter linking it back to its source URL, language, product, and version; structured JSON reports and a persistent `conversion_log.csv` give per-run statistics
- **Handles real-world messiness** — dozens of product-specific quirks (fake-list tables, DITA task markup, encoding artifacts, missing alias files, multi-language variants) are handled declaratively through preprocessor passes and settings, not manual cleanup

---

## How This Tool Was Developed

This pipeline is built and maintained using **[Claude Code](https://claude.ai/code)** — Anthropic's agentic CLI that operates directly on the codebase. The development workflow is:

1. **Discover** — a product-specific quirk is observed in the output (e.g. a numbered list rendered as bullets, a broken link, a missing TOC entry). The problem is described in plain language.
2. **Investigate** — Claude Code reads the relevant source HTML, traces through the preprocessor and converter code, and identifies the exact line or transform responsible.
3. **Fix** — a minimal, surgical change is made to the correct script without touching unrelated code.
4. **Verify** — the affected step is re-run against real data; the output is inspected to confirm the fix without regressions.
5. **Document** — `CLAUDE.md` (the AI context file) and this README are updated to record the new behaviour.

`CLAUDE.md` acts as the **persistent memory** for Claude Code across sessions: it describes the HTML source structure, what each script does, which quirks have already been handled, and what conventions the pipeline follows. This means:

- **No onboarding cost** — a new Claude Code session immediately understands the architecture and can make precise, targeted changes
- **Incremental, safe evolution** — the modular phase/step design means each fix is contained; unrelated behaviour is not disturbed
- **Self-updating documentation** — every structural change is reflected in both `CLAUDE.md` and this README in the same session that made the change

This approach has made it practical for a small team to build and evolve a production-grade documentation pipeline covering thousands of product versions, across multiple languages and output formats, without dedicated engineering time per product.

---

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
# Full pipeline run for a phase (includes DITA + PDF sub-pipelines automatically)
python run.py --phase businessevents

# Resume from step 3 (steps 1–2 already done)
python run.py --phase businessevents --from-step 3

# Dry run — no files written
python run.py --phase businessevents --dry-run

# Re-convert already-processed files
python run.py --phase businessevents --from-step 3 --force-rerun

# Run a single step directly
python scripts/03_convert.py --phase businessevents
```

## Complete Workflow

`run.py` runs the full end-to-end conversion for a phase in three sequential stages:

```
python run.py --phase <name>
```

### Stage 1 — Main Pipeline (Steps 1–7)

| Step | Script | What it does |
|------|--------|-------------|
| 1 | `01_build_manifest.py` | Resolve product version URLs → build manifest JSON |
| 2a | `02a_download_zip.py` | Download full documentation ZIPs and extract |
| 2 | `02_download.py` | Download individual HTML pages (fallback for missing ZIPs) |
| 3 | `03_convert.py` | Convert HTML → Markdown (use `--scan-cache` flag if ZIP path structure differs from sitemap URLs) |
| 4 | `04_build_csh_maps.py` | Build context-sensitive help maps from alias.xml |
| 5 | `05_postprocess.py` | Rewrite links, strip variable tokens |
| 6 | `06_build_toc.py` | Build `_toc.json` per version |
| 7 | `07_generate_report.py` | Write `phase_report.csv` and update `conversion_log.csv` |

If any step fails, the pipeline stops and prints a resume command.

### Stage 2 — DITA Sub-pipeline (automatic if DITA versions detected)

After Step 7 completes, `run.py` checks `manifests/dita_versions_<phase>.json`. If it is non-empty, the DITA sub-pipeline runs automatically:

```
scripts/dita/run.py --phase <name>
```

| Step | Script | What it does |
|------|--------|-------------|
| 1 | `01_rename_guids.py` | Rename GUID filenames to human-readable names (sdl_dita only) |
| 2 | `02_convert.py` | Convert DITA HTML → Markdown |
| 3 | `03_build_csh_maps.py` | Build CSH maps from head.js |
| 4 | `04_build_toc.py` | Build TOC from body.js / suitehelp_topic_list.html |

Skip with `--skip-dita` if you want to run the DITA pipeline separately.

### Stage 3 — PDF Release Notes Sub-pipeline (always runs)

After the main pipeline (and DITA if applicable), release notes PDFs are converted:

```
scripts/pdf/convert.py --phase <name>
```

- Finds all `*relnotes*.pdf` / `*release-notes*.pdf` files in cache for the phase
- Extracts text using pymupdf with font-size-based heading detection
- Skips cover pages, TOC pages, and boilerplate sections
- Outputs `output/.../doc/pdf/relnotes.md` alongside the HTML conversion

Skip with `--skip-pdf` if you only want HTML conversion.

### Stage 4 — WebWorks ePublisher Sub-pipeline (automatic if detected)

After the PDF sub-pipeline, `run.py` checks whether any version in cache contains a `wwhelp/books.htm` file (the signature of WebWorks ePublisher output). If found, the WebWorks sub-pipeline runs automatically:

```
scripts/webworks/run.py --phase <name>
```

| Step | Script | What it does |
|------|--------|-------------|
| 1 | `convert.py` | Extract `<blockquote>` content, convert WebWorks elements (N1/N2/N3Heading, Bullet_outer, Step_outer, Code) → Markdown |
| 2 | `build_toc.py` | Parse `wwhdata/xml/toc.xml` per guide → hierarchical `_toc.json` |
| 3 | `build_csh_maps.py` | Parse `ctx/<guide><id>.htm` JS redirects + `wwhdata/xml/files.xml` → `csh_map.json` + inject `csh_ids` into frontmatter |

**When this applies:** Legacy TIBCO products authored in Adobe FrameMaker and published via WebWorks ePublisher (e.g. ActiveMatrix BusinessWorks 5.x, BusinessEvents Data Modeling). These use XHTML output with numeric filenames (`admin.4.01.htm`) instead of MadCap Flare's HTML5 output.

TOC source is `wwhdata/xml/toc.xml` (hierarchical, authoritative). The `_toc.json` `"_source"` field will be `"webworks_toc_xml"`.

Skip with `--skip-webworks` if you want to run the WebWorks pipeline separately.

### CLI flags

| Flag | Applies to | Description |
|------|-----------|-------------|
| `--from-step N` | Main pipeline | Start from step N |
| `--to-step N` | Main pipeline | Stop after step N |
| `--dry-run` | All stages | Parse and plan but write no files |
| `--force-rerun` | All stages | Re-process already-done files |
| `--force-refresh` | Step 2 only | Re-download cached HTML |
| `--ignore-registry` | Step 1 only | Include already-converted versions |
| `--scan-cache` | Step 3 only | Drive conversion from cached files instead of sitemap manifest (use when ZIP paths differ from sitemap URLs) |
| `--skip-dita` | DITA stage | Skip DITA sub-pipeline |
| `--skip-pdf` | PDF stage | Skip PDF sub-pipeline |
| `--skip-webworks` | WebWorks stage | Skip WebWorks ePublisher sub-pipeline |

## Folder Structure

```
html-to-md/
├── run.py                          # Orchestrator (--phase, --from-step, --to-step, --dry-run)
├── requirements.txt
├── config/
│   ├── settings.yaml               # All tunable settings
│   └── phases/
│       ├── phase_template.yaml     # Annotated template — copy to create a new phase
│       └── <name>.yaml             # Product version page URLs (one per phase)
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
│   ├── catalog/
│   │   └── fetch_versions.py       # Fetch all product versions from docs.tibco.com → tibco_versions.csv
│   ├── dita/                       # DITA WebHelp Responsive sub-pipeline
│   │   └── run.py                  # DITA orchestrator
│   ├── pdf/
│   │   └── convert.py              # PDF release notes → Markdown (pymupdf)
│   ├── webworks/                   # WebWorks ePublisher sub-pipeline (FrameMaker legacy)
│   │   ├── convert.py              # WebWorks HTML → Markdown
│   │   ├── build_toc.py            # toc.xml → _toc.json
│   │   ├── build_csh_maps.py       # ctx/*.htm → csh_map.json
│   │   ├── run.py                  # WebWorks orchestrator
│   │   └── utils.py                # Shared discovery + file-reading helpers
│   └── lib/
│       ├── manifest_utils.py       # Shared URL/path helpers (skip logic, alias.xml URL, output path)
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
| 1 | `01_build_manifest.py` | Phase YAML | `manifests/manifest_<phase>.json`, `empty_versions_<phase>.json` |
| 2a | `02a_download_zip.py` | Manifest JSON | `cache/` — full ZIP extracted; manifest expanded to per-page entries; `zip_registry_<phase>.json`, `zip_missing_<phase>.json` |
| 2 | `02_download.py` | Manifest + zip_registry | `cache/` — HTML, images, alias.xml (skips versions covered by ZIP) |
| 3 | `03_convert.py` | Manifest + cache/ | `output/**/*.md` + images |
| 4 | `04_build_csh_maps.py` | cache/ alias.xml | `output/.../csh_map.json` + updated frontmatter |
| 5 | `05_postprocess.py` | output/**/*.md | Updated .md files (in-place) |
| 6 | `06_build_toc.py` | cache/ TOC JS + output/**/*.md | `output/.../_toc.json` per version |
| 7 | `07_generate_report.py` | All manifests + output/ | `logs/.../phase_report.csv`, `manifests/conversion_log.csv` |

### ZIP-first download (Step 2a)

Step 2a downloads the full documentation ZIP per version and extracts it into `cache/`. After extraction it scans the HTML files and rewrites the manifest with per-page entries (expanding the version-level entries from Step 1). This provides:

- **Authoritative TOC** — `Data/Tocs/*.js` files give exact hierarchy and page order (Step 6 prefers these over breadcrumb reconstruction)
- **Efficiency** — one request per version instead of hundreds of individual page requests
- **Completeness** — all HTML pages, images, and PDFs in one download

For new-format phase files (product version URLs), the ZIP URL is resolved via the products API — no sitemap crawl needed. For legacy sitemap phases, the ZIP URL is inferred from the sitemap slug.

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

Phase files (`config/phases/<name>.yaml`) define which product versions to process. Copy `config/phases/phase_template.yaml` to create a new phase.

### Primary format — product version page URLs (recommended)

```yaml
name: "BusinessEvents"
products:
  - https://docs.tibco.com/products/tibco-businessevents-enterprise-edition-6-4-0
  - https://docs.tibco.com/products/tibco-businessevents-enterprise-edition-6-3-2
```

Each entry is a versioned product page URL. Step 1 calls `/api/products/<slug>` to get the `folder_path`, then constructs the exact documentation ZIP URL:

```
https://docs.tibco.com/pub/<folder_path>/<versioned-slug>_documentation.zip
```

Step 2a downloads and extracts the ZIP, then scans the HTML files to produce per-page manifest entries. No sitemap crawl is needed.

**Finding version URLs:** Run `scripts/catalog/fetch_versions.py` to generate `tibco_versions.csv` — a full catalog of all 736 products and their versions with `doc_url` column ready to paste into a phase file. You can also find version URLs on [docs.tibco.com](https://docs.tibco.com) product pages.

### Legacy format — sitemap URLs (backward-compatible)

```yaml
name: "Phase (legacy)"

# L2 product sitemaps — all versions discovered automatically
products:
  - https://docs.tibco.com/ftp_portal/coveo/tibco-businessevents-enterprise-edition.xml

# L3 version sitemaps — target a specific version directly
versions:
  - https://docs.tibco.com/ftp_portal/coveo/tibco-businessevents-enterprise-edition-6-4-0.xml
```

| Key | When it's a sitemap URL | Behavior |
|-----|------------------------|---------|
| `products:` (`.xml`) | L2 product sitemapindex | Crawls all version sitemaps under the product |
| `versions:` | L3 version urlset | Processes exactly the specified version |

Step 1 detects the URL type automatically — `.xml` / `ftp_portal` paths use the sitemap crawl; `/products/` paths use the API approach.

> **Note:** The docs.tibco.com sitemaps contain stale `localhost:5001` hostnames for many products and do not list current versions reliably. The product version URL approach is preferred for all new phases.

## Fetching Product Version URLs

`scripts/catalog/fetch_versions.py` queries the docs.tibco.com products API and writes a CSV of every product version across all 736 products:

```bash
python scripts/catalog/fetch_versions.py
# → writes tibco_versions.csv (takes ~1 minute, ~4500 rows)

# Limit to a single product
python scripts/catalog/fetch_versions.py --product tibco-businessevents-enterprise-edition

# Custom output path
python scripts/catalog/fetch_versions.py --out catalog/versions_2026.csv
```

Output columns:

| Column | Description |
|--------|-------------|
| `product_name` | Human-readable product name |
| `product_slug` | Parent product slug |
| `version` | Version string (e.g. `6.4.0`) |
| `doc_url` | Product version page URL — paste directly into a phase `products:` list |
| `is_archived` | `True` if listed under "Other Versions" |
| `zip_url` | Direct ZIP download URL (archived versions only) |
| `ga_date` | GA release date (archived versions only) |

The `doc_url` column contains the exact URLs used in `products:` phase entries.

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

## Uploading Output to Google Drive (Optional)

Output files can be synced to a shared Google Drive using [rclone](https://rclone.org/).

### One-time setup

1. Download rclone from [rclone.org/downloads](https://rclone.org/downloads/)
2. Configure a Google Drive remote:
   ```bash
   rclone config
   ```
   Choose: `n` (new remote) → name it `gdrive` → type `drive` → leave Client ID/Secret blank → scope `1` (full access) → auto config `y` (browser OAuth) → confirm.

3. To target a **Shared Drive**, find the drive ID:
   ```bash
   rclone backend drives gdrive:
   ```

### Upload command

```bash
# Upload to a personal Google Drive folder
rclone copy output/ gdrive:tibco-docs-md/output --progress

# Upload to a Shared Drive (replace DRIVE_ID with the ID from the list above)
rclone copy output/ gdrive:tibco-docs-md/output \
  --drive-team-drive DRIVE_ID \
  --progress
```

For the **Technical Communication** shared drive (ID: `0ABuCk67wIMFvUk9PVA`):

```bash
rclone copy output/ gdrive:tibco-docs-md/output \
  --drive-team-drive 0ABuCk67wIMFvUk9PVA \
  --progress
```

Use `rclone sync` instead of `rclone copy` to mirror exactly (deletes files on Drive that no longer exist locally).

---

## Known Source Variations

| Product | Variation | Handling |
|---------|-----------|----------|
| BusinessWorks | `AutoNumber_p_*` table classes as fake lists | `fake_list_tables` transform |
| BusinessWorks | `AutoNumber_p_Bullet` class occasionally used on numbered-step tables | `data-mc-autonum` attribute checked as tiebreaker; digit value → `<ol>` |
| BusinessEvents 6.4.0 | DITA task/concept/reference structure | `task_sections` + `definition_lists` transforms |
| SDL Trisoft / DITA products | GUID-based filenames (`GUID-xxx.html`) | Filtered in Step 1 as `dita`; written to `dita_versions_<phase>.json` |
| Javadoc products | `/api/javadoc/` path segment | Filtered in Step 1 as `non-madcap-html` |
| EBX | Relative `Java_API/` links in converted Markdown | Rewritten in Step 5 to `https://stg-docs.onebx.com/us/en/ebx/resources/javadocs/<version>/` |
| EBX | TOC grouping nodes with no source page (`"file": null`) | Step 6 generates `_section_<slug>.md` index pages; TOC node gets a `url:` in `toc.yml` |
| All products | MadCap variable tokens in TOC path (`[%=System.LinkedHeader%]`) | Stripped in Step 5 |
| All products | Empty or 404 `alias.xml` | Handled silently (not an error) |
| All products | ZIP unavailable (HTTP 404) | Logged to `zip_missing_<phase>.json`; Step 2 falls back to web crawl |
