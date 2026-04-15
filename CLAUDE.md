# TIBCO Docs Converter — Claude Code Context

## Project Overview

Python pipeline that converts TIBCO product documentation (~2000 product versions) from HTML
(docs.tibco.com) to plain Markdown. Source is MadCap Flare WebHelp2 HTML output, crawled via
a 3-level sitemap hierarchy.

**Workspace root:** `c:\github\html-to-md\`
**Python:** 3.11+ (venv at `.venv/`)
**Run:** `python run.py --phase phase_01`

---

## Folder Structure

```
html-to-md/
├── run.py                        # Orchestrator (--phase, --from-step, --to-step, --dry-run)
├── requirements.txt
├── CLAUDE.md
├── config/
│   ├── settings.yaml             # All tunable settings
│   └── phases/
│       ├── phase_01.yaml         # List of L2 product sitemap URLs for phase 1
│       └── phase_02.yaml
├── scripts/
│   ├── 01_build_manifest.py      # Sitemap crawl → manifests/manifest_<phase>.json
│   ├── 02_download.py            # HTML + images + alias.xml → cache/
│   ├── 03_convert.py             # HTML → Markdown with preprocessor transforms
│   ├── 04_build_csh_maps.py      # alias.xml → csh_map.json + frontmatter injection
│   ├── 05_postprocess.py         # Rewrite .htm links → .md, strip variable tokens
│   ├── 06_build_toc.py           # Reconstruct TOC from toc_path breadcrumbs → _toc.json
│   └── lib/
│       ├── sitemap_parser.py     # 3-level sitemap crawl functions
│       ├── preprocessor.py       # 8 BeautifulSoup transform passes
│       ├── table_classifier.py   # Tier 1/2/3 table classification
│       └── reporter.py           # Structured logging + JSON report writing
├── manifests/                    # Generated JSON manifests — commit these
├── cache/                        # Downloaded HTML + images — gitignore
├── output/                       # Converted Markdown files — gitignore
└── logs/                         # Per-run logs and reports — gitignore
```

---

## Pipeline Steps

| Step | Script | Input | Output |
|------|--------|-------|--------|
| 1 | `01_build_manifest.py` | Phase YAML | `manifests/manifest_<phase>.json` |
| 2 | `02_download.py` | Manifest JSON | `cache/` — HTML, images, alias.xml |
| 3 | `03_convert.py` | Manifest + cache/ | `output/**/*.md` + images |
| 4 | `04_build_csh_maps.py` | cache/ alias.xml files | `output/.../csh_map.json` + updated frontmatter |
| 5 | `05_postprocess.py` | output/**/*.md | Updated .md files (in-place) |
| 6 | `06_build_toc.py` | output/**/*.md frontmatter | `output/.../_toc.json` per version |

---

## Key Technical Facts

### Sitemap Hierarchy (3 levels)
```
https://docs.tibco.com/sitemap.xml                              (master sitemapindex)
  → https://docs.tibco.com/ftp_portal/coveo/tibco-foo.xml      (product sitemapindex, L2)
    → https://docs.tibco.com/ftp_portal/coveo/tibco-foo-1-0.xml (version urlset, L3)
```
- Phase YAML files list L2 (product-level) sitemap URLs — pipeline starts from here, not from root
- L3 urlset uses namespace `http://www.sitemaps.org/schemas/sitemap/0.9/sitemap.xsd` (note: `/sitemap.xsd` suffix variant) plus `coveo:` namespace for metadata
- Always parse XML with explicit namespace mapping; do not use wildcard namespace queries

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
URLs containing `/api/javadoc/` are standard Javadoc output, not MadCap Flare.
Logged as skipped with reason `non-madcap-html`. No conversion attempted.

### SDL Trisoft / DITA WebHelp Responsive (filter in Step 1)
Some TIBCO products were authored in SDL Tridion Docs (formerly SDL Trisoft) and published as
DITA WebHelp Responsive output. These have GUID-based filenames like
`GUID-07C4296F-B4D9-481A-A97F-9608231B1429.html` instead of human-readable names.
Filtered by `skip_filename_patterns` in settings.yaml; logged as `non-madcap-dita`.
When DITA conversion support is added, these products should use a separate phase YAML and
dedicated scripts (`01_dita.py`, `03_dita.py`, etc.).

### Non-Content Directories (filter in Step 1)
`_globalpages/`, `MicroContent/`, `_templates/`, `Skins/`, `Resources/`
These paths appear in the version URL base but contain auxiliary MadCap files, not topics.

### alias.xml (Context-Sensitive Help)
- URL derived per version: `<version-html-root>/Data/Alias.xml`
- Not listed in sitemap — must be fetched separately once per version
- Format: `<Map Name="TOPIC_ID" Link="relative/path.htm" ResolvedId="1000"/>`
  - `Name` = alphanumeric CSH identifier
  - `ResolvedId` = numeric CSH identifier
  - `Link` = relative path to the topic .htm file
