# TIBCO Docs Converter — Claude Code Context

## Project Overview

Python pipeline that converts TIBCO product documentation from HTML (docs.tibco.com) to
structured Markdown for Adobe Experience Manager (AEM) Guides import. Covers 600K+ pages,
584 products, 1500+ versions, 4 business units (TIBCO, IBI, DataSynapse, EBX). Source formats:
MadCap Flare WebHelp2, WebWorks ePublisher (FrameMaker), DITA WebHelp Responsive (SDL Trisoft),
and PDF.

**Workspace root:** `c:\github\html-to-md\`
**Python:** 3.11+ (venv at `.venv/`)
**Run:** `python run.py --phase <name>`

See `README.md` for human-readable overview, setup instructions, and Google Drive upload guide.
See `README-EBX.md` for EBX-specific end-to-end run guide.

---

## Folder Structure

```
html-to-md/
├── run.py                          # Orchestrator (--phase, --from-step, --to-step, --dry-run)
├── pyrightconfig.json              # Pylance config — excludes cache/output/logs from indexing
├── requirements.txt
├── CLAUDE.md
├── config/
│   ├── settings.yaml               # All tunable settings
│   ├── pdf_slug_mappings.yaml      # Filename slug → human-readable guide label
│   └── phases/
│       ├── phase_template.yaml     # Annotated template for new phases
│       └── <name>.yaml             # Phase definition (product version URLs or sitemap URLs)
├── scripts/
│   ├── 01_build_manifest.py        # Sitemap/API crawl → manifests/manifest_<phase>.json
│   ├── 02a_download_zip.py         # Download + extract per-version documentation ZIPs
│   ├── 02_download.py              # HTML + images + alias.xml → cache/ (fallback for missing ZIPs)
│   ├── 03_convert.py               # HTML → Markdown with preprocessor transforms
│   ├── 04_build_csh_maps.py        # alias.xml → csh_map.json + frontmatter injection
│   ├── 05_postprocess.py           # Rewrite .htm links → .md, strip variable tokens
│   ├── 06_build_toc.py             # Build _toc.json (prefers ZIP TOC JS, falls back to breadcrumbs)
│   ├── 07_generate_report.py       # Write phase_report.csv + update manifests/conversion_log.csv
│   ├── 08_restructure_ebx.py       # EBX main: URL-mirror → language-first AEM layout + PDF/doc assets
│   ├── ebx_addon_restructure.py    # EBX add-on: version-first → addon-first layout
│   ├── tibco_restructure.py        # TIBCO/DataSynapse: version-first → language-first AEM layout
│   ├── copy_assets.py              # Generic: copy PDF/doc assets + generate index.md + toc.yml
│   ├── 10_copy_ebx_addon_pdfs.py   # EBX add-on: copy all PDFs → ebx-addons repo + index.md + toc.yml
│   ├── audit_tables.py             # Migration planning: classify all <table> elements in HTML source
│   ├── audit_tables_context.md     # Context doc for audit_tables.py (classification rules, examples)
│   ├── compare_toc.py              # Compare _toc.json against authoritative MadCap TOC JS files
│   ├── catalog/
│   │   ├── fetch_versions.py       # Query docs.tibco.com API → tibco_versions.csv (all products)
│   │   └── diff_versions.py        # Diff two catalog snapshots → added/removed/changed versions
│   ├── dita/                       # DITA WebHelp Responsive sub-pipeline (SDL Trisoft)
│   │   └── run.py                  # DITA orchestrator
│   ├── pdf/
│   │   └── convert.py              # PDF release notes → Markdown (pymupdf, font-size heading detect)
│   ├── webworks/                   # WebWorks ePublisher sub-pipeline (legacy FrameMaker)
│   │   ├── convert.py              # WebWorks HTML → Markdown
│   │   ├── build_toc.py            # toc.xml → _toc.json
│   │   ├── build_csh_maps.py       # ctx/*.htm JS redirects → csh_map.json
│   │   ├── run.py                  # WebWorks orchestrator
│   │   └── utils.py                # Discovery + file-reading helpers
│   └── lib/
│       ├── io_utils.py             # load_settings, load_manifest, read/write_frontmatter
│       ├── manifest_utils.py       # URL/path helpers, skip logic, alias.xml URL, output path
│       ├── sitemap_parser.py       # 3-level sitemap crawl functions
│       ├── toc_parser.py           # MadCap WebHelp2 TOC JS parsing (shared by steps 6 + compare_toc)
│       ├── preprocessor.py         # 13 BeautifulSoup transform passes
│       ├── table_classifier.py     # Tier 1/2/3 table classification
│       ├── reporter.py             # Structured logging + JSON report writing
│       ├── asset_copy.py           # PDF/doc asset copy + slug resolution (shared by 08 + copy_assets)
│       └── version_registry.py     # Track already-converted versions across runs
├── manifests/                      # Generated JSON manifests — commit these
│   ├── conversion_log.csv          # Persistent cross-phase conversion log (committed)
│   └── catalog/                    # Dated tibco_versions.csv snapshots for release-delta runs
├── audit-output/                   # Table audit reports (tables.csv + summary.md per phase/src)
├── cache/                          # Downloaded HTML + images — gitignored
├── output/                         # Converted Markdown files — gitignored
└── logs/                           # Per-run logs and reports — gitignored
```

---

## Pipeline Steps

`run.py` runs steps 1–7 in sequence, then automatically triggers sub-pipelines if applicable.

| Step | Script | Input | Output |
|------|--------|-------|--------|
| 1 | `01_build_manifest.py` | Phase YAML | `manifests/manifest_<phase>.json`, `empty_versions_<phase>.json` |
| 2a | `02a_download_zip.py` | Manifest | `cache/` — full ZIP extracted; manifest expanded; `zip_registry_<phase>.json`, `zip_missing_<phase>.json` |
| 2 | `02_download.py` | Manifest + zip_registry | `cache/` — HTML, images, alias.xml (skips versions covered by ZIP) |
| 3 | `03_convert.py` | Manifest + cache/ | `output/**/*.md` + images |
| 4 | `04_build_csh_maps.py` | cache/ alias.xml | `output/.../csh_map.json` + updated frontmatter |
| 5 | `05_postprocess.py` | output/**/*.md | Updated .md files (in-place) |
| 6 | `06_build_toc.py` | cache/ TOC JS + output/**/*.md | `output/.../_toc.json` + `toc.yml` + `_section_*.md` per version |
| 7 | `07_generate_report.py` | All manifests + output/ | `logs/.../phase_report.csv`, `manifests/conversion_log.csv` |

### Sub-pipelines (automatic after Step 7)

| Sub-pipeline | Trigger | Script |
|---|---|---|
| DITA | `dita_versions_<phase>.json` non-empty | `scripts/dita/run.py` |
| PDF release notes | Always (unless `--skip-pdf`) | `scripts/pdf/convert.py` |
| WebWorks ePublisher | `wwhelp/books.htm` found in cache | `scripts/webworks/run.py` |

### Restructure scripts (manual — not called by run.py)

Must be run after the pipeline completes, and re-run after any step 3/5/6 rerun:

| Script | What it does |
|---|---|
| `scripts/08_restructure_ebx.py` | EBX main: `output/pub/ebx/` → `output/ebx/` (language-first) |
| `scripts/ebx_addon_restructure.py` | EBX add-on: `output/pub/ebx-addon/` → `output/ebx-addon/` (addon-first) |
| `scripts/tibco_restructure.py` | TIBCO/DataSynapse: `output/pub/<product>/` → `output/<product>/` (language-first) |

### CLI flags

| Flag | Applies to | Description |
|------|-----------|-------------|
| `--from-step N` | Main pipeline | Start from step N |
| `--to-step N` | Main pipeline | Stop after step N |
| `--dry-run` | All stages | Parse and plan but write no files |
| `--force-rerun` | All stages | Re-process already-done files |
| `--force-refresh` | Step 2 only | Re-download cached HTML |
| `--scan-cache` | Step 3 only | Drive conversion from cached files (use when ZIP paths differ from sitemap URLs) |
| `--skip-dita` | DITA stage | Skip DITA sub-pipeline |
| `--skip-pdf` | PDF stage | Skip PDF sub-pipeline |
| `--skip-webworks` | WebWorks stage | Skip WebWorks ePublisher sub-pipeline |

---

## Table Audit Tool

`scripts/audit_tables.py` — standalone migration-planning utility, **not part of the pipeline**.
Scans Flare HTML source files and classifies every `<table>` by structural complexity.

```bash
python scripts/audit_tables.py --phase bw_plugins_poc          # from manifest file list
python scripts/audit_tables.py --src cache/pub/activematrix_businessworks  # from directory
```

Output in `audit-output/<name>/`: `tables.csv` (per table) + `summary.md` (counts + top 10 complex).

**Categories** (a table can match multiple):

| Category | Trigger |
|---|---|
| `SIMPLE` | No other categories matched |
| `FAKE_LIST` | `AutoNumber` class or `*_inner`/`*_outer` divs — preprocessor handles these |
| `MULTI_PARAGRAPH` | Cell has >1 `<p>` or `<p>` + other block sibling |
| `NOTE_OR_ADMONITION` | Cell has `<div class="note*">` → **HIGH RISK**: becomes `<blockquote>` which AEM **silently drops** |
| `LIST_IN_CELL` | Cell has `<ul>`, `<ol>`, or `<dl>` |
| `MERGED_CELLS` | Any cell has `colspan > 1` or `rowspan > 1` |
| `NESTED_TABLE` | `<table>` inside any cell |
| `WIDE` | More than 5 columns |
| `IMAGE_IN_CELL` | Cell has `<img>` |
| `CODE_IN_CELL` | Cell has `<pre>` or `<code>` |
| `OTHER_BLOCK_ELEMENTS` | Direct cell child is `<h1>`–`<h6>`, `<blockquote>`, `<figure>`, `<aside>`, `<details>`, `<summary>` |

Admonition container class regex: `^note([A-Z]|$)` — matches `note`, `noteImportant`, `noteCaution`,
`noteTip`, `noteWarning`, `noteNote`. Does not match child-label classes (`noteHead`, `noteHeadInTable`)
or `AuthorNote`. See `scripts/audit_tables_context.md` for full detail.

---

## Key Technical Facts

### Phase Files (config/phases/)

Two supported formats — Step 1 detects automatically:

**Product version URL format (preferred):**
```yaml
name: "BusinessEvents"
products:
  - https://docs.tibco.com/products/tibco-businessevents-enterprise-edition-6-4-0
