# Codebase Audit Report — html-to-md Pipeline

Generated: 2026-08-02. Independent review; no attachment to existing decisions.
Critical bugs marked **[FIXED]** where patched in this session.

---

## 1. Architecture & Structure

| Finding | Location | Severity |
|---|---|---|
| **[FIXED]** Two scripts shared step number `07` and two shared `09`, breaking the `run.py` resume-key scheme. Renamed: `07_restructure_ebx_addon.py` → `ebx_addon_restructure.py`, `09_copy_assets.py` → `copy_assets.py`, `09_restructure_tibco.py` → `tibco_restructure.py`. | `scripts/` dir | Moderate |
| **[FIXED]** `load_settings()` and `load_manifest()` were copy-pasted in 13+ scripts. Extracted into `scripts/lib/io_utils.py`; all numbered scripts, dita/ sub-pipeline, and run.py now import from there. | `01:34`, `02:38`, `03:66`, `04:32`, `05:55`, `06:34` (+8 more) | Moderate |
| **[FIXED]** `read_frontmatter` / `write_frontmatter` had three incompatible implementations. Unified in `lib/io_utils.py` with `parse_frontmatter` (string-based), `read_frontmatter` (file-based, returns tuple), `format_frontmatter`, and `write_frontmatter`. | `04_build_csh_maps.py:83`, `05_postprocess.py:81`, `06_build_toc.py:46` | Moderate |
| **[FIXED]** The entire asset-copy block (~190 lines) was duplicated inside `08_restructure_ebx.py` with three diverged bugs. Refactored to import from `lib/asset_copy.py`; local duplicate removed. | `08_restructure_ebx.py:65–253` vs `lib/asset_copy.py` | Moderate |
| Large set of undocumented utility/one-off scripts (`cache_sitemaps.py`, `compare_html_md.py`, `compare_toc.py`, `count_html_pages.py`, `create_ebx_export.py`, `download_zips.py`, `estimate_corpus.py`, `list_products.py`, `patch_missing_counts.py`, `preview_html.py`, `qa_check.py`) live in `scripts/` with no CLAUDE.md mention and no inclusion in `run.py`. Unclear which are active tooling and which are abandoned. | `scripts/` dir | Minor |
| **[FIXED]** `run.py` docstring said "Runs all 6 steps in sequence" but STEPS has 8 entries. Updated. | `run.py:6` | Minor |

---

## 2. Dead Code & Duplication

