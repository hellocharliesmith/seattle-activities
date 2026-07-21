"""
Turns each pool's raw schedule table (shape varies a lot - see below) into a
flat list of structured "sessions": one per row, with parsed weekdays,
start/end time in minutes-since-midnight, and a program category for
color-coding and filtering.

Schedule table shapes actually seen in pools.json:
  - 4-col: [Program, Day, Time, Details]           (Ballard, Evans, Helene
    Madison, Medgar Evers, Mounger, Meadowbrook - "Note" instead of "Details")
  - 3-col: [Program, Day, Time]                     (Southwest)
  - 2-col: [TimeRange, Description]                 (Colman - program AND
    day-of-week are embedded in free text, e.g. "Lap Swim (daily) and
    weekend Recreation Swim (Fri-Sun only)")

This is deliberately permissive rather than strict: a row that doesn't parse
cleanly still becomes a session (unparsed fields as None) so it stays
searchable, rather than getting silently dropped. Same "don't overclaim
precision" posture as the rest of this project - the calendar/filter UI
should degrade gracefully, not hide data it isn't sure about.
"""
import re

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

_DAY_TOKEN = re.compile(
    r"\b(mon|tue|tues|wed|wednesday|thu|thur|thurs|thursday|fri|sat|sun)[a-z]*\b",
    re.I,
)
_DAY_CANON = {
    "mon": "Mon", "tue": "Tue", "tues": "Tue", "wed": "Wed", "wednesday": "Wed",
    "thu": "Thu", "thur": "Thu", "thurs": "Thu", "thursday": "Thu",
    "fri": "Fri", "sat": "Sat", "sun": "Sun",
}
_TIME_RANGE = re.compile(
    r"(\d{1,2})(?::(\d{2}))?\s*[-–—]\s*(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)",
    re.I,
)
_TIME_RANGE_BOTH_AMPM = re.compile(
    r"(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)\s*[-–—]\s*(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)",
    re.I,
)

CATEGORY_RULES = [
    ("Family Swim", re.compile(r"family swim", re.I)),
    ("Recreation Swim", re.compile(r"rec(?:reation)? swim", re.I)),
    ("Lap Swim", re.compile(r"lap swim|lap lanes?\b", re.I)),
    ("Adult/Senior Swim", re.compile(r"adult.{0,10}swim|senior.{0,10}swim", re.I)),
    ("Water Exercise", re.compile(r"water (fitness|exercise|walk)|aqua ?(jog|fit)|deep water|shallow water", re.I)),
    ("Swim Lessons", re.compile(r"swim lesson|lessons?\b", re.I)),
]


def parse_days(text: str):
    """'Mon/Wed/Fri', 'Mon-Fri', 'Sat-Sun', 'Daily', 'Mon & Wed' -> ['Mon','Wed',...] or None if unparseable."""
    if not text:
        return None
    if re.search(r"\bdaily\b|\bevery ?day\b", text, re.I):
        return list(WEEKDAYS)

    tokens = []
    for m in _DAY_TOKEN.finditer(text):
        word = m.group(1).lower()
        canon = _DAY_CANON.get(word)
        if canon:
            tokens.append(canon)
    if not tokens:
        return None

    # "Mon-Fri" / "Sat-Sun" style range: exactly two tokens joined by a bare hyphen (not "/" or "&")
    range_m = re.search(
        r"\b(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*\s*-\s*(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*\b",
        text, re.I,
    )
    if range_m and len(tokens) == 2:
        start = _DAY_CANON[range_m.group(1).lower()]
        end = _DAY_CANON[range_m.group(2).lower()]
        si, ei = WEEKDAYS.index(start), WEEKDAYS.index(end)
        if si <= ei:
            return WEEKDAYS[si:ei + 1]
        return WEEKDAYS[si:] + WEEKDAYS[:ei + 1]

    # de-dupe, keep week order
    return [d for d in WEEKDAYS if d in tokens]


