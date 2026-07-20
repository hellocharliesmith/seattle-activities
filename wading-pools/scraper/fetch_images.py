"""
Fetches each wading pool's header image from its own seattle.gov page and
caches it in data/pool-images.json, keyed by pool name.

This is deliberately NOT part of the daily scrape (wading_pools.py) - these
are static park photos that essentially never change, so re-fetching them
every day would just be ~19 extra requests to seattle.gov for no benefit.
Run this by hand occasionally (once a year is plenty) to pick up new photos
or newly added pools. site/generate.py merges this file's images into the
daily data by pool name at render time.

Usage: python3 fetch_images.py
"""
import json
import sys
import time
from pathlib import Path

from wading_pools import OUT_PATH as WADING_POOLS_DATA, fetch_pool_image

IMAGES_PATH = Path(__file__).resolve().parent.parent / "data" / "pool-images.json"


def main():
    if not WADING_POOLS_DATA.exists():
        print(f"Run wading_pools.py first - {WADING_POOLS_DATA} doesn't exist yet.", file=sys.stderr)
        sys.exit(1)

    data = json.loads(WADING_POOLS_DATA.read_text(encoding="utf-8"))
    images = {}
    missing = []
    for pool in data["wading_pools"]:
        url = fetch_pool_image(pool.get("info_url"))
        if url:
            images[pool["name"]] = url
        else:
            missing.append(pool["name"])
        time.sleep(0.15)  # be polite to seattle.gov

    IMAGES_PATH.parent.mkdir(parents=True, exist_ok=True)
    IMAGES_PATH.write_text(json.dumps(images, indent=2), encoding="utf-8")
    print(f"Wrote {IMAGES_PATH} ({len(images)} images)")
    if missing:
        print(f"No header image found for: {', '.join(missing)}", file=sys.stderr)


if __name__ == "__main__":
    main()