| Finding | Location | Severity |
|---|---|---|
| **[FIXED]** `rewrite_image_src` was a complete no-op (`if False else src` self-assignment, counted everything as "rewritten"). Fixed: now returns `0` immediately — relative src paths are preserved intentionally since output mirrors source URL structure. | `preprocessor.py:949–954` | Moderate |
| **[FIXED]** Phase 5 (Java_API removal) in `ebx_addon_restructure.py` was unreachable — `build_path_mapping` excludes Java_API with `continue`, so no Java_API dirs are created in dst, so `rglob("Java_API") + shutil.rmtree` was always a no-op. Removed the dead phase. | `ebx_addon_restructure.py:293–300` | Moderate |
| Phase 6 (cross-addon link rewriting) stub removed — the misleading "Skipped — not implemented" message is gone; cross-link warning is now emitted by the Phase 0 pre-flight section only. | `ebx_addon_restructure.py:302–306` | Moderate |
| **[FIXED]** `dita_versions_{phase}.json` write block was dead code — docstring states `dita_versions` is always empty; `if dita_versions:` was always `False`. Block removed. | `01_build_manifest.py:511–517` | Moderate |
| `phases_dir` is computed but never referenced; the loop that follows iterates two hardcoded `Path` candidates. | `01_build_manifest.py:39` | Minor |
| Unreachable `return False` at end of retry loop — all exit paths return or sleep-then-loop before falling through. | `02_download.py:128` | Minor |
| `_MD_LINK_RE` and `_TOKEN_RE` regexes copy-pasted into 7 different files (`05`, `06`, `07`, `08`, `09`, `compare_html_md.py`, `qa_check.py`). Should live in `lib/`. | `05_postprocess.py:29–32` and 6 others | Minor |
| `preflight_scan` function is structurally identical in scripts 07 and 08. One shared implementation belongs in `lib/`. | `07_restructure_ebx_addon.py:111`, `08_restructure_ebx.py:339` | Minor |
| `for ebx_idx in [single_path]: ... break` — loop with one element and unconditional `break`. Replace with a plain `if`. | `06_build_toc.py:234–256` | Minor |
| `BeautifulSoup` imported at module level AND again inside `build_version_toc` under a different alias (`_BS`). | `06_build_toc.py:24` and `06_build_toc.py:239` | Minor |
| `from collections import Counter` deferred inside `build_version_toc`; `defaultdict` from the same module IS a module-level import. | `06_build_toc.py:263–264` | Minor |
| `from datetime import datetime` deferred inside `main()` in scripts 02, 03, 04, 05, 06, and several `dita/`/`pdf/` scripts, but is a module-level import in script 01. | `02:282`, `03:577`, `04:233`, `05:365`, `06:745` | Minor |
| `base_url` parameter in `build_url_to_md_index` is accepted and passed by `main()` but never used inside the function body. | `05_postprocess.py:67–78` | Minor |
| Dead condition in script 08: `parts[3] != "_toc.json"` can never be `False` when `len(parts) >= 5` — position 3 is always a language directory. | `08_restructure_ebx.py:307` | Minor |
| `_promote_first_row_as_header` returns `True`/`False` but no caller uses the return value. | `table_classifier.py:130` | Minor |
| `if ol.get("class"):` guard inside `find_all("ol", class_="substeps")` is always `True`. | `preprocessor.py:573–576` | Minor |
| Section comment `# ── Transform 5: inline spans ─────` precedes `_normalize_whitespace`, not `inline_spans`. Comment numbering is off and inconsistent with both the module docstring and `run_all` call order. | `preprocessor.py:580`, `669` | Minor |
| `adopt_orphan_list_images` is called in `run_all` and recorded in stats but absent from the module-level docstring. | `preprocessor.py:275–300` | Minor |

---

## 3. Bugs & Correctness

