"""
scripts/lib/manifest_utils.py — Shared URL/path helpers for manifest building.

Used by 01_build_manifest.py and 02a_download_zip.py.
"""

import re
from pathlib import Path
from urllib.parse import urlparse


def should_skip_url(loc: str, settings: dict) -> tuple[bool, str]:
    """
    Return (True, reason) if the URL should be excluded from the manifest.
    Return (False, "") if it should be included.
    """
    parsed = urlparse(loc)
    path = parsed.path
    filename = Path(path).name

    suffix = Path(path).suffix.lower()
    html_exts = set(settings.get("html_extensions", [".htm", ".html"]))
    if suffix and suffix not in html_exts:
        return True, f"non-html-extension:{suffix}"

    skip_filenames = [f.lower() for f in settings.get("skip_filenames", [])]
    if filename.lower() in skip_filenames:
        return True, f"shell-page:{filename}"

    for pattern in settings.get("skip_filename_patterns", []):
        if re.match(pattern, filename, re.IGNORECASE):
            return True, "non-madcap-dita"

    skip_segments = settings.get("skip_path_segments", [])
    for seg in skip_segments:
        if seg.rstrip("/") in path:
            return True, f"skip-path:{seg.strip('/')}"

    return False, ""


def url_to_output_path(loc: str) -> str:
    """
    Map a docs.tibco.com URL to its output .md path.
    e.g. https://docs.tibco.com/pub/foo/1.0/doc/html/Admin/file.htm
      →  pub/foo/1.0/doc/html/Admin/file.md
    """
    path = urlparse(loc).path.lstrip("/")
    return str(Path(path).with_suffix(".md"))


def infer_alias_xml_url(loc: str) -> str:
    """
    Derive the alias.xml URL for a version given one of its page URLs.
    Finds the /doc/html/ root and appends Data/Alias.xml.
    e.g. https://docs.tibco.com/pub/foo/1.0/doc/html/Admin/file.htm
      →  https://docs.tibco.com/pub/foo/1.0/doc/html/Data/Alias.xml
    """
    parsed = urlparse(loc)
    path = parsed.path
    marker = "/doc/html/"
    idx = path.find(marker)
    if idx == -1:
        html_root = Path(path).parent.parent.as_posix() + "/"
    else:
        html_root = path[: idx + len(marker)]
    base = f"{parsed.scheme}://{parsed.netloc}"
    return f"{base}{html_root}Data/Alias.xml"
