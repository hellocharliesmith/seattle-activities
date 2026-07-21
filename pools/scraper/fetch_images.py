"""
Fetches each pool's header image from its own seattle.gov page and caches
it in data/pool-images.json, keyed by slug.

Deliberately NOT part of the daily scrape (pools.py) - these are static
facility photos that don't change day to day. Run by hand occasionally
(once a year is plenty). site/generate.py merges this file's images into
the daily data by slug at render time.

Usage: python3 fetch_images.py
"""
import json
import sys
import time
from pathlib import Path

from pools import OUT_PATH as POOLS_DATA, fetch_pool_image

IMAGES_PATH = Path(__file__).resolve().parent.parent / "data" / "pool-images.json"


def main():
    if not POOLS_DATA.exists():
        print(f"Run pools.py first - {POOLS_DATA} doesn't exist yet.", file=sys.stderr)
        sys.exit(1)

    data = json.loads(POOLS_DATA.read_text(encoding="utf-8"))
    images = {}
    missing = []
    for pool in data["pools"]:
        url = fetch_pool_image(pool.get("url"))
        if url:
            images[pool["slug"]] = url
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