def parse_time_range(text: str):
    """'6am-7:30am' / '10:30 – 11:30 am' / '12:00pm - 1:30pm' -> (start_min, end_min, label) or (None, None, None)."""
    if not text:
        return None, None, None
    m = _TIME_RANGE_BOTH_AMPM.search(text) or _TIME_RANGE.search(text)
    if not m:
        return None, None, None
    if len(m.groups()) == 6:
        h1, mi1, ap1, h2, mi2, ap2 = m.groups()
    else:
        h1, mi1, h2, mi2, ap2 = m.groups()
        ap1 = ap2  # single trailing am/pm applies to both, e.g. "6-7:30am"
    start = _to_minutes(h1, mi1, ap1)
    end = _to_minutes(h2, mi2, ap2)
    label = m.group(0).strip()
    return start, end, label


def _to_minutes(hour, minute, ampm):
    h = int(hour) % 12
    if ampm and ampm.lower().startswith("p"):
        h += 12
    return h * 60 + int(minute or 0)


def _match(text: str):
    return [name for name, pattern in CATEGORY_RULES if pattern.search(text)]


def classify(program: str, details: str = ""):
    """Returns (category, matched_count) - matched_count > 1 means the text
    plausibly describes more than one kind of session (e.g. Colman's "Lap
    Swim and Recreation Swim" in one slot), which the UI shows as "Multiple".

    Classifies on the program name first; only consults the details text if
    the program alone is ambiguous or blank. Otherwise a detail like "Three
    lap lanes" on an Adult/Senior Swim session would wrongly pull in "Lap
    Swim" as a second hit and mislabel the whole thing "Multiple"."""
    hits = _match(program or "")
    if len(hits) == 1:
        return hits[0], 1
    if len(hits) > 1:
        return "Multiple", len(hits)

    hits = _match(((program or "") + " " + (details or "")).strip())
    if not hits:
        return "Other", 0
    if len(hits) > 1:
        return "Multiple", len(hits)
    return hits[0], 1


def pool_sessions(pool: dict):
    """Flattens one pool's schedule table into a list of session dicts."""
    sched = pool.get("schedule")
    if not sched or not sched.get("rows"):
        return []

    cols = [c.lower() for c in (sched.get("columns") or [])]
    sessions = []
    for row in sched["rows"]:
        if cols and "program" in cols:
            idx = {name: i for i, name in enumerate(cols)}
            program = row[idx.get("program", 0)] if idx.get("program", 0) < len(row) else ""
            day_text = row[idx["day"]] if "day" in idx and idx["day"] < len(row) else ""
            time_text = row[idx["time"]] if "time" in idx and idx["time"] < len(row) else ""
            details_idx = idx.get("details", idx.get("note"))
            details = row[details_idx] if details_idx is not None and details_idx < len(row) else ""
            if not (day_text or "").strip() and not (time_text or "").strip():
                continue  # scraper artifact row - no day/time cell, nothing to show
        else:
            # 2-col Colman/Mounger-without-columns style: [TimeRange, Description]
            time_text = row[0] if len(row) > 0 else ""
            program = row[1] if len(row) > 1 else ""
            day_text = row[1] if len(row) > 1 else ""  # day hints are embedded in the description text too
            details = ""

        program_clean = re.sub(r"\*+$", "", (program or "").strip()).strip()
        category, multi = classify(program_clean, details)
        start_min, end_min, time_label = parse_time_range(time_text)
        days = parse_days(day_text)

        sessions.append({
            "pool_slug": pool["slug"],
            "pool_name": pool["name"],
            "pool_kind": pool["kind"],
            "program": program_clean or "Session",
            "category": category,
            "days": days,  # None = couldn't parse -> treat as "every day" for filtering
            "start_min": start_min,
            "end_min": end_min,
            "time_label": time_label or (time_text or "").strip(),
            "details": (details or "").strip(),
            "day_label": (day_text or "").strip(),
        })
    return sessions


def all_sessions(pools: list):
    out = []
    for p in pools:
        out.extend(pool_sessions(p))
    return out