- Many products have empty `<CatapultAliasFile />` — handle silently, not an error
- Some products have 404 alias.xml — handle silently

### TOC
- `Data/Tocs/Default.js` exists on server but is stripped/0-bytes on docs.tibco.com — unusable
- Only reliable TOC data is the `data-mc-toc-path` attribute on each page's `<html>` tag
- Step 6 reconstructs the tree from these breadcrumbs; manifest URL order = page sort order
- Pages with empty/missing toc_path go into `_orphans` list in `_toc.json`

### Tables (3 tiers — see table_classifier.py)
- **Tier 1:** Text-only cells → GFM pipe table
- **Tier 2:** Cells with inline HTML only (strong, em, code, a) → flatten + GFM pipe table
- **Tier 3:** Cells with block content (ul, ol, pre, nested tables, h2+) → raw HTML passthrough,
  marked with `data-converter-passthrough="true"` for manual review

### Preprocessor Transforms (order matters — see preprocessor.py)
1. `strip_chrome` — removes nav/UI elements listed in `chrome_selectors` in settings.yaml
2. `fake_list_tables` — `AutoNumber_p_*` table class → proper `<ul>`/`<ol>`
3. `callout_divs` — `div.note/warning/caution/tip/important` → `<blockquote>` with bold label
4. `task_sections` — DITA task elements (prereq, steps, result, postreq, context, example) → semantic HTML
5. `inline_spans` — uicontrol/wintitle/option → `<strong>`, filepath/codeph → `<code>`, varname → `<em>`
6. `anchor_only_links` — strip `<a name="...">` with no href (MadCap navigation anchors)
7. `classify_and_handle_tables` — applies 3-tier logic, calls table_classifier.py
8. `rewrite_image_src` — make image src relative to output .md location

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

content_selector: "div[role='main']#mc-main-content"

skip_filenames:
  - Default.htm
  - Default_CSH.htm
  - Home.htm

skip_path_segments:
  - /api/javadoc/
  - /_globalpages/
  - /MicroContent/
  - /_templates/
  - /Skins/
  - /Resources/

skip_filename_patterns:
  - "^GUID-[0-9A-Fa-f]{8}-...-[0-9A-Fa-f]{12}\\.html?$"  # SDL Trisoft DITA WebHelp

html_extensions:
  - .htm
  - .html

chrome_selectors:
  - p.MCWebHelpFramesetLink
  - div#prdnm
  - div.toolbar
  - div.breadcrumbs
  - div.MCMiniTocBox_0
  - div#feedback-survey
  - p.Copyright

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

## Phase Files (config/phases/)

```yaml
# Example phase file
name: "Phase 1 - POC"
products:
  - https://docs.tibco.com/ftp_portal/coveo/tibco-spotfire-connector-for-postgresql.xml
  - https://docs.tibco.com/ftp_portal/coveo/tibco-spotfire-connector-for-sap-bw.xml
```
Each entry is a product-level (L2) sitemapindex URL. All version sitemaps under a product are
discovered automatically by the pipeline.

---

## Running the Pipeline

```bash
# Activate venv first
.venv\Scripts\activate    # Windows
source .venv/bin/activate # Linux/Mac

# Full pipeline run
python run.py --phase phase_01

# Resume from a specific step (steps 1-2 already done)
python run.py --phase phase_01 --from-step 3

# Run only specific steps
python run.py --phase phase_01 --from-step 1 --to-step 2

# Dry run — no files written, prints what would happen
python run.py --phase phase_01 --dry-run

# Re-download/re-convert already-processed files
python run.py --phase phase_01 --force-rerun

# Run a single step directly
python scripts/01_build_manifest.py --phase phase_01
```

---

## Logging & Reports

Each run creates a timestamped folder:
```
logs/<phase>/<YYYYMMDD-HHMMSS>/
  run.log              # Full verbose log (all steps)
  errors.log           # Errors only
  skipped.log          # Filtered URLs with reason
  01_manifest.json     # Step 1 stats
  02_download.json     # Step 2 stats
  03_convert.json      # Step 3 stats
  04_csh.json          # Step 4 stats
  05_postprocess.json  # Step 5 stats
  06_toc.json          # Step 6 stats
  summary.json         # Full rollup
```

Progress is checkpointed in `logs/progress.db` (SQLite). Re-runs skip already-completed URLs.

---

## Known Variations Across Products

- Some products have empty alias.xml (`<CatapultAliasFile />`) — not an error
- Some pages have `[%=System.LinkedHeader%]` tokens in `data-mc-toc-path` — strip in Step 5
- BusinessWorks HTML uses `AutoNumber_p_*` table classes as fake lists — handled by preprocessor
- BE 6.4.0 HTML uses DITA task/concept/reference structure — handled by preprocessor
- coveo:metadata product name fields may contain encoding artifacts (e.g. `â„¢` for `™`) —
  always open sitemap XML with explicit utf-8 encoding
