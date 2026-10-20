#!/usr/bin/env python3
"""
Job site weather logger (runs automatically on GitHub Actions).

Only sites marked with ", history" in sites.txt are logged. For each of them it
builds one record per day from hourly weather data (Central time):

  overnight_rain_in   rain from 17:01 the previous day to 08:00
  temp_8am_f          temperature at 08:00
  am_conditions       Sunny / Partly cloudy / Cloudy (average cloud cover 08:00-11:59),
                      or Rain (or Snow) if any precipitation fell in that window
  am_rain_in          rain from 08:01 to 11:59
  temp_12pm_f         temperature at 12:00
  pm_conditions       same method for 12:00-16:59
  pm_rain_in          rain from 12:01 to 16:59
  temp_5pm_f          temperature at 17:00

Each run fills in whatever parts of today (and yesterday) are complete, so it is
safe to run any time, and a missed run is caught up by the next one.
Results are saved to data/daily.json and data/daily.csv.
"""
import csv
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Chicago")
HERE = os.path.dirname(os.path.abspath(__file__))
SITES_PATH = os.path.join(HERE, "sites.txt")
DATA_DIR = os.path.join(HERE, "data")
JSON_PATH = os.path.join(DATA_DIR, "daily.json")
CSV_PATH = os.path.join(DATA_DIR, "daily.csv")
FIELDS = ["date", "site", "overnight_rain_in", "temp_8am_f", "am_conditions",
          "am_rain_in", "temp_12pm_f", "pm_conditions", "pm_rain_in"]

RAIN_MIN = 0.01                 # inches; anything at or above this counts as rain
SUNNY_BELOW, CLOUDY_FROM = 25, 70   # average cloud cover (%) cut-offs
SNOW_CODES = {71, 73, 75, 77, 85, 86}
FLAG = re.compile(r",\s*(history|log|yes)\s*$", re.I)


def load_sites():
    sites = []
    with open(SITES_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            history = bool(FLAG.search(line))
            line = FLAG.sub("", line)
            try:
                name, lat, lon = line.rsplit(",", 2)
                sites.append({"name": name.strip(), "lat": float(lat), "lon": float(lon), "history": history})
            except ValueError:
                print("Skipping line (use: Name, latitude, longitude): %s" % line, file=sys.stderr)
    return sites


def fetch_hourly(site, past_days):
    params = {
        "latitude": site["lat"], "longitude": site["lon"],
        "hourly": "temperature_2m,precipitation,cloud_cover,weather_code",
        "temperature_unit": "fahrenheit", "precipitation_unit": "inch",
        "past_days": past_days, "forecast_days": 1, "timezone": "America/Chicago",
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                h = json.load(r)["hourly"]
            return {t: {"t": h["temperature_2m"][i], "p": h["precipitation"][i],
                        "c": h["cloud_cover"][i], "w": h["weather_code"][i]}
                    for i, t in enumerate(h["time"])}
        except Exception as e:
            last = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(last)


def at(d, h):
    return datetime(d.year, d.month, d.day, h)


def stamps(start, end):
    out, dt = [], start
    while dt <= end:
        out.append(dt.strftime("%Y-%m-%dT%H:00"))
        dt += timedelta(hours=1)
    return out


def col(H, keys, field):
    vals = [H.get(k, {}).get(field) for k in keys]
    return None if any(v is None for v in vals) else vals


def classify(precip, cloud, codes):
    if sum(precip) >= RAIN_MIN or any(p >= RAIN_MIN for p in precip):
        return "Snow" if any(c in SNOW_CODES for c in codes) else "Rain"
    avg = sum(cloud) / len(cloud)
    return "Sunny" if avg < SUNNY_BELOW else ("Partly cloudy" if avg < CLOUDY_FROM else "Cloudy")


def window(H, d, rain_hours, cloud_hours):
    """Rain uses hourly totals stamped at the END of each hour; cloud/codes are readings at the hour."""
    p = col(H, stamps(at(d, rain_hours[0]), at(d, rain_hours[1])), "p")
    c = col(H, stamps(at(d, cloud_hours[0]), at(d, cloud_hours[1])), "c")
    w = col(H, stamps(at(d, rain_hours[0]), at(d, rain_hours[1])), "w") or []
    if p is None or c is None:
        return None
    return round(sum(p), 2), classify(p, c, w)


def compute_day(H, d, now):
    """Return only the fields whose time windows have fully passed."""
    rec = {}
    prev = d - timedelta(days=1)
    if now >= at(d, 8):
        p = col(H, stamps(at(prev, 18), at(d, 8)), "p")      # 17:01 yesterday -> 08:00
        if p is not None:
            rec["overnight_rain_in"] = round(sum(p), 2)
        t = col(H, stamps(at(d, 8), at(d, 8)), "t")
        if t is not None:
            rec["temp_8am_f"] = round(t[0])
    if now >= at(d, 12):
        am = window(H, d, (9, 12), (8, 11))                  # 08:01 -> 11:59
        if am:
            rec["am_rain_in"], rec["am_conditions"] = am
        t = col(H, stamps(at(d, 12), at(d, 12)), "t")
        if t is not None:
            rec["temp_12pm_f"] = round(t[0])
    if now >= at(d, 17):
        pm = window(H, d, (13, 17), (12, 16))                # 12:01 -> 16:59
        if pm:
            rec["pm_rain_in"], rec["pm_conditions"] = pm
    return rec


def load_records():
    if os.path.exists(JSON_PATH):
        with open(JSON_PATH, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_records(recs):
    os.makedirs(DATA_DIR, exist_ok=True)
    recs.sort(key=lambda r: (r["date"], r["site"]))
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(recs, f, indent=0)
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, restval="")
        w.writeheader()
        w.writerows(recs)


def main(now=None, backfill=None):
    now = now or datetime.now(TZ)
    local = now.replace(tzinfo=None)
    if backfill is None:
        try:
            backfill = int(os.environ.get("BACKFILL_DAYS", "0") or 0)
        except ValueError:
            backfill = 0
    n = max(2, min(backfill + 1, 91))          # number of days to (re)compute, ending today

    sites = [s for s in load_sites() if s["history"]]
    if not sites:
        print('No sites are marked for history. Add ", history" to a line in sites.txt.')
        return 0

    by_key = {(r["date"], r["site"]): r for r in load_records()}
    ok = failed = 0
    for site in sites:
        try:
            H = fetch_hourly(site, n)
        except Exception as e:
            failed += 1
            print("FAIL %s: %s" % (site["name"], e), file=sys.stderr)
            continue
        ok += 1
        for i in range(n):
            d = (local - timedelta(days=i)).date()
            rec = compute_day(H, d, local)
            if rec:
                key = (d.strftime("%Y-%m-%d"), site["name"])
                by_key.setdefault(key, {"date": key[0], "site": key[1]}).update(rec)
        print("OK   %s" % site["name"])
    if ok:
        save_records(list(by_key.values()))
    print("Updated %d site(s)." % ok)
    return 1 if failed and not ok else 0


if __name__ == "__main__":
    sys.exit(main())