```
Step 1 calls `/api/products/<slug>` → gets `folder_path` → constructs ZIP URL. No sitemap crawl needed.

**Legacy sitemap format:**
```yaml
name: "Phase (legacy)"
products:                    # L2 sitemapindex — all versions discovered automatically
  - https://docs.tibco.com/ftp_portal/coveo/tibco-businessevents-enterprise-edition.xml
versions:                    # L3 urlset — target a specific version
  - https://docs.tibco.com/ftp_portal/coveo/tibco-businessevents-enterprise-edition-6-4-0.xml
```

### Sitemap Hierarchy (3 levels)
```
https://docs.tibco.com/sitemap.xml                              (master sitemapindex)
  → https://docs.tibco.com/ftp_portal/coveo/tibco-foo.xml      (product sitemapindex, L2)
    → https://docs.tibco.com/ftp_portal/coveo/tibco-foo-1-0.xml (version urlset, L3)
```
- L3 urlset uses namespace `http://www.sitemaps.org/schemas/sitemap/0.9/sitemap.xsd` (note `/sitemap.xsd` suffix variant) plus `coveo:` namespace for metadata
- Always parse XML with explicit namespace mapping; do not use wildcard namespace queries
- Sitemaps contain stale `localhost:5001` hostnames for many products — product version URL format preferred for new phases

