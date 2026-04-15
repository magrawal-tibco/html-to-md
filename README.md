# TIBCO Docs HTML → Markdown Converter

A Python pipeline that converts TIBCO product documentation (~2000 product versions) from HTML to plain Markdown. The source is MadCap Flare WebHelp2 HTML output published at [docs.tibco.com](https://docs.tibco.com), crawled via a 3-level sitemap hierarchy.

## Overview

The pipeline downloads documentation HTML, runs a series of BeautifulSoup preprocessing transforms to clean up MadCap-specific markup, then converts to GitHub-Flavored Markdown using [markdownify](https://github.com/matthewwithanm/python-markdownify). Each Markdown file includes YAML frontmatter with metadata (title, TOC path, product version, context-sensitive help IDs).

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
├── run.py                        # Orchestrator (--phase, --from-step, --to-step, --dry-run)
├── requirements.txt
├── config/
│   ├── settings.yaml             # All tunable settings
│   └── phases/
│       ├── phase_01.yaml         # L2 product sitemap URLs for phase 1
│       ├── phase_02.yaml
│       └── phase_03.yaml
├── scripts/
│   ├── 01_build_manifest.py      # Sitemap crawl → manifests/manifest_<phase>.json
│   ├── 02_download.py            # HTML + images + alias.xml → cache/
│   ├── 03_convert.py             # HTML → Markdown with preprocessor transforms
│   ├── 04_build_csh_maps.py      # alias.xml → csh_map.json + frontmatter injection
│   ├── 05_postprocess.py         # Rewrite .htm links → .md, strip variable tokens
│   ├── 06_build_toc.py           # Reconstruct TOC from toc_path breadcrumbs → _toc.json
│   └── lib/
│       ├── sitemap_parser.py     # 3-level sitemap crawl
│       ├── preprocessor.py       # 13 BeautifulSoup transform passes
│       ├── table_classifier.py   # Tier 1/2/3 table classification
│       └── reporter.py           # Structured logging + JSON report writing
├── manifests/                    # Generated JSON manifests — committed to git
├── cache/                        # Downloaded HTML + images — gitignored
├── output/                       # Converted Markdown files — gitignored
└── logs/                         # Per-run logs and reports — gitignored
```

## Pipeline Steps

| Step | Script | Input | Output |
|------|--------|-------|--------|
| 1 | `01_build_manifest.py` | Phase YAML | `manifests/manifest_<phase>.json` |
| 2 | `02_download.py` | Manifest JSON | `cache/` — HTML, images, alias.xml |
| 3 | `03_convert.py` | Manifest + cache/ | `output/**/*.md` + images |
| 4 | `04_build_csh_maps.py` | cache/ alias.xml files | `output/.../csh_map.json` + updated frontmatter |
| 5 | `05_postprocess.py` | output/**/*.md | Updated .md files (in-place) |
| 6 | `06_build_toc.py` | output/**/*.md frontmatter | `output/.../_toc.json` per version |

Step 5 (`05_postprocess.py`) must be run after Step 3 — it rewrites `.htm` cross-reference links to `.md` and strips MadCap variable tokens from TOC paths.

## Phase Files

Phase files (`config/phases/<name>.yaml`) list product-level (L2) sitemap URLs. All version sitemaps under each product are discovered automatically.

```yaml
name: "Phase 1 - POC"
products:
  - https://docs.tibco.com/ftp_portal/coveo/tibco-spotfire-connector-for-postgresql.xml
  - https://docs.tibco.com/ftp_portal/coveo/tibco-spotfire-connector-for-sap-bw.xml
```

To run a subset for testing, create a minimal manifest JSON directly:

```python
import json
from pathlib import Path
manifest = json.loads(Path('manifests/manifest_phase_03.json').read_text(encoding='utf-8'))
subset = [e for e in manifest if 'businessevents-enterprise/6.4.0' in e['url']]
Path('manifests/manifest_be640.json').write_text(json.dumps(subset, indent=2), encoding='utf-8')
# then: python run.py --phase be640 --from-step 3
```

## Sitemap Hierarchy

```
https://docs.tibco.com/sitemap.xml                              (master sitemapindex)
  └─ https://docs.tibco.com/ftp_portal/coveo/tibco-foo.xml     (product sitemapindex, L2)
       └─ https://docs.tibco.com/ftp_portal/coveo/tibco-foo-1-0.xml  (version urlset, L3)
```

L3 urlsets use the `coveo:` namespace extension for metadata (product name, version, doc name).

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

Each run creates a timestamped folder under `logs/<phase>/<YYYYMMDD-HHMMSS>/`:

```
run.log              # Full verbose log
errors.log           # Errors only
skipped.log          # Filtered URLs with reason
01_manifest.json     # Step 1 stats
02_download.json     # Step 2 stats
03_convert.json      # Step 3 stats
...
summary.json         # Full rollup across all steps
```

Progress is checkpointed in `logs/progress.db` (SQLite). Re-runs skip already-completed URLs unless `--force-rerun` is passed.

## Known Source Variations

| Product | Variation | Handling |
|---------|-----------|----------|
| BusinessWorks | `AutoNumber_p_*` table classes as fake lists | `fake_list_tables` transform |
| BusinessEvents 6.4.0 | DITA task/concept/reference structure | `task_sections` + `definition_lists` transforms |
| SDL Trisoft / DITA products | GUID-based filenames (`GUID-xxx.html`) | Filtered in Step 1 as `non-madcap-dita` |
| Javadoc products | `/api/javadoc/` path segment | Filtered in Step 1 as `non-madcap-html` |
| All products | MadCap variable tokens in TOC path (`[%=System.LinkedHeader%]`) | Stripped in Step 5 |
| All products | Empty or 404 `alias.xml` | Handled silently (not an error) |
