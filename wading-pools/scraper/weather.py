"""
Fetches today's Seattle forecast from Open-Meteo (free, no API key, includes
UV index which most no-key weather APIs skip) and writes a small summary to
data/weather.json for the site's weather card.

Unlike pool images, this DOES need to run daily - it's a forecast, not a
static fact.
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "weather.json"

# Downtown Seattle
LATITUDE = 47.6062
LONGITUDE = -122.3321

API_URL = (
    "https://api.open-meteo.com/v1/forecast"
    f"?latitude={LATITUDE}&longitude={LONGITUDE}"
    "&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max,uv_index_max"
    "&timezone=America%2FLos_Angeles&forecast_days=1&temperature_unit=fahrenheit"
)

# WMO weather codes -> (condition label, icon key). Reference:
# https://open-meteo.com/en/docs (see "WMO Weather interpretation codes")
WEATHER_CODES = {
    0: ("Clear", "sun"),
    1: ("Mostly Clear", "sun"),
    2: ("Partly Cloudy", "partly-cloudy"),
    3: ("Cloudy", "cloudy"),
    45: ("Foggy", "fog"),
    48: ("Foggy", "fog"),
    51: ("Light Drizzle", "rain"),
    53: ("Drizzle", "rain"),
    55: ("Heavy Drizzle", "rain"),
    56: ("Freezing Drizzle", "rain"),
    57: ("Freezing Drizzle", "rain"),
    61: ("Light Rain", "rain"),
    63: ("Rain", "rain"),
    65: ("Heavy Rain", "rain"),
    66: ("Freezing Rain", "rain"),
    67: ("Freezing Rain", "rain"),
    71: ("Light Snow", "snow"),
    73: ("Snow", "snow"),
    75: ("Heavy Snow", "snow"),
    77: ("Snow Grains", "snow"),
    80: ("Rain Showers", "rain"),
    81: ("Rain Showers", "rain"),
    82: ("Heavy Showers", "rain"),
    85: ("Snow Showers", "snow"),
    86: ("Snow Showers", "snow"),
    95: ("Thunderstorms", "storm"),
    96: ("Thunderstorms", "storm"),
    99: ("Thunderstorms", "storm"),
}


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    try:
        raw = fetch_json(API_URL)
        daily = raw["daily"]
        code = daily["weathercode"][0]
        condition, icon = WEATHER_CODES.get(code, ("Unknown", "cloudy"))
        result = {
            "date": daily["time"][0],
            "condition": condition,
            "icon": icon,
            "high_f": round(daily["temperature_2m_max"][0]),
            "low_f": round(daily["temperature_2m_min"][0]),
            "rain_chance": round(daily["precipitation_probability_max"][0]),
            "uv_index": round(daily["uv_index_max"][0]),
        }
    except (urllib.error.URLError, TimeoutError, OSError, KeyError, IndexError) as e:
        print(f"WARNING: weather fetch failed ({e}); leaving weather.json untouched.", file=sys.stderr)
        return

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Wrote {OUT_PATH}: {result['condition']}, {result['low_f']}-{result['high_f']}F, "
          f"UV {result['uv_index']}, {result['rain_chance']}% rain")


if __name__ == "__main__":
    main()
