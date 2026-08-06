"""
05_postprocess.py — Step 5: Rewrite links and clean up variable tokens.

For each .md file in output/:
  1. Rewrite internal absolute .htm links → relative .md links
     (cross-version and external links are left as absolute URLs)
  2. Strip unresolved MadCap variable tokens: [%=System.LinkedHeader%] etc.
  3. Clean up toc_path in frontmatter: remove empty pipe segments

Usage:
  python scripts/05_postprocess.py --phase phase_01 [--config config/settings.yaml] [--dry-run]
"""

import argparse
import json
import re
import sys
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse, urljoin

import yaml
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.lib.io_utils import format_frontmatter, load_manifest, load_settings, parse_frontmatter
from scripts.lib.reporter import Reporter

# Matches MadCap variable tokens like [%=System.LinkedHeader%] or [%=productvar.productName%]
_TOKEN_RE = re.compile(r"\[%=[\w.\s]+%\]")

# Matches Markdown links: [text](url)  — captures the URL portion
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")

# Matches malformed autolinks: <http://> (no host) or <http://...path> (ellipsis placeholder)
_MALFORMED_AUTOLINK_RE = re.compile(r"<(https?://(?:[^>]*\.\.\..*|))>")

# Strips EBX bottom-breadcrumb lines left by unconverted div#ebx_breadcrumbBottom:
#   [Home](./index.html)>Page - Table of contents
_EBX_BREADCRUMB_LINE_RE = re.compile(r"^\[.+?\]\(\.\/index\.html\)[^\n]*\n?", re.MULTILINE)

# Matches Java_API links in Markdown body — both relative and absolute docs.tibco.com forms:
#   ../Java_API/...   ../../../../Java_API/...   Java_API/...
#   https://docs.tibco.com/.../Java_API/...
# Handles method-signature parens in fragment: foo.html#setByDelta(boolean)
# Non-capturing prefix group so group(1)=text, group(2)=path-after-Java_API/.
_JAVA_API_LINK_RE = re.compile(
    r"\[([^\]]*)\]\("
    r"(?:https?://docs\.tibco\.com/[^)]*?/|(?:\.\.\/)*)"
    r"Java_API/"
    r"([^()]*(?:\([^)]*\)[^()]*)*)"
    r"\)"
)

# Matches .htm/.html href attributes inside raw HTML passthrough blocks
_HTML_HREF_RE = re.compile(r'href="([^"]*\.html?(?:#[^"]*)?)"')

# Matches Java_API href attributes inside raw HTML passthrough blocks
_HTML_JAVA_API_HREF_RE = re.compile(r'href="(?:\.\.\/)*Java_API/([^"]+)"')


def build_url_to_md_index(manifest: list[dict], base_url: str) -> dict[str, str]:
    """
    Build a lookup: normalised URL path → output_path (.md)
    Used to rewrite internal links.
    """
    index = {}
    for entry in manifest:
        if "url" not in entry:
            continue
        url_path = urlparse(entry["url"]).path.lower().rstrip("/")
        index[url_path] = entry["output_path"]
    return index


def clean_toc_path(toc_path: str) -> str:
    """Remove empty segments from a pipe-separated toc_path."""
    if not toc_path:
        return toc_path
    # First strip any token remnants
    cleaned = _TOKEN_RE.sub("", toc_path)
    # Split, strip, remove empties, rejoin
    segments = [s.strip() for s in cleaned.split("|")]
    segments = [s for s in segments if s]
    return "|".join(segments)


