"""
list_missing_images.py -- Find images present in EBX HTML source but missing from MD output.

For each .md file flagged with image_count mismatch, parse the HTML and MD
to find which specific image filenames appear in the HTML but not in the MD.
"""
import json
import re
import sys
from pathlib import Path

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: beautifulsoup4 not installed", file=sys.stderr)
    sys.exit(1)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

REPORT_PATH = "C:/Users/Mayur.Agrawal/AppData/Local/Temp/ebx_images.json"
CACHE_PUB   = Path("cache/pub")
OUTPUT_PUB  = Path("output/pub")

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

def _md_images(md_content: str) -> set[str]:
    """Extract image filenames referenced in Markdown."""
    # Matches ![alt](path) and <img src="path">
    md_links   = re.findall(r'!\[[^\]]*\]\(([^)]+)\)', md_content)
    html_imgs  = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', md_content, re.IGNORECASE)
    names = set()
    for url in md_links + html_imgs:
        names.add(Path(url.split("?")[0]).name)
    return names

data = json.loads(open(REPORT_PATH, encoding="utf-8").read())
image_issues = [i for i in data["issues"] if i["check"] == "image_count"]

print(f"Files with image count differences: {len(image_issues)}")
print("=" * 72)

all_missing = []  # (html_file, [missing_image_names])

for issue in image_issues:
    html_rel = issue["html"].replace("\\", "/")
    md_rel   = issue["md"].replace("\\", "/")

    html_path = CACHE_PUB / html_rel
    md_path   = OUTPUT_PUB / md_rel

    if not html_path.exists() or not md_path.exists():
        continue

    try:
        soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
        main = _main_content(soup)
        html_imgs = set()
        if main:
            for img in main.find_all("img"):
                src = img.get("src", "")
                if src:
                    html_imgs.add(Path(src.split("?")[0]).name)

        md_imgs = _md_images(md_path.read_text(encoding="utf-8"))
        missing = sorted(html_imgs - md_imgs)

        if missing:
            all_missing.append((html_rel, missing))
            print(f"\n{html_rel}")
            print(f"  HTML images : {sorted(html_imgs)}")
            print(f"  MD images   : {sorted(md_imgs)}")
            print(f"  MISSING     : {missing}")
    except Exception as e:
        print(f"  ERROR reading {html_rel}: {e}")

print()
print("=" * 72)
print(f"\nSummary: {len(all_missing)} files with missing images")
all_names = sorted({img for _, imgs in all_missing for img in imgs})
print(f"Unique missing image filenames ({len(all_names)}):")
for name in all_names:
    count = sum(1 for _, imgs in all_missing if name in imgs)
    print(f"  {count:3d}x  {name}")
