"""
compare_html_md.py -- Compare HTML source files in cache/ against converted Markdown in output/.

For each content .htm/.html file in cache/pub (applying the same skip rules as the pipeline),
checks whether a corresponding .md file exists in output/pub at the same relative path,
then runs a suite of content checks on each matched pair.

Existence checks:
  missing_md        -- HTML file with no corresponding .md output
  orphan_md         -- .md file with no corresponding HTML source  (--check-orphans)

Content checks (run on matched pairs):
  empty_body        -- MD body is empty after frontmatter
  title_mismatch    -- HTML title does not match MD frontmatter title
  content_ratio     -- MD body text < 20% of HTML body text length (content likely dropped)
  heading_count     -- MD has < 50% of the HTML heading count (h2+)
  image_count       -- Image count differs between HTML and MD

  lang_mismatch     -- HTML lang attribute does not match MD lang frontmatter
  topic_type_mismatch -- HTML class (concept/task/reference) != MD topic_type frontmatter
  toc_path_mismatch -- HTML data-mc-toc-path does not match MD toc_path frontmatter
  unresolved_tokens -- [%=...%] MadCap variable tokens still present in MD body
  htm_links         -- Relative .htm/.html links not converted to .md in MD
  passthrough_tables -- Raw HTML tables (data-converter-passthrough) needing manual review

Usage:
  python scripts/compare_html_md.py --product dsp_gridserver
  python scripts/compare_html_md.py --product dsp_gridserver --version 7.1.1
  python scripts/compare_html_md.py                                  # all of cache/pub
  python scripts/compare_html_md.py --no-content-checks              # existence only (fast)
  python scripts/compare_html_md.py --check-orphans                  # also find orphan MDs
  python scripts/compare_html_md.py --summary-only                   # no per-file detail
  python scripts/compare_html_md.py --report compare_report.json     # write JSON report
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

import yaml

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False


# ---------------------------------------------------------------------------
# Skip rules (mirrors pipeline settings)
# ---------------------------------------------------------------------------

def load_settings(config_path: str) -> dict:
    return yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))


def build_skip_rules(settings: dict):
    skip_filenames = {s.lower() for s in settings.get("skip_filenames", [])}
    skip_segments  = settings.get("skip_path_segments", [])
    html_exts      = {e.lower() for e in settings.get("html_extensions", [".htm", ".html"])}
    skip_patterns  = [re.compile(p, re.IGNORECASE)
                      for p in settings.get("skip_filename_patterns", [])]
    return skip_filenames, skip_segments, html_exts, skip_patterns


def is_content_html(rel_posix: str, filename: str,
                    skip_filenames, skip_segments, html_exts, skip_patterns) -> bool:
    if PurePosixPath(filename).suffix.lower() not in html_exts:
        return False
    if filename.lower() in skip_filenames:
        return False
    for seg in skip_segments:
        if seg in rel_posix:
            return False
    for pat in skip_patterns:
        if pat.match(filename):
            return False
    return True


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

MAX_SAMPLES = 30


@dataclass
class Issue:
    check: str
    severity: str       # "error" | "warning" | "info"
    html_rel: str
    md_rel: str
    detail: str


# Check metadata: (name, description, severity)
CHECKS = [
    ("missing_md",           "HTML file with no .md output",                        "error"),
    ("orphan_md",            ".md file with no HTML source",                        "info"),
    ("empty_body",           "MD body empty after frontmatter",                     "error"),
    ("lang_mismatch",        "Language mismatch between HTML and MD frontmatter",   "error"),
    ("title_mismatch",       "HTML title does not match MD frontmatter title",      "warning"),
    ("content_ratio",        "MD body < 20% of HTML text (content likely dropped)", "warning"),
    ("heading_count",        "MD has < 50% of HTML heading count (h2-h4)",          "warning"),
    ("image_count",          "Image count differs between HTML and MD",             "warning"),

    ("unresolved_tokens",    "Unresolved [%=...%] MadCap tokens in MD body",        "warning"),
    ("htm_links",            "Relative .htm/.html links not converted to .md",      "warning"),
    ("topic_type_mismatch",  "HTML class != MD topic_type frontmatter",             "info"),
    ("toc_path_mismatch",    "HTML data-mc-toc-path != MD toc_path frontmatter",    "info"),
    ("passthrough_tables",   "Raw HTML tables needing manual review",               "info"),
    ("parse_error",          "File could not be parsed",                            "error"),
]

SEV_ORDER = {"error": 0, "warning": 1, "info": 2}
SEV_ICON  = {"error": "FAIL", "warning": "WARN", "info": "INFO"}

# Content ratio: flag MD body if it is less than this fraction of the HTML body text
CONTENT_RATIO_THRESHOLD = 0.20


# ---------------------------------------------------------------------------
# HTML parsing helpers
# ---------------------------------------------------------------------------

_CONTENT_SELECTORS = [
    lambda s: s.find("div", {"role": "main", "id": "mc-main-content"}),
    lambda s: s.select_one("div#center article"),
    lambda s: s.find("article"),
    lambda s: s.find("div", {"id": "ebx_main"}),
    lambda s: s.find("body"),
]


def _main_content(soup):
    for sel in _CONTENT_SELECTORS:
        el = sel(soup)
        if el:
            return el
    return None


def _html_title(soup) -> str:
    main = _main_content(soup)
    if main:
        h1 = main.find("h1")
        if h1:
            # Use separator=' ' and normalise whitespace — mirrors what 03_convert.py does
            return re.sub(r"\s+", " ", h1.get_text(separator=" ", strip=True)).strip()
    tag = soup.find("title")
    if tag:
        t = tag.get_text(strip=True)
        for suffix in (" - TIBCO Documentation", " | TIBCO Documentation"):
            if t.endswith(suffix):
                t = t[: -len(suffix)]
        return t.strip()
    return ""


def _html_body_text(soup) -> str:
    main = _main_content(soup)
    return main.get_text(separator=" ", strip=True) if main else ""


def _html_headings(soup) -> int:
    # Count h2-h4 only. h5/h6 are frequently used for navigation chrome in EBX
    # ("See also" boxes, notes) that the converter correctly strips.
    main = _main_content(soup)
    return len(main.find_all(["h2", "h3", "h4"])) if main else 0


def _html_images(soup) -> int:
    main = _main_content(soup)
    return len(main.find_all("img")) if main else 0


def _html_toc_path(soup) -> str:
    tag = soup.find("html")
    return (tag.get("data-mc-toc-path") or "") if tag else ""


def _html_lang(soup) -> str:
    tag = soup.find("html")
    return (tag.get("lang") or "").lower().strip() if tag else ""


def _html_topic_type(soup) -> str:
    tag = soup.find("html")
    if tag:
        for cls in (tag.get("class") or []):
            if cls in ("concept", "task", "reference"):
                return cls
    return ""


# ---------------------------------------------------------------------------
# MD parsing helpers
# ---------------------------------------------------------------------------

_FENCE_RE       = re.compile(r"^(`{3,}|~{3,})", re.MULTILINE)
_HEADING_RE     = re.compile(r"^(#{2,6})\s",     re.MULTILINE)
_IMAGE_MD_RE    = re.compile(r"!\[[^\]]*\]\([^)]+\)")
_IMAGE_HTML_RE  = re.compile(r'<img\b[^>]+\bsrc=["\'][^"\']+["\']', re.IGNORECASE)
_PASSTHROUGH_RE = re.compile(r'data-converter-passthrough\s*=\s*["\']true["\']', re.IGNORECASE)
_TOKEN_RE       = re.compile(r"\[%=[\w.\s]+%\]")
_HTM_LINK_RE    = re.compile(r"\[[^\]]*\]\(([^)#]*\.html?)(?:[)#])", re.IGNORECASE)


def _read_frontmatter(content: str) -> tuple[dict, str]:
    if not content.startswith("---"):
        return {}, content
    end = content.find("\n---\n", 3)
    if end == -1:
        return {}, content
    try:
        fm = yaml.safe_load(content[3:end]) or {}
    except yaml.YAMLError:
        fm = {}
    return fm, content[end + 5:]


def _strip_fences(body: str) -> str:
    """Replace fenced code block contents with blank lines (preserve line count)."""
    out, in_fence, marker = [], False, ""
    for line in body.splitlines(keepends=True):
        m = _FENCE_RE.match(line)
        if m:
            ch = m.group(1)[0] * 3
            if not in_fence:
                in_fence, marker = True, ch
                out.append(line)
            elif line.lstrip().startswith(marker):
                in_fence = False
                out.append(line)
            else:
                out.append("\n")
        else:
            out.append("\n" if in_fence else line)
    return "".join(out)


def _md_headings(body: str) -> int:
    return len(_HEADING_RE.findall(_strip_fences(body)))


def _md_images(body: str) -> int:
    # Count both Markdown image syntax ![alt](src) and raw <img src="..."> tags
    # (the latter appear inside passthrough HTML tables)
    return len(_IMAGE_MD_RE.findall(body)) + len(_IMAGE_HTML_RE.findall(body))


def _md_passthrough_tables(body: str) -> int:
    return len(_PASSTHROUGH_RE.findall(body))


def _md_tokens(body: str) -> list[str]:
    return _TOKEN_RE.findall(body)


def _md_htm_links(body: str) -> int:
    return sum(1 for url in _HTM_LINK_RE.findall(body) if not url.startswith("http"))


# ---------------------------------------------------------------------------
# Content checks for a matched HTML + MD pair
# ---------------------------------------------------------------------------

def check_pair(html_path: Path, md_path: Path,
               cache_pub: Path, output_pub: Path) -> list[Issue]:
    html_rel = html_path.relative_to(cache_pub).as_posix()
    md_rel   = md_path.relative_to(output_pub).as_posix()
    issues: list[Issue] = []

    # Parse HTML
    try:
        soup = BeautifulSoup(
            html_path.read_text(encoding="utf-8", errors="replace"), "html.parser"
        )
    except Exception as exc:
        return [Issue("parse_error", "error", html_rel, md_rel, f"HTML parse failed: {exc}")]

    # Parse MD
    try:
        md_content = md_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return [Issue("parse_error", "error", html_rel, md_rel, f"MD read failed: {exc}")]

    fm, body = _read_frontmatter(md_content)

    # 1. Empty body — no point running other checks if body is empty
    if not body.strip():
        return [Issue("empty_body", "error", html_rel, md_rel,
                      "MD body is empty after frontmatter")]

    # 2. Title match
    html_title = _html_title(soup)
    md_title   = str(fm.get("title") or "").strip()
    if html_title and md_title:
        if re.sub(r"\s+", " ", html_title).lower() != re.sub(r"\s+", " ", md_title).lower():
            issues.append(Issue("title_mismatch", "warning", html_rel, md_rel,
                                f'HTML: "{html_title[:60]}" | MD: "{md_title[:60]}"'))
    elif not md_title:
        issues.append(Issue("title_mismatch", "warning", html_rel, md_rel,
                            "MD frontmatter has no title field"))

    # 3. Content length ratio
    html_text  = _html_body_text(soup)
    html_chars = len(html_text)
    md_chars   = len(body.strip())
    if html_chars > 300:
        ratio = md_chars / html_chars
        if ratio < CONTENT_RATIO_THRESHOLD:
            issues.append(Issue("content_ratio", "warning", html_rel, md_rel,
                                f"MD is {ratio:.0%} of HTML text ({md_chars} vs {html_chars} chars)"))

    # 4. Heading count (h2+)
    html_h = _html_headings(soup)
    md_h   = _md_headings(body)
    if html_h > 2 and md_h < html_h * 0.5:
        issues.append(Issue("heading_count", "warning", html_rel, md_rel,
                            f"HTML has {html_h} subheadings, MD has {md_h}"))

    # 5. Image count
    html_img = _html_images(soup)
    md_img   = _md_images(body)
    if html_img != md_img:
        issues.append(Issue("image_count", "warning", html_rel, md_rel,
                            f"HTML has {html_img} image(s), MD has {md_img}"))

    # 6. Language match
    html_lang = _html_lang(soup)
    md_lang   = str(fm.get("lang") or "").lower().strip()
    if html_lang and md_lang and html_lang != md_lang:
        issues.append(Issue("lang_mismatch", "error", html_rel, md_rel,
                            f"HTML lang={html_lang!r}, MD lang={md_lang!r}"))

    # 8. Topic type match
    html_type = _html_topic_type(soup)
    md_type   = str(fm.get("topic_type") or "").strip()
    if html_type and md_type and html_type != md_type:
        issues.append(Issue("topic_type_mismatch", "info", html_rel, md_rel,
                            f"HTML class={html_type!r}, MD topic_type={md_type!r}"))

    # 9. TOC path match (skip if HTML path has unresolved tokens)
    html_toc = _html_toc_path(soup)
    md_toc   = str(fm.get("toc_path") or "").strip()
    if html_toc and "[%=" not in html_toc and md_toc and html_toc.strip() != md_toc:
        issues.append(Issue("toc_path_mismatch", "info", html_rel, md_rel,
                            f'HTML: "{html_toc[:80]}" | MD: "{md_toc[:80]}"'))

    # 10. Unresolved tokens
    tokens = _md_tokens(body)
    if tokens:
        sample = list(set(tokens))[:3]
        issues.append(Issue("unresolved_tokens", "warning", html_rel, md_rel,
                            f"{len(tokens)} token(s): {sample}"))

    # 11. Unconverted .htm links
    htm_count = _md_htm_links(body)
    if htm_count:
        issues.append(Issue("htm_links", "warning", html_rel, md_rel,
                            f"{htm_count} relative .htm link(s) not converted to .md"))

    # 12. Passthrough tables
    pt_count = _md_passthrough_tables(body)
    if pt_count:
        issues.append(Issue("passthrough_tables", "info", html_rel, md_rel,
                            f"{pt_count} raw HTML table(s) needing manual review"))

    return issues


# ---------------------------------------------------------------------------
# File collection
# ---------------------------------------------------------------------------

def load_manifest_paths(manifests_dir: Path, product: str | None) -> set[str] | None:
    """
    Return a set of cache-relative HTML paths (e.g. "ebx/6.2.3/doc/html/en/foo.html")
    derived from manifest JSON files for the given product.

    Returns None if no manifests are found (no filtering applied).
    Auto-discovers manifests by looking for manifests/manifest_<product>*.json.
    """
    if not product or not manifests_dir.is_dir():
        return None

    pattern = f"manifest_{product}*.json"
    manifest_files = list(manifests_dir.glob(pattern))
    if not manifest_files:
        return None

    allowed: set[str] = set()
    for mf in manifest_files:
        try:
            entries = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            continue
        for e in entries:
            op = e.get("output_path", "")
            if not op:
                continue
            # output_path is like "pub\ebx\6.2.3\doc\html\en\foo.md"
            # Cache paths are relative to cache/pub/ so strip leading "pub/" segment.
            posix = op.replace("\\", "/")
            if posix.startswith("pub/"):
                posix = posix[4:]
            base = PurePosixPath(posix).with_suffix("")
            allowed.add(str(base) + ".html")
            allowed.add(str(base) + ".htm")

    if allowed:
        print(f"  Manifest filter: {len(manifest_files)} manifest(s), "
              f"{len(allowed)//2} entries → restricting to manifest-listed files")
    return allowed if allowed else None


def collect_html_files(cache_pub: Path, skip_filenames, skip_segments,
                       html_exts, skip_patterns,
                       product_filter: str | None,
                       version_filter: str | None,
                       manifest_paths: set[str] | None = None) -> list[Path]:
    root = cache_pub
    if product_filter:
        root = root / product_filter
        if version_filter:
            root = root / version_filter
    if not root.exists():
        print(f"ERROR: path not found: {root}", file=sys.stderr)
        return []
    files = [
        p for p in root.rglob("*")
        if p.is_file()
        and is_content_html(p.relative_to(cache_pub).as_posix(), p.name,
                            skip_filenames, skip_segments, html_exts, skip_patterns)
    ]
    if manifest_paths is not None:
        files = [p for p in files
                 if p.relative_to(cache_pub).as_posix() in manifest_paths]
    return files


def group_by_version(html_files: list[Path], cache_pub: Path) -> dict[str, list[Path]]:
    groups: dict[str, list[Path]] = defaultdict(list)
    for p in html_files:
        parts = p.relative_to(cache_pub).parts
        groups[f"{parts[0]}/{parts[1]}" if len(parts) >= 2 else parts[0]].append(p)
    return dict(groups)


# ---------------------------------------------------------------------------
# Main comparison loop
# ---------------------------------------------------------------------------

def run_comparison(cache_pub: Path, output_pub: Path,
                   html_files: list[Path],
                   check_orphans: bool,
                   content_checks: bool) -> tuple[list[Issue], list[str], list[str], list[str]]:
    all_issues: list[Issue] = []
    missing:    list[str]   = []
    matched:    list[str]   = []

    total = len(html_files)
    for i, html_path in enumerate(html_files, 1):
        if i % 100 == 0 or i == total:
            print(f"  {i}/{total} files checked ...", end="\r", flush=True)

        rel_html = html_path.relative_to(cache_pub)
        rel_md   = rel_html.with_suffix(".md")
        md_path  = output_pub / rel_md

        if not md_path.exists():
            missing.append(rel_html.as_posix())
            all_issues.append(Issue("missing_md", "error",
                                    rel_html.as_posix(), rel_md.as_posix(), "No .md file found"))
        else:
            matched.append(rel_md.as_posix())
            if content_checks:
                all_issues.extend(check_pair(html_path, md_path, cache_pub, output_pub))

    print(" " * 40, end="\r")  # clear progress line

    orphans: list[str] = []
    if check_orphans:
        matched_set = set(matched)
        for md_path in output_pub.rglob("*.md"):
            name = md_path.name
            if name.startswith("_section_") or name.startswith("_toc"):
                continue
            rel_md = md_path.relative_to(output_pub).as_posix()
            if rel_md not in matched_set:
                orphans.append(rel_md)
                all_issues.append(Issue("orphan_md", "info", "", rel_md,
                                        ".md has no corresponding HTML source"))

    return all_issues, missing, matched, orphans


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def build_report(all_issues: list[Issue],
                 html_files: list[Path],
                 missing: list[str],
                 matched: list[str],
                 orphans: list[str],
                 by_version: dict,
                 summary_only: bool,
                 content_checks: bool) -> str:
    """Return the full report as a string."""
    out = io.StringIO()
    w   = lambda *a, **kw: print(*a, **kw, file=out)   # noqa: E731

    total    = len(html_files)
    n_match  = len(matched)
    coverage = n_match / total * 100 if total else 0.0

    w("=" * 72)
    w("  HTML -> Markdown Comparison Report")
    w("=" * 72)
    w(f"  HTML source topics   : {total:>6}")
    w(f"  Matched .md files    : {n_match:>6}  ({coverage:.1f}%)")
    w(f"  Missing .md files    : {len(missing):>6}")
    if orphans:
        w(f"  Orphan .md files     : {len(orphans):>6}")
    if content_checks:
        content_issues = [i for i in all_issues if i.check not in ("missing_md", "orphan_md")]
        files_with_issues = len({i.html_rel for i in content_issues if i.html_rel})
        w(f"  Files with issues    : {files_with_issues:>6}  (out of {n_match} matched)")

    # Per-version table
    w(f"\n  {'Product/Version':<55} {'HTML':>5} {'MD':>5} {'Miss':>5} {'Cov%':>6}")
    w(f"  {'-'*55} {'-'*5} {'-'*5} {'-'*5} {'-'*6}")
    for ver, vd in sorted(by_version.items()):
        cov  = vd["matched"] / vd["html_total"] * 100 if vd["html_total"] else 0
        flag = "  !!!" if vd["missing_count"] else ""
        w(f"  {ver:<55} {vd['html_total']:>5} {vd['matched']:>5} "
          f"{vd['missing_count']:>5} {cov:>5.1f}%{flag}")

    if not content_checks:
        w("\n  Tip: omit --no-content-checks to also run content comparisons.")

    if summary_only or not all_issues:
        w("=" * 72)
        return out.getvalue()

    # Per-check breakdown
    by_check: dict[str, list[Issue]] = defaultdict(list)
    for issue in all_issues:
        by_check[issue.check].append(issue)

    w(f"\n{'=' * 72}")
    w("  Check Results")
    w(f"{'=' * 72}")

    for name, desc, sev in sorted(CHECKS, key=lambda x: (SEV_ORDER[x[2]], x[0])):
        issues = by_check.get(name, [])
        icon   = SEV_ICON[sev]
        status = f"{len(issues)} files" if issues else "OK"
        w(f"\n{icon} [{sev.upper():7}] {name}")
        w(f"   {desc}")
        w(f"   -> {status}")
        for iss in issues[:5]:
            label = iss.html_rel or iss.md_rel
            w(f"     * {label}")
            w(f"       {iss.detail}")
        if len(issues) > 5:
            w(f"     ... and {len(issues) - 5} more (use --report for full list)")

    w(f"\n{'=' * 72}")
    return out.getvalue()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare HTML source files against converted Markdown output"
    )
    parser.add_argument("--cache-dir",         default="cache")
    parser.add_argument("--output-dir",        default="output")
    parser.add_argument("--config",            default="config/settings.yaml")
    parser.add_argument("--product",           metavar="SLUG",
                        help="Limit to one product slug under cache/pub/")
    parser.add_argument("--version",           metavar="VER",
                        help="Limit to one version (requires --product)")
    parser.add_argument("--check-orphans",     action="store_true",
                        help="Also report .md files with no HTML source")
    parser.add_argument("--no-content-checks", action="store_true",
                        help="Skip content comparison — existence check only (fast)")
    parser.add_argument("--summary-only",      action="store_true",
                        help="Print summary table only, no per-file detail")
    parser.add_argument("--report",            metavar="PATH",
                        help="Write full JSON report to this path")
    parser.add_argument("--ratio-threshold",   type=float, default=0.20,
                        help="Content length ratio threshold (default: 0.20)")
    parser.add_argument("--manifests-dir",     default="manifests",
                        help="Directory containing manifest_*.json files (default: manifests/). "
                             "When --product is given, auto-discovers matching manifests and "
                             "restricts comparison to manifest-listed files only.")
    parser.add_argument("--no-manifest-filter", action="store_true",
                        help="Disable manifest-based filtering even when manifests exist")
    args = parser.parse_args()

    if args.version and not args.product:
        parser.error("--version requires --product")

    global CONTENT_RATIO_THRESHOLD
    CONTENT_RATIO_THRESHOLD = args.ratio_threshold

    # Fix stdout encoding on Windows
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

    settings = load_settings(args.config)
    skip_filenames, skip_segments, html_exts, skip_patterns = build_skip_rules(settings)

    cache_pub  = Path(args.cache_dir)  / "pub"
    output_pub = Path(args.output_dir) / "pub"

    for p, label in [(cache_pub, "cache/pub"), (output_pub, "output/pub")]:
        if not p.exists():
            print(f"ERROR: {label} not found: {p}", file=sys.stderr)
            return 1

    content_checks = not args.no_content_checks
    if content_checks and not BS4_AVAILABLE:
        print("WARNING: beautifulsoup4 not installed — falling back to existence check only.\n"
              "         Install it with: pip install beautifulsoup4", file=sys.stderr)
        content_checks = False

    print(f"Scanning {cache_pub} ...", flush=True)
    manifest_paths = None
    if not args.no_manifest_filter:
        manifest_paths = load_manifest_paths(Path(args.manifests_dir), args.product)
    html_files = collect_html_files(
        cache_pub, skip_filenames, skip_segments, html_exts, skip_patterns,
        args.product, args.version, manifest_paths
    )
    print(f"  {len(html_files)} content HTML files found")

    if not html_files:
        print("Nothing to compare.")
        return 0

    mode = "full content checks" if content_checks else "existence check only"
    print(f"Comparing ({mode}) ...", flush=True)

    all_issues, missing, matched, orphans = run_comparison(
        cache_pub, output_pub, html_files, args.check_orphans, content_checks
    )

    # Per-version breakdown
    groups    = group_by_version(html_files, cache_pub)
    miss_set  = set(missing)
    by_version = {
        ver: {
            "html_total":    len(vfiles),
            "matched":       sum(1 for f in vfiles
                                 if f.relative_to(cache_pub).as_posix() not in miss_set),
            "missing_count": sum(1 for f in vfiles
                                 if f.relative_to(cache_pub).as_posix() in miss_set),
        }
        for ver, vfiles in groups.items()
    }

    report_text = build_report(all_issues, html_files, missing, matched, orphans,
                               by_version, args.summary_only, content_checks)
    print(report_text, end="")

    # Auto-write log file to logs/
    slug      = args.product or "all"
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_dir   = Path(args.cache_dir).parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path  = log_dir / f"compare_{slug}_{timestamp}.log"
    log_path.write_text(report_text, encoding="utf-8")
    print(f"Log written to {log_path}")

    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps({
                "html_total": len(html_files),
                "matched":    len(matched),
                "missing":    missing,
                "orphans":    orphans,
                "by_version": by_version,
                "issues": [
                    {"check": i.check, "severity": i.severity,
                     "html": i.html_rel, "md": i.md_rel, "detail": i.detail}
                    for i in all_issues
                ],
            }, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        print(f"JSON report written to {report_path}")

    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
