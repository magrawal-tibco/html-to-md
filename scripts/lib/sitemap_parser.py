"""
sitemap_parser.py — 3-level sitemap crawl for docs.tibco.com.

Level 2 (product sitemapindex) → Level 3 (version urlset) → content URLs.
Phase YAML files provide Level 2 URLs; this module discovers everything below.

Namespace note: the version urlset uses a non-standard namespace URI ending in
/sitemap.xsd rather than the usual /0.9 — we normalise both.
"""

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Iterator

import httpx

# Both namespace variants seen in docs.tibco.com sitemaps
_SM_NAMESPACES = {
    "sm":    "http://www.sitemaps.org/schemas/sitemap/0.9",
    "sm2":   "http://www.sitemaps.org/schemas/sitemap/0.9/sitemap.xsd",
    "coveo": "http://www.coveo.com/schemas/metadata",
}


@dataclass
class UrlEntry:
    loc: str
    lastmod: str = ""
    product_name: str = ""
    product_version: str = ""
    doc_name: str = ""
    access_level: str = ""
    parent_product: str = ""


def _fetch_xml(client: httpx.Client, url: str) -> ET.Element:
    """Fetch a URL and return the parsed XML root element."""
    resp = client.get(url)
    resp.raise_for_status()
    return ET.fromstring(resp.content)


def _ns(tag: str, root: ET.Element) -> str:
    """Build a namespaced tag string matching whatever namespace root uses."""
    ns_uri = root.tag.split("}")[0].lstrip("{") if "}" in root.tag else ""
    return f"{{{ns_uri}}}{tag}" if ns_uri else tag


def _is_sitemapindex(root: ET.Element) -> bool:
    local = root.tag.split("}")[-1] if "}" in root.tag else root.tag
    return local == "sitemapindex"


def _get_locs(root: ET.Element, child_tag: str) -> list[str]:
    """
    Extract <loc> text from children named child_tag ('sitemap' or 'url').
    Handles both namespace variants transparently.
    """
    ns_uri = root.tag.split("}")[0].lstrip("{") if "}" in root.tag else ""
    loc_tag = f"{{{ns_uri}}}loc" if ns_uri else "loc"
    child_full = f"{{{ns_uri}}}{child_tag}" if ns_uri else child_tag

    locs = []
    for child in root.findall(child_full):
        loc_el = child.find(loc_tag)
        if loc_el is not None and loc_el.text:
            locs.append(loc_el.text.strip())
    return locs


def _parse_coveo_metadata(url_el: ET.Element) -> dict:
    """
    Extract coveo:metadata fields from a <url> element.

    The <coveo:metadata> container is in the coveo namespace, but its children
    (<name>, <productversion>, etc.) are unqualified elements that inherit the
    document's default namespace (the sitemap xsd namespace). We therefore match
    child elements by local name only, ignoring namespace.
    """
    ns_uri = "http://www.coveo.com/schemas/metadata"
    meta: dict[str, str] = {}

    meta_el = url_el.find(f"{{{ns_uri}}}metadata")
    if meta_el is None:
        return meta

    field_map = {
        "name":           "product_name",
        "productversion": "product_version",
        "d_name":         "doc_name",
        "access_level":   "access_level",
        "parent_product": "parent_product",
    }
    for child in meta_el:
        # Strip namespace prefix to get local tag name
        local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if local in field_map and child.text:
            meta[field_map[local]] = child.text.strip()
    return meta


def _parse_urlset(root: ET.Element) -> list[UrlEntry]:
    """Parse a urlset element into a list of UrlEntry objects."""
    ns_uri = root.tag.split("}")[0].lstrip("{") if "}" in root.tag else ""
    url_tag   = f"{{{ns_uri}}}url"   if ns_uri else "url"
    loc_tag   = f"{{{ns_uri}}}loc"   if ns_uri else "loc"
    lmod_tag  = f"{{{ns_uri}}}lastmod" if ns_uri else "lastmod"

    entries = []
    for url_el in root.findall(url_tag):
        loc_el  = url_el.find(loc_tag)
        lmod_el = url_el.find(lmod_tag)
        if loc_el is None or not loc_el.text:
            continue
        meta = _parse_coveo_metadata(url_el)
        entries.append(UrlEntry(
            loc=loc_el.text.strip(),
            lastmod=lmod_el.text.strip() if lmod_el is not None and lmod_el.text else "",
            **meta,
        ))
    return entries


def iter_product_versions(
    client: httpx.Client,
    product_sitemap_url: str,
) -> Iterator[tuple[str, list[UrlEntry]]]:
    """
    Given a Level 2 product sitemapindex URL, yield (version_sitemap_url, [UrlEntry])
    for every version sitemap found underneath it.
    """
    root = _fetch_xml(client, product_sitemap_url)
    if not _is_sitemapindex(root):
        # Some products only have a single urlset — treat it as one version
        entries = _parse_urlset(root)
        yield product_sitemap_url, entries
        return

    version_urls = _get_locs(root, "sitemap")
    for version_url in version_urls:
        try:
            v_root = _fetch_xml(client, version_url)
            entries = _parse_urlset(v_root)
            yield version_url, entries
        except Exception as exc:
            # Caller handles logging
            raise RuntimeError(f"Failed to fetch version sitemap {version_url}: {exc}") from exc


def build_http_client(settings: dict) -> httpx.Client:
    """Create a synchronous httpx client from settings."""
    http = settings.get("http", {})
    return httpx.Client(
        headers={"User-Agent": http.get("user_agent", "tibco-docs-converter/1.0")},
        timeout=httpx.Timeout(
            connect=http.get("timeout_connect", 10),
            read=http.get("timeout_read", 30),
            write=10,
            pool=10,
        ),
        follow_redirects=True,
    )
