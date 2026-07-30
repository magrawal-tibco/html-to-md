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

# Matches relative Java_API links in Markdown body — all prefix variants:
#   ../Java_API/...   ../../../../Java_API/...   Java_API/...
# Handles method-signature parens in fragment: foo.html#setByDelta(boolean)
_JAVA_API_LINK_RE = re.compile(
    r"\[([^\]]*)\]\(((?:\.\.\/)*|)Java_API/([^()]*(?:\([^)]*\)[^()]*)*)\)"
)


def load_settings(config_path: str) -> dict:
    return yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))


def load_manifest(phase: str, settings: dict) -> list[dict]:
    manifests_dir = Path(settings.get("manifests_dir", "manifests"))
    path = manifests_dir / f"manifest_{phase}.json"
    if not path.exists():
        raise FileNotFoundError(f"Manifest not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def build_url_to_md_index(manifest: list[dict], base_url: str) -> dict[str, str]:
    """
    Build a lookup: normalised URL path → output_path (.md)
    Used to rewrite internal links.
    """
    index = {}
    for entry in manifest:
        url_path = urlparse(entry["url"]).path.lower().rstrip("/")
        index[url_path] = entry["output_path"]
    return index


def read_frontmatter(content: str) -> tuple[dict, str]:
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


def write_frontmatter(fm: dict, body: str) -> str:
    fm_text = yaml.dump(fm, allow_unicode=True, default_flow_style=False)
    return f"---\n{fm_text}---\n\n{body.lstrip()}"


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
            # Relative .htm links that couldn't be resolved: rewrite as their
            # absolute URL so they're not left as broken relative paths in the MD output.
            if not m.group(2).startswith("http"):
                return f"[{text}]({url})"
            return m.group(0)  # already absolute — leave unchanged

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
    prev_level = 0
    fixes = 0
    out = []

    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
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


def rewrite_java_api_links(body: str, version_dashed: str) -> tuple[str, int]:
    """Replace relative Java_API/... links with the external EBX Java API hosting URL."""
    base = f"https://stg-docs.onebx.com/us/en/ebx/resources/javadocs/{version_dashed}/"
    count = 0

    def replace_java_link(m: re.Match) -> str:
        nonlocal count
        text, path = m.group(1), m.group(3)
        count += 1
        return f"[{text}]({base}{path})"

    updated = _JAVA_API_LINK_RE.sub(replace_java_link, body)
    return updated, count


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
        fm, body = read_frontmatter(content)

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

        if not dry_run:
            md_path.write_text(write_frontmatter(fm, body), encoding="utf-8")

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
        md_path = output_dir / entry["output_path"]
        if not md_path.exists():
            reporter.skip(entry["url"], "md-file-not-found")
            continue
        postprocess_file(md_path, entry["output_path"], url_to_md, base_url, entry["url"], reporter, args.dry_run)

    report = reporter.finish()
    return 0 if report["error_count"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
