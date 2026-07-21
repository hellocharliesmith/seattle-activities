"""
Scrapes all 10 Seattle Parks pools (2 summer-only outdoor pools + 8 year-round
indoor pools) into structured JSON.

The pool list and indoor/outdoor split come from https://www.seattle.gov/parks/pools
(the hub page). Each pool then has its own page with its own hand-built
layout - a season heading ("Main Season: ..." for the two outdoor pools,
"Summer 2026: ..." style for the indoor ones, since indoor pools also
publish a seasonal drop-in schedule even though the facility itself is open
year-round) followed by a drop-in schedule table, plus a separate
holiday/closure note. One indoor pool (Queen Anne, as of this writing) is
fully closed for a maintenance project and has no schedule heading at all -
that's handled as its own case rather than forced into the schedule shape.

Table markup is NOT consistent even among the 8 indoor pages: most use a
blank leading cell to mean "same program as the row above" (no rowspan),
while the two outdoor pages use a real rowspan on the leading cell. This
scraper treats both as the same underlying pattern - "this row continues the
previous row's first column" - and forward-fills accordingly. It does not
try to unify column *counts* or *names* across pools beyond that; a table's
columns are rendered generically by the site from whatever this scraper
captured.

Addresses come from the "Swimming Pools" ArcGIS FeatureServer maintained by
Seattle's GIS program (data-seattlecitygis.opendata.arcgis.com) - unlike
wading pools, which only exist as an HTML table with no open-data
counterpart, full pools DO have a real open-data feed for locations (no
hours/schedule, same limitation as everywhere else on seattle.gov, hence
still scraping the HTML for schedules). If that feed is ever unreachable or
a pool's name doesn't match, the address simply comes back None rather than
crashing - the site renders without an address/map link in that case rather
than failing the whole pool.

Re-run this daily; on any parse failure for a given pool it fills that pool
in with an "error" field rather than crashing, so the site can show a stale
or partial page instead of failing outright.
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

HUB_URL = "https://www.seattle.gov/parks/pools"
FEATURE_SERVER_URL = (
    "https://services.arcgis.com/ZOyb2t4B0UYuYNYH/arcgis/rest/services/"
    "Swimming_Pools/FeatureServer/0/query"
)
OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "pools.json"

# Slug + display name + indoor/outdoor, normally scraped fresh from the hub
# page's "Alphabetical List of All Pools" - see fetch_pool_index(). This
# constant list is only a fallback if the hub page can't be parsed, so the
# scraper still has something to work from rather than producing nothing.
FALLBACK_POOLS = [
    {"slug": "ballard-pool", "name": "Ballard Pool", "kind": "indoor"},
    {"slug": "colman-pool", "name": "Colman Pool", "kind": "outdoor"},
    {"slug": "evans-pool", "name": "Evans Pool", "kind": "indoor"},
    {"slug": "helene-madison-pool", "name": "Helene Madison Pool", "kind": "indoor"},
    {"slug": "meadowbrook-pool", "name": "Meadowbrook Pool", "kind": "indoor"},
    {"slug": "medgar-evers-pool", "name": "Medgar Evers Pool", "kind": "indoor"},
    {"slug": "mounger-pool", "name": "Mounger Pool", "kind": "outdoor"},
    {"slug": "queen-anne-pool", "name": "Queen Anne Pool", "kind": "indoor"},
    {"slug": "rainier-beach-pool", "name": "Rainier Beach Pool", "kind": "indoor"},
    {"slug": "southwest-pool", "name": "Southwest Pool", "kind": "indoor"},
]
OUTDOOR_SLUGS = {"colman-pool", "mounger-pool"}

SEASON_HEADING_RE = re.compile(
    r"(Spring|Summer|Fall|Winter)\s+\d{4}\s*:\s*"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)",
    re.I,
)


def fetch_html(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip(" \n\t")


def map_url_for(address: str) -> str:
    return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(address)


def fetch_pool_image(url: str):
    """Each pool's own seattle.gov page has a hero image div near the top:
    <div class="featureWrapper ..." data-backgroundurl="/images/...jpg">
    Soft-fails to None - a missing thumbnail shouldn't break anything."""
    if not url:
        return None
    try:
        html = fetch_html(url)
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    m = re.search(r'class="featureWrapper[^"]*"\s+data-backgroundurl="([^"]+)"', html)
    if not m or not m.group(1).strip():
        return None
    return urllib.parse.urljoin(url, m.group(1))