def rewrite_links(
    body: str,
    current_output_path: str,
    url_to_md: dict[str, str],
    base_url: str,
    source_url: str,
    reporter: Reporter,
) -> tuple[str, int, int]:
    """
    Rewrite internal .htm links in the Markdown body to relative .md links.

    Handles both absolute (https://...) and relative (.htm) links.
    Returns (updated_body, rewritten_count, unresolvable_count).
    """
    rewritten = 0
    unresolvable = 0
    # Normalize to forward slashes so PurePosixPath splits correctly on Windows
    current_output_path = current_output_path.replace("\\", "/")
    current_md_dir = PurePosixPath(current_output_path).parent

    def replace_link(m: re.Match) -> str:
        nonlocal rewritten, unresolvable
        text, url = m.group(1), m.group(2)

        # Leave pure anchors, mailto, data URIs unchanged
        if url.startswith("#") or url.startswith("mailto:") or url.startswith("data:"):
            return m.group(0)

        # Resolve relative links to absolute using source_url
        if not url.startswith("http"):
            # Strip fragment before resolving, preserve it separately
            if "#" in url:
                url_no_frag, frag = url.split("#", 1)
            else:
                url_no_frag, frag = url, ""
            # Only process .htm/.html relative links
            suffix = PurePosixPath(url_no_frag).suffix.lower()
            if suffix not in (".htm", ".html"):
                return m.group(0)
            # Java_API/ links are handled by rewrite_java_api_links — leave them unchanged
            # so that pass can rewrite them to the external EBX hosting URL.
            if "Java_API/" in url_no_frag:
                return m.group(0)
            url = urljoin(source_url, url_no_frag)
            if frag:
                url = f"{url}#{frag}"

        parsed = urlparse(url)

        # External links (not docs.tibco.com) — leave unchanged
        if parsed.netloc and parsed.netloc not in ("docs.tibco.com", "stag-docs.tibco.com"):
            return m.group(0)

        # Non-HTML links (.pdf, .txt, etc.) — leave unchanged
        suffix = PurePosixPath(parsed.path).suffix.lower()
        if suffix and suffix not in (".htm", ".html", ""):
            return m.group(0)

        # Normalise and look up in the index
        norm_path = parsed.path.lower().rstrip("/")
        if norm_path not in url_to_md:
            unresolvable += 1
            reporter.count("links_unresolvable")
            reporter.debug(f"Unresolvable link: {url}")
            return m.group(0)  # leave as-is, don't break the doc

        target_md = url_to_md[norm_path].replace("\\", "/")
        # Compute relative path from current .md to target .md
        target_posix = PurePosixPath(target_md)
        try:
            rel = target_posix.relative_to(current_md_dir)
        except ValueError:
            # Target is in a different branch — use relative path with ../
            parts_current = current_md_dir.parts
            parts_target  = target_posix.parent.parts
            common_len = 0
            for a, b in zip(parts_current, parts_target):
                if a == b:
                    common_len += 1
                else:
                    break
            up = len(parts_current) - common_len
            down = parts_target[common_len:]
            rel_str = ("../" * up) + "/".join(down)
            if rel_str and not rel_str.endswith("/"):
                rel_str += "/"
            rel_str += target_posix.name
            rel = PurePosixPath(rel_str)

        # Preserve fragment if present
        fragment = f"#{parsed.fragment}" if parsed.fragment else ""
        rewritten += 1
        reporter.count("links_rewritten")
        return f"[{text}]({rel}{fragment})"

    updated = _MD_LINK_RE.sub(replace_link, body)
    return updated, rewritten, unresolvable


def fix_malformed_autolinks(body: str) -> tuple[str, int]:
    """Replace <http://> and <http://...> autolinks with backtick inline code.

    code_urls_to_links() in the preprocessor promotes incomplete <code>http://</code>
    spans (no host, or ellipsis placeholder host) to <a href> links. Markdownify then
    renders them as autolinks that crash DITA-OT's URI parser.
    """
    count = [0]

    def _replace(m: re.Match) -> str:
        count[0] += 1
        return f"`{m.group(1)}`"

    return _MALFORMED_AUTOLINK_RE.sub(_replace, body), count[0]


def strip_ebx_breadcrumb_lines(body: str) -> tuple[str, int]:
    count = [0]

    def _replace(m: re.Match) -> str:
        count[0] += 1
        return ""

    return _EBX_BREADCRUMB_LINE_RE.sub(_replace, body), count[0]


def normalize_heading_levels(body: str) -> tuple[str, int]:
    """Cap heading level jumps so no heading descends more than one level from the previous.

    Walks lines in order, skipping fenced code blocks. When a heading jumps by more
    than one level (e.g. H1 → H3), it is promoted to prev_level + 1. Going back up
    (e.g. H3 → H1) is always permitted and resets the tracking anchor.
    """
    lines = body.split("\n")
    in_fence = False
    fence_char = ""
    fence_len = 0
    prev_level = 0
    fixes = 0
    out = []

    for line in lines:
        stripped = line.lstrip()

        if not in_fence:
            # Opening fence: 3+ backticks or tildes; info string is allowed after the run.
            m_fence = re.match(r"^(`{3,}|~{3,})", stripped)
            if m_fence:
                fence_char = stripped[0]
                fence_len = len(m_fence.group(1))
                in_fence = True
                out.append(line)
                continue
        else:
            # Closing fence: same character, at least as long, no content after.
            if stripped and stripped[0] == fence_char:
                closing_run = len(stripped) - len(stripped.lstrip(fence_char))
                if closing_run >= fence_len and not stripped[closing_run:].strip():
                    in_fence = False
            out.append(line)
            continue

        if not in_fence:
            m = re.match(r"^(#{1,6})(\s.*)$", line)
            if m:
                level = len(m.group(1))
                if prev_level > 0 and level > prev_level + 1:
                    level = prev_level + 1
                    fixes += 1
                    line = "#" * level + m.group(2)
                prev_level = level

        out.append(line)

    return "\n".join(out), fixes


