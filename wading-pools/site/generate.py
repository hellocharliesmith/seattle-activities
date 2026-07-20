"""
Turns data/wading-pools.json into a self-contained mobile-first HTML page.

"Today" status (open/closed/weather-override) is computed here, at generation
time, in Seattle local time - not client-side - because the page is meant to
be regenerated daily by a scheduled job. If that job stops running, the
"last checked" stamp baked into the footer is the tell.
"""
import json
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data" / "wading-pools.json"
OUT_PATH = ROOT / "site" / "index.html"
DOCS_OUT = ROOT.parent / "docs" / "wading-pools" / "index.html"

SEATTLE_TZ = ZoneInfo("America/Los_Angeles")
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jun.": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def month_num(word: str):
    w = word.strip(".").strip().lower()
    for key in (w, w[:4], w[:3]):
        if key in MONTHS:
            return MONTHS[key]
    return None


def parse_date_range(text: str, fallback_year: int):
    """'July 1 - Aug. 20' / 'June 27 - Sept. 7' / 'May 23 to September 7, 2026' -> (date, date) or None."""
    if not text:
        return None
    text = re.sub(r"\bfull season\b", "", text, flags=re.I).strip()
    m = re.search(
        r"([A-Za-z]{3,9}\.?)\s+(\d{1,2})\s*(?:,\s*(\d{4}))?\s*(?:-|to)\s*"
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


def split_names(names_part: str):
    names_part = re.sub(r"\s+and\s+", ", ", names_part)
    return [n.strip(" .") for n in names_part.split(",") if n.strip(" .")]


def parse_exceptions(exceptions: list[str]):
    """['Delridge and Hiawatha: 12pm-5:30pm', ...] -> [{'names': [...], 'hours': '...'}]"""
    parsed = []
    for line in exceptions:
        if ":" not in line:
            continue
        names_part, hours_part = line.split(":", 1)
        parsed.append({"names": split_names(names_part), "hours": hours_part.strip(" .")})
    return parsed


def notice_matches_today(notice: dict, today: date) -> bool:
    if not notice or not notice.get("heading"):
        return False
    m = re.search(r"([A-Za-z]+)\s+(\d{1,2})", notice["heading"])
    if not m:
        return False
    month = month_num(m.group(1))
    return month == today.month and int(m.group(2)) == today.day


def build_today(data: dict, today: date, weekday: str):
    notice = data.get("today_notice")
    notice_active = notice_matches_today(notice, today)
    notice_closes = notice_active and bool(re.search(r"\bclos", notice.get("intro") or "", re.I))
    exceptions = parse_exceptions(notice["exceptions"]) if notice_active else []

    def match_exception(pool_name: str):
        for exc in exceptions:
            for token in exc["names"]:
                if pool_name.lower().startswith(token.lower()) or token.lower() in pool_name.lower():
                    return exc
        return None

    pools_today = []
    for pool in data["wading_pools"]:
        season = parse_date_range(pool["season"], data["schedule_year"])
        in_season = season is None or (season[0] <= today <= season[1])
        scheduled_hours = pool["schedule"].get(weekday)
        exc = match_exception(pool["name"])

        status, hours, note = "closed", None, None
        if in_season and scheduled_hours:
            status, hours = "open", scheduled_hours
        if exc:
            if notice_closes:
                status, hours = "closed", None
                note = "Closed today - weather"
            else:
                status, hours = "open", exc["hours"]
                note = "Open today despite forecast" if not scheduled_hours else "Confirmed open today"

        pools_today.append({
            "name": pool["name"], "info_url": pool.get("info_url"),
            "address": pool["address"], "map_url": pool["map_url"], "schedule": pool["schedule"],
            "status": status, "hours": hours, "note": note, "in_season": in_season,
        })

    spray_season = parse_date_range(data["sprayparks"]["season"], data["schedule_year"])
    sprayparks_open_today = spray_season is not None and spray_season[0] <= today <= spray_season[1]

    return {
        "date_label": today.strftime("%A, %B %-d"),
        "weekday": weekday,
        "notice_active": notice_active,
        "notice": notice if notice_active else None,
        "pools": pools_today,
        "sprayparks_open_today": sprayparks_open_today,
    }


def annotate_full_schedule(data: dict, today: date):
    out = []
    for pool in data["wading_pools"]:
        season = parse_date_range(pool["season"], data["schedule_year"])
        out.append({
            **pool,
            "season_active": season is None or (season[0] <= today <= season[1]),
            "season_upcoming": season is not None and today < season[0],
            "season_ended": season is not None and today > season[1],
        })
    return out


def render(data: dict) -> str:
    now = datetime.now(SEATTLE_TZ)
    today = now.date()
    weekday = WEEKDAYS[today.weekday()]

    view = {
        "generated_at": now.strftime("%A, %B %-d at %-I:%M %p") + " Pacific",
        "source_url": data["source_url"],
        "hotline": data.get("hotline"),
        "facebook_url": data.get("facebook_url"),
        "policy": data.get("policy"),
        "today": build_today(data, today, weekday),
        "wading_pools": annotate_full_schedule(data, today),
        "sprayparks": data["sprayparks"],
        "weekdays": WEEKDAYS,
        "low_confidence": len(data["wading_pools"]) < 10 or len(data["sprayparks"]["locations"]) < 5,
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
