# EBX Documentation Converter — Setup and Run Guide

This guide covers running the EBX documentation conversion pipeline end-to-end, from raw
documentation ZIPs downloaded from docs.tibco.com to a Markdown output tree ready for AEM
Guides import.

---

## Prerequisites

- **Python 3.11 or later** — check with `python --version`
- **Network access** to `docs.tibco.com` (to download ZIP files)
- **Disk space** — expect ~2–4 GB for cache (ZIPs + extracted HTML) and ~500 MB for output

---

## Setup

```bash
# 1. Extract the provided archive and enter the directory
cd ebx-converter

# 2. Create a virtual environment
python -m venv .venv

# 3. Activate it
.venv\Scripts\activate       # Windows
source .venv/bin/activate    # Linux/Mac

# 4. Install dependencies
pip install -r requirements.txt
```

The pipeline creates three directories automatically on first run:

| Directory | Contents |
|-----------|----------|
| `cache/` | Downloaded ZIPs and extracted HTML (large; not committed) |
| `output/` | Converted Markdown files — the final deliverable |
| `logs/` | Per-run logs and JSON reports |

---

## Preparing ZIP URL Lists

EBX documentation is distributed as ZIP files on docs.tibco.com. The two phase config files
included (`config/phases/ebx.yaml` and `config/phases/ebx-addon-62x.yaml`) tell the pipeline
which product versions to process. The pipeline fetches each version's ZIP URL automatically
from the product metadata API — you do not need to supply ZIP URLs separately for the standard
phases.

If you need to add new EBX versions not listed in the phase files, edit the phase YAML:

```yaml
# config/phases/ebx.yaml
name: "ebx POC"
products:
  - https://docs.tibco.com/products/tibco-ebx-6-2-3
  - https://docs.tibco.com/products/tibco-ebx-6-2-2
  # Add new versions here
```

Each entry is a product page URL in the form `https://docs.tibco.com/products/<slug>`.
The slug follows the pattern `tibco-ebx-<major>-<minor>-<patch>`.

---

## Running the Pipeline

The conversion is split into two phases (EBX main docs and EBX Add-ons), each followed by a
restructure step. Run them in sequence.

### Phase A — EBX Main Documentation

Covers EBX versions 5.9.x through 6.2.x (all languages: en-us, fr-fr, ja-jp).

```bash
# Step 1: Run the full pipeline (downloads ZIPs, converts HTML → Markdown, runs PDF sub-pipeline)
python run.py --phase ebx

# Step 2: Restructure to AEM Guides layout (language-first tree)
# Uses defaults: source = output/pub/ebx, destination = output/ebx
python scripts/08_restructure_ebx.py
```

### Phase B — EBX Add-ons 6.2.x

Covers EBX Add-ons versions 6.2.0 through 6.2.3 (English only).

```bash
# Step 1: Run the full pipeline for add-ons
python run.py --phase ebx-addon-62x

# Step 2: Restructure directly to final layout (addon-first, language-first)
# Source = output/pub/ebx-addon, destination = output/ebx-addon
python scripts/07_restructure_ebx_addon.py --dst output/ebx-addon
```

> **Important**: The restructure scripts (`07_restructure_ebx_addon.py`, `08_restructure_ebx.py`)
> are **not** called automatically by `run.py`. They must always be run manually after the
> pipeline completes.

---

## Output Location

After both phases complete, the final Markdown is under:

```
output/
  ebx/
    en-us/ebx/webhelp/<version>/    # EBX main docs — English
    fr-fr/ebx/webhelp/<version>/    # EBX main docs — French
    ja-jp/ebx/webhelp/<version>/    # EBX main docs — Japanese
    en-us/ebx/relnotes/<version>/   # Release notes (PDF-extracted, English only)
    en-us/ebx-addon/<addon>/<ver>/  # In-tree addon content from EBX main phase

  ebx-addon/
    en-us/ebx-addon/<addon>/<ver>/  # Add-ons 6.2.x content (Phase B)
```

Versions use dashes instead of dots (e.g. `6.2.3` → `6-2-3`).

---

## Java_API Content

Javadoc content under `Java_API/` subdirectories is copied as-is into the output tree without
any Markdown conversion. It is included by default.

To omit Java_API content entirely:

```bash
python scripts/08_restructure_ebx.py --exclude-java-api
```

---

## Resuming / Partial Re-runs

```bash
# Resume from step 3 if steps 1–2 already completed
python run.py --phase ebx --from-step 3

# Re-convert already-processed files (e.g. after a code fix)
python run.py --phase ebx --from-step 3 --force-rerun

# Preview what would run without writing any files
python run.py --phase ebx --dry-run

# Skip the PDF release notes sub-pipeline
python run.py --phase ebx --skip-pdf

# Pre-flight scan only (checks cross-language links, writes nothing)
python scripts/08_restructure_ebx.py --preflight-only
```

---

## Pipeline Steps

`run.py` runs these steps in order:

| Step | Script | What it does |
|------|--------|-------------|
| 1 | `01_build_manifest.py` | Resolve product version URLs → build manifest JSON |
| 2a | `02a_download_zip.py` | Download full documentation ZIPs and extract |
| 2 | `02_download.py` | Download individual HTML pages (fallback for missing ZIPs) |
| 3 | `03_convert.py` | Convert HTML → Markdown |
| 4 | `04_build_csh_maps.py` | Build context-sensitive help maps from alias.xml |
| 5 | `05_postprocess.py` | Rewrite links, strip variable tokens |
| 6 | `06_build_toc.py` | Build `_toc.json` per version |
| 7 | `07_generate_report.py` | Write conversion report |

After Step 7, the PDF release notes sub-pipeline runs automatically (unless `--skip-pdf`),
extracting content from the documentation ZIPs' PDF files and writing `relnotes.md` and
`toc.yml` alongside each version.

---

## Logs and Reports

Each run writes to a timestamped folder:

```
logs/
  ebx/
    <YYYYMMDD-HHMMSS>/
      run.log          # Full verbose log
      errors.log       # Errors only
      skipped.log      # Filtered/skipped URLs with reason
      01_manifest.json # Step-by-step statistics
      ...
      summary.json     # Full rollup
```

If the pipeline stops on an error, it prints a `--from-step N` resume command to continue
from where it left off.

---

## Troubleshooting

**`ModuleNotFoundError: No module named 'scripts'`**
Run scripts from the repo root directory, not from inside `scripts/`. Always run:
```bash
python run.py --phase ebx
# not: cd scripts && python 03_convert.py
```

**`FileNotFoundError: config/phases/ebx.yaml`**
Also run from the repo root. The working directory must be the folder that contains `run.py`.

**Network timeouts during ZIP download**
The pipeline retries automatically (3 attempts with exponential backoff). If a product
consistently fails, check network access to docs.tibco.com and re-run from `--from-step 2a`.

**ZIP not found (HTTP 404)**
The URL embedded in the product metadata may have changed. Check the product page at
`https://docs.tibco.com/products/<slug>` and update the phase YAML if needed.

**`output/pub/ebx-addon-reorg` not found when running Phase B restructure**
`07_restructure_ebx_addon.py` must complete successfully before running `08_restructure_ebx.py`
with `--src output/pub/ebx-addon-reorg`. Check the previous step's output for errors.