| Finding | Location | Severity |
|---|---|---|
| **[FIXED]** `fitz` was never imported in `08_restructure_ebx.py`, so `_read_pdf_title` always raised `NameError`, caught silently by `except Exception: return None`. PDF title extraction returned `None` for every file; every display name degraded to slug-lookup or title-case fallback. `pymupdf` in `requirements.txt` was completely unused by this script. | `08_restructure_ebx.py:100–105` | Critical |
| **[FIXED]** Broken slug regex for multi-segment product codes. `_SLUG_RE = re.compile(r"^TIB_[^_]+_\d[\d.]+_(.+)$")` failed for filenames like `TIB_dsp_gridserver_7.2.0_admin-guide` because `[^_]+` rejects underscores in the product segment. `lib/asset_copy.py` fixed this with `_VERSION_ANCHOR_RE`; the fix was never back-ported to the local copy in script 08. | `08_restructure_ebx.py:60–62` | Critical |
| **[FIXED]** `dir_fallback` in TOC builder did not drop the last segment. The code comment says "drop the last segment — that will be the page title we append ourselves" but `segs[:-1]` was not applied. Orphan pages (missing `toc_path`) were placed one level too deep in the TOC, as children of a peer page rather than siblings. Example: siblings `"Admin\|Config\|Manage Connections"` and `"Admin\|Config\|SSL Setup"` — orphan `"Audit Logging"` would be placed at `Admin → Config → Manage Connections → Audit Logging` instead of `Admin → Config → Audit Logging`. | `06_build_toc.py:277–282` | Critical |
| **[FIXED]** `--from-step 2a` resume hint was invalid. `--from-step` declared `type=int` so argparse would reject `"2a"`. Added `_parse_step()` validator that accepts both integers and strings like `"2a"`, mapping them to the correct step number. | `run.py:327`, `383–385` | Moderate |
| Case normalization mismatch in CSH link lookup. `link_to_csh` keys are stored without lowercasing; the fallback query lowercases only the search key. A mixed-case stored key never matches. Fix: normalize at insertion time. | `04_build_csh_maps.py:152`, `199` | Moderate |
| **[FIXED]** `save_slug_mappings` argument order was inverted between script 08's local copy and `lib/asset_copy.py`. Moot: script 08 was migrated to import from lib; the local copy was removed entirely. | `08_restructure_ebx.py:72` vs `lib/asset_copy.py:35` | Moderate |
| **[FIXED]** `patch_toc_json` in `ebx_addon_restructure.py` only rewrote the `root` field, leaving all `file` fields in the `tree` as stale absolute paths. Added `_walk_tree` recursive rewrite; now matches the lib implementation. | `ebx_addon_restructure.py:158` | Moderate |
| **[FIXED]** `inject_external_urls` received empty `version_dashed` when `_version_meta` returned `{}`, writing `"…/javadocs//"` (double slash) into `_toc.json` with no warning. Added guard that skips injection and logs a warning when `version_dashed` is empty. | `06_build_toc.py:770–771` | Moderate |
| **[FIXED]** `iter_version_entries` silently truncated to the first version when an L2 sitemapindex was passed — processed only `version_urls[0]`, discarding all remaining versions silently. Now raises `ValueError` to surface the miscall. | `sitemap_parser.py:160–166` | Moderate |
| **[FIXED]** `insert_into_tree` silently overwrote file on title collision. Two pages with identical `toc_path` + leaf title caused the first page's file to be replaced by the second with no log entry. Now emits `warnings.warn` on collision. | `06_build_toc.py:123–124` | Moderate |
| **[FIXED]** Fenced code block detection was fence-type and length agnostic — ` ```python ` could be "closed" by `~~~`, and ` ```` ` by ` ``` `. Per CommonMark, mismatched type/length are not valid closers. Fixed: closing fence must match opening fence character and be at least as long. | `05_postprocess.py:251–252` | Moderate |
| **[FIXED]** `_table_column_count` called `int(c.get("colspan", 1))` with no error handling — a non-integer `colspan` value (`"auto"`, `""`) raised `ValueError` and crashed the table classifier. Fixed with `_safe_colspan()` helper that returns `1` on any non-integer value. | `preprocessor.py:695–698` | Moderate |
| **[FIXED]** `table.find("tr")` descended into nested tables by default. If the outer table's first row contained a nested `<table>`, the nested `<tr>` could be returned instead of the outer first row, producing wrong header promotion. Fixed with `find("tr", recursive=False)`. | `table_classifier.py:77` | Moderate |
| **[FIXED]** `version_html_root` derivation hardcoded depth assumption — `Path(...).parent.parent` assumes `Alias.xml` is always exactly two levels below version root. Any deviation silently produced a wrong `csh_map_path`. Added depth validation that logs a warning when the assumption doesn't hold. | `04_build_csh_maps.py:175–176` | Moderate |
| **[FIXED]** `output_path` slice in step 4 had no guard against path mismatch. If `entry["output_path"]` didn't start with `version_html_root`, the slice yielded a garbled path that silently failed all CSH lookups. Added `startswith` guard that skips and warns on mismatch. | `04_build_csh_maps.py:195` | Moderate |
| **[FIXED]** `scan-cache` mode skipped the `sdl_dita` version filter. DITA files with non-GUID filenames passed through to the MadCap converter in scan-cache mode. Fixed: removed the `not args.scan_cache` guard so the filter always applies. | `03_convert.py:637` | Moderate |
| `_check_postprocessed` exits after the first warning, providing no total count of affected files. Name implies a full scan; behavior is a one-shot. | `08_restructure_ebx.py:453` | Minor |
| `_ADDON_MODULES` is a hardcoded set — adding a new EBX addon requires a code change rather than a config update. | `08_restructure_ebx.py:298` | Minor |
| Script 07 `--preflight-only` returns exit code `0` even when cross-addon links are found, preventing CI from detecting the warning state. | `07_restructure_ebx_addon.py:221–223` | Minor |
| Double `rglob("*")` traversal in script 07 — `build_path_mapping` and `build_javadocs_mapping` each walk the full source tree independently. | `07_restructure_ebx_addon.py:63`, `93` | Minor |