### HTML Structure (MadCap Flare WebHelp2)
```html
<html lang="en-us" data-mc-toc-path="Section|Subsection|Page Title" class="concept">
  <body>
    <p class="MCWebHelpFramesetLink">...</p>   <!-- strip -->
    <div id="prdnm">...</div>                  <!-- strip -->
    <div class="toolbar">...</div>             <!-- strip -->
    <div class="page-content">
      <div class="breadcrumbs">...</div>       <!-- strip -->
      <div class="topic-frame">
        <div>
          <div role="main" id="mc-main-content">  <!-- EXTRACT THIS -->
            <h1>...</h1>
            ... content ...
          </div>
          <div class="MCMiniTocBox_0">...</div>   <!-- strip -->
        </div>
        <div id="feedback-survey">...</div>       <!-- strip -->
      </div>
    </div>
    <div><p class="Copyright">...</p></div>       <!-- strip -->
  </body>
</html>
```
- **Content selector:** `div[role="main"]#mc-main-content`
- **Language:** `<html lang="...">` attribute
- **TOC path:** `<html data-mc-toc-path="Section|Sub Section">` — pipe-separated breadcrumb
- **Topic type:** `<html class="concept|task|reference">` — drives frontmatter field

### Shell Pages (filter in Step 1)
`Default.htm`, `Default_CSH.htm`, `Home.htm` — JS-only frameset entry points, no content body.
Controlled by `skip_filenames` in settings.yaml.

