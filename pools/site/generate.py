"""
Turns data/pools.json into a self-contained mobile-first HTML page.

Like the wading-pools generator, "today" status is computed here at
generation time in Seattle local time (not client-side), since this page is
meant to be regenerated daily by a scheduled job.

This does NOT try to compute a precise "open right now" minute-by-minute
status - across 10 pools the underlying tables mix time ranges, day-of-week
ranges, and asterisked exceptions in ways that would be fragile to parse
confidently. Instead it computes the one thing that's safe to derive
generically: whether today falls inside the currently-published schedule's
date range (and whether today matches one of the pool's published holiday
dates), and shows the full schedule table for the reader to read the actual
times off of - same "don't overclaim precision" posture as flagging
low_confidence in the wading-pools generator.

Outdoor and indoor pools get different status wording even though the
underlying date-range math is identical: an outdoor pool literally closes
for the winter, so "season ended" means the facility itself is shut. An
indoor pool stays open year-round - what "ends" is just the specific
schedule table posted for the current session, so the wording there is
"schedule may be outdated" rather than implying the building closed.
"""
import json
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data" / "pools.json"
IMAGES_PATH = ROOT / "data" / "pool-images.json"
OUT_PATH = ROOT / "site" / "index.html"
DOCS_OUT = ROOT.parent / "docs" / "pools" / "index.html"

SEATTLE_TZ = ZoneInfo("America/Los_Angeles")
MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def month_num(word: str):
    w = word.strip(".").strip().lower()
    for key in (w, w[:4], w[:3]):
        if key in MONTHS:
            return MONTHS[key]
    return None


def parse_date_range(text: str, fallback_year: int):
    """'June 13 - Aug. 30, 2026' / 'June 22 - September 6.' -> (date, date) or None."""
    if not text:
        return None
    m = re.search(
        r"([A-Za-z]{3,9}\.?)\s+(\d{1,2})\s*(?:,\s*(\d{4}))?\s*(?:-|to|–)\s*"
        r"([A-Za-z]{3,9}\.?)\s+(\d{1,2})\s*(?:,\s*(\d{4}))?",
        text,
    )
    if not m:
        return None
    m1, d1, y1, m2, d2, y2 = m.groups()
    start_month, end_month = month_num(m1), month_num(m2)
    if not start_month or not end_month:
        return None
    year = int(y2 or y1 or fallback_year)
    try:
        return (date(year, start_month, int(d1)), date(year, end_month, int(d2)))
    except ValueError:
        return None


def parse_single_date(text: str, fallback_year: int):
    """'Monday, May 25' / 'Saturday, July 4' -> date or None."""
    if not text:
        return None
    m = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:,\s*(\d{4}))?", text)
    if not m:
        return None
    month = month_num(m.group(1))
    if not month:
        return None
    year = int(m.group(3) or fallback_year)
    try:
        return date(year, month, int(m.group(2)))
    except ValueError:
        return None


def holiday_matches_today(holiday_list, today: date, fallback_year: int):
    for line in holiday_list:
        d = parse_single_date(line, fallback_year)
        if d == today:
            return line
    return None


def build_pool_view(pool: dict, today: date, fallback_year: int):
    if pool.get("closure"):
        return {**pool, "status": "facility_closed",
                "season_start": None, "season_end": None,
                "in_season": False, "season_upcoming": False, "season_ended": False,
                "todays_holiday": None}

    season = parse_date_range(pool.get("season_label") or "", fallback_year)
    in_season = season is not None and season[0] <= today <= season[1]
    upcoming = season is not None and today < season[0]
    ended = season is not None and today > season[1]
    todays_holiday = holiday_matches_today(pool.get("holiday_list") or [], today, fallback_year)

    if season is None:
        status = "unknown"
    elif in_season:
        status = "open" if not todays_holiday else "holiday"
    elif upcoming:
        status = "upcoming"
    else:
        status = "ended"

    return {
        **pool,
        "season_start": season[0].strftime("%b %-d") if season else None,
        "season_end": season[1].strftime("%b %-d") if season else None,
        "in_season": in_season,
        "season_upcoming": upcoming,
        "season_ended": ended,
        "status": status,
        "todays_holiday": todays_holiday,
    }


def merge_images(data: dict) -> dict:
    """Pool header images are cached separately (fetch_images.py, run by
    hand every so often) rather than re-fetched on every daily scrape."""
    if IMAGES_PATH.exists():
        images = json.loads(IMAGES_PATH.read_text(encoding="utf-8"))
        for pool in data["pools"]:
            pool["image_url"] = images.get(pool["slug"])
    return data


def render(data: dict) -> str:
    now = datetime.now(SEATTLE_TZ)
    today = now.date()
    fallback_year = data.get("schedule_year") or today.year

    data = merge_images(data)
    pools = [build_pool_view(p, today, fallback_year) for p in data["pools"]]
    outdoor = [p for p in pools if p["kind"] == "outdoor"]
    indoor = [p for p in pools if p["kind"] == "indoor"]

    parseable = [p for p in pools if p["status"] != "facility_closed"]
    parsed_ok = sum(1 for p in parseable if p.get("schedule") or p.get("schedule_pdfs"))

    view = {
        "generated_at": now.strftime("%A, %B %-d at %-I:%M %p") + " Pacific",
        "date_label": today.strftime("%A, %B %-d"),
        "hub_url": data["hub_url"],
        "outdoor_pools": outdoor,
        "indoor_pools": indoor,
        "low_confidence": parsed_ok < len(parseable),
    }

    template = (Path(__file__).parent / "template.html").read_text(encoding="utf-8")
    return template.replace("__POOL_DATA__", json.dumps(view))


def main():
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    html = render(data)
    OUT_PATH.write_text(html, encoding="utf-8")
    DOCS_OUT.parent.mkdir(parents=True, exist_ok=True)
    DOCS_OUT.write_text(html, encoding="utf-8")
    print(f"Wrote {OUT_PATH} and {DOCS_OUT}")


if __name__ == "__main__":
    main()
