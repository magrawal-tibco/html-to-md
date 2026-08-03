# TIBCO Docs Converter — Claude Code Context

## Project Overview

Python pipeline that converts TIBCO product documentation (~2000 product versions) from HTML
(docs.tibco.com) to plain Markdown. Source is MadCap Flare WebHelp2 HTML output, crawled via
a 3-level sitemap hierarchy.

**Workspace root:** `c:\github\html-to-md\`
**Python:** 3.11+ (venv at `.venv/`)
**Run:** `python run.py --phase phase_01`

---

## Why This Tool Exists

### The Problem

TIBCO's documentation catalog spans **600,000+ pages** across **584 products**, **1,500+ versions**,
and **4 business units** — TIBCO, IBI, DataSynapse, and EBX. Content was authored over many years
in MadCap Flare, DocBook, FrameMaker (published via WebWorks ePublisher), DITA (SDL Trisoft
WebHelp Responsive), and Word. Each tool produces a different HTML or PDF output format, none of
which is directly ingestible by Adobe Experience Manager (AEM) Guides — the platform used as the
central authoring and publishing hub.

AEM Guides requires structured Markdown with YAML frontmatter, a specific folder layout per
language and product version, and machine-readable TOC files (`toc.yml`). Manually converting
even a single product version was a multi-day exercise. At catalog scale, documentation was
effectively trapped in siloed HTML — invisible to AI systems and impossible to reformat
consistently by hand.

### What This Tool Does

The pipeline automates the full conversion lifecycle for all four business units and all
supported source formats:

- **Discovers** all product versions from the docs.tibco.com API or sitemap hierarchy (no manual
  URL lists)
- **Downloads** documentation ZIPs (or individual HTML pages as fallback), along with images,
  alias.xml CSH mappings, and archive assets to a local cache
- **Converts** each HTML topic to clean Markdown with accurate frontmatter (title, language, TOC
  path, product name/version, CSH IDs), with format-aware transforms per authoring tool
- **Post-processes** the converted files: rewrites internal `.htm` links to relative `.md` links,
  strips authoring-tool tokens, normalises heading levels, rewrites external Java API links, and
  generates synthetic section index pages for TOC grouping nodes that have no source page
- **Reconstructs the TOC** from authoritative MadCap TOC JS files (when available from ZIPs) or
  from per-page `data-mc-toc-path` breadcrumbs as fallback, emitting `_toc.json` and `toc.yml`
  compatible with AEM Guides
- **Runs sub-pipelines** automatically for DITA WebHelp (SDL Trisoft), WebWorks ePublisher
  (legacy FrameMaker), and PDF release notes within the same orchestrated run
- **Restructures** EBX output into the language-first folder layout required by AEM Guides, and
  copies PDF/doc assets alongside with generated index pages

### Key Benefits

- **Zero manual work per version** — adding a new product or version requires only one URL added
  to a phase YAML; the pipeline handles everything else
- **Format-agnostic** — a single orchestrator handles MadCap Flare, DocBook, DITA, WebWorks
  ePublisher, and PDF source formats through dedicated sub-pipelines
- **Idempotent and resumable** — each step is independently re-runnable; a SQLite progress
  database checkpoints completed URLs so interrupted runs pick up where they left off
- **Auditable output** — every converted page carries frontmatter linking it back to its source
  URL, language, product, and version; structured JSON reports and a persistent
  `conversion_log.csv` give per-run statistics
- **Handles real-world messiness** — dozens of product-specific quirks (fake-list tables, DITA
  task markup, encoding artifacts, missing alias files, multi-language variants) are handled
  declaratively through preprocessor passes and settings, not manual cleanup

---

## How This Tool Was Developed (and How Claude Code Evolves It)

This pipeline was built and is maintained **entirely through Claude Code** — Anthropic's
agentic CLI that operates directly on the codebase. The development workflow is:

1. **Discovery via conversation** — a product-specific quirk is observed in the output (e.g.
   a numbered list rendered as bullets, a broken link, a missing TOC entry). The problem is
   described in plain language to Claude Code.
2. **Root-cause investigation** — Claude Code reads the relevant source HTML, traces through the
   preprocessor and converter code, and identifies the exact line or transform responsible.
3. **Targeted fix** — a minimal, surgical change is made to the correct script. Claude Code
   writes the fix, explains the reasoning, and leaves the surrounding code untouched.
4. **Verification** — the affected step is re-run against real data; the output is inspected to
   confirm the fix without regressions.
5. **Documentation update** — CLAUDE.md is updated to record the new behaviour, so the next
   session starts with full context.

Because CLAUDE.md is loaded at the start of every Claude Code session, it acts as the
**persistent memory** for the project: what the HTML source looks like, what each script does,
what quirks have already been handled, and what conventions the pipeline follows. This means:

- **No onboarding cost** — a new Claude Code session immediately understands the architecture
  and can make precise changes without re-exploring the codebase
- **Incremental evolution** — each fix or feature is added to the right layer of the pipeline
  without breaking unrelated behaviour; the modular phase/step design makes this safe
- **Self-improving documentation** — every structural change (new script, removed flag, new
  output convention) is reflected in CLAUDE.md in the same session that made the change,
  keeping documentation and code in sync automatically

This approach has made it practical for a small team to build and evolve a production-grade
documentation pipeline covering thousands of product versions, across multiple languages and
output formats, without dedicated engineering time per product.

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
│   ├── ebx_addon_restructure.py  # EBX add-on: version-first → addon-first layout
│   ├── 08_restructure_ebx.py     # EBX main: URL-mirror → language-first AEM layout + PDF/doc assets
│   ├── copy_assets.py            # Generic: copy PDF/doc assets + generate index.md + toc.yml
│   ├── 10_copy_ebx_addon_pdfs.py # EBX add-on: copy all PDFs from cache → ebx-addons repo + index.md + toc.yml
│   └── lib/
│       ├── io_utils.py           # Shared I/O helpers: load_settings, load_manifest, read/write_frontmatter
│       ├── sitemap_parser.py     # 3-level sitemap crawl functions
│       ├── preprocessor.py       # BeautifulSoup transform passes
│       ├── table_classifier.py   # Tier 1/2/3 table classification
│       ├── reporter.py           # Structured logging + JSON report writing
│       └── asset_copy.py         # Shared PDF/doc asset copy + slug resolution utilities
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
| 6 | `06_build_toc.py` | output/**/*.md frontmatter | `output/.../_toc.json` + `toc.yml` + `_section_*.md` per version |

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
- **Section index pages:** After building `_toc.json`, Step 6 generates `_section_<slug>.md` for
  every TOC node that has children but no source page (`"file": null`). Each file gets a
  frontmatter + `# Title` heading + nested `- [child](rel.md)` listing of its subtree. The node's
  `file` field is updated in-place so `toc.yml` emits a `url:` for it (required by AEM Guides).