### Non-MadCap HTML (filter in Step 1)
- URLs containing `/api/javadoc/` — standard Javadoc, logged as `non-madcap-html`
- GUID-based filenames (`GUID-xxx.html`) — SDL Trisoft DITA, logged as `non-madcap-dita`,
  written to `dita_versions_<phase>.json` for the DITA sub-pipeline

### Non-Content Directories (filter in Step 1)
`_globalpages/`, `MicroContent/`, `_templates/`, `Skins/`, `Resources/`

### ZIP-first download (Step 2a)
Downloads the full documentation ZIP per version and extracts into `cache/`. After extraction,
rewrites the manifest with per-page entries. Provides:
- Authoritative TOC JS files (`Data/Tocs/*.js`) for Step 6
- All HTML pages + images in one request per version

Versions where ZIP is missing/404 go to `zip_missing_<phase>.json` and fall back to Step 2.
Already-extracted versions (non-empty `Data/Tocs/`) are skipped unless `--force-rerun`.

ZIP settings in `config/settings.yaml`:
```yaml
zip:
  enabled: true
  store_zip: true        # Keep .zip after extraction
  zip_cache_dir: "cache/zip"
  min_free_gb: 20        # Skip version if disk space drops below this
```

### alias.xml (Context-Sensitive Help)
- URL: `<version-html-root>/Data/Alias.xml` — not in sitemap, fetched separately per version
- Format: `<Map Name="TOPIC_ID" Link="relative/path.htm" ResolvedId="1000"/>`
  - `Name` = alphanumeric CSH identifier (case-significant by design)
  - `ResolvedId` = numeric CSH identifier
  - `Link` = relative path to the topic .htm file
- Empty `<CatapultAliasFile />` — handle silently, not an error
- 404 alias.xml — handle silently

### TOC (Step 6)
Two sources, in preference order:
1. **MadCap TOC JS** (authoritative) — `Data/Tocs/*.js` from extracted ZIP. Gives exact
   hierarchy and page order as authored. `_toc.json` `"_source"` = `"toc_js"`.
2. **Breadcrumbs** (fallback) — reconstructs tree from `data-mc-toc-path` on each `<html>` tag.
   `_toc.json` `"_source"` = `"breadcrumbs"`.

- Pages with empty/missing `toc_path` go into `_orphans` in `_toc.json`
- **Section index pages:** Step 6 generates `_section_<slug>.md` for every TOC node with
  children but no source page (`"file": null`). Gets frontmatter + `# Title` + subtree link list.
  Node's `file` field updated so `toc.yml` emits a `url:`.
