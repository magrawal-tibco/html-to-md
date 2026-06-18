"""
cache_sitemaps.py — Download and cache all L2 + L3 sitemaps
from a product-listing sitemap.xml.

Usage:
  python scripts/cache_sitemaps.py --sitemap "sample sitemaps/sitemap.xml"
                                   [--cache-dir cache/_sitemaps]
                                   [--config config/settings.yaml]
"""

import argparse
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import yaml
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts.lib.reporter import Reporter
from scripts.lib.sitemap_parser import _get_locs, _is_sitemapindex, build_http_client

L2_BASE = "https://docs.tibco.com/ftp_portal/coveo/tibco-{slug}.xml"


def parse_product_slugs(sitemap_path: Path) -> list[tuple[str, str]]:
    """Return [(slug, name), ...] from a flat product sitemap."""
    root = ET.parse(sitemap_path).getroot()
    ns_uri = root.tag.split("}")[0].lstrip("{") if "}" in root.tag else ""
    url_tag  = f"{{{ns_uri}}}url"  if ns_uri else "url"
    loc_tag  = f"{{{ns_uri}}}loc"  if ns_uri else "loc"
    name_tag = f"{{{ns_uri}}}name" if ns_uri else "name"

    results = []
    for url_el in root.findall(url_tag):
        loc_el  = url_el.find(loc_tag)
        name_el = url_el.find(name_tag)
        if loc_el is None or not loc_el.text:
            continue
        path = urlparse(loc_el.text.strip()).path
        if "/products/" not in path:
            continue
        slug = path.rstrip("/").split("/products/")[-1]
        name = name_el.text.strip() if name_el is not None and name_el.text else slug
        results.append((slug, name))
    return results


def save_xml(content: bytes, url: str, cache_dir: Path) -> Path:
    """Save raw XML bytes to cache mirroring the URL path."""
    rel  = urlparse(url).path.lstrip("/")
    dest = cache_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    return dest


def main():
    parser = argparse.ArgumentParser(description="Cache L2 + L3 sitemaps from a product sitemap")
    parser.add_argument("--sitemap",   required=True, help="Path to the product-listing sitemap.xml")
    parser.add_argument("--cache-dir", default="cache/_sitemaps")
    parser.add_argument("--config",    default="config/settings.yaml")
    parser.add_argument("--cookie",    default="", help="Cookie header string, e.g. 'name=value; name2=value2'")
    args = parser.parse_args()

    settings  = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    cache_dir = Path(args.cache_dir)
    slugs     = parse_product_slugs(Path(args.sitemap))
    client    = build_http_client(settings)
    if args.cookie:
        client.headers["Cookie"] = args.cookie
    delay     = settings.get("http", {}).get("delay_seconds", 0.5)

    logs_dir = Path(settings.get("logs_dir", "logs"))
    run_dir  = logs_dir / "cache_sitemaps" / datetime.now().strftime("%Y%m%d-%H%M%S")
    reporter = Reporter(run_dir, "cache_sitemaps")

    reporter.info(f"Products in sitemap: {len(slugs)}")
    reporter.info(f"Cache dir: {cache_dir}")

    l2_ok = l2_auth = l2_skip = l3_total = l3_skip = 0

    for slug, name in tqdm(slugs, desc="Products"):
        l2_url = L2_BASE.format(slug=slug)
        try:
            resp = client.get(l2_url)
            if resp.status_code == 404:
                reporter.info(f"  SKIP (404): {l2_url}")
                l2_skip += 1
                time.sleep(delay)
                continue
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "")
            if "html" in ct:
                reporter.info(f"  AUTH (login page): {l2_url}")
                l2_auth += 1
                time.sleep(delay)
                continue
            save_xml(resp.content, l2_url, cache_dir)
            l2_ok += 1
            reporter.info(f"  L2 OK: {l2_url}")

            root = ET.fromstring(resp.content)
            if _is_sitemapindex(root):
                l3_urls = _get_locs(root, "sitemap")
                for l3_url in l3_urls:
                    try:
                        r3 = client.get(l3_url)
                        r3.raise_for_status()
                        r3_ct = r3.headers.get("content-type", "")
                        if "html" in r3_ct:
                            reporter.warning(f"    L3 AUTH: {l3_url}")
                            l3_skip += 1
                        else:
                            save_xml(r3.content, l3_url, cache_dir)
                            l3_total += 1
                        time.sleep(delay)
                    except Exception as exc:
                        reporter.warning(f"    L3 FAIL {l3_url}: {exc}")
            else:
                l3_total += 1

        except Exception as exc:
            reporter.warning(f"  L2 FAIL {l2_url}: {exc}")
            l2_skip += 1

        time.sleep(delay)

    reporter.info(f"Done — L2 ok={l2_ok} auth={l2_auth} skip/fail={l2_skip} | L3 cached={l3_total} auth={l3_skip}")
    reporter.finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