def rewrite_html_hrefs(
    body: str,
    current_output_path: str,
    url_to_md: dict[str, str],
    base_url: str,
    source_url: str,
    reporter: Reporter,
) -> tuple[str, int, int]:
    """Rewrite .htm/.html href attributes inside raw HTML passthrough blocks to .md links."""
    rewritten = 0
    unresolvable = 0
    current_output_path = current_output_path.replace("\\", "/")
    current_md_dir = PurePosixPath(current_output_path).parent

    def replace_href(m: re.Match) -> str:
        nonlocal rewritten, unresolvable
        url = m.group(1)
        if url.startswith("#") or url.startswith("mailto:") or url.startswith("data:"):
            return m.group(0)
        if "#" in url:
            url_no_frag, frag = url.split("#", 1)
        else:
            url_no_frag, frag = url, ""
        # Java_API/ hrefs are handled by rewrite_java_api_html_hrefs — leave them
        if "Java_API/" in url_no_frag:
            return m.group(0)
        if not url_no_frag.startswith("http"):
            suffix = PurePosixPath(url_no_frag).suffix.lower()
            if suffix not in (".htm", ".html"):
                return m.group(0)
            abs_url = urljoin(source_url, url_no_frag)
        else:
            abs_url = url_no_frag
        parsed = urlparse(abs_url)
        if parsed.netloc and parsed.netloc not in ("docs.tibco.com", "stag-docs.tibco.com"):
            return m.group(0)
        suffix = PurePosixPath(parsed.path).suffix.lower()
        if suffix and suffix not in (".htm", ".html", ""):
            return m.group(0)
        norm_path = parsed.path.lower().rstrip("/")
        if norm_path not in url_to_md:
            unresolvable += 1
            reporter.count("html_hrefs_unresolvable")
            reporter.debug(f"Unresolvable HTML href: {abs_url}")
            return m.group(0)
        target_md = url_to_md[norm_path].replace("\\", "/")
        target_posix = PurePosixPath(target_md)
        try:
            rel = target_posix.relative_to(current_md_dir)
        except ValueError:
            parts_current = current_md_dir.parts
            parts_target = target_posix.parent.parts
            common_len = sum(1 for a, b in zip(parts_current, parts_target) if a == b)
            up = len(parts_current) - common_len
            down = parts_target[common_len:]
            rel_str = ("../" * up) + "/".join(down)
            if rel_str and not rel_str.endswith("/"):
                rel_str += "/"
            rel_str += target_posix.name
            rel = PurePosixPath(rel_str)
        fragment = f"#{frag}" if frag else ""
        rewritten += 1
        reporter.count("html_hrefs_rewritten")
        return f'href="{rel}{fragment}"'

    updated = _HTML_HREF_RE.sub(replace_href, body)
    return updated, rewritten, unresolvable


def rewrite_java_api_links(body: str, version_dashed: str) -> tuple[str, int]:
    """Replace relative Java_API/... links with the external EBX Java API hosting URL."""
    base = f"https://stg-docs.onebx.com/us/en/ebx/resources/javadocs/{version_dashed}/"
    count = 0

    def replace_java_link(m: re.Match) -> str:
        nonlocal count
        text, path = m.group(1), m.group(2)
        count += 1
        return f"[{text}]({base}{path})"

    updated = _JAVA_API_LINK_RE.sub(replace_java_link, body)
    return updated, count


def rewrite_java_api_html_hrefs(body: str, version_dashed: str) -> tuple[str, int]:
    """Replace relative Java_API href attributes in raw HTML blocks with the external EBX Java API URL."""
    base = f"https://stg-docs.onebx.com/us/en/ebx/resources/javadocs/{version_dashed}/"
    count = 0

    def replace_java_href(m: re.Match) -> str:
        nonlocal count
        count += 1
        return f'href="{base}{m.group(1)}"'

    return _HTML_JAVA_API_HREF_RE.sub(replace_java_href, body), count


def rewrite_blockquotes_in_tables(body: str) -> tuple[str, int]:
    """Replace <blockquote> HTML tags with <div class="note-inline">.

    Raw HTML table passthrough blocks (Tier 3) preserve <blockquote> tags as literal
    HTML, which AEM publishing drops. This renames the outer tag only; inner content
    (<p>, <ul>, <div>, etc.) is left completely unchanged.

    Markdown-syntax blockquotes (lines starting with "> ") are never affected because
    markdownify converts them at step-3 time and they never appear as <blockquote> HTML
    in the .md output.
    """
    count = body.count("<blockquote>")
    if not count:
        return body, 0
    body = body.replace("<blockquote>", '<div class="note-inline">')
    body = body.replace("</blockquote>", "</div>")
    return body, count