- **External URL injection:** TOC nodes matching known external titles (e.g. "Java API") have
  `file` set to the external URL. `node_to_yaml()` detects `http://`/`https://` prefix and
  emits `url:` directly.
- **WebWorks TOC source:** `wwhdata/xml/toc.xml` (hierarchical, authoritative).
  `_toc.json` `"_source"` = `"webworks_toc_xml"`.

### Tables (3 tiers — see table_classifier.py)
- **Tier 1:** Text-only cells → GFM pipe table
- **Tier 2:** Cells with inline HTML only (strong, em, code, a) → flatten + GFM pipe table
- **Tier 3:** Cells with block content (ul, ol, pre, nested tables, h2+) → raw HTML passthrough,
  marked with `data-converter-passthrough="true"` for manual review

Tables without `<thead>` have first row promoted to header automatically.

### Preprocessor Transforms (order matters — see preprocessor.py)
13 transforms applied before markdownify:

| # | Name | What it does |
|---|------|------|
| 1 | `strip_chrome` | Removes nav/UI chrome (`chrome_selectors` in settings.yaml) |
| 2 | `fake_list_tables` | `AutoNumber_p_*` table class → `<ul>`/`<ol>` |
| 3 | `callout_divs` | `div.note/warning/caution/tip/important` → `<blockquote>` with bold label |
| 4 | `text_popups` | MCTextPopup inline popups → Note blockquotes; trigger marker removed |
| 5 | `definition_lists` | DITA `div.dl/dlentry/dt/dd` → bold term + unwrapped definition |
| 6 | `task_sections` | DITA task elements (prereq, steps, result, postreq, context) → semantic HTML |
| 7 | `inline_spans` | MadCap span classes → `<strong>`, `<code>`, `<em>` |
| 8 | `anchor_only_links` | Strip `<a name="...">` with no href (MadCap nav anchors) |
| 9 | `split_colspan_tables` | Full-width colspan rows → `<h4>` headings + sub-tables |
| 10 | `classify_tables` | 3-tier table classification (calls table_classifier.py) |
| 11 | `normalize_whitespace` | Collapse `\r\n\t` in text nodes (browser whitespace rules) |
| 12 | `fix_pre_linebreaks` | Replace `<br>` inside `<pre>` with actual newlines |
| 13 | `rewrite_image_src` | Make image paths relative to output .md location |

`inline_spans` class mappings:
- `uicontrol`, `wintitle`, `option`, `menucascade` → `<strong>`
- `filepath`, `codeph` → `<code>`
- `varname`, `parmname`, `term` → `<em>`
- `<var>` element → `<em>`

### Frontmatter Schema
```yaml
---
title: "Page Title"
source_url: "https://docs.tibco.com/pub/product/version/doc/html/path/file.htm"
lang: "en-us"
topic_type: "concept"               # concept | task | reference | "" if unknown
toc_path: "Section|Subsection"      # from data-mc-toc-path; empty segments removed
product_name: "TIBCO BusinessEvents® Enterprise Edition"
product_version: "6.4.0"
doc_name: "Administration Guide"    # from coveo:metadata d_name field
csh_ids: [1000, 1001]              # only if alias.xml maps this page; omit field if none
csh_names: ["TOPIC_ID"]            # only if alias.xml maps this page; omit field if none
---
```

### Output Path Structure
Mirrors URL path from docs.tibco.com, extension changed to .md:
```
https://docs.tibco.com/pub/businessevents-enterprise/6.4.0/doc/html/Admin/file.htm
→ output/pub/businessevents-enterprise/6.4.0/doc/html/Admin/file.md

Images alongside:
→ output/pub/businessevents-enterprise/6.4.0/doc/html/Admin/images/figure1.png
```

Restructure scripts remap to language-first AEM layout:
```
output/pub/ebx/<version>/doc/html/<lang>/<content>
  → output/ebx/<lang-norm>/ebx/webhelp/<ver-dashed>/<content>
```

