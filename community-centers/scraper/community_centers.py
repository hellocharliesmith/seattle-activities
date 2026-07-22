"""
Scrapes all ~26 Seattle Parks community centers into structured JSON.

There's no open-data feed for community centers (unlike full pools, which
have an ArcGIS FeatureServer for addresses) - everything here comes from
HTML: the hub page's link list, plus each center's own page, which follows
one consistent hand-built template (name/address/phone/hours widget near
the top, an "Amenities" two-column list further down, and - where a center
runs one - a "Drop-In Programs" schedule table). Some pages have DNS quirks
(the hub page is officially linked as www1.seattle.gov, which doesn't
resolve directly in some network setups; www.seattle.gov, which it
redirects to, always does) - this scraper talks to www.seattle.gov
throughout to sidestep that.

Hours are published as a `<ul>` of `<li>` rows, e.g.
  <li><strong>Mon-Fri:</strong> 8:30am-6:30pm</li>
  <li>Closed Saturdays &amp; Sundays</li>
Day tokens vary (single day, "Mon-Fri" ranges, "Mon, Wed, Fri" lists, "Thur"
vs "Thu", pluralized "Sundays" in the no-<strong> closed-day form) and so do
time formats ("8:30am-6:30pm" vs "11 a.m. - 9 p.m."). This scraper handles
all of those, but if any row in a center's hours block doesn't match a known
shape, it gives up on that ONE center's hours_parsed (sets it to null) while
keeping the raw hours_text intact - same "don't overclaim precision"
posture as the pools/wading-pools scrapers, so a markup surprise degrades a
single center's status to "can't tell" rather than crashing or lying about
open/closed.

Teen program info: seattle.gov names 3 dedicated "Teen Life Centers"
(Garfield, Meadowbrook, Southwest) plus a handful of ordinary community
centers that also run "Teen Late Night" (Fri/Sat 7pm-12am, ages 13-19) -
scraped from the Teen Programs hub page and its Teen Late Night sub-page,
then matched against the 26 community center names by substring so a
future roster change is picked up automatically rather than hardcoded.

Re-run this whenever a refresh is wanted; on any parse failure for a given
center it fills that center in with an "error" field rather than crashing,
so the site can show a partial page instead of failing outright.
"""
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

HUB_URL = "https://www.seattle.gov/parks/community-centers"
TEEN_PROGRAMS_URL = "https://www.seattle.gov/parks/childcare/teen-programs"
TEEN_LATE_NIGHT_URL = "https://www.seattle.gov/parks/childcare/teen-programs/teen-late-night"
OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "community_centers.json"

# Fallback list (slug -> display name), only used if the hub page's own
# markup can't be parsed, so a hub-page redesign degrades gracefully
# instead of producing nothing.
FALLBACK_CENTERS = [
    ("alki-community-center", "Alki Community Center"),
    ("ballard-community-center", "Ballard Community Center"),
    ("bitter-lake-community-center", "Bitter Lake Community Center"),
    ("delridge-community-center", "Delridge Community Center"),
    ("garfield-community-center", "Garfield Community Center"),
    ("green-lake-community-center", "Green Lake Community Center"),
    ("hiawatha-community-center", "Hiawatha Community Center"),
    ("high-point-community-center", "High Point Community Center"),
    ("international-districtchinatown-community-center", "International District/Chinatown Community Center"),
    ("jefferson-community-center", "Jefferson Community Center"),
    ("lake-city-community-center", "Lake City Community Center"),
    ("laurelhurst-community-center", "Laurelhurst Community Center"),
    ("loyal-heights-community-center", "Loyal Heights Community Center"),
    ("magnolia-community-center", "Magnolia Community Center"),
    ("magnuson-community-center", "Magnuson Community Center"),
    ("meadowbrook-community-center", "Meadowbrook Community Center"),
    ("miller-community-center", "Miller Community Center"),
    ("montlake-community-center", "Montlake Community Center"),
    ("northgate-community-center", "Northgate Community Center"),
    ("queen-anne-community-center", "Queen Anne Community Center"),
    ("rainier-beach-community-center", "Rainier Beach Community Center"),
    ("rainier-community-center", "Rainier Community Center"),
    ("ravenna-eckstein-community-center", "Ravenna-Eckstein Community Center"),
    ("south-park-community-center", "South Park Community Center"),
    ("van-asselt-community-center", "Van Asselt Community Center"),
    ("yesler-community-center", "Yesler Community Center"),
]