- **External URL injection:** TOC nodes matching known external titles (currently "Java API") have
  their `file` set to the external URL (e.g. `https://stg-docs.onebx.com/us/en/ebx/resources/javadocs/<version>/`).
  `node_to_yaml()` detects `http://`/`https://` prefixes and emits `url:` directly.

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
5. `inline_spans` — maps span/element classes to inline HTML tags:
   - `uicontrol`, `wintitle`, `option`, `menucascade` → `<strong>`
   - `filepath`, `codeph` → `<code>`
   - `varname`, `parmname`, `term` → `<em>`
   - `<var>` element → `<em>`
6. `anchor_only_links` — strip `<a name="...">` with no href (MadCap navigation anchors)
7. `classify_and_handle_tables` — applies 3-tier logic, calls table_classifier.py
8. `rewrite_image_src` — intentional no-op; relative image `src` paths are left unchanged because
   the output mirrors the source URL directory structure, so relative paths resolve correctly as-is

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

## Test Suite

```bash
# Run all tests
.venv/Scripts/python -m pytest tests/ -v

# Run a specific test file
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

EBX documentation ZIPs have a richer structure than standard MadCap products and require
additional post-processing steps run after the standard pipeline (Steps 1–6).

### Archive structure (cache/pub/ebx/<version>/doc/)
```
doc/
├── html/
│   ├── en/      → webhelp (converted by Steps 1-6)
│   ├── fr/      → webhelp (French, 6.1.1+)
│   └── ja/      → webhelp (Japanese, 6.1.1+)
├── relnotes/    → relnotes.md (generated separately)
├── pdf/         → PDF files (copied as-is by Step 08/09)
└── doc/         → Other documents, e.g. readme.txt (copied as-is by Step 08/09)
```

### Step 08 — EBX restructure (`scripts/08_restructure_ebx.py`)

Transforms the URL-mirroring output layout into the AEM Guides language-first layout, and
copies PDF/doc assets alongside the restructured webhelp output.

**Webhelp restructure:**
```
output/pub/ebx/<version>/doc/html/<lang>/<content>
  → output/ebx/<lang-norm>/ebx/webhelp/<ver-dashed>/<content>
```

**PDF/doc asset copy (Phase 4 of the script):**
```
cache/pub/ebx/<version>/doc/pdf/   → output/ebx/en-us/ebx/pdf/<ver-dashed>/
cache/pub/ebx/<version>/doc/doc/   → output/ebx/en-us/ebx/doc/<ver-dashed>/
```

Each version folder under `pdf/` and `doc/` gets two generated files:
- `index.md` — frontmatter + heading + sorted hyperlinked file list with resolved display names
- `toc.yml` — `docs_list_title` + single entry pointing to `index.md`

Usage:
```bash
python scripts/08_restructure_ebx.py [--src output/pub/ebx] [--dst output/ebx] \
                                      [--cache-src cache/pub/ebx] \
                                      [--preflight-only]