---

## 4. Error Handling & Robustness

| Finding | Location | Severity |
|---|---|---|
| `load_settings` calls `Path.read_text()` and `yaml.safe_load()` with no error handling. Missing config or malformed YAML propagates as uncaught exception in every pipeline script. | `run.py:50`, `01:34`, `02:38`, etc. | Moderate |
| **[FIXED]** `load_checkpoint` had no `JSONDecodeError` handling — a partially-written checkpoint file from a prior crash would abort the script when `--delta` is used. Now catches `json.JSONDecodeError` and returns `{}`. | `01_build_manifest.py:199–204` | Moderate |
| **[FIXED]** `parse_alias_xml` swallowed all read exceptions silently — a permissions error was indistinguishable from a legitimately empty alias file. Fixed: `OSError` propagates; the caller logs a warning and distinguishes the failure. | `04_build_csh_maps.py:56–58` | Moderate |
| **[FIXED]** HTTP status errors (429, 503) were retried immediately with no delay while network errors used exponential backoff. Fixed: 429/503 responses now sleep with exponential backoff before retry. | `02_download.py:119–122` | Moderate |
| Image downloads bypass the concurrency semaphore. The `async with semaphore:` block covers only the page fetch; image URLs found on that page download outside it. With `concurrency=20` and 50+ images per page, effective concurrent connections far exceed the configured limit. | `02_download.py:200–212` | Moderate |
| **[FIXED]** `06_build_toc.py --from-json` mode had no error handling — raw exceptions propagated inconsistently with all other code paths. Now catches `OSError`/`json.JSONDecodeError`, prints to stderr, and returns exit code 1. | `06_build_toc.py:721–735` | Moderate |
| Phase 6 (Java API URL patching) file I/O in `ebx_addon_restructure.py` has no error handling. A single unreadable or unwritable file aborts the entire phase with no count of files successfully patched. | `ebx_addon_restructure.py:351–357` | Moderate |
| `preflight_scan` in `ebx_addon_restructure.py` silently skips unreadable files (`except Exception: continue`). Cross-link scan results are silently incomplete. | `ebx_addon_restructure.py:127` | Moderate |
| Script 08 Phase 4 cache-miss exits `0`. If the cache source directory doesn't exist, a warning is printed but `main()` returns `0 if errors == 0 else 1` where `errors` counts only webhelp copy errors. | `08_restructure_ebx.py:406–408` | Moderate |
| `copy_assets.py:main()` always returns `0` regardless of failures in `copy_asset_folder`, `save_slug_mappings`, or missing versions. | `copy_assets.py:120` | Moderate |
| **[FIXED]** `Reporter.__init__` opened `skipped.log` with no `__del__`. A crash before `finish()` leaked the file handle. Added `__del__` to close the handle on garbage collection. | `reporter.py:58` | Moderate |
| **[FIXED]** `Reporter` logger handler guard was process-global — same-named loggers shared handlers, causing cross-run log pollution on force-rerun or parallel use. Fixed: each instance now uses a unique logger name `{step_name}__{id(self)}`. | `reporter.py:33–34` | Moderate |
| `reporter._counts` accessed directly in two places, bypassing any Reporter API contract. | `01_build_manifest.py:489–490` | Minor |
| Duplicate URL paths in `build_url_to_md_index` silently overwrite — second entry replaces first with no warning; links to the first entry then rewrite to the wrong file. | `05_postprocess.py:76–77` | Minor |
| No error context on outer `_fetch_xml` call. A product sitemap fetch failure produces no mention of which product URL was being fetched. | `sitemap_parser.py:132` | Minor |
| Corrupt manifest silently skipped in `09_copy_assets.py` — product name falls back to slug with no warning. | `09_copy_assets.py:48` | Minor |
| `fitz.open()` without context manager. If `doc.metadata` raises, `doc.close()` is never called, leaking a PyMuPDF file handle. | `lib/asset_copy.py:68–70`, `08_restructure_ebx.py:101–103` | Minor |
| `has_webworks_versions` swallows all exceptions with `except Exception: pass`. A cache permissions error silently skips the WebWorks sub-pipeline. | `run.py:173` | Minor |
| `BeautifulSoup` import inside `has_webworks_versions`. If `beautifulsoup4` is not installed, `ImportError` is raised at runtime during pipeline execution rather than at import time. | `run.py:159` | Minor |
| `manifests_dir` hardcoded as `Path("manifests")` in script 09, not read from `settings.yaml`. Running from any directory other than repo root silently returns `None` for all product names. | `09_copy_assets.py:37` | Minor |
| `SLUG_MAPPINGS_FILE = Path("config/pdf_slug_mappings.yaml")` is CWD-relative, evaluated at import time. Running from any other directory silently reads/writes the wrong file. | `lib/asset_copy.py:16`, `08_restructure_ebx.py:55` | Minor |
| `--phase` in script 06 is declared non-required then manually validated, producing a different error message format than argparse. | `06_build_toc.py:714`, `737` | Minor |
| `write` and `pool` timeouts in `sitemap_parser.py` are hardcoded to `10`; `connect` and `read` come from settings. | `sitemap_parser.py:178–179` | Minor |

