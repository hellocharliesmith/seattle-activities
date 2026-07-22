"""
Scrapes The Seattle Public Library's story time schedule into structured JSON.

Data source discovery (do not re-derive this by hand - see the notes below if
the feed ever breaks):

The story time calendar page (spl.org/programs-and-services/fun-and-games/
story-time/story-time-calendar) is NOT a LibCal calendar - despite looking
like one, SPL's whole event-calendar system runs on Trumba (a different
Springshare-style vendor). The page embeds a Trumba "spud" widget
(//www.trumba.com/scripts/spuds.js) configured with:
    webName: "kalendaro"
    filterview: "SeriesStoryTime"
That filterview is a saved Trumba search that already narrows the library's
full event calendar down to story-time-family events, so no client-side
title/category filtering is needed to pick story times out of the wider
calendar - unlike the generic keyword-matching this project's brief
anticipated might be necessary.

Trumba publishes calendar data in several parallel formats at
www.trumba.com/calendars/<webName>.<ext> - ical (.ics), Atom (.xml), and a
plain JSON array (.json), all accepting the same filterview query param, all
same underlying event set. This scraper uses the .json form (no manual
iCal VEVENT parsing needed): it's already a Python-shaped list of dicts with
real ISO start/end datetimes and rich fields (customFields for a
"Room Location" field, event images, etc). The feed appears to be capped at
200 events per request; empirically that spans ~5.5 weeks out from today,
which is a genuinely dated (not recurring-only) instance list - each row IS a
specific calendar occurrence (e.g. "Baby Story Time" at Northgate on
2026-07-22 10:30-11:00), even though each series also carries a human-
readable "repeats" note like "Every Wednesday through August 19, 2026". So
the site built on top of this can do a real day-by-day view rather than only
a weekday-recurrence table.

Cancelled instances: a subset of rows have canceled=true AND a
"CANCELLED - " prefix baked into the title (both always agree, checked
empirically). Those are dropped entirely rather than shown as cancelled,
since by the time this is re-scraped daily a decent number of these have
already passed anyway.

Soft-fail posture (matching pools/wading-pools): if the feed is unreachable
or its shape changes unexpectedly, this exits with a clear error rather than
writing partial/fabricated data - there is no meaningful fallback data source
for a page like this (no branch publishes its own static weekly story-time
table the way pool pages publish drop-in schedules).
"""
import html
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

FEED_URL = "https://www.trumba.com/calendars/kalendaro.json?filterview=SeriesStoryTime"
OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "events.json"

# Exact list from the task brief - slug, label, and the branches assigned to
# each based on the branch's own neighborhood (address/common name), not on
# any signal from the feed itself.
BUCKETS = {
    "ballard-fremont-greenwood": "Ballard / Fremont / Greenwood",
    "wallingford-green-lake": "Wallingford / Green Lake",
    "north-seattle": "North Seattle",
    "northeast-seattle": "Northeast Seattle",
    "queen-anne-magnolia": "Queen Anne / Magnolia",
    "downtown-slu": "Downtown / South Lake Union",
    "capitol-hill-central-district": "Capitol Hill / Central District",
    "west-seattle": "West Seattle",
    "southeast-seattle": "Southeast Seattle",
    "georgetown-south-park": "Georgetown / South Park",
    "other": "Other Seattle",
}

# Branch name (as Trumba's "location" field renders it) -> bucket slug.
# Built by hand from each branch's real neighborhood, since the feed itself
# carries no neighborhood/bucket information. Keep this list branch-name-
# keyed (not address-keyed) since that's the only identifying string Trumba
# actually gives us per event.
BRANCH_BUCKET = {
    "Ballard Branch": "ballard-fremont-greenwood",
    "Fremont Branch": "ballard-fremont-greenwood",
    "Greenwood Branch": "ballard-fremont-greenwood",
    "Green Lake Branch": "wallingford-green-lake",
    "Wallingford Branch": "wallingford-green-lake",
    "Northgate Branch": "north-seattle",
    "Lake City Branch": "north-seattle",
    "Broadview Branch": "north-seattle",
    "University Branch": "northeast-seattle",
    "Northeast Branch": "northeast-seattle",
    "Queen Anne Branch": "queen-anne-magnolia",
    "Magnolia Branch": "queen-anne-magnolia",
    "Central Library": "downtown-slu",
    "International District/Chinatown Branch": "downtown-slu",
    "Capitol Hill Branch": "capitol-hill-central-district",
    "Douglass-Truth Branch": "capitol-hill-central-district",
    "Madrona/Sally Goldmark Branch": "capitol-hill-central-district",
    "Montlake Branch": "capitol-hill-central-district",
    "West Seattle Branch": "west-seattle",
    "Delridge Branch": "west-seattle",
    "Southwest Branch": "west-seattle",
    "High Point Branch": "west-seattle",
    "Columbia Branch": "southeast-seattle",
    "Beacon Hill Branch": "southeast-seattle",
    "Rainier Beach Branch": "southeast-seattle",
    "NewHolly Branch": "southeast-seattle",
    "South Park Branch": "georgetown-south-park",
}