---

## Configuration (config/settings.yaml)

```yaml
base_url: "https://docs.tibco.com"
output_dir: "output"
cache_dir: "cache"
manifests_dir: "manifests"
logs_dir: "logs"

http:
  concurrency: 20
  delay_seconds: 0.5
  max_retries: 3
  backoff_factor: 2
  timeout_connect: 10
  timeout_read: 30
  user_agent: "tibco-docs-converter/1.0"

zip:
  enabled: true
  store_zip: true
  zip_cache_dir: "cache/zip"
  min_free_gb: 20

content_selectors:
  - "div[role='main']#mc-main-content"   # MadCap Flare WebHelp2
  - "div#center article"                 # DITA WebHelp Responsive
  - "article"

chrome_selectors:                        # Elements stripped by strip_chrome transform
  - p.MCWebHelpFramesetLink
  - div#prdnm
  - div.toolbar
  - div.breadcrumbs
  - div.MCMiniTocBox_0
  - div#feedback-survey
  - p.Copyright
  - a.codeSnippetCopyButton

image_skip_prefixes:
  - Skins/
  - Resources/Scripts/
  - Resources/Stylesheets/

tables:
  passthrough_block_tags:
    - ul
    - ol
    - pre
    - blockquote
    - h1
    - h2
    - h3
    - table
```

---

## Logging & Reports

```
logs/<phase>/<YYYYMMDD-HHMMSS>/
  run.log              # Full verbose log (all steps)
  errors.log           # Errors only
  skipped.log          # Filtered URLs with reason code
  01_manifest.json     # Step 1 stats
  02a_zip.json         # Step 2a stats (ZIP downloads)
  02_download.json     # Step 2 stats
  03_convert.json      # Step 3 stats
  04_csh.json          # Step 4 stats
  05_postprocess.json  # Step 5 stats
  06_toc.json          # Step 6 stats
  07_report.json       # Step 7 stats
  phase_report.csv     # Per-version report for this run
```

`manifests/conversion_log.csv` — persistent log appended every run, committed to git.

Progress checkpointed in SQLite (`logs/progress.db`). Re-runs skip already-completed URLs.

---

## Test Suite

```bash
.venv/Scripts/python -m pytest tests/ -v
.venv/Scripts/python -m pytest tests/test_preprocessor.py -v
```

| File | Tests | Coverage |
|---|---|---|
| `tests/test_preprocessor.py` | 48 | `strip_chrome`, `fake_list_tables` (incl. `data-mc-autonum` tiebreaker), `callout_divs`, `ebx_callout_divs`, `inline_spans`, `anchor_only_links`, `code_urls_to_links`, `_table_column_count`, `rewrite_image_src` |
| `tests/test_table_classifier.py` | 22 | `_cell_tier`, `classify_table`, `_promote_first_row_as_header`, `handle_tables` |
| `tests/test_toc.py` | 19 | `insert_into_tree`, `version_html_root`, `dir_fallback` majority-vote logic |

Total: **89 tests**. All must pass before committing changes to preprocessor, table classifier, or TOC logic.

---

## EBX-Specific Post-Processing

EBX documentation ZIPs have a richer structure and require restructure scripts after Steps 1–7.

### EBX archive structure (cache/pub/ebx/<version>/doc/)
```
doc/
├── html/
│   ├── en/      → webhelp (converted by Steps 1-7)
│   ├── fr/      → webhelp (French, 6.1.1+)
│   └── ja/      → webhelp (Japanese, 6.1.1+)
├── relnotes/    → relnotes.md (generated by PDF sub-pipeline)
├── pdf/         → PDF files (copied by 08_restructure_ebx.py)
└── doc/         → Other documents (copied by 08_restructure_ebx.py)
```

### 08_restructure_ebx.py

Transforms URL-mirroring layout → AEM Guides language-first layout. Copies PDF/doc assets.

**Webhelp restructure:**
```
output/pub/ebx/<version>/doc/html/<lang>/<content>
  → output/ebx/<lang-norm>/ebx/webhelp/<ver-dashed>/<content>
```

