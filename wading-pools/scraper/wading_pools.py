"""
Scrapes the Seattle Parks wading pools & sprayparks page into structured JSON.

Source has no schedule API (only location data is open-data-fed), so we parse
the server-rendered HTML table + the daily weather-override banner directly.
Re-run this daily; it's designed to no-op safely (empty lists) if the page
markup changes, rather than crash, so the site generator can flag stale data.
"""
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

SOURCE_URL = "https://www.seattle.gov/parks/pools/wading-pools-and-sprayparks"
OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "wading-pools.json"

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def fetch_html(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip("  \n\t")


def parse_today_notice(soup: BeautifulSoup):
    banner = soup.select_one("#mainColMain ~ * .seagovBanner, .notificationContainer .seagovBanner")
    if not banner:
        banner = soup.select_one(".seagovBanner")
    if not banner:
        return None
    heading = banner.find("h2")
    intro = banner.find("p")
    items = [clean(li.get_text()) for li in banner.select("ul li")]
    if not heading and not items:
        return None
    return {
        "heading": clean(heading.get_text()) if heading else None,
        "intro": clean(intro.get_text()) if intro else None,
        "exceptions": items,
    }


def parse_schedule_year(soup: BeautifulSoup):
    heading = soup.find("h3", string=re.compile(r"Wading Pool schedule", re.I))
    if heading:
        m = re.search(r"\d{4}", heading.get_text())
        if m:
            return int(m.group(0))
    return datetime.now().year


def parse_wading_pools(soup: BeautifulSoup):
    heading = soup.find("h3", string=re.compile(r"Wading Pool schedule", re.I))
    table = heading.find_next("table") if heading else soup.select_one("table.table-striped")
    if not table:
        return []

    pools = []
    rows = table.select("tr")
    for row in rows:
        head_cell = row.find("th")
        data_cells = row.find_all("td")
        if head_cell is None or not data_cells or len(data_cells) != 7:
            continue  # header row or malformed row

        ps = head_cell.find_all("p")
        if not ps:
            continue
        name_link = ps[0].find("a")
        name = clean(name_link.get_text()) if name_link else clean(ps[0].get_text())
        info_url = urljoin(SOURCE_URL, name_link["href"]) if name_link and name_link.get("href") else None
        address = clean(ps[1].get_text()) if len(ps) > 1 else None
        address_url = ps[1].find("a")["href"] if len(ps) > 1 and ps[1].find("a") else None
        season = clean(ps[2].get_text(separator=" ")) if len(ps) > 2 else None

        schedule = {}
        for day, cell in zip(DAYS, data_cells):
            text = clean(cell.get_text(separator=" "))
            if not text or text.startswith("--"):
                schedule[day] = None
            else:
                # "Wed 12-7pm" -> keep just the hours part
                m = re.match(r"^\w+\.?\s+(.*)$", text)
                schedule[day] = m.group(1) if m else text

        pools.append({
            "name": name,
            "info_url": info_url,
            "address": address,
            "map_url": address_url,
            "season": season,
            "schedule": schedule,
        })
    return pools


def fetch_park_page_details(info_url: str):
    """Every park's own seattle.gov page has a hero image div near the top
    (<div class="featureWrapper ..." data-backgroundurl="/images/...jpg">)
    and a map-marker link with the street address
    (<a ...><i class="fas fa-map-marker">...</i>123 Main St, Seattle, WA 98xxx</a>).
    Fetch that page and pull both out. Soft-fails to Nones - a missing
    thumbnail/address shouldn't break the whole scrape."""
    if not info_url:
        return {"image_url": None, "address": None, "map_url": None}
    try:
        html = fetch_html(info_url)
    except (urllib.error.URLError, TimeoutError, OSError):
        return {"image_url": None, "address": None, "map_url": None}

    image_url = None
    m = re.search(r'class="featureWrapper[^"]*"\s+data-backgroundurl="([^"]+)"', html)
    if m and m.group(1).strip():
        image_url = urljoin(info_url, m.group(1))

    address, map_url = None, None
    soup = BeautifulSoup(html, "html.parser")
    marker_icon = soup.select_one("i.fa-map-marker")
    map_link = marker_icon.find_parent("a") if marker_icon else None
    if map_link:
        map_url = map_link.get("href")
        text = clean(map_link.get_text())
        address = text or None

    return {"image_url": image_url, "address": address, "map_url": map_url}


def parse_sprayparks(soup: BeautifulSoup):
    heading = soup.find("h2", string=re.compile(r"^Sprayparks", re.I))
    if not heading:
        return {"season": None, "hours": None, "locations": []}

    season = None
    hours = None
    for sib in heading.find_all_next(["p", "ul"], limit=6):
        if sib.name == "p" and season is None:
            txt = clean(sib.get_text())
            if txt:
                season = txt
                continue
        if sib.name == "p" and hours is None and "hours" not in (season or "") and re.search(r"\bam\b|\bpm\b", sib.get_text(), re.I):
            hours = clean(sib.get_text())
        if sib.name == "ul":
            break

    ul = heading.find_next("ul")
    locations = []
    if ul:
        for li in ul.find_all("li", recursive=False):
            link = li.find("a")
            name = clean(link.get_text()) if link else clean(li.get_text().split("\n")[0])
            info_url = urljoin(SOURCE_URL, link["href"]) if link and link.get("href") else None
            nested = li.find("ul")
            note = clean(nested.get_text()) if nested else None
            # strip nested note text out of the main li text if no link
            locations.append({"name": name, "info_url": info_url, "note": note})
    return {"season": season, "hours": hours, "locations": locations}


def parse_meta(soup: BeautifulSoup):
    hotline_link = soup.select_one('a[href^="tel:"]')
    fb_link = soup.select_one('a[href*="facebook.com"]')
    policy_marker = soup.find(string=re.compile(r"open with an attendant", re.I))
    policy_p = policy_marker.find_parent("p") if policy_marker else None
    return {
        "hotline": clean(hotline_link.get_text()) if hotline_link else None,
        "facebook_url": fb_link["href"] if fb_link else None,
        "policy": clean(policy_p.get_text(separator=" ")) if policy_p else None,
    }


def scrape(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    meta = parse_meta(soup)
    return {
        "source_url": SOURCE_URL,
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "schedule_year": parse_schedule_year(soup),
        "today_notice": parse_today_notice(soup),
        "policy": meta["policy"],
        "hotline": meta["hotline"],
        "facebook_url": meta["facebook_url"],
        "wading_pools": parse_wading_pools(soup),
        "sprayparks": parse_sprayparks(soup),
    }


def main():
    html = fetch_html(SOURCE_URL)
    result = scrape(html)

    # Pool header images are fetched separately (see fetch_images.py) and are
    # not part of this daily run - they're static park photos that don't need
    # re-checking every day. site/generate.py merges them back in by name.

    if len(result["wading_pools"]) < 10 or len(result["sprayparks"]["locations"]) < 5:
        print(
            f"WARNING: parsed suspiciously few entries "
            f"({len(result['wading_pools'])} pools, "
            f"{len(result['sprayparks']['locations'])} sprayparks) "
            "- page markup may have changed. Writing anyway for inspection.",
            file=sys.stderr,
        )

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Wrote {OUT_PATH} ({len(result['wading_pools'])} pools, "
          f"{len(result['sprayparks']['locations'])} sprayparks)")


if __name__ == "__main__":
    main()