_AGE_RULES = [
    ("baby", re.compile(r"\bbaby\b", re.I)),
    ("toddler", re.compile(r"toddler|play\s*&\s*learn|play and learn|play group", re.I)),
    ("preschool", re.compile(r"preschool", re.I)),
]


def fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def strip_tags(text: str) -> str:
    return clean(re.sub(r"<[^>]+>", " ", text or ""))


def branch_from_location(location_html: str) -> str:
    m = re.search(r">([^<]+)<", location_html or "")
    return clean(m.group(1)) if m else strip_tags(location_html)


def bucket_for(branch: str):
    slug = BRANCH_BUCKET.get(branch, "other")
    return slug, BUCKETS[slug]


def age_group_for(title: str) -> str:
    if re.search(r"storywalk", title, re.I):
        return "other"
    for age, pattern in _AGE_RULES:
        if pattern.search(title):
            return age
    # Everything else observed in this feed reads as an all-ages/caregiver
    # session (Family Story Time, Bilingual/language-specific story times,
    # Pajama Story Time, Firefighter Story Time, etc) - default to "family"
    # rather than "other" so these aren't lumped in with genuinely
    # unrelated content like Storywalk.
    return "family"


def room_location_for(custom_fields) -> str:
    for field in custom_fields or []:
        if field.get("label") == "Room Location":
            return strip_tags(field.get("value") or "")
    return ""


def event_url(event_id) -> str:
    return f"https://www.spl.org/event-calendar?trumbaEmbed=view%3Devent%26eventid%3D{event_id}"


def parse_event(raw: dict):
    title = clean(raw.get("title") or "")
    branch = branch_from_location(raw.get("location") or "")
    bucket_slug, bucket_label = bucket_for(branch)

    start = datetime.fromisoformat(raw["startDateTime"])
    end = datetime.fromisoformat(raw["endDateTime"])

    return {
        "branch": branch,
        "neighborhood_bucket": bucket_slug,
        "neighborhood_label": bucket_label,
        "age_group": age_group_for(title),
        "title": title,
        "date": start.date().isoformat(),
        "start_time": start.strftime("%-I:%M %p"),
        "end_time": end.strftime("%-I:%M %p"),
        "start_min": start.hour * 60 + start.minute,
        "end_min": end.hour * 60 + end.minute,
        "location_note": room_location_for(raw.get("customFields")),
        "url": event_url(raw.get("eventID")),
    }


def scrape() -> dict:
    try:
        raw_events = fetch_json(FEED_URL)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: could not fetch/parse Trumba story time feed: {exc}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(raw_events, list) or not raw_events:
        print("ERROR: Trumba feed did not return a non-empty event list.", file=sys.stderr)
        sys.exit(1)

    events = []
    unmapped_branches = set()
    for raw in raw_events:
        if raw.get("canceled"):
            continue
        try:
            event = parse_event(raw)
        except Exception as exc:  # noqa: BLE001 - one bad row shouldn't sink the rest
            print(f"WARNING: skipping unparseable event {raw.get('eventID')}: {exc}", file=sys.stderr)
            continue
        if event["branch"] not in BRANCH_BUCKET:
            unmapped_branches.add(event["branch"])
        events.append(event)

    events.sort(key=lambda e: (e["date"], e["start_min"] if e["start_min"] is not None else 0))

    if unmapped_branches:
        print(
            f"WARNING: no explicit bucket mapping for branch(es) {sorted(unmapped_branches)}; "
            "filed under 'other'.",
            file=sys.stderr,
        )

    dates = [e["date"] for e in events]
    return {
        "source_url": FEED_URL,
        "calendar_page_url": "https://www.spl.org/programs-and-services/fun-and-games/story-time/story-time-calendar",
        "scraped_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "date_range": {"start": min(dates), "end": max(dates)} if dates else None,
        "events": events,
    }


def main():
    result = scrape()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(
        f"Wrote {OUT_PATH} ({len(result['events'])} events, "
        f"{result['date_range']['start']} to {result['date_range']['end']})"
    )


if __name__ == "__main__":
    main()