---

## 5. Security

| Finding | Location | Severity |
|---|---|---|
| **[FIXED]** YAML injection in `write_toc_yml` and `write_index_md`. Product names/versions containing `:`, `{`, `[`, `#`, or `®` produced malformed YAML. Fixed: each interpolated string value now goes through `yaml.dump(v, allow_unicode=True).strip()` before insertion. | `lib/asset_copy.py:143–170` | Moderate |
| **[FIXED]** YAML injection in `save_slug_mappings`. Slug labels containing `"` produced malformed YAML. Fixed: each label value now uses `yaml.dump(label, ...).strip()` for safe quoting. | `lib/asset_copy.py:46–47` | Moderate |
| HTML injection from source content into BeautifulSoup in `callout_divs`. `h5.get_text()` interpolated into an HTML string fed to `BeautifulSoup`. Risk is low (trusted source docs, output is Markdown) but structurally unsafe. | `preprocessor.py:338` | Minor |
| `load_slug_mappings` coerces YAML `false`/`0`/`null` to `""`. Accidental YAML booleans/integers become indistinguishable from intentionally-empty mappings. | `lib/asset_copy.py:31` | Minor |

---

## 6. Performance

| Finding | Location | Severity |
|---|---|---|
| Image downloads bypass the semaphore — effectively multiplying concurrency by average images per page. With `concurrency=20` and 10+ images per page, actual concurrent connections can reach 200+. | `02_download.py:200–212` | Moderate |
| Unconditional HEAD request for `zip_last_modified` regardless of `--delta` flag. Every new-format product URL fires a HEAD request even when `delta=False`, where the result is unused. | `01_build_manifest.py:266` | Moderate |
| Double `rglob("*")` traversal in script 07. `build_path_mapping` and `build_javadocs_mapping` each walk the full source tree independently. | `07_restructure_ebx_addon.py:63`, `93` | Minor |
| `BeautifulSoup("", "lxml")` constructed just to call `new_tag()`. Any existing `Tag` object supports `new_tag()`. Repeated in `task_sections`, `callout_divs`, `icon_tables`, `inline_spans`. | `preprocessor.py:528` | Minor |

---

## 7. Test Coverage