```

**Note:** The `Java_API/` folder is unconditionally excluded from the restructure — Java API
is now hosted externally. Relative `Java_API/` links in `.md` files are rewritten to the
external URL by Step 5 (`05_postprocess.py`).

**Sequencing requirement:** Step 5 must be run before Step 8. If Step 3 is re-run (e.g.
force-rerun), always follow it with Step 5 before running Step 8 — otherwise Step 8 copies
un-postprocessed files (still containing `.html` links) into `output/ebx/`. Step 8 emits a
warning if it detects this condition.

### EBX add-on restructure (`scripts/ebx_addon_restructure.py`)

Transforms the version-first URL-mirroring layout of EBX add-on output into an addon-first
layout, writing a separate copy without touching the original.

**Webhelp restructure:**
```
output/pub/ebx-addon/<version>/doc/<addon>/<content>
  → output/ebx-addon/en-us/ebx-addon/<addon>/<ver-dashed>/<content>
```

**Java API restructure (separate tree):**
```
output/pub/ebx-addon/<version>/doc/<addon>/Java_API/<content>
  → output/ebx-addon-javadocs/en-us/ebx-addons/<addon>/javadocs/<ver-dashed>/<content>
```

**Phases:**
- Phase 0: Pre-flight cross-addon link scan (warns on links that will break after restructure)
- Phase 1: Build webhelp path mapping (Java_API excluded)
- Phase 2: Build javadocs path mapping
- Phase 3: Copy webhelp files
- Phase 4: Copy javadoc files
- Phase 5: Patch `_toc.json` root and `file` paths to new locations
- Phase 6: Rewrite EBX-main javadoc URLs → addon-specific URLs, strip MadCap popup links

Phase 6 corrects a URL mismatch: Step 5 rewrites relative `Java_API/` links to the EBX **main**
javadoc URL (`https://stg-docs.onebx.com/us/en/ebx/resources/javadocs/{ver}/`), which is wrong
for addon content. Phase 6 replaces these with the per-addon URL
(`https://stg-docs.onebx.com/us/en/ebx-addons/resources/{addon}/javadocs/{ver}/`). Do not fix
this in Step 5 — Step 5 has no access to the addon slug.

```bash
python scripts/ebx_addon_restructure.py [--src output/pub/ebx-addon] \
                                         [--dst output/ebx-addon] \
                                         [--javadocs-dst output/ebx-addon-javadocs] \
                                         [--preflight-only]
```

### Generic asset copy (`scripts/copy_assets.py`)

Runs the same PDF/doc asset copy for any product (not EBX-specific). Used for products
whose archives contain `<version>/doc/pdf/` and `<version>/doc/doc/` subfolders.

```bash
python scripts/copy_assets.py \
  --cache-src cache/pub/<product> \
  --dst       output/<product> \
  --product-slug <slug> \
  --product-name "<Full Product Name>" \
  [--lang en-us]
```

**Output structure produced by both scripts:**
```
output/<product>/<lang>/<slug>/
├── webhelp/<ver-dashed>/    ← existing converted Markdown
├── pdf/<ver-dashed>/
│   ├── TIB_*.pdf            ← copied as-is
│   ├── index.md             ← generated listing page
│   └── toc.yml              ← generated TOC pointer
└── doc/<ver-dashed>/
    ├── TIB_*.txt            ← copied as-is
    ├── index.md             ← generated listing page
    └── toc.yml              ← generated TOC pointer
```

### PDF slug mapping (`config/pdf_slug_mappings.yaml`)

Maps filename slugs (the part after `TIB_<product>_<version>_`) to human-readable guide labels.
Display name = `"<product_name> <version> <label>"`.

```yaml
admin-guide: "Administration Guide"
installation: "Installation Guide"
relnotes: "Release Notes"
# ...
```

Resolution order per file:
1. PDF `Title` metadata via PyMuPDF — strips `<product_name> <version>` prefix; auto-populates mapping
2. Slug mapping lookup (`config/pdf_slug_mappings.yaml`)
3. Title-case the slug as fallback
4. Raw filename as last resort

The script auto-adds newly discovered slugs to the mapping file (empty value = needs manual review).
Manual corrections persist across all future runs since the file is committed.

Shared utilities live in `scripts/lib/asset_copy.py` and are used by `08_restructure_ebx.py` and `copy_assets.py`.

### Step 10 — EBX add-on PDF copy (`scripts/10_copy_ebx_addon_pdfs.py`)

Standalone script that copies all PDFs from the EBX add-on cache into the
`en-us-onebx-ebx-addons` publishing repo and generates `index.md` + `toc.yml` per version.