def fetch_pool_index():
    """Scrape the hub page's pool list -> [{slug, name, kind}, ...].

    Outdoor pools are the ones whose hub-page link text says "summer only";
    everything else in the list is indoor. Falls back to FALLBACK_POOLS
    (with a stderr WARNING) if the hub page's markup doesn't yield a
    plausible list, so a hub-page redesign degrades gracefully instead of
    silently returning nothing.
    """
    try:
        html = fetch_html(HUB_URL)
        soup = BeautifulSoup(html, "html.parser")
        main = soup.select_one("#mainColMain") or soup.select_one("main") or soup.body
        seen = {}
        for a in main.find_all("a", href=True):
            href = a["href"]
            m = re.search(r"(?:^|/)parks/pools/([a-z-]+)/?$", href)
            if not m:
                continue
            slug = m.group(1)
            if slug in ("fees-and-programs", "swim-seattle", "wading-pools-and-sprayparks"):
                continue
            text = clean(a.get_text())
            if not text or slug in seen:
                continue
            name = re.sub(r"\s*\([^)]*\)\s*$", "", text).strip()
            kind = "outdoor" if "summer only" in text.lower() else "indoor"
            seen[slug] = {"slug": slug, "name": name, "kind": kind}
        pools = list(seen.values())
        if len(pools) < 8:
            raise ValueError(f"only found {len(pools)} pool links")
        return pools
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: could not parse pool index from hub page ({exc}); using fallback list.", file=sys.stderr)
        return FALLBACK_POOLS


def fetch_addresses():
    """Swimming Pools ArcGIS FeatureServer -> {lowercased short name: address}.

    Returns {} (not a crash) if the service is unreachable or its schema
    changed - pools then just render without an address/map link.
    """
    url = FEATURE_SERVER_URL + "?" + urllib.parse.urlencode(
        {"where": "1=1", "outFields": "*", "f": "json", "outSR": "4326"}
    )
    try:
        raw = fetch_html(url)
        data = json.loads(raw)
        out = {}
        for feat in data.get("features", []):
            attrs = feat.get("attributes", {})
            name = attrs.get("NAME") or attrs.get("FULL_NAME") or attrs.get("OFFICIAL_N")
            address = attrs.get("ADDRESS")
            if name and address:
                out[clean(name).lower()] = clean(address)
        if not out:
            raise ValueError("no features returned")
        return out
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: could not fetch pool addresses from ArcGIS ({exc}); pools will have no address.", file=sys.stderr)
        return {}


def match_address(pool_name: str, addresses: dict):
    pn = pool_name.lower()
    for feature_name, address in addresses.items():
        if feature_name in pn or pn in feature_name:
            return address
    return None


def find_heading(scope, predicate):
    """First h2/h3 in document order whose get_text() matches predicate
    (a callable, since some headings are wrapped in nested tags like
    accordion <button>s so a plain string= match on the tag fails)."""
    for h in scope.find_all(["h2", "h3"]):
        text = h.get_text(" ", strip=True)
        if text and predicate(text):
            return h
    return None


def find_heading_containing(scope, substr):
    return find_heading(scope, lambda t: substr.lower() in t.lower())


def pos(tag):
    """Document-order sort key for a tag, so callers can tell whether one
    element truly precedes another rather than just being *reachable* via
    find_next() (which happily crosses into unrelated later sections)."""
    if tag is None:
        return (float("inf"), float("inf"))
    return (tag.sourceline if tag.sourceline is not None else float("inf"),
            tag.sourcepos if tag.sourcepos is not None else float("inf"))


def find_section_table(heading):
    """The table that actually belongs to this heading's section: the next
    <table> in the document, but only if it appears before the next *h2*
    (deliberately not h3 - seattle.gov routinely puts an h3 caption like
    "Click or tap to view this schedule..." between the season h2 and its
    table, which is still part of the same section, so h3s don't count as
    boundaries here). Without this check, find_next('table') would silently
    walk past an empty/PDF-only section into some unrelated table much
    further down the page - this happened on Rainier Beach Pool, whose
    schedule is PDF-only and where find_next('table') was grabbing an SSL
    registration table instead."""
    next_h2 = heading.find_next("h2")
    candidate = heading.find_next("table")
    if candidate is None:
        return None
    if next_h2 is not None and pos(candidate) > pos(next_h2):
        return None
    return candidate


