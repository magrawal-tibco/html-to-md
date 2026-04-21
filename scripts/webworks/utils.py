"""Shared utilities for the WebWorks ePublisher sub-pipeline."""

import re
from pathlib import Path

from bs4 import BeautifulSoup


def is_version_level_books(books_path: Path) -> bool:
    """True if this is the version-level books.htm (links to multiple guides)."""
    soup = BeautifulSoup(
        books_path.read_text(encoding="utf-8", errors="replace"), "html.parser"
    )
    links = [
        a["href"]
        for div in soup.find_all("div")
        if (a := div.find("a")) and a.get("href")
    ]
    # Version-level links include guide dir segment: "../tib_bw_admin/wwhdata/files.htm"
    # Per-guide links are shorter: "../wwhdata/files.htm"
    # Version-level: "../tib_bw_administration/wwhdata/files.htm" → 3 slashes
    # Per-guide:     "../wwhdata/files.htm"                        → 2 slashes
    return any(link.count("/") >= 3 for link in links)


def discover_webworks_versions(cache_dir: Path):
    """Yield (version_html_root, product_slug, version) for each WebWorks version."""
    for books_path in sorted(cache_dir.glob("**/wwhelp/books.htm")):
        if not is_version_level_books(books_path):
            continue
        version_html_root = books_path.parent.parent
        parts = version_html_root.relative_to(cache_dir).parts
        version = None
        slug_parts = []
        for i, p in enumerate(parts):
            if re.match(r"^\d+\.\d+", p):
                version = p
                slug_parts = list(parts[:i])
                break
        if version is None:
            continue
        yield version_html_root, "/".join(slug_parts), version


def read_books_htm(version_html_root: Path) -> list[Path]:
    """Return list of guide directories from wwhelp/books.htm."""
    books_path = version_html_root / "wwhelp" / "books.htm"
    soup = BeautifulSoup(
        books_path.read_text(encoding="utf-8", errors="replace"), "html.parser"
    )
    guides = []
    for div in soup.find_all("div"):
        a = div.find("a")
        if a and a.get("href"):
            href = a["href"].replace("../", "")
            guide_dir = version_html_root / Path(href).parts[0]
            guides.append(guide_dir)
    return guides


def read_files_index(guide_dir: Path) -> list[tuple[str, str]]:
    """Return [(relative_href, title), ...] from wwhdata/files.htm (0-based)."""
    files_htm = guide_dir / "wwhdata" / "files.htm"
    if not files_htm.exists():
        return []
    soup = BeautifulSoup(
        files_htm.read_text(encoding="utf-8", errors="replace"), "html.parser"
    )
    entries = []
    for div in soup.find_all("div"):
        a = div.find("a")
        if a and a.get("href"):
            href = a["href"].replace("../", "")
            title = a.get("title") or a.get_text(strip=True)
            entries.append((href, title))
    return entries
