"""
qa_check.py -- Post-conversion quality checks for Markdown output.

Scans a directory of converted .md files and reports quality issues
grouped by check type. Designed to run after step 5 (postprocess).

Checks performed:
  1. heading_jumps          -- heading level skips (H1->H3 etc.)
  2. malformed_autolinks    -- <http://> and <http://...> autolinks
  3. variable_tokens        -- unresolved [%=...%] MadCap tokens
  4. htm_links              -- relative links still pointing to .htm files
  5. passthrough_tables     -- raw HTML tables (data-converter-passthrough)
  6. frontmatter_missing    -- required frontmatter fields absent
  7. encoding_artifacts     -- UTF-8 mojibake patterns (double-decoded UTF-8)
  8. empty_body             -- .md files with no meaningful body content
  9. broken_relative_links  -- relative .md links to non-existent files
                              (only with --full, slow on large trees)

Usage:
  python scripts/qa_check.py --dir output/dsp_gridserver
  python scripts/qa_check.py --dir output/ --full --report qa_report.json
  python scripts/qa_check.py --dir output/ebx/en-us --checks heading_jumps,htm_links
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

import yaml


# -- data structures ------------------------------------------------------------

MAX_SAMPLES = 20  # max findings to store per check


@dataclass
class Finding:
    file: str
    detail: str


@dataclass
class CheckResult:
    name: str
    description: str
    severity: str          # "error" | "warning" | "info"
    affected_files: int = 0
    total_occurrences: int = 0
    samples: list[Finding] = field(default_factory=list)

    def add(self, file: str, detail: str, occurrences: int = 1) -> None:
        self.affected_files += 1
        self.total_occurrences += occurrences
        if len(self.samples) < MAX_SAMPLES:
            self.samples.append(Finding(file, detail))


# -- compiled patterns ----------------------------------------------------------

_HEADING_RE      = re.compile(r"^(#{1,6})\s", re.MULTILINE)
_FENCE_RE        = re.compile(r"^(`{3,}|~{3,})", re.MULTILINE)
_AUTOLINK_RE     = re.compile(r"<(https?://(?:[^>]*\.\.\..*|))>")
_TOKEN_RE        = re.compile(r"\[%=[\w.\s]+%\]")
_HTM_LINK_RE     = re.compile(r"\[([^\]]*)\]\(([^)#]*\.html?)([)#])", re.IGNORECASE)
_PASSTHROUGH_RE  = re.compile(r'data-converter-passthrough\s*=\s*["\']true["\']', re.IGNORECASE)
_MD_LINK_RE      = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
# Common UTF-8 mojibake: UTF-8 bytes decoded as Latin-1.
# Markers are constructed at import time via chr() to keep this source ASCII-clean.
_MOJIBAKE_MARKERS = ['â€', 'Â®', 'Â©', 'Ã©', 'Ã¨', 'â\x84¢']
_ENCODING_RE = re.compile("|".join(re.escape(s) for s in _MOJIBAKE_MARKERS))

REQUIRED_FRONTMATTER = {"title", "source_url", "lang"}


# -- helpers -------------------------------------------------------------------

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
    """Return body with content inside fenced code blocks replaced by blanks."""
    result = []
    in_fence = False
    fence_marker = ""
    for line in body.splitlines(keepends=True):
        m = _FENCE_RE.match(line)
        if m:
            marker = m.group(1)[0] * 3
            if not in_fence:
                in_fence = True
                fence_marker = marker
                result.append(line)
            elif line.lstrip().startswith(fence_marker):
                in_fence = False
                result.append(line)
            else:
                result.append("\n")
        elif in_fence:
            result.append("\n")
        else:
            result.append(line)
    return "".join(result)


def _rel(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


# -- check functions ------------------------------------------------------------

def check_heading_jumps(files: list[Path], base: Path) -> CheckResult:
    result = CheckResult(
        name="heading_jumps",
        description="Heading level skips (e.g. H1 -> H3)",
        severity="error",
    )
    for path in files:
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue
        _, body = _read_frontmatter(content)
        body_no_fence = _strip_fences(body)

        headings = [(len(m.group(1)), m.start()) for m in _HEADING_RE.finditer(body_no_fence)]
        jumps = []
        prev = 0
        for level, pos in headings:
            if prev > 0 and level > prev + 1:
                # find the heading text for the detail message
                line_start = body_no_fence.rfind("\n", 0, pos) + 1
                line_end = body_no_fence.find("\n", pos)
                heading_text = body_no_fence[line_start:line_end].strip()[:60]
                jumps.append(f"H{prev}->H{level}: {heading_text}")
            prev = level if level <= prev + 1 or prev == 0 else prev + 1

        if jumps:
            result.add(_rel(path, base), "; ".join(jumps[:3]), len(jumps))
    return result


def check_malformed_autolinks(files: list[Path], base: Path) -> CheckResult:
    result = CheckResult(
        name="malformed_autolinks",
        description="<http://> / <http://...> autolinks that crash DITA-OT",
        severity="error",
    )
    for path in files:
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue
        _, body = _read_frontmatter(content)
        matches = _AUTOLINK_RE.findall(body)
        if matches:
            result.add(_rel(path, base), str(matches[:3]), len(matches))
    return result


def check_variable_tokens(files: list[Path], base: Path) -> CheckResult:
    result = CheckResult(
        name="variable_tokens",
        description="Unresolved MadCap variable tokens [%=...%]",
        severity="warning",
    )
    for path in files:
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue
        _, body = _read_frontmatter(content)
        matches = _TOKEN_RE.findall(body)
        if matches:
            result.add(_rel(path, base), str(list(set(matches))[:3]), len(matches))
    return result


def check_htm_links(files: list[Path], base: Path) -> CheckResult:
    result = CheckResult(
        name="htm_links",
        description="Relative links still pointing to .htm/.html files",
        severity="warning",
    )
    for path in files:
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue
        _, body = _read_frontmatter(content)
        matches = _HTM_LINK_RE.findall(body)
        # Only flag relative links (not absolute https:// Javadoc URLs ending in .html)
        rel_matches = [m for m in matches if not m[1].startswith("http")]
        if rel_matches:
            samples = [m[1] for m in rel_matches[:3]]
            result.add(_rel(path, base), str(samples), len(rel_matches))
    return result


def check_passthrough_tables(files: list[Path], base: Path) -> CheckResult:
    result = CheckResult(
        name="passthrough_tables",
        description="Raw HTML tables (data-converter-passthrough) requiring manual review",
        severity="info",
    )
    for path in files:
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue
        matches = _PASSTHROUGH_RE.findall(content)
        if matches:
            result.add(_rel(path, base), f"{len(matches)} table(s)", len(matches))
    return result


def check_frontmatter(files: list[Path], base: Path) -> CheckResult:
    result = CheckResult(
        name="frontmatter_missing",
        description=f"Required frontmatter fields missing ({', '.join(sorted(REQUIRED_FRONTMATTER))})",
        severity="error",
    )
    for path in files:
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue
        fm, _ = _read_frontmatter(content)
        missing = REQUIRED_FRONTMATTER - set(fm.keys())
        if missing:
            result.add(_rel(path, base), f"missing: {sorted(missing)}", len(missing))
    return result


def check_encoding_artifacts(files: list[Path], base: Path) -> CheckResult:
    result = CheckResult(
        name="encoding_artifacts",
        description="UTF-8 mojibake (double-decoded Latin-1) from mis-read source encoding",
        severity="warning",
    )
    for path in files:
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue
        matches = _ENCODING_RE.findall(content)
        if matches:
            result.add(_rel(path, base), str(list(set(matches))[:3]), len(matches))
    return result


def check_empty_body(files: list[Path], base: Path) -> CheckResult:
    result = CheckResult(
        name="empty_body",
        description="Files with no meaningful body content after frontmatter",
        severity="warning",
    )
    for path in files:
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue
        _, body = _read_frontmatter(content)
        if not body.strip():
            result.add(_rel(path, base), "empty body", 1)
    return result


def check_broken_relative_links(files: list[Path], base: Path) -> CheckResult:
    result = CheckResult(
        name="broken_relative_links",
        description="Relative .md links pointing to non-existent files",
        severity="error",
    )
    file_set = {p.resolve() for p in files}

    for path in files:
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue
        _, body = _read_frontmatter(content)
        broken = []
        for m in _MD_LINK_RE.finditer(body):
            url = m.group(2)
            if url.startswith(("http", "#", "mailto:", "data:")):
                continue
            url_clean = url.split("#")[0]
            if not url_clean or not url_clean.endswith(".md"):
                continue
            target = (path.parent / url_clean).resolve()
            if target not in file_set and not target.exists():
                broken.append(url_clean)
        if broken:
            result.add(_rel(path, base), str(broken[:3]), len(broken))
    return result


# -- registry ------------------------------------------------------------------

ALL_CHECKS: dict[str, callable] = {
    "heading_jumps":         check_heading_jumps,
    "malformed_autolinks":   check_malformed_autolinks,
    "variable_tokens":       check_variable_tokens,
    "htm_links":             check_htm_links,
    "passthrough_tables":    check_passthrough_tables,
    "frontmatter_missing":   check_frontmatter,
    "encoding_artifacts":    check_encoding_artifacts,
    "empty_body":            check_empty_body,
    "broken_relative_links": check_broken_relative_links,  # slow -- requires --full
}

DEFAULT_CHECKS = [k for k in ALL_CHECKS if k != "broken_relative_links"]


# -- output --------------------------------------------------------------------

SEVERITY_ICON = {"error": "FAIL", "warning": "WARN", "info": "INFO"}
SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


def _print_report(results: list[CheckResult], total_files: int) -> None:
    print(f"\n{'-'*70}")
    print(f"  QA Report  --  {total_files} files scanned")
    print(f"{'-'*70}")

    errors = warnings = infos = 0
    for r in sorted(results, key=lambda x: SEVERITY_ORDER[x.severity]):
        icon = SEVERITY_ICON[r.severity]
        status = f"{r.affected_files} files" if r.affected_files else "OK"
        print(f"\n{icon} [{r.severity.upper():7}] {r.name}")
        print(f"   {r.description}")
        print(f"   -> {status}", end="")
        if r.total_occurrences > r.affected_files:
            print(f"  ({r.total_occurrences} occurrences)", end="")
        print()
        for s in r.samples[:5]:
            print(f"     • {s.file}")
            print(f"       {s.detail}")
        if len(r.samples) > 5:
            print(f"     … and {r.affected_files - 5} more files")

        if r.severity == "error":   errors   += r.affected_files
        elif r.severity == "warning": warnings += r.affected_files
        else:                         infos    += r.affected_files

    print(f"\n{'-'*70}")
    print(f"  SUMMARY  errors:{errors}  warnings:{warnings}  info:{infos}")
    print(f"{'-'*70}\n")


# -- main ----------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="QA checks for converted Markdown output"
    )
    parser.add_argument(
        "--dir", default="output",
        help="Directory to scan (default: output/)"
    )
    parser.add_argument(
        "--checks", default=",".join(DEFAULT_CHECKS),
        help="Comma-separated list of checks to run (default: all except broken_relative_links)"
    )
    parser.add_argument(
        "--full", action="store_true",
        help="Also run broken_relative_links check (slow)"
    )
    parser.add_argument(
        "--report", metavar="PATH",
        help="Write JSON report to this path"
    )
    parser.add_argument(
        "--fail-on", default="error",
        choices=["error", "warning", "info", "never"],
        help="Exit 1 if any issue at this severity or above (default: error)"
    )
    args = parser.parse_args()

    scan_dir = Path(args.dir)
    if not scan_dir.exists():
        print(f"Error: directory not found: {scan_dir}", file=sys.stderr)
        return 1

    # Resolve which checks to run
    requested = [c.strip() for c in args.checks.split(",") if c.strip()]
    if args.full and "broken_relative_links" not in requested:
        requested.append("broken_relative_links")

    unknown = [c for c in requested if c not in ALL_CHECKS]
    if unknown:
        print(f"Error: unknown checks: {unknown}", file=sys.stderr)
        print(f"Available: {list(ALL_CHECKS)}", file=sys.stderr)
        return 1

    print(f"Scanning {scan_dir} …", flush=True)
    files = sorted(scan_dir.rglob("*.md"))
    print(f"  {len(files)} .md files found")
    print(f"  Running checks: {', '.join(requested)}\n", flush=True)

    results: list[CheckResult] = []
    for name in requested:
        print(f"  checking {name} …", end="\r", flush=True)
        r = ALL_CHECKS[name](files, scan_dir)
        results.append(r)
        icon = SEVERITY_ICON[r.severity]
        status = f"{r.affected_files} files affected" if r.affected_files else "OK"
        print(f"  {icon} {name:<28} {status}          ")

    _print_report(results, len(files))

    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_data = {
            "scan_dir": str(scan_dir),
            "total_files": len(files),
            "checks": [
                {**{k: v for k, v in asdict(r).items() if k != "samples"},
                 "samples": [asdict(s) for s in r.samples]}
                for r in results
            ],
        }
        report_path.write_text(json.dumps(report_data, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Report written to {report_path}")

    if args.fail_on != "never":
        threshold = SEVERITY_ORDER[args.fail_on]
        for r in results:
            if SEVERITY_ORDER[r.severity] <= threshold and r.affected_files:
                return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