**PDF/doc asset copy (Phase 4):**
```
cache/pub/ebx/<version>/doc/pdf/   → output/ebx/en-us/ebx/pdf/<ver-dashed>/
cache/pub/ebx/<version>/doc/doc/   → output/ebx/en-us/ebx/doc/<ver-dashed>/
```

Each `pdf/` and `doc/` version folder gets `index.md` + `toc.yml`.

```bash
python scripts/08_restructure_ebx.py [--src output/pub/ebx] [--dst output/ebx] \
                                      [--cache-src cache/pub/ebx] [--preflight-only]
```

**Sequencing:** Step 5 must run before Step 8. If Step 3 is re-run, always follow with Step 5
before Step 8 — otherwise Step 8 copies un-postprocessed files. Step 8 warns if it detects this.

`Java_API/` folder is unconditionally excluded — Java API is hosted externally. Step 5 rewrites
relative `Java_API/` links to `https://stg-docs.onebx.com/us/en/ebx/resources/javadocs/<version>/`.

### ebx_addon_restructure.py

Transforms version-first layout → addon-first layout. Runs 6 phases:
- Phase 0: Pre-flight cross-addon link scan
- Phase 1: Build webhelp path mapping (excludes Java_API)
- Phase 2: Build javadocs path mapping
- Phase 3: Copy webhelp files
- Phase 4: Copy javadoc files
- Phase 5: Patch `_toc.json` root and `file` paths
- Phase 6: Rewrite EBX-main javadoc URLs → addon-specific URLs; strip MadCap popup links

**Webhelp restructure:**
```
output/pub/ebx-addon/<version>/doc/<addon>/<content>
  → output/ebx-addon/en-us/ebx-addon/<addon>/<ver-dashed>/<content>
```

**Java API restructure:**
```
output/pub/ebx-addon/<version>/doc/<addon>/Java_API/<content>
  → output/ebx-addon-javadocs/en-us/ebx-addons/<addon>/javadocs/<ver-dashed>/<content>
```

Phase 6 fixes URL mismatch: Step 5 rewrites `Java_API/` links to the EBX **main** javadoc URL
(`https://stg-docs.onebx.com/us/en/ebx/resources/javadocs/{ver}/`), which is wrong for addon
content. Phase 6 replaces with per-addon URL:
`https://stg-docs.onebx.com/us/en/ebx-addons/resources/{addon}/javadocs/{ver}/`.
Do not fix this in Step 5 — Step 5 has no access to the addon slug.

```bash
python scripts/ebx_addon_restructure.py [--src output/pub/ebx-addon] \
                                         [--dst output/ebx-addon] \
                                         [--javadocs-dst output/ebx-addon-javadocs] \
                                         [--preflight-only]
```

### PDF slug mapping (config/pdf_slug_mappings.yaml)

Maps filename slugs (part after `TIB_<product>_<version>_`) to human-readable guide labels.
Display name = `"<product_name> <version> <label>"`.

Resolution order per file:
1. PDF `Title` metadata via PyMuPDF — strips product/version prefix; auto-populates mapping
2. Slug mapping lookup (`config/pdf_slug_mappings.yaml`)
3. Title-case the slug as fallback
4. Raw filename as last resort

Script auto-adds newly discovered slugs (empty value = needs manual review). Manual corrections
persist since the file is committed. Shared utilities in `scripts/lib/asset_copy.py`.

### 10_copy_ebx_addon_pdfs.py

Copies all EBX add-on PDFs from cache → `en-us-onebx-ebx-addons` publishing repo. Generates
`index.md` + `toc.yml` per version.

**Source:** `cache/pub/ebx-addon/<version>/pdf/` (root level). Falls back to
`cache/pub/ebx-addon/<version>/doc/pdf/` for versions (e.g. 6.2.3) with only the nested path.

