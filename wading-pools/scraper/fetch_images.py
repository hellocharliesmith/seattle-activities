"""
Fetches each wading pool's header image, and each spraypark's header image
+ address + map link, from their own seattle.gov pages. Caches results in
data/pool-images.json (wading pools - image only, they already have an
address from the daily schedule table) and data/sprayparks-details.json
(sprayparks - image + address + map, none of which the daily scrape can
get from the sprayparks list alone).

This is deliberately NOT part of the daily scrape (wading_pools.py) - park
photos and addresses essentially never change, so re-fetching them every
day would just be ~30 extra requests to seattle.gov for no benefit. Run
this by hand occasionally (once a year is plenty) to pick up new photos,
new locations, or an address change. site/generate.py merges these files
into the daily data by name at render time.

Usage: python3 fetch_images.py
"""
import json
import sys
import time
from pathlib import Path

from wading_pools import OUT_PATH as WADING_POOLS_DATA, fetch_park_page_details

IMAGES_PATH = Path(__file__).resolve().parent.parent / "data" / "pool-images.json"
SPRAYPARKS_DETAILS_PATH = Path(__file__).resolve().parent.parent / "data" / "sprayparks-details.json"


def main():
    if not WADING_POOLS_DATA.exists():
        print(f"Run wading_pools.py first - {WADING_POOLS_DATA} doesn't exist yet.", file=sys.stderr)
        sys.exit(1)

    data = json.loads(WADING_POOLS_DATA.read_text(encoding="utf-8"))

    images = {}
    missing_images = []
    for pool in data["wading_pools"]:
        details = fetch_park_page_details(pool.get("info_url"))
        if details["image_url"]:
            images[pool["name"]] = details["image_url"]
        else:
            missing_images.append(pool["name"])
        time.sleep(0.15)  # be polite to seattle.gov

    IMAGES_PATH.parent.mkdir(parents=True, exist_ok=True)
    IMAGES_PATH.write_text(json.dumps(images, indent=2), encoding="utf-8")
    print(f"Wrote {IMAGES_PATH} ({len(images)} images)")
    if missing_images:
        print(f"No header image found for: {', '.join(missing_images)}", file=sys.stderr)

    spray_details = {}
    missing_spray = []
    for loc in data["sprayparks"]["locations"]:
        details = fetch_park_page_details(loc.get("info_url"))
        if details["image_url"] or details["address"]:
            spray_details[loc["name"]] = details
        else:
            missing_spray.append(loc["name"])
        time.sleep(0.15)

    SPRAYPARKS_DETAILS_PATH.write_text(json.dumps(spray_details, indent=2), encoding="utf-8")
    print(f"Wrote {SPRAYPARKS_DETAILS_PATH} ({len(spray_details)} sprayparks)")
    if missing_spray:
        print(f"No page details found for: {', '.join(missing_spray)}", file=sys.stderr)


if __name__ == "__main__":
    main()