| Finding | Severity |
|---|---|
| **[FIXED]** Zero automated tests in the repository. Created `tests/`, `pytest.ini`, `tests/test_preprocessor.py` (48 tests), and `tests/test_table_classifier.py` (22 tests). Added `pytest>=8.0` to `requirements.txt`. All 70 tests pass. | Critical |
| **[FIXED]** Preprocessor transforms were entirely untested. `tests/test_preprocessor.py` now covers `strip_chrome`, `fake_list_tables` (including the `data-mc-autonum` tiebreaker), `callout_divs`, `ebx_callout_divs`, `inline_spans`, `anchor_only_links`, `code_urls_to_links`, `_table_column_count` (including the `colspan` crash), and `rewrite_image_src` (no-op documented as a test). | Critical |
| **[FIXED]** Table classifier tier decisions were untested. `tests/test_table_classifier.py` now covers `_cell_tier` (all three tiers, custom block tags), `classify_table` (worst-case propagation, EBX override), `_promote_first_row_as_header` (including the recursive `find("tr")` bug, documented as a passing test that will fail when the bug is fixed), and `handle_tables` (counts, passthrough attribute, nested-table skip, header promotion). | Moderate |
| TOC reconstruction (the most complex part of the pipeline) has no tests. The `insert_into_tree` collision bug is invisible without tests. | Moderate |
| Step 4 CSH matching has no tests. The case normalization bug has been silent since the feature was added. | Moderate |

---

## 8. Consistency

| Finding | Location | Severity |
|---|---|---|
| **[FIXED]** `run.py` subprocess flag routing used fragile substring matching (`"02_download.py" not in script`). Changed to `Path(script).name` exact comparisons — a renamed script no longer silently breaks routing. | `run.py:71–86` | Moderate |
| Staging domain `stg-docs.onebx.com` hardcoded in three separate scripts (`05`, `06`, `07`, `08`) with no settings key. The `stag-docs.tibco.com` in `05_postprocess.py:132` is a different TIBCO domain (not an inconsistency) — the `onebx.com` domain is consistently `stg-docs` across all 4 occurrences. Remaining work: move to settings.yaml key and thread through scripts. | `05_postprocess.py:260`; `06_build_toc.py:474`; `ebx_addon_restructure.py:339–340` | Minor |
| `count = [0]` mutable-list closure workaround mixed with `nonlocal` in the same file. | `05_postprocess.py:217–231` | Minor |
| `table.find("tr")` uses default recursive depth; `find_all("td", recursive=False)` in the same function uses explicit non-recursive depth. Inconsistent depth limiting on adjacent lines. | `table_classifier.py:77`, `85` | Minor |
| Two `slugify` functions with different outputs live in the same file and generate filenames in the same version directory. Downstream tooling can't predict which was used for which file. | `06_build_toc.py:359–363`, `500–501` | Minor |
| EBX-specific CSS class `ebx_definitionList` hardcoded in the generic table classifier. Product-specific overrides should be injected via configuration. | `table_classifier.py:49` | Minor |
| `reporter.py` module docstring has parameter order for `fail()` backwards (`fail(url, step, error)` vs actual `fail(url, error, step=None)`). | `reporter.py:7`, `87` | Minor |
| `step: str = None` type annotation should be `str \| None = None`. Current annotation is a static analysis error. | `reporter.py:87` | Minor |
| `dry_run` suppresses the JSON report but not `skipped.log`, which is created and written regardless. Dry-run mode still produces real filesystem side effects. | `reporter.py:58–59`, `109` | Minor |
| `--phase` is `required=False` only in script 06, then manually validated. All other scripts declare it `required=True`. | `06_build_toc.py:714`, `737` | Minor |
| Script 09 has no `--config` argument for specifying settings.yaml. Scripts 05 and 06 have one. Settings-dependent paths in script 09 fall back to hardcoded defaults. | `09_copy_assets.py` | Minor |
| **[FIXED]** `_write_toc_yml` / `patch_toc_json` existed in three versions with different completeness. Script 08's local copy was removed (now imports from lib); `ebx_addon_restructure.py` had `_walk_tree` added. Only one canonical implementation remains in `lib/asset_copy.py`. | `ebx_addon_restructure.py:170`, `lib/asset_copy.py` | Moderate |
