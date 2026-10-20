#!/usr/bin/env python3
"""
Job site weather logger (runs automatically on GitHub Actions).

Reads the site list from sites.txt, gets current temperature and conditions for
each site from the free Open-Meteo service, and saves one reading per site for
the 8 am, 12 noon and 5 pm Central-time slots into data/readings.json and
data/readings.csv.
"""
import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Chicago")
SLOTS = {8: "Morning", 12: "Midday", 17: "Afternoon"}  # Central-time hours

HERE = os.path.dirname(os.path.abspath(__file__))
SITES_PATH = os.path.join(HERE, "sites.txt")
DATA_DIR = os.path.join(HERE, "data")
JSON_PATH = os.path.join(DATA_DIR, "readings.json")
CSV_PATH = os.path.join(DATA_DIR, "readings.csv")
FIELDS = ["date", "slot", "time", "site", "temp_f", "conditions", "wind_mph", "precip_in"]

CODES = {
    45: "Foggy", 48: "Foggy",
    51: "Drizzle", 53: "Drizzle", 55: "Drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rainy", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snowy", 75: "Heavy snow", 77: "Snowy",
    80: "Rain showers", 81: "Rain showers", 82: "Heavy rain showers",
    85: "Snow showers", 86: "Snow showers",
    95: "Thunderstorms", 96: "Thunderstorms with hail", 99: "Thunderstorms with hail",
}


def label(code, is_day):
    if code == 0:
        return "Sunny" if is_day else "Clear"
    if code == 1:
        return "Mostly sunny" if is_day else "Mostly clear"
    if code == 2:
        return "Partly cloudy"
    if code == 3:
        return "Cloudy"
    return CODES.get(code, "Unknown")


def load_sites():
    sites = []
    with open(SITES_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                name, lat, lon = line.rsplit(",", 2)
                sites.append({"name": name.strip(), "lat": float(lat), "lon": float(lon)})
            except ValueError:
                print("Skipping line (use: Name, latitude, longitude): %s" % line, file=sys.stderr)
    return sites


def fetch_current(site):
    params = {
        "latitude": site["lat"],
        "longitude": site["lon"],
        "current": "temperature_2m,weather_code,is_day,wind_speed_10m,precipitation",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timezone": "America/Chicago",
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                c = json.load(r)["current"]
            return {
                "temp_f": round(c["temperature_2m"]),
                "conditions": label(c["weather_code"], c["is_day"]),
                "wind_mph": round(c["wind_speed_10m"]),
                "precip_in": round(c["precipitation"], 2),
            }
        except Exception as e:
            last = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(last)


def load_readings():
    if os.path.exists(JSON_PATH):
        with open(JSON_PATH, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_readings(readings):
    os.makedirs(DATA_DIR, exist_ok=True)
    readings.sort(key=lambda r: (r["date"], r["time"], r["site"]))
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(readings, f, indent=0)
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(readings)


def main(now=None, force=None):
    now = now or datetime.now(TZ)
    if force is None:
        force = os.environ.get("FORCE", "").lower() in ("1", "true", "yes")
    if force:
        hour = min(SLOTS, key=lambda h: abs(h - now.hour))  # nearest slot
    else:
        # Accept the slot hour or the hour after it (covers late GitHub start times).
        hour = next((h for h in SLOTS if now.hour in (h, h + 1)), None)
        if hour is None:
            print("Not a logging hour in Central time (%s). Nothing to do." % now.strftime("%H:%M"))
            return 0
    slot, date = SLOTS[hour], now.strftime("%Y-%m-%d")

    sites = load_sites()
    if not sites:
        print("No sites found in sites.txt", file=sys.stderr)
        return 1

    readings = load_readings()
    have = {(r["date"], r["slot"], r["site"]) for r in readings}
    new, failed = [], 0
    for site in sites:
        key = (date, slot, site["name"])
        if key in have and not force:
            print("Already logged: %s (%s)" % (site["name"], slot))
            continue
        try:
            row = {"date": date, "slot": slot, "time": now.strftime("%H:%M"), "site": site["name"]}
            row.update(fetch_current(site))
            new.append(row)
            print("OK   %s: %s F, %s" % (site["name"], row["temp_f"], row["conditions"]))
        except Exception as e:
            failed += 1
            print("FAIL %s: %s" % (site["name"], e), file=sys.stderr)

    if new:
        replaced = {(r["date"], r["slot"], r["site"]) for r in new}
        readings = [r for r in readings if (r["date"], r["slot"], r["site"]) not in replaced] + new
        save_readings(readings)
    return 1 if failed and not new else 0


if __name__ == "__main__":
    sys.exit(main())
