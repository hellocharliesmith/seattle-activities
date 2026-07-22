"""
Turns data/events.json into a self-contained mobile-first HTML page.

Unlike the pools/wading-pools generators, the underlying feed here (see
scraper/story_time.py) already gives genuinely dated calendar occurrences -
"Baby Story Time at Northgate Branch on 2026-07-22, 10:30-11:00am" - rather
than only a recurring weekly pattern. So this follows the shape of pools'
kid-friendly single-day calendar (day-arrow nav computing a date from
today_iso client-side), but simplified to a flat, filterable list per day
instead of a time-grid: with 21 branches and up to a few dozen sessions on a
busy day, a flat sorted list reads better on mobile than a lane-packed
calendar grid would, and it matches the "prefer flat lists over grouped
sections" feedback already applied to the pools page.

"Today" and the day-nav bounds are computed here at generation time in
Seattle local time, since this page is meant to be regenerated daily by a
scheduled job.
"""
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data" / "events.json"
OUT_PATH = ROOT / "site" / "index.html"
DOCS_OUT = ROOT.parent / "docs" / "story-time" / "index.html"

SEATTLE_TZ = ZoneInfo("America/Los_Angeles")

# Same exact bucket list/order as the scraper - kept here too (rather than
# just reading whatever's present in the data) so the neighborhood dropdown
# has a stable, predictable order even if a bucket has zero events on a
# given refresh.
BUCKET_ORDER = [
    ("ballard-fremont-greenwood", "Ballard / Fremont / Greenwood"),
    ("wallingford-green-lake", "Wallingford / Green Lake"),
    ("north-seattle", "North Seattle"),
    ("northeast-seattle", "Northeast Seattle"),
    ("queen-anne-magnolia", "Queen Anne / Magnolia"),
    ("downtown-slu", "Downtown / South Lake Union"),
    ("capitol-hill-central-district", "Capitol Hill / Central District"),
    ("west-seattle", "West Seattle"),
    ("southeast-seattle", "Southeast Seattle"),
    ("georgetown-south-park", "Georgetown / South Park"),
    ("other", "Other Seattle"),
]
AGE_GROUP_ORDER = [
    ("baby", "Baby"),
    ("toddler", "Toddler"),
    ("preschool", "Preschool"),
    ("family", "Family / All ages"),
    ("other", "Other"),
]


def render(data: dict) -> str:
    now = datetime.now(SEATTLE_TZ)
    today = now.date()

    events = data.get("events") or []
    present_buckets = {e["neighborhood_bucket"] for e in events}
    present_ages = {e["age_group"] for e in events}

    date_range = data.get("date_range") or {}
    range_start = date.fromisoformat(date_range["start"]) if date_range.get("start") else today
    range_end = date.fromisoformat(date_range["end"]) if date_range.get("end") else today
    # Day-nav bounds: never go earlier than today (this is a "what's coming
    # up" finder, not an archive) and never later than the last date the
    # feed actually covers.
    min_offset = min(0, (range_start - today).days)
    max_offset = max(0, (range_end - today).days)

    view = {
        "generated_at": now.strftime("%A, %B %-d at %-I:%M %p") + " Pacific",
        "today_iso": today.isoformat(),
        "min_day_offset": min_offset,
        "max_day_offset": max_offset,
        "calendar_page_url": data.get("calendar_page_url"),
        "source_url": data.get("source_url"),
        "events": events,
        "neighborhoods": [{"value": slug, "label": label} for slug, label in BUCKET_ORDER if slug in present_buckets],
        "age_groups": [{"value": val, "label": label} for val, label in AGE_GROUP_ORDER if val in present_ages],
        "low_confidence": len(events) < 20,
    }

    template = (Path(__file__).parent / "template.html").read_text(encoding="utf-8")
    return template.replace("__STORY_DATA__", json.dumps(view))


def main():
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    html = render(data)
    OUT_PATH.write_text(html, encoding="utf-8")
    DOCS_OUT.parent.mkdir(parents=True, exist_ok=True)
    DOCS_OUT.write_text(html, encoding="utf-8")
    print(f"Wrote {OUT_PATH} and {DOCS_OUT}")


if __name__ == "__main__":
    main()