NEIGHBORHOOD_BUCKETS = [
    {"slug": "ballard-fremont-greenwood", "label": "Ballard / Fremont / Greenwood",
     "keywords": ["ballard", "crown hill", "phinney ridge", "greenwood", "fremont"]},
    {"slug": "wallingford-green-lake", "label": "Wallingford / Green Lake",
     "keywords": ["wallingford", "green lake", "tangletown"]},
    {"slug": "north-seattle", "label": "North Seattle",
     "keywords": ["northgate", "lake city", "bitter lake", "broadview", "haller lake"]},
    {"slug": "northeast-seattle", "label": "Northeast Seattle",
     "keywords": ["university district", "u district", "ravenna", "wedgwood", "view ridge",
                  "sand point", "laurelhurst", "magnuson"]},
    {"slug": "queen-anne-magnolia", "label": "Queen Anne / Magnolia",
     "keywords": ["queen anne", "magnolia", "interbay"]},
    {"slug": "downtown-slu", "label": "Downtown / South Lake Union",
     "keywords": ["downtown", "belltown", "south lake union", "slu", "first hill",
                  "international district", "chinatown", "pioneer square"]},
    {"slug": "capitol-hill-central-district", "label": "Capitol Hill / Central District",
     "keywords": ["capitol hill", "central district", "madison valley", "madrona", "leschi",
                  "montlake"]},
    {"slug": "west-seattle", "label": "West Seattle",
     "keywords": ["alki", "admiral", "genesee", "delridge", "high point", "fauntleroy"]},
    {"slug": "southeast-seattle", "label": "Southeast Seattle",
     "keywords": ["columbia city", "rainier valley", "rainier ", "beacon hill", "rainier beach",
                  "mount baker", "hiawatha"]},
    {"slug": "georgetown-south-park", "label": "Georgetown / South Park",
     "keywords": ["georgetown", "south park"]},
]