def postprocess_file(
    md_path: Path,
    output_path_rel: str,
    url_to_md: dict[str, str],
    base_url: str,
    source_url: str,
    reporter: Reporter,
    dry_run: bool,
) -> bool:
    try:
        content = md_path.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(content)

        # 1. Clean toc_path in frontmatter
        if "toc_path" in fm:
            original = fm["toc_path"]
            cleaned  = clean_toc_path(str(original))
            if cleaned != str(original):
                fm["toc_path"] = cleaned
                reporter.count("toc_paths_cleaned")

        # 2. Strip variable tokens from body
        token_count = len(_TOKEN_RE.findall(body))
        if token_count:
            body = _TOKEN_RE.sub("", body)
            reporter.count("tokens_stripped", token_count)

        # 3. Rewrite internal links
        body, rewritten, unresolvable = rewrite_links(
            body, output_path_rel, url_to_md, base_url, source_url, reporter
        )

        # 4. Fix malformed autolinks that crash DITA-OT URI parser
        body, bad_links = fix_malformed_autolinks(body)
        if bad_links:
            reporter.count("malformed_autolinks_fixed", bad_links)

        # 5. Normalize heading levels (cap jumps like H1→H3 to H1→H2)
        body, heading_fixes = normalize_heading_levels(body)
        if heading_fixes:
            reporter.count("heading_levels_normalized", heading_fixes)

        # 6. Strip EBX bottom-breadcrumb lines: [Home](./index.html)>...
        body, breadcrumb_count = strip_ebx_breadcrumb_lines(body)
        if breadcrumb_count:
            reporter.count("ebx_breadcrumb_lines_stripped", breadcrumb_count)

        # 7. Rewrite relative Java_API links to external EBX Java API hosting
        product_version = fm.get("product_version", "")
        if product_version:
            version_dashed = str(product_version).replace(".", "-")
            body, java_api_count = rewrite_java_api_links(body, version_dashed)
            if java_api_count:
                reporter.count("java_api_links_rewritten", java_api_count)
            # 7b. Same rewrite for href attributes inside raw HTML passthrough blocks
            body, java_api_html_count = rewrite_java_api_html_hrefs(body, version_dashed)
            if java_api_html_count:
                reporter.count("java_api_html_hrefs_rewritten", java_api_html_count)

        # 8. Rewrite .htm/.html href attributes inside raw HTML passthrough blocks
        body, html_rewritten, _ = rewrite_html_hrefs(
            body, output_path_rel, url_to_md, base_url, source_url, reporter
        )

        # 9. Replace <blockquote> HTML tags with <div class="note-inline"> in EBX/EBX-addon files.
        #    Only HTML <blockquote> tags are affected; these only appear inside raw HTML table
        #    passthrough blocks (Tier 3). Markdown "> " blockquotes are never touched.
        if "/pub/ebx" in source_url:
            body, bq_count = rewrite_blockquotes_in_tables(body)
            if bq_count:
                reporter.count("blockquotes_rewritten", bq_count)

        if not dry_run:
            md_path.write_text(format_frontmatter(fm, body), encoding="utf-8")

        reporter.count("pages_postprocessed")
        return True

    except Exception as exc:
        reporter.fail(str(md_path), f"{type(exc).__name__}: {exc}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Step 5: Postprocess Markdown files")
    parser.add_argument("--phase",   required=True)
    parser.add_argument("--config",  default="config/settings.yaml")
    parser.add_argument("--dry-run",     action="store_true")
    parser.add_argument("--force-rerun", action="store_true", help="Accepted for orchestrator compat")
    args = parser.parse_args()

    settings   = load_settings(args.config)
    manifest   = load_manifest(args.phase, settings)
    output_dir = Path(settings.get("output_dir", "output"))
    base_url   = settings.get("base_url", "https://docs.tibco.com")

    from datetime import datetime
    logs_dir = Path(settings.get("logs_dir", "logs"))
    run_dir  = logs_dir / args.phase / datetime.now().strftime("%Y%m%d-%H%M%S")
    reporter = Reporter(run_dir, "05_postprocess", dry_run=args.dry_run)

    reporter.info(f"=== Step 5: Postprocess | phase={args.phase} dry_run={args.dry_run} ===")

    url_to_md = build_url_to_md_index(manifest, base_url)
    reporter.info(f"Link index built: {len(url_to_md)} URLs")

    for entry in tqdm(manifest, desc="Postprocessing"):
        if "url" not in entry:
            continue
        md_path = output_dir / entry["output_path"]
        if not md_path.exists():
            reporter.skip(entry["url"], "md-file-not-found")
            continue
        postprocess_file(md_path, entry["output_path"], url_to_md, base_url, entry["url"], reporter, args.dry_run)

    report = reporter.finish()
    return 0 if report["error_count"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