def find_section_pdfs(heading):
    """PDF links within this heading's section (see find_section_table) -
    the fallback when a pool only publishes its schedule as a PDF rather
    than an HTML table (Rainier Beach Pool, as of this writing)."""
    next_h2 = heading.find_next("h2")
    boundary = pos(next_h2) if next_h2 is not None else (float("inf"), float("inf"))
    out = []
    seen = set()
    for a in heading.find_all_next("a", href=True):
        if pos(a) >= boundary:
            break
        href = a["href"]
        if not href.lower().split("?")[0].endswith(".pdf"):
            continue
        url = urllib.parse.urljoin("https://www.seattle.gov/", href)
        if url in seen:
            continue
        seen.add(url)
        label = urllib.parse.unquote(href.rsplit("/", 1)[-1]).replace(".pdf", "").replace("_ADA", "")
        out.append({"label": clean(label), "url": url})
        if len(out) >= 4:
            break
    return out


def parse_table(table):
    """Generic table -> {columns, rows}.

    Handles two different "this cell continues the row above" conventions
    seen across seattle.gov's pool pages: a real rowspan attribute (the two
    outdoor pools), and a blank leading `<td>`/`<th>` with no rowspan at all
    (most indoor pools) - both are normalized by forward-filling an empty
    first column from the previous row. Header-row detection doesn't rely on
    <th> vs <td> (seattle.gov mixes both for header cells); it treats the
    first row as a header only if it has more than one cell and none of its
    cells contain a digit - true for header rows like "Program / Day / Time
    / Details", while every observed first *data* row starts with a time or
    day range and so has a digit.
    """
    if table is None:
        return None

    grid = []
    pending = {}  # col_index -> (text, rows_remaining) for real rowspans
    for tr in table.find_all("tr"):
        row = []
        col = 0
        cell_iter = iter(tr.find_all(["th", "td"]))
        while True:
            while col in pending:
                text, remaining = pending[col]
                row.append(text)
                remaining -= 1
                if remaining <= 0:
                    del pending[col]
                else:
                    pending[col] = (text, remaining)
                col += 1
            cell = next(cell_iter, None)
            if cell is None:
                break
            text = clean(cell.get_text(" ", strip=True))
            rowspan = int(cell.get("rowspan", 1) or 1)
            row.append(text)
            if rowspan > 1:
                pending[col] = (text, rowspan - 1)
            col += 1
        if row:
            grid.append(row)

    if not grid:
        return None

    columns = None
    body = grid
    if len(grid) > 1 and len(grid[0]) > 1 and not any(re.search(r"\d", c) for c in grid[0]):
        columns = grid[0]
        body = grid[1:]
    if not body:
        return None

    # Forward-fill a blank leading cell from the previous row (the
    # "no-rowspan, just leave it blank" convention).
    filled = []
    prev = None
    for row in body:
        if prev is not None and len(row) == len(prev) and row and row[0] == "":
            row = [prev[0]] + row[1:]
        filled.append(row)
        prev = row

    return {"columns": columns, "rows": filled}


def parse_schedule_heading(main):
    """Find whichever season heading actually precedes the current drop-in
    schedule table - "Main Season: ..." (outdoor pools) or "Summer 2026:
    ..." style (indoor pools, which also run other seasons but only ever
    show the current one on the page)."""
    h = find_heading_containing(main, "Main Season")
    if h:
        return h
    return find_heading(main, lambda t: SEASON_HEADING_RE.search(t) is not None)


def parse_closure(main):
    """Queen Anne Pool (and possibly others in the future) publish no
    schedule heading at all because the facility is fully closed. Detect
    that by: no schedule heading found, and the page's first heading reads
    as a closure notice."""
    first_h2 = main.find("h2")
    heading_text = clean(first_h2.get_text(" ", strip=True)) if first_h2 else None
    if heading_text and re.search(r"\bclosed\b", heading_text, re.I):
        note = None
        p = first_h2.find_next("p")
        if p:
            note = clean(p.get_text(" ", strip=True))
        return {"heading": heading_text, "note": note}
    return None