**Destination:** `C:\github\ebx\en-us-onebx-ebx-addons\en-us\ebx-addon\pdf\<version-dashed>\`

Title derivation — filenames follow `TIB_ebx-<addon>_<addon_version>[_<slug>].pdf`:
- Addon code (`adix`, `common`, `moda`, etc.) → TIBCO EBX product name (hardcoded map)
- Guide slug (`relnotes`, `license`, `versioning_and_packaging_guide`, etc.) → label
- No slug → append "Documentation" to product name
- `addon` code → "TIBCO EBX Add-ons"

```bash
python scripts/10_copy_ebx_addon_pdfs.py [--dry-run] \
  [--cache-src cache/pub/ebx-addon] \
  [--dest C:\github\ebx\en-us-onebx-ebx-addons\en-us\ebx-addon\pdf]
```

Uses only stdlib (`pathlib`, `shutil`, `argparse`). Processes all 42 versions (4.5.7–6.2.3).

---

## Known Variations Across Products

- Empty alias.xml (`<CatapultAliasFile />`) — not an error; handle silently
- Pages with `[%=System.LinkedHeader%]` tokens in `data-mc-toc-path` — strip in Step 5
- BusinessWorks `AutoNumber_p_*` table classes as fake lists — handled by `fake_list_tables`
- `AutoNumber_p_Bullet` on numbered-step tables: `data-mc-autonum` attribute is ground truth;
  if value starts with a digit → `<ol>`, not `<ul>` (tiebreaker in preprocessor.py)
- BE 6.4.0 uses DITA task/concept/reference structure — handled by `task_sections` + `definition_lists`
- coveo:metadata product name fields may contain encoding artifacts (e.g. `â„¢` for `™`) —
  always open sitemap XML with explicit utf-8 encoding
- EBX Java API hosted externally at `https://stg-docs.onebx.com/us/en/ebx/resources/javadocs/<version>/`
  — Step 5 rewrites all relative `Java_API/` links; `08_restructure_ebx.py` excludes the folder
- EBX add-on Java API URL: per-addon at `https://stg-docs.onebx.com/us/en/ebx-addons/resources/{addon}/javadocs/{ver}/`
  — corrected by `ebx_addon_restructure.py` Phase 6 (not Step 5, which has no addon slug)
- EBX pages carry in-page mini TOC in `<div id="toc">` (nested `<ul class="toc1/toc2">` anchor links).
  Retained — markdownify converts to nested Markdown link list. Links resolve because EBX heading
  `id` attributes are preserved as `<a name="id"></a>` anchors.
- EBX HTML headings carry `id` attributes. `_TibcoMarkdownConverter` (subclass of `MarkdownConverter`
  in `scripts/03_convert.py`) overrides `convert_hN` to append `<a name="id"></a>` after each
  heading with an `id`, e.g. `## Overview <a name="overview"></a>`.
- EBX `ebx_definitionList` tables forced to Tier 3 passthrough in `table_classifier.py` —
  prevents `_promote_first_row_as_header` from wrongly promoting first `<td>` row
- EBX `p.noPrint` stripped by `ebx_chrome_selectors` in settings.yaml — removes search icon +
  "User guide [table of contents]" nav link from topic bottoms
- EBX addon ZIP archives store HTML at `doc/<addon>/` (no `/html/` in path), but canonical URL
  must include `/html/`. Manifest entries for ZIP-based products carry a `cache_path` field
  (actual filesystem path) separate from canonical `url`. `convert_entry` in `scripts/03_convert.py`
  uses `entry["cache_path"]` when present, falls back to URL-derived path.
- `01_build_manifest.py` only fires HEAD request for `zip_last_modified` when `--delta` is set.
  Without `--delta`, field is stored as `""`. Do not assume it is populated.
- `02_download.py` image concurrency: image downloads acquire the semaphore independently after
  the parent page's slot is released. `concurrency` limit applies uniformly to pages and images.
- WebWorks phase manifests contain version-level metadata entries (with `version_url` instead of
  `url`). Steps 5 and 6 guard against these with `if "url" not in entry: continue`.
