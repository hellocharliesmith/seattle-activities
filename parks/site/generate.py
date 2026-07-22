"""
Turns data/parks.json into a self-contained mobile-first HTML page - one
flat, client-side-filterable card list of Seattle parks (no per-day/
per-hour "today" status to compute here, unlike pools/wading-pools; a
park's amenities don't change day to day, so this generator's job is just
to shape data/parks.json into the page's embedded JSON and stamp a
"checked" timestamp, same as the other two sites do for their own data).

Filtering (by neighborhood bucket and by amenity) happens entirely in the
browser over the embedded JSON blob - see template.html's makeMultiSelect.

low_confidence here means the last scrape run couldn't reach every amenity
layer it expects (scraper writes an "amenity_layer_errors" list when a
layer 404s or its schema changes) - shown as a banner so a reader knows
the amenity badges might be undercounting rather than wrong.
"""
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data" / "parks.json"
IMAGES_PATH = ROOT / "data" / "park-images.json"
OUT_PATH = ROOT / "site" / "index.html"
DOCS_OUT = ROOT.parent / "docs" / "parks" / "index.html"

SEATTLE_TZ = ZoneInfo("America/Los_Angeles")

NEIGHBORHOODS = [
    {"slug": "ballard-fremont-greenwood", "label": "Ballard / Fremont / Greenwood"},
    {"slug": "wallingford-green-lake", "label": "Wallingford / Green Lake"},
    {"slug": "north-seattle", "label": "North Seattle"},
    {"slug": "northeast-seattle", "label": "Northeast Seattle"},
    {"slug": "queen-anne-magnolia", "label": "Queen Anne / Magnolia"},
    {"slug": "downtown-slu", "label": "Downtown / South Lake Union"},
    {"slug": "capitol-hill-central-district", "label": "Capitol Hill / Central District"},
    {"slug": "west-seattle", "label": "West Seattle"},
    {"slug": "southeast-seattle", "label": "Southeast Seattle"},
    {"slug": "georgetown-south-park", "label": "Georgetown / South Park"},
    {"slug": "other", "label": "Other Seattle"},
]


def merge_images(parks: list) -> list:
    """Park header images are cached separately (fetch_images.py, run by
    hand every so often) rather than re-fetched on every daily scrape."""
    if IMAGES_PATH.exists():
        images = json.loads(IMAGES_PATH.read_text(encoding="utf-8"))
        for park in parks:
            park["image_url"] = images.get(park["slug"])
    return parks


def render(data: dict) -> str:
    now = datetime.now(SEATTLE_TZ)
    parks = merge_images(data.get("parks", []))

    # Only offer neighborhood filter options that actually have a park in
    # this refresh, so the dropdown doesn't show empty buckets - but always
    # keep them in NEIGHBORHOODS' canonical order.
    present_buckets = {p["neighborhood_bucket"] for p in parks}
    neighborhoods = [n for n in NEIGHBORHOODS if n["slug"] in present_buckets]

    view = {
        "generated_at": now.strftime("%A, %B %-d at %-I:%M %p") + " Pacific",
        "source_url": "https://data-seattlecitygis.opendata.arcgis.com/",
        "low_confidence": bool(data.get("amenity_layer_errors")),
        "neighborhoods": neighborhoods,
        "parks": parks,
    }

    template = (Path(__file__).parent / "template.html").read_text(encoding="utf-8")
    return template.replace("__PARK_DATA__", json.dumps(view))


def main():
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    html = render(data)
    OUT_PATH.write_text(html, encoding="utf-8")
    DOCS_OUT.parent.mkdir(parents=True, exist_ok=True)
    DOCS_OUT.write_text(html, encoding="utf-8")
    print(f"Wrote {OUT_PATH} and {DOCS_OUT} ({len(data.get('parks', []))} parks)")


if __name__ == "__main__":
    main()