def parse_pool_page(html: str, meta: dict, address):
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one("#mainColMain") or soup.select_one("main") or soup.body

    meta_desc = soup.find("meta", attrs={"name": "description"})
    blurb = clean(meta_desc["content"]) if meta_desc and meta_desc.get("content") else None

    season_heading = parse_schedule_heading(main)
    season_label = clean(season_heading.get_text(" ", strip=True)) if season_heading else None
    season_note = None
    schedule = None
    schedule_pdfs = []
    if season_heading:
        p = season_heading.find_next("p")
        if p:
            season_note = clean(p.get_text(" ", strip=True))
        schedule = parse_table(find_section_table(season_heading))
        if schedule is None:
            schedule_pdfs = find_section_pdfs(season_heading)

    closure = None if season_heading else parse_closure(main)

    holiday_heading = find_heading_containing(main, "Holiday")
    if not holiday_heading:
        holiday_heading = find_heading_containing(main, "Closures")
    holiday_note = None
    holiday_list = []
    holiday_schedule = None
    if holiday_heading:
        p = holiday_heading.find_next(["p", "ul"])
        if p and p.name == "p":
            holiday_note = clean(p.get_text(" ", strip=True))
        ul = holiday_heading.find_next("ul")
        if ul:
            holiday_list = [
                clean(li.get_text(" ", strip=True))
                for li in ul.find_all("li", recursive=False)
                if clean(li.get_text())
            ]
        possible_table = holiday_heading.find_next("table")
        if possible_table is not None:
            if schedule is not None and season_heading is not None:
                main_table = find_section_table(season_heading)
                # Only attribute this table to the holiday section if it's
                # genuinely a different, earlier table than the main
                # schedule table (holiday section usually comes first).
                if main_table is not None and pos(possible_table) < pos(main_table):
                    holiday_schedule = parse_table(possible_table)
            else:
                holiday_schedule = parse_table(find_section_table(holiday_heading))

    return {
        "slug": meta["slug"],
        "name": meta["name"],
        "kind": meta["kind"],
        "url": f"https://www.seattle.gov/parks/pools/{meta['slug']}",
        "address": address,
        "map_url": map_url_for(address) if address else None,
        "blurb": blurb,
        "season_label": season_label,
        "season_note": season_note,
        "schedule": schedule,
        "schedule_pdfs": schedule_pdfs,
        "holiday_note": holiday_note,
        "holiday_list": holiday_list,
        "holiday_schedule": holiday_schedule,
        "closure": closure,
    }


def scrape() -> dict:
    pool_index = fetch_pool_index()
    addresses = fetch_addresses()

    pools = []
    for meta in pool_index:
        # The hub page's own "summer only" wording is the source of truth
        # for indoor/outdoor, but cross-check against the manual
        # OUTDOOR_SLUGS list in case wording ever changes.
        if meta["slug"] in OUTDOOR_SLUGS:
            meta = {**meta, "kind": "outdoor"}
        url = f"https://www.seattle.gov/parks/pools/{meta['slug']}"
        try:
            html = fetch_html(url)
            address = match_address(meta["name"], addresses)
            pool = parse_pool_page(html, meta, address)
        except Exception as exc:  # noqa: BLE001 - keep going, flag instead of crash
            print(f"WARNING: failed to parse {meta['name']}: {exc}", file=sys.stderr)
            pool = {
                "slug": meta["slug"], "name": meta["name"], "kind": meta["kind"], "url": url,
                "address": None, "map_url": None, "blurb": None,
                "season_label": None, "season_note": None, "schedule": None, "schedule_pdfs": [],
                "holiday_note": None, "holiday_list": [], "holiday_schedule": None,
                "closure": None, "error": str(exc),
            }
        pools.append(pool)

    parseable = [p for p in pools if not p.get("closure")]
    parsed_ok = sum(1 for p in parseable if p.get("schedule") or p.get("schedule_pdfs"))
    if parsed_ok < len(parseable):
        print(
            f"WARNING: only {parsed_ok}/{len(parseable)} open pools parsed a schedule table "
            "- page markup may have changed. Writing anyway for inspection.",
            file=sys.stderr,
        )

    return {
        "hub_url": HUB_URL,
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "schedule_year": datetime.now().year,
        "pools": pools,
    }


def main():
    result = scrape()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    parseable = [p for p in result["pools"] if not p.get("closure")]
    parsed_ok = sum(1 for p in parseable if p.get("schedule") or p.get("schedule_pdfs"))
    print(f"Wrote {OUT_PATH} ({parsed_ok}/{len(parseable)} open pools with a parsed schedule, "
          f"{len(result['pools']) - len(parseable)} closed, {len(result['pools'])} total)")


if __name__ == "__main__":
    main()
