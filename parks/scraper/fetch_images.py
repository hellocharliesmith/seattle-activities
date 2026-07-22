"""
Fetches each park's header image from its own seattle.gov page (when we
found one - see fetch_park_url_index() in parks.py) and caches it in
data/park-images.json, keyed by slug.

Deliberately NOT part of the daily scrape (parks.py) - these are static
facility photos that don't change day to day, and with ~300+ matched pages
this is a much heavier network job than the daily refresh should be doing.
Run by hand occasionally (once a year is plenty). site/generate.py merges
this file's images into the daily data by slug at render time - same
pattern as pools/scraper/fetch_images.py.

Usage: python3 fetch_images.py
"""
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "parks.json"
IMAGES_PATH = Path(__file__).resolve().parent.parent / "data" / "park-images.json"


def fetch_html(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_park_image(url: str):
    """Same hero-image markup pools' page template uses:
    <div class="featureWrapper ..." data-backgroundurl="/images/...jpg">
    Soft-fails to None - a missing thumbnail shouldn't break anything."""
    if not url:
        return None
    try:
        html = fetch_html(url)
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    m = re.search(r'class="featureWrapper[^"]*"\s+data-backgroundurl="([^"]+)"', html)
    if not m or not m.group(1).strip():
        return None
    return urllib.parse.urljoin(url, m.group(1))


def main():
    if not DATA_PATH.exists():
        print(f"Run parks.py first - {DATA_PATH} doesn't exist yet.", file=sys.stderr)
        sys.exit(1)

    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    images = {}
    missing = 0
    no_url = 0
    for park in data["parks"]:
        if not park.get("seattle_url"):
            no_url += 1
            continue
        url = fetch_park_image(park["seattle_url"])
        if url:
            images[park["slug"]] = url
        else:
            missing += 1
        time.sleep(0.15)  # be polite to seattle.gov

    IMAGES_PATH.parent.mkdir(parents=True, exist_ok=True)
    IMAGES_PATH.write_text(json.dumps(images, indent=2), encoding="utf-8")
    print(f"Wrote {IMAGES_PATH} ({len(images)} images)")
    print(f"  {missing} parks had a Seattle.gov page but no header image found", file=sys.stderr)
    print(f"  {no_url} parks had no Seattle.gov page to check", file=sys.stderr)


if __name__ == "__main__":
    main()