DAY_ALIASES = {
    "mon": "Mon", "monday": "Mon", "mondays": "Mon",
    "tue": "Tue", "tues": "Tue", "tuesday": "Tue", "tuesdays": "Tue",
    "wed": "Wed", "weds": "Wed", "wednesday": "Wed", "wednesdays": "Wed",
    "thu": "Thu", "thur": "Thu", "thurs": "Thu", "thursday": "Thu", "thursdays": "Thu",
    "fri": "Fri", "friday": "Fri", "fridays": "Fri",
    "sat": "Sat", "saturday": "Sat", "saturdays": "Sat",
    "sun": "Sun", "sunday": "Sun", "sundays": "Sun",
}
WEEK_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def fetch_html(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip(" \n\t")


def map_url_for(address: str) -> str:
    return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(address)


def fetch_center_index():
    """Scrape the hub page's community-center list -> [(slug, name), ...].

    Falls back to FALLBACK_CENTERS (with a stderr WARNING) if the hub page's
    markup doesn't yield a plausible list.
    """
    try:
        html = fetch_html(HUB_URL)
        soup = BeautifulSoup(html, "html.parser")
        main = soup.select_one("#mainColMain") or soup.select_one("main") or soup.body
        seen = {}
        for a in main.find_all("a", href=True):
            href = a["href"]
            m = re.search(r"(?:^|/)parks/community-centers/([a-z0-9-]+)/?$", href)
            if not m:
                continue
            slug = m.group(1)
            text = clean(a.get_text())
            if not text or slug in seen:
                continue
            seen[slug] = text
        centers = list(seen.items())
        if len(centers) < 15:
            raise ValueError(f"only found {len(centers)} community center links")
        return centers
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: could not parse center index from hub page ({exc}); using fallback list.", file=sys.stderr)
        return FALLBACK_CENTERS


# A handful of centers sit in neighborhoods whose name never actually
# appears in their own address/name text (e.g. Garfield CC's address is on
# "E Cherry St", not "Central District") - these are well-known enough
# Seattle landmarks that a small manual override beats stretching the
# keyword-matching heuristic to catch them.
BUCKET_OVERRIDES = {
    "garfield": "capitol-hill-central-district",     # E Cherry St - Central District
    "jefferson": "southeast-seattle",                # Beacon Ave S - Beacon Hill
    "loyal heights": "ballard-fremont-greenwood",     # NW 77th St - Loyal Heights, part of the Ballard cluster
    "meadowbrook": "northeast-seattle",               # 35th Ave NE - Meadowbrook/Wedgwood
    "miller": "capitol-hill-central-district",        # 19th Ave E - Capitol Hill
    "van asselt": "southeast-seattle",                # S Myrtle St - South Beacon Hill
    "yesler": "capitol-hill-central-district",        # E Yesler Way - Central District/First Hill
}


def assign_bucket(name: str, address: str):
    core = re.sub(r"\s*community center\s*$", "", name, flags=re.I).strip().lower()
    if core in BUCKET_OVERRIDES:
        slug = BUCKET_OVERRIDES[core]
        label = next(b["label"] for b in NEIGHBORHOOD_BUCKETS if b["slug"] == slug)
        return slug, label
    haystack = f"{name} {address or ''}".lower()
    for bucket in NEIGHBORHOOD_BUCKETS:
        if any(kw in haystack for kw in bucket["keywords"]):
            return bucket["slug"], bucket["label"]
    return "other", "Other Seattle"


def expand_days(token: str):
    token = clean(token).strip(" :.*")
    if not token:
        return []
    # Range like "Mon-Fri" / "Mon - Thur" (guard against this accidentally
    # matching a time range by requiring both sides to be day words).
    range_m = re.match(r"^([A-Za-z]+)\s*-\s*([A-Za-z]+)$", token)
    if range_m:
        a = DAY_ALIASES.get(range_m.group(1).lower())
        b = DAY_ALIASES.get(range_m.group(2).lower())
        if a and b:
            i, j = WEEK_ORDER.index(a), WEEK_ORDER.index(b)
            return WEEK_ORDER[i:j + 1] if i <= j else []
        return []
    # "Sat/Sun" and similar - a plain list, not a range.
    parts = re.split(r",|&|/|\band\b", token)
    days = []
    for p in parts:
        key = clean(p).lower()
        if key in DAY_ALIASES:
            days.append(DAY_ALIASES[key])
    return days


def parse_time_range(text: str):
    # Fix the occasional missing-colon typo ("845am" meant "8:45am",
    # "1230pm" meant "12:30pm") before the real regex runs.
    text = re.sub(
        r"\b(\d{3,4})\s*([apAP])\.?\s*[mM]?\.?\b",
        lambda m: f"{m.group(1)[:-2]}:{m.group(1)[-2:]}{m.group(2)}m",
        text,
    )
    matches = re.findall(r"(\d{1,2})(?::(\d{2}))?\s*([apAP])\.?\s*[mM]?\.?", text)
    if len(matches) < 2:
        return None

    def conv(h, m, ap):
        h = int(h)
        m = int(m) if m else 0
        ap = ap.lower()
        if ap == "p" and h != 12:
            h += 12
        if ap == "a" and h == 12:
            h = 0
        return f"{h:02d}:{m:02d}"

    return conv(*matches[0]), conv(*matches[1])


def extract_hour_rows(block):
    """A center's hours are published either as a <ul><li> list (most
    centers) or, on a handful of pages, as a single <p> with <strong>day</strong>
    text<br/> repeated instead of real list items (e.g. Delridge, Green Lake,
    Laurelhurst). Both shapes are normalized here into [(day_token_or_None,
    full_row_text), ...] so parse_hours_block doesn't need to care which one
    it got."""
    lis = block.find_all("li")
    if lis:
        rows = []
        for li in lis:
            strong = li.find("strong")
            strong_text = clean(strong.get_text()) if strong else None
            full_text = clean(li.get_text(" ", strip=True))
            rows.append((strong_text, full_text))
        return rows

    rows = []
    for p in block.find_all("p"):
        current_strong = None
        parts = []

        def flush():
            text = clean(" ".join(parts))
            if text:
                rows.append((current_strong, text))

        for node in p.contents:
            name = getattr(node, "name", None)
            if name == "br":
                flush()
                current_strong = None
                parts = []
            elif name == "strong":
                s = clean(node.get_text())
                if s and current_strong is None:
                    current_strong = s
                if s:
                    parts.append(s)
            else:
                t = clean(str(node)) if isinstance(node, str) else clean(node.get_text())
                if t:
                    parts.append(t)
        flush()
    return rows


def parse_hours_block(block):
    """<div class="hoursOverrideContent"> -> (hours_text, hours_parsed or None)."""
    if block is None:
        return None, None

    heading = block.find(["h3"])
    season_label = clean(heading.get_text(" ", strip=True)) if heading else None
    rows = extract_hour_rows(block)
    row_texts = [text for _, text in rows]
    hours_text = clean((season_label + ". " if season_label else "") + "; ".join(row_texts))
    if not rows:
        return hours_text or None, None

    parsed = {}
    ok = True
    for strong_text, text in rows:
        if strong_text:
            day_token = strong_text
            rest = text[len(strong_text):].strip() if text.startswith(strong_text) else text.replace(strong_text, "", 1).strip()
        else:
            m = re.match(r"(?i)^closed\s+(.+)$", text)
            if not m:
                ok = False
                continue
            day_token, rest = m.group(1), "Closed"

        days = expand_days(day_token)
        if not days:
            ok = False
            continue

        if re.search(r"(?i)\bclosed\b", rest):
            for d in days:
                parsed[d] = None
        else:
            tr = parse_time_range(rest)
            if tr is None:
                ok = False
                continue
            for d in days:
                parsed[d] = {"open": tr[0], "close": tr[1]}

    if not ok or not parsed:
        return hours_text or None, None

    for d in WEEK_ORDER:
        parsed.setdefault(d, None)
    return hours_text or None, parsed


def parse_center_page(html: str, slug: str, name: str):
    soup = BeautifulSoup(html, "html.parser")
    contact_block = soup.select_one("#parkHoursAndContact")

    address = None
    if contact_block is not None:
        addr_a = contact_block.select_one('a[href*="google.com/maps"]') or contact_block.select_one(".contactAddress a")
        if addr_a:
            address = clean(addr_a.get_text(" ", strip=True))

    phone = None
    if contact_block is not None:
        phone_a = contact_block.select_one(".featureContactPhoneNumber a") or contact_block.select_one("a[href^='tel']")
        if phone_a:
            phone = clean(phone_a.get_text(" ", strip=True))

    hours_block = soup.select_one(".hoursOverrideContent")
    hours_text, hours_parsed = parse_hours_block(hours_block)

    amenities = []
    amen_div = soup.select_one(".amenities")
    if amen_div:
        amenities = [clean(li.get_text(" ", strip=True)) for li in amen_div.select("li") if clean(li.get_text())]
    amenities_lower = {a.lower() for a in amenities}
    has_drop_in_gym = "gym" in amenities_lower and any("drop" in a for a in amenities_lower)

    bucket_slug, bucket_label = assign_bucket(name, address)

    return {
        "slug": slug,
        "name": name,
        "address": address,
        "phone": phone,
        "neighborhood_bucket": bucket_slug,
        "neighborhood_label": bucket_label,
        "hours_text": hours_text,
        "hours_parsed": hours_parsed,
        "amenities": amenities,
        "has_teen_program": False,  # filled in by main() once all centers are scraped
        "has_drop_in_gym": has_drop_in_gym,
        "url": f"https://www.seattle.gov/parks/community-centers/{slug}",
    }


def fetch_teen_late_night_centers():
    """Names of community centers (not the 3 dedicated Teen Life Centers)
    that run Teen Late Night, scraped from the Teen Late Night page's
    "Locations" list. Soft-fails to [] - a missing/changed page just means
    no center gets flagged, rather than crashing the whole scrape."""
    try:
        html = fetch_html(TEEN_LATE_NIGHT_URL)
        soup = BeautifulSoup(html, "html.parser")
        # Each Teen Late Night site is its own card, titled with a
        # ".cardTitle" div (e.g. "Bitter Lake Community Center", "Garfield
        # Teen Life Center") - a cleaner source than the prose paragraphs on
        # this page, which mix in unrelated internship-placement sites.
        names = [clean(d.get_text(" ", strip=True)) for d in soup.select(".cardTitle")]
        names = [n for n in names if n]
        if not names:
            raise ValueError("no .cardTitle elements found")
        return names
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: could not fetch Teen Late Night locations ({exc}).", file=sys.stderr)
        return []


def scrape() -> dict:
    center_index = fetch_center_index()
    teen_location_names = fetch_teen_late_night_centers()

    centers = []
    for slug, name in center_index:
        url = f"https://www.seattle.gov/parks/community-centers/{slug}"
        try:
            html = fetch_html(url)
            center = parse_center_page(html, slug, name)
        except Exception as exc:  # noqa: BLE001 - keep going, flag instead of crash
            print(f"WARNING: failed to parse {name}: {exc}", file=sys.stderr)
            center = {
                "slug": slug, "name": name, "address": None, "phone": None,
                "neighborhood_bucket": "other", "neighborhood_label": "Other Seattle",
                "hours_text": None, "hours_parsed": None, "amenities": [],
                "has_teen_program": False, "has_drop_in_gym": False,
                "url": url, "error": str(exc),
            }
        centers.append(center)

    # Match Teen Late Night location names (e.g. "Bitter Lake Community
    # Center", "Garfield Teen Life Center") against our center names by
    # substring on the shared core token ("Bitter Lake", "Garfield") so this
    # doesn't need a hand-maintained list.
    def core_token(n):
        return re.sub(r"\s*(Community|Teen Life)\s*Center\s*$", "", n, flags=re.I).strip().lower()

    teen_cores = {core_token(n) for n in teen_location_names if core_token(n)}
    for center in centers:
        if core_token(center["name"]) in teen_cores:
            center["has_teen_program"] = True

    parsed_ok = sum(1 for c in centers if c.get("hours_parsed"))
    if parsed_ok < len(centers) * 0.5:
        print(
            f"WARNING: only {parsed_ok}/{len(centers)} centers parsed hours into structured form "
            "- page markup may have changed. Writing anyway for inspection.",
            file=sys.stderr,
        )

    return {
        "hub_url": HUB_URL,
        "teen_programs_url": TEEN_PROGRAMS_URL,
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "centers": centers,
    }


def main():
    result = scrape()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    parsed_ok = sum(1 for c in result["centers"] if c.get("hours_parsed"))
    teen_count = sum(1 for c in result["centers"] if c.get("has_teen_program"))
    print(
        f"Wrote {OUT_PATH} ({len(result['centers'])} centers, "
        f"{parsed_ok} with structured hours, {teen_count} with a teen program)"
    )


if __name__ == "__main__":
    main()