**Source:** `cache/pub/ebx-addon/<version>/pdf/` (root level, present in all versions).
Falls back to `cache/pub/ebx-addon/<version>/doc/pdf/` for versions (e.g. 6.2.3) that only
have the nested path.

**Destination:**
```
C:\github\ebx\en-us-onebx-ebx-addons\en-us\ebx-addon\pdf\<version-dashed>\
  TIB_ebx-*.pdf          ← copied as-is
  index.md               ← generated listing page (all PDFs as bullet links)
  toc.yml                ← generated TOC pointer
```

**Title derivation** — filenames follow `TIB_ebx-<addon>_<addon_version>[_<slug>].pdf`:
- Addon code (`adix`, `common`, `moda`, etc.) maps to a hardcoded TIBCO EBX product name
- Guide slug (`relnotes`, `license`, `versioning_and_packaging_guide`, etc.) maps to a label
- No slug → append "Documentation" to the product name
- `addon` code (package-level files like license, vpat) maps to "TIBCO EBX Add-ons"

```bash
python scripts/10_copy_ebx_addon_pdfs.py [--dry-run] \
  [--cache-src cache/pub/ebx-addon] \
  [--dest C:\github\ebx\en-us-onebx-ebx-addons\en-us\ebx-addon\pdf]
```

Uses only stdlib (`pathlib`, `shutil`, `argparse`) — no dependency on `scripts/lib/`.
Processes all 42 versions (4.5.7 → 6.2.3). Safe to re-run (overwrites existing output).

---

## Known Variations Across Products

- Some products have empty alias.xml (`<CatapultAliasFile />`) — not an error
- Some pages have `[%=System.LinkedHeader%]` tokens in `data-mc-toc-path` — strip in Step 5
- BusinessWorks HTML uses `AutoNumber_p_*` table classes as fake lists — handled by preprocessor
- BE 6.4.0 HTML uses DITA task/concept/reference structure — handled by preprocessor
- coveo:metadata product name fields may contain encoding artifacts (e.g. `â„¢` for `™`) —
  always open sitemap XML with explicit utf-8 encoding
- `AutoNumber_p_Bullet` class is sometimes used on numbered-step tables in MadCap — the
  `data-mc-autonum` attribute on the content `<td>` is the ground truth: if its value starts
  with a digit the table is treated as `<ol>`, not `<ul>` (tiebreaker in `preprocessor.py`)
- EBX Java API is hosted externally at
  `https://stg-docs.onebx.com/us/en/ebx/resources/javadocs/<version>/` — Step 5 rewrites all
  relative `Java_API/` links to this URL; `08_restructure_ebx.py` excludes the `Java_API/` folder
  from the restructured output. For EBX add-ons, `ebx_addon_restructure.py` Phase 6 further
  rewrites these URLs to the per-addon javadoc URL.
- EBX pages carry an in-page mini TOC in `<div id="toc">` (nested `<ul class="toc1/toc2">`
  anchor links to headings). This is **retained** — `ebx_chrome_selectors` is now empty so
  markdownify converts it to a nested Markdown link list. The links resolve because EBX heading
  `id` attributes are preserved as `<a name="id"></a>` anchors (see next point).
- EBX HTML headings carry `id` attributes (e.g. `<h2 id="overview">`). Step 3 uses
  `_TibcoMarkdownConverter` (a `MarkdownConverter` subclass in `scripts/03_convert.py`) that
  overrides `convert_hN` to append `<a name="id"></a>` after each heading that has an `id`,
  producing e.g. `## Overview <a name="overview"></a>`. This makes mini TOC anchor links resolve.
- markdownify's `chomp()` function can introduce a spurious space after opening `**` bold markers
  when `<strong>` content starts with whitespace (e.g. `** word**` instead of `**word**`).
  `clean_markdown()` in `scripts/03_convert.py` fixes this with
  `re.sub(r'\*\* +(\S)', r'**\1', text)`.
- EBX addon ZIP archives store HTML at `doc/<addon>/` (no `/html/` in path), but the canonical
  URL must include `/html/` (e.g. `doc/html/<addon>/`). Manifest entries for ZIP-based products
  now carry a `cache_path` field (actual filesystem path relative to cache root) separate from
  the canonical `url`. `convert_entry` in `scripts/03_convert.py` uses `entry["cache_path"]`
  when present to locate the file, falling back to URL-derived path for non-ZIP products.
- `01_build_manifest.py` only fires a HEAD request for `zip_last_modified` when `--delta` is
  set. Without `--delta`, `zip_last_modified` is stored as `""` in the manifest. Do not assume
  the field is always populated.
- `02_download.py` image concurrency: each image download acquires the semaphore independently
  after the parent page's semaphore slot is released. The configured `concurrency` limit applies
  uniformly to both page and image downloads.
