"""
Builds one row per Seattle park (not per playground/court/etc) into
data/parks.json, using Seattle Parks' own open-data GIS layers rather than
scraping per-park HTML pages (unlike pools/wading-pools, there is no single
seattle.gov page per park worth scraping - the useful structured data lives
in ArcGIS FeatureServer layers published by Seattle Parks & Recreation at
data-seattlecitygis.opendata.arcgis.com, all hosted under the same ArcGIS
Online org as the Swimming Pools feed the pools scraper already uses:
services.arcgis.com/ZOyb2t4B0UYuYNYH/arcgis/rest/services/<LayerName>/FeatureServer

The base park list comes from "Park Boundary (Centroids)" - one point per
park, with a stable numeric PMA (Property Management Area) id and a lat/lon
already computed for us. Addresses come from "Park Boundary (details)"
(one row per tax parcel, so many rows share a PMA - we take the first
non-blank ADDRESS for that PMA).

Amenity layers (Play Equipment, Picnic Tables, Park Restrooms, Basketball/
Tennis/Pickleball Courts, Dog Off-Leash Areas, Spray Parks, Wading Pools)
are each a set of points somewhere inside a park. Most of them publish a
PMAID field that lines up directly with the park layer's PMA (e.g.
PMAID "307" == PMA 307 for Green Lake Park) - that's used as the join key
when present. The one big exception is Play Equipment: PMAID/PARK_NM are
null on effectively every row (a known gap in that particular layer as
published), even though PA_NM (Play Area Name, a messy free-text park name)
and x/y coordinates ARE populated. Rather than trying to normalize 200+
inconsistent PA_NM spellings ("Bay view Playground " vs "Bayview Playground "
vs "Bayview Kinnear Playground "), playgrounds are joined to a park purely
by nearest-centroid distance in feet (state-plane 2926, the layers' native
SR - no reprojection needed for a same-CRS distance comparison). The same
nearest-centroid fallback is used for any other amenity row whose PMAID
doesn't resolve. This is a deliberate "don't overclaim precision" choice
(same posture as the pools/wading-pools scrapers): a playground within
~600ft of a park's centroid is almost always that park's playground in
practice for Seattle's park sizes, but this can occasionally misattribute
a feature at the edge of a large park, or between two adjacent small parks.

Every field the site renders should degrade gracefully: a park with no
resolvable address renders without one, an amenity that doesn't match any
park just doesn't set that park's flag, and a whole layer failing to fetch
(network error, schema change) only removes that one amenity signal rather
than crashing the whole scrape.

Splashpads: rather than name-matching against wading-pools/data/wading-pools.json
(free-text names, inconsistent with GIS names), has_splashpad is derived
straight from the authoritative "Spray Parks" and "Wading Pools" GIS layers
(same nearest-centroid join as other amenities) - arguably more reliable
than a text match would be. Parks with has_splashpad get a link to the
existing wading-pools mini-site instead of duplicating its schedule data.

Neighborhood buckets: park street addresses turn out to rarely contain any
recognizable neighborhood name (a park's ADDRESS is a plain street address
like "1902 13th Ave. S" - no "Beacon Hill" in sight), so naive keyword
matching against the address string alone sorted the overwhelming majority
of parks into the "other" fallback bucket. Instead, this scraper pulls
Seattle's own "Neighborhood Map Atlas Neighborhoods" polygon layer
(nma_nhoods_sub, same ArcGIS org) and does a point-in-polygon test of each
park's centroid against it, which gives a real City-defined district name
(L_HOOD, e.g. "Northeast") and sub-neighborhood (S_HOOD, e.g. "Wedgwood")
per park - those two text fields are then keyword-matched against the
required 10-bucket list. The address-text keyword match is kept as a
fallback for any park whose centroid doesn't land inside a mapped
neighborhood polygon (parks right on the water or at the city edge), and
"other" remains the final fallback if neither resolves.
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

ORG_BASE = "https://services.arcgis.com/ZOyb2t4B0UYuYNYH/arcgis/rest/services"
PARK_INDEX_PAGES = [
    "https://www.seattle.gov/parks/parks/parks-a-d",
    "https://www.seattle.gov/parks/parks/parks-e-h",
    "https://www.seattle.gov/parks/parks/parks-i-l",
    "https://www.seattle.gov/parks/parks/parks-m-p",
    "https://www.seattle.gov/parks/parks/parks-q-t",
    "https://www.seattle.gov/parks/parks/parks-u-z",
]
OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "parks.json"

# Nearest-centroid join distance cap, in feet (native SR 2926 is US-feet).
# ~600ft is generous enough to cover a large park's amenities from its
# centroid without routinely bleeding into a neighboring park.
MAX_JOIN_DISTANCE_FT = 2000

NEIGHBORHOODS = [
    {"slug": "ballard-fremont-greenwood", "label": "Ballard / Fremont / Greenwood",
     "keywords": ["ballard", "crown hill", "phinney", "greenwood", "fremont"]},
    {"slug": "wallingford-green-lake", "label": "Wallingford / Green Lake",
     "keywords": ["wallingford", "green lake", "tangletown"]},
    {"slug": "north-seattle", "label": "North Seattle",
     "keywords": ["northgate", "lake city", "bitter lake", "broadview", "haller lake"]},
    {"slug": "northeast-seattle", "label": "Northeast Seattle",
     "keywords": ["university district", "u district", "ravenna", "wedgwood", "view ridge",
                  "sand point", "laurelhurst", "u-district"]},
    {"slug": "queen-anne-magnolia", "label": "Queen Anne / Magnolia",
     "keywords": ["queen anne", "magnolia", "interbay"]},
    {"slug": "downtown-slu", "label": "Downtown / South Lake Union",
     "keywords": ["downtown", "belltown", "south lake union", "slu", "first hill",
                  "international district", "pioneer square"]},
    {"slug": "capitol-hill-central-district", "label": "Capitol Hill / Central District",
     "keywords": ["capitol hill", "central district", "madison valley", "madrona",
                  "leschi", "montlake"]},
    {"slug": "west-seattle", "label": "West Seattle",
     "keywords": ["alki", "admiral", "genesee", "delridge", "high point", "fauntleroy",
                  "west seattle"]},
    {"slug": "southeast-seattle", "label": "Southeast Seattle",
     "keywords": ["columbia city", "rainier valley", "beacon hill", "rainier beach",
                  "mount baker"]},
    {"slug": "georgetown-south-park", "label": "Georgetown / South Park",
     "keywords": ["georgetown", "south park"]},
]
OTHER_BUCKET = {"slug": "other", "label": "Other Seattle"}

# Populated as layers fail to fetch, so the generator can show a
# low-confidence banner instead of silently under-reporting amenities.
LAYER_ERRORS = []


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def query_all(layer_url: str, out_fields: str, geometry=True, where="1=1"):
    """Paginate a FeatureServer layer's /query endpoint (native SR, so point
    geometries come back as {"x":.., "y":..} in US feet) - returns a list of
    {"attributes": {...}, "geometry": {...}|None}. Returns [] (not a crash)
    on any fetch error, so one bad/renamed layer just yields no amenity data
    for that layer rather than failing the whole scrape."""
    out = []
    offset = 0
    page_size = 2000
    try:
        while True:
            params = {
                "where": where,
                "outFields": out_fields,
                "returnGeometry": "true" if geometry else "false",
                "resultOffset": offset,
                "resultRecordCount": page_size,
                "f": "json",
            }
            url = layer_url + "?" + urllib.parse.urlencode(params)
            data = fetch_json(url)
            if "error" in data:
                raise ValueError(data["error"])
            feats = data.get("features", [])
            out.extend(feats)
            if len(feats) < page_size:
                break
            offset += page_size
        return out
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: could not fetch {layer_url} ({exc}); skipping this layer.", file=sys.stderr)
        LAYER_ERRORS.append(f"{layer_url}: {exc}")
        return []


def map_url_for(address: str):
    if not address:
        return None
    return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(address)


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "park"


def smart_title(name: str) -> str:
    """ALL-CAPS source names ('12TH AVE SQUARE PARK', 'NE') need
    title-casing for display, but str.title() mangles ordinals like
    '12TH' -> '12Th'. This lowercases each word's letters after the first
    and preserves any leading digits as-is."""
    def fix_word(w):
        m = re.match(r"^(\d+)([A-Za-z]*)(.*)$", w)
        if m and m.group(1):
            return m.group(1) + m.group(2).lower() + m.group(3)
        return w[:1] + w[1:].lower() if w else w
    return " ".join(fix_word(w) for w in name.split(" "))


def clean(text):
    if text is None:
        return None
    t = re.sub(r"\s+", " ", str(text)).strip()
    return t or None


def bucket_for_text(text):
    text = (text or "").lower()
    for bucket in NEIGHBORHOODS:
        if any(kw in text for kw in bucket["keywords"]):
            return bucket["slug"], bucket["label"]
    return None


def point_in_ring(x, y, ring):
    """Standard ray-casting point-in-polygon test for a single ring."""
    inside = False
    n = len(ring)
    x1, y1 = ring[0]
    for i in range(1, n + 1):
        x2, y2 = ring[i % n]
        if ((y1 > y) != (y2 > y)) and (x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-12) + x1):
            inside = not inside
        x1, y1 = x2, y2
    return inside


def point_in_polygon(x, y, rings):
    """A GeoJSON Polygon's rings: rings[0] is the outer boundary, any
    further rings are holes. True if inside the outer ring and not inside
    any hole."""
    if not rings:
        return False
    if not point_in_ring(x, y, rings[0]):
        return False
    for hole in rings[1:]:
        if point_in_ring(x, y, hole):
            return False
    return True


def fetch_neighborhood_polygons():
    """Seattle's 'Neighborhood Map Atlas Neighborhoods' layer (94 named
    sub-areas covering the city) - see module docstring. Returns a list of
    {"l_hood", "s_hood", "polygons": [ [ring, ring, ...], ... ]} (a feature
    can be a MultiPolygon, hence a list of ring-lists). Returns [] (not a
    crash) if unreachable - every park then just falls back to address
    keyword matching, then 'other'."""
    url = f"{ORG_BASE}/nma_nhoods_sub/FeatureServer/0/query"
    params = {"where": "1=1", "outFields": "L_HOOD,S_HOOD", "outSR": "4326", "f": "geojson"}
    try:
        data = fetch_json(url + "?" + urllib.parse.urlencode(params))
        out = []
        for feat in data.get("features", []):
            geom = feat.get("geometry") or {}
            coords = geom.get("coordinates") or []
            if geom.get("type") == "Polygon":
                polygons = [coords]
            elif geom.get("type") == "MultiPolygon":
                polygons = coords
            else:
                continue
            props = feat.get("properties", {})
            out.append({
                "l_hood": props.get("L_HOOD"),
                "s_hood": props.get("S_HOOD"),
                "polygons": polygons,
            })
        return out
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: could not fetch neighborhood polygons ({exc}); falling back to address text matching only.", file=sys.stderr)
        LAYER_ERRORS.append(f"nma_nhoods_sub: {exc}")
        return []


def bucket_for_park(lat, lon, address, neighborhoods):
    if lat is not None and lon is not None:
        for nb in neighborhoods:
            for rings in nb["polygons"]:
                # GeoJSON coordinates are [lon, lat]; point_in_polygon takes (x, y).
                if point_in_polygon(lon, lat, rings):
                    hit = bucket_for_text(nb["s_hood"]) or bucket_for_text(nb["l_hood"])
                    if hit:
                        return hit
    hit = bucket_for_text(address)
    if hit:
        return hit
    return OTHER_BUCKET["slug"], OTHER_BUCKET["label"]


def normalize_park_name(name: str) -> str:
    """Loose match key for tying our GIS-derived park name to seattle.gov's
    own park-page link text - lowercase, "&" spelled out (their index pages
    use "and" in link text/slugs even when the visible name says "&"),
    punctuation dropped, whitespace collapsed. Exact-match only (no fuzzy/
    substring matching) so a mismatch just means no button rather than a
    wrong one - see module docstring's "don't overclaim precision" posture."""
    s = (name or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def fetch_park_url_index():
    """Seattle Parks publishes every park's real page across 6 alphabetical
    index pages (A-D, E-H, ... U-Z) at a stable, guessable URL pattern -
    scraped here instead of guessing a slug from our own park name, since
    seattle.gov's slugs don't always match a plain slugify (e.g. "&" becomes
    "and" in their URLs, not just stripped). Returns {normalized_name: url},
    and returns {} (not a crash) if a page fails to fetch - parks just get
    no Seattle.gov button that run rather than a guessed/broken link."""
    index = {}
    for page_url in PARK_INDEX_PAGES:
        try:
            req = urllib.request.Request(page_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: could not fetch {page_url} ({exc}); some parks may be missing a Seattle.gov link.", file=sys.stderr)
            LAYER_ERRORS.append(f"{page_url}: {exc}")
            continue
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/parks/parks/" not in href or href.rstrip("/").endswith("/parks/parks"):
                continue
            text = clean(a.get_text())
            if not text or text.lower().startswith("parks "):
                continue
            key = normalize_park_name(text)
            if key:
                index[key] = urllib.parse.urljoin("https://www.seattle.gov", href)
    return index


def fetch_park_base():
    """Park Boundary (Centroids): one row per park, with a numeric PMA id
    and lat/lon already computed - the base park list. Falls back to an
    empty list (which surfaces as "0 parks written", loud in the summary
    print, rather than a crash) if this layer is unreachable, since nothing
    else in this scraper works without it."""
    url = f"{ORG_BASE}/Park_Boundary_(Centroids)/FeatureServer/0/query"
    rows = query_all(url, "NAME,PMA", geometry=True)
    parks = {}
    for row in rows:
        attrs = row.get("attributes", {})
        geom = row.get("geometry")
        name = clean(attrs.get("NAME"))
        pma = attrs.get("PMA")
        if not name or geom is None:
            continue
        # A handful of PMAs cover multiple disjoint centroid rows (e.g. a
        # park split across a levy vs. base layer edit); keep the first.
        if pma in parks:
            continue
        parks[pma] = {
            "pma": pma,
            "name": smart_title(name) if name.isupper() else name,
            "slug": slugify(name),
            "x": geom.get("x"),
            "y": geom.get("y"),
        }
    return parks


def fetch_lat_lon(pmas):
    """Second pass at the same centroid layer, this time asking for WGS84
    lat/lon (outSR=4326) for display - kept separate from fetch_park_base's
    native-SR fetch so the nearest-centroid distance math elsewhere in this
    file can stay in one consistent unit (feet) without reprojecting."""
    url = f"{ORG_BASE}/Park_Boundary_(Centroids)/FeatureServer/0/query"
    params = {
        "where": "1=1", "outFields": "PMA", "returnGeometry": "true",
        "outSR": "4326", "f": "json",
    }
    try:
        data = fetch_json(url + "?" + urllib.parse.urlencode(params))
        out = {}
        for feat in data.get("features", []):
            pma = feat["attributes"].get("PMA")
            geom = feat.get("geometry")
            if pma in pmas and geom:
                out[pma] = (geom.get("y"), geom.get("x"))  # (lat, lon)
        return out
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: could not fetch lat/lon for parks ({exc}); parks will have no map coordinates.", file=sys.stderr)
        LAYER_ERRORS.append(f"Park_Boundary_(Centroids) lat/lon: {exc}")
        return {}


def fetch_addresses(pmas):
    """Park Boundary (details): one row per tax parcel, so many rows share
    a PMA - the first non-blank ADDRESS for a PMA is used. Soft-fails to {}
    (parks then just render without an address) if unreachable."""
    url = f"{ORG_BASE}/Park_Boundary_(details)/FeatureServer/2/query"
    rows = query_all(url, "PMA,ADDRESS", geometry=False)
    out = {}
    for row in rows:
        attrs = row.get("attributes", {})
        pma = attrs.get("PMA")
        address = clean(attrs.get("ADDRESS"))
        if pma in pmas and address and pma not in out:
            out[pma] = address
    return out


def nearest_pma(x, y, parks_by_pma, max_dist=MAX_JOIN_DISTANCE_FT):
    best_pma, best_dist = None, None
    for pma, p in parks_by_pma.items():
        if p["x"] is None or p["y"] is None:
            continue
        d = ((p["x"] - x) ** 2 + (p["y"] - y) ** 2) ** 0.5
        if best_dist is None or d < best_dist:
            best_pma, best_dist = pma, d
    if best_pma is not None and best_dist is not None and best_dist <= max_dist:
        return best_pma
    return None


def mark_amenity(parks_by_pma, flag_name, layer_name, layer_id, pmaid_field):
    """Fetch a point layer and set parks_by_pma[pma][flag_name] = True for
    every park it resolves to, via its PMAID field when present (cast to
    int and checked against a real park's pma) and nearest-centroid
    distance otherwise. A layer that fails to fetch just leaves this flag
    unset everywhere (default False) rather than crashing."""
    url = f"{ORG_BASE}/{layer_name}/FeatureServer/{layer_id}/query"
    fields = "OBJECTID" + (f",{pmaid_field}" if pmaid_field else "")
    rows = query_all(url, fields, geometry=True)
    matched = 0
    for row in rows:
        geom = row.get("geometry")
        if not geom or geom.get("x") is None or geom.get("y") is None:
            continue
        pma = None
        if pmaid_field:
            raw = row.get("attributes", {}).get(pmaid_field)
            try:
                candidate = int(str(raw).strip())
                if candidate in parks_by_pma:
                    pma = candidate
            except (TypeError, ValueError):
                pma = None
        if pma is None:
            pma = nearest_pma(geom["x"], geom["y"], parks_by_pma)
        if pma is not None:
            parks_by_pma[pma][flag_name] = True
            matched += 1
    print(f"  {layer_name}: {len(rows)} features, matched to {matched} parks", file=sys.stderr)


def scrape() -> dict:
    parks_by_pma = fetch_park_base()
    if not parks_by_pma:
        return {"scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "parks": []}

    pmas = set(parks_by_pma)
    lat_lon = fetch_lat_lon(pmas)
    addresses = fetch_addresses(pmas)
    neighborhoods = fetch_neighborhood_polygons()
    park_url_index = fetch_park_url_index()

    for pma, p in parks_by_pma.items():
        p["address"] = addresses.get(pma)
        latlon = lat_lon.get(pma)
        p["lat"] = latlon[0] if latlon else None
        p["lon"] = latlon[1] if latlon else None
        p["has_playground"] = False
        p["has_picnic"] = False
        p["has_restroom"] = False
        p["has_sport_court"] = False
        p["has_splashpad"] = False
        p["has_offleash"] = False

    print("Matching amenity layers to parks...", file=sys.stderr)
    # Play Equipment publishes no usable PMAID (see module docstring) - pmaid_field=None
    # forces nearest-centroid matching for every row in this one layer.
    mark_amenity(parks_by_pma, "has_playground", "Play_Equipment", 0, None)
    mark_amenity(parks_by_pma, "has_picnic", "Picnic_Tables", 0, "PMAID")
    mark_amenity(parks_by_pma, "has_picnic", "Picnic_Sites", 0, "PMAID")
    mark_amenity(parks_by_pma, "has_restroom", "Parks_Restrooms", 0, "PMAID")
    mark_amenity(parks_by_pma, "has_sport_court", "Basketball_Court_Points", 0, "PMAID")
    mark_amenity(parks_by_pma, "has_sport_court", "Tennis_Courts", 0, "PMAID")
    mark_amenity(parks_by_pma, "has_sport_court", "Pickleball_Courts", 0, "PMA")
    mark_amenity(parks_by_pma, "has_offleash", "Dog_Off_Leash_Areas", 0, "PMAID")
    mark_amenity(parks_by_pma, "has_splashpad", "Spray_Parks", 0, "PMAID")
    mark_amenity(parks_by_pma, "has_splashpad", "Wading_Pools", 0, "PMAID")

    parks = []
    seen_slugs = {}
    for pma, p in sorted(parks_by_pma.items(), key=lambda kv: kv[1]["name"]):
        slug = p["slug"]
        if slug in seen_slugs:
            seen_slugs[slug] += 1
            slug = f"{slug}-{seen_slugs[slug]}"
        else:
            seen_slugs[slug] = 1
        bucket_slug, bucket_label = bucket_for_park(p["lat"], p["lon"], p["address"], neighborhoods)
        seattle_url = park_url_index.get(normalize_park_name(p["name"]))
        parks.append({
            "slug": slug,
            "name": p["name"],
            "address": p["address"],
            "map_url": map_url_for(p["address"]),
            "seattle_url": seattle_url,
            "lat": p["lat"],
            "lon": p["lon"],
            "neighborhood_bucket": bucket_slug,
            "neighborhood_label": bucket_label,
            "has_playground": p["has_playground"],
            "has_picnic": p["has_picnic"],
            "has_restroom": p["has_restroom"],
            "has_sport_court": p["has_sport_court"],
            "has_splashpad": p["has_splashpad"],
            "has_offleash": p["has_offleash"],
        })

    return {
        "source": "Seattle GeoData (data-seattlecitygis.opendata.arcgis.com), Seattle Parks & Recreation layers",
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "amenity_layer_errors": list(LAYER_ERRORS),
        "parks": parks,
    }


def main():
    result = scrape()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    parks = result["parks"]
    by_bucket = {}
    for p in parks:
        by_bucket[p["neighborhood_bucket"]] = by_bucket.get(p["neighborhood_bucket"], 0) + 1
    print(f"Wrote {OUT_PATH} ({len(parks)} parks)")
    matched_urls = sum(1 for p in parks if p.get("seattle_url"))
    print(f"  seattle.gov page matched for {matched_urls}/{len(parks)} parks")
    for slug, count in sorted(by_bucket.items(), key=lambda kv: -kv[1]):
        print(f"  {slug}: {count}")


if __name__ == "__main__":
    main()
