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

Rain amounts (and the Rain/Snow call) come from the NOAA radar-and-gauge estimate
(Stage IV, via Iowa State's Mesonet). The same periods from the Open-Meteo weather
model are kept in separate model_* columns, after a blank column, for comparison.
Radar data arrives about an hour late and can be revised, so each run recomputes the
last three days. While some hours of a period are still missing, the radar value holds the
hours that are in and the period is listed in radar_pending (shown as a "+" on the page);
a later run replaces it with the final total.
Temperatures and cloud cover (Sunny / Partly cloudy / Cloudy) are from the weather model.

Each run fills in whatever parts of the recent days are complete, so it is
safe to run any time, and a missed run is caught up by the next one.
Results are saved to data/daily.json, data/daily.csv and data/daily.xlsx
(the Excel file has one sheet per site).
"""
import csv
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from xml.sax.saxutils import escape as xesc
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Chicago")
HERE = os.path.dirname(os.path.abspath(__file__))
SITES_PATH = os.path.join(HERE, "sites.txt")
DATA_DIR = os.path.join(HERE, "data")
JSON_PATH = os.path.join(DATA_DIR, "daily.json")
CSV_PATH = os.path.join(DATA_DIR, "daily.csv")
FIELDS = ["date", "site", "overnight_rain_in", "temp_8am_f", "am_conditions",
          "am_rain_in", "temp_12pm_f", "pm_conditions", "pm_rain_in", "temp_5pm_f",
          "radar_pending",   # periods whose radar total is still missing its latest hour(s)
          "",   # blank separator column: everything to its right is weather-model data
          "model_overnight_rain_in", "model_am_rain_in", "model_pm_rain_in"]
MODEL_RAIN = {"overnight_rain_in": "model_overnight_rain_in", "am_rain_in": "model_am_rain_in",
              "pm_rain_in": "model_pm_rain_in"}
RADAR_URL = "https://mesonet.agron.iastate.edu/json/stage4.py"
XLSX_PATH = os.path.join(DATA_DIR, "daily.xlsx")
RECHECK_DAYS = 3                # days recomputed on every run so late/revised radar data is picked up

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


def fetch_radar(site, n, local_now):
    """Hourly radar+gauge rain (inches) keyed like the model data: Central-time stamp of the END of each hour.
    A value of None means that hour is not available yet. Returns {} if the service cannot be reached."""
    first = datetime(local_now.year, local_now.month, local_now.day) - timedelta(days=n + 1)
    d0 = first.replace(tzinfo=TZ).astimezone(timezone.utc).date()
    d1 = datetime.now(timezone.utc).date()
    out = {}
    d = d0
    while d <= d1:
        params = urllib.parse.urlencode({"valid": d.strftime("%Y-%m-%d"), "lat": site["lat"], "lon": site["lon"]})
        j, last = None, None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(RADAR_URL + "?" + params, timeout=60) as r:
                    j = json.load(r)
                break
            except Exception as e:
                last = e
                time.sleep(5 * (attempt + 1))
        if j is None:
            print("radar unavailable for %s (%s): %s" % (site["name"], d, last), file=sys.stderr)
            return {}
        for e in j.get("data", []):
            t = datetime.fromisoformat(e["end_valid"].replace("Z", "+00:00")).astimezone(TZ).replace(tzinfo=None)
            k = t.strftime("%Y-%m-%dT%H:00")
            v = e.get("precip_in")
            if k in out:   # the repeated hour when clocks fall back: add the two real hours together
                v = None if (v is None or out[k]["p"] is None) else v + out[k]["p"]
            out[k] = {"p": v}
        d += timedelta(days=1)
    return out


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


def colsum(H, keys, field):
    """Sum of the hours that are available. Returns (total, pending) or None when no hour is available yet."""
    vals = [H.get(k, {}).get(field) for k in keys]
    got = [v for v in vals if v is not None]
    return (round(sum(got), 2), len(got) < len(vals)) if got else None


def classify(precip, cloud, codes):
    if sum(precip) >= RAIN_MIN or any(p >= RAIN_MIN for p in precip):
        return "Snow" if any(c in SNOW_CODES for c in codes) else "Rain"
    avg = sum(cloud) / len(cloud)
    return "Sunny" if avg < SUNNY_BELOW else ("Partly cloudy" if avg < CLOUDY_FROM else "Cloudy")


def window(H, R, d, rain_hours, cloud_hours):
    """Returns (model_rain, radar_rain or None, radar_pending, conditions). Rain uses hourly totals stamped at
    the END of each hour; cloud/codes are readings at the hour. The Rain/Snow call uses radar when any is in."""
    keys = stamps(at(d, rain_hours[0]), at(d, rain_hours[1]))
    p = col(H, keys, "p")
    rad = colsum(R, keys, "p")
    c = col(H, stamps(at(d, cloud_hours[0]), at(d, cloud_hours[1])), "c")
    w = col(H, keys, "w") or []
    if p is None or c is None:
        return None
    use = [R[k]["p"] for k in keys if R.get(k, {}).get("p") is not None] if rad else p
    return round(sum(p), 2), (rad[0] if rad else None), (rad[1] if rad else True), classify(use, c, w)


def compute_day(H, R, d, now):
    """Return only the fields whose time windows have fully passed."""
    rec = {}
    pending = []
    prev = d - timedelta(days=1)
    if now >= at(d, 8):
        keys = stamps(at(prev, 18), at(d, 8))                # 17:01 yesterday -> 08:00
        p = col(H, keys, "p")
        if p is not None:
            rec["model_overnight_rain_in"] = round(sum(p), 2)
        rad = colsum(R, keys, "p")
        if rad:
            rec["overnight_rain_in"] = rad[0]
        if not rad or rad[1]:
            pending.append("overnight")
        t = col(H, stamps(at(d, 8), at(d, 8)), "t")
        if t is not None:
            rec["temp_8am_f"] = round(t[0])
    if now >= at(d, 12):
        am = window(H, R, d, (9, 12), (8, 11))               # 08:01 -> 11:59
        if am:
            rec["model_am_rain_in"], radar, pend, rec["am_conditions"] = am
            if radar is not None:
                rec["am_rain_in"] = radar
            if pend:
                pending.append("am")
        t = col(H, stamps(at(d, 12), at(d, 12)), "t")
        if t is not None:
            rec["temp_12pm_f"] = round(t[0])
    if now >= at(d, 17):
        pm = window(H, R, d, (13, 17), (12, 16))             # 12:01 -> 16:59
        if pm:
            rec["model_pm_rain_in"], radar, pend, rec["pm_conditions"] = pm
            if radar is not None:
                rec["pm_rain_in"] = radar
            if pend:
                pending.append("pm")
        t = col(H, stamps(at(d, 17), at(d, 17)), "t")
        if t is not None:
            rec["temp_5pm_f"] = round(t[0])
    if rec:
        rec["radar_pending"] = ",".join(pending)             # empty once every finished period is final
    return rec


def load_records():
    if not os.path.exists(JSON_PATH):
        return []
    with open(JSON_PATH, encoding="utf-8") as f:
        recs = json.load(f)
    # records written before radar was added held weather-model rain in the main columns:
    # move it to the model_* columns (a radar value fills the main column when it is computed)
    for rec in recs:
        for main, model in MODEL_RAIN.items():
            if model not in rec and main in rec:
                rec[model] = rec.pop(main)
    return recs


# (record key, column heading, style, width) for the Excel sheets; style 0 plain, 2 = 0.00, 3 = date
XLSX_COLS = [("date", "Date", 3, 12), ("overnight_rain_in", "Overnight rain (in)", 2, 12), ("temp_8am_f", "8:00 temp (F)", 0, 10),
             ("am_conditions", "AM conditions", 0, 15), ("am_rain_in", "AM rain (in)", 2, 10), ("temp_12pm_f", "12:00 temp (F)", 0, 10),
             ("pm_conditions", "PM conditions", 0, 15), ("pm_rain_in", "PM rain (in)", 2, 10), ("temp_5pm_f", "17:00 temp (F)", 0, 10),
             ("radar_pending", "Radar still updating", 0, 14), (None, "", 0, 3),
             ("model_overnight_rain_in", "MODEL overnight rain (in)", 2, 14), ("model_am_rain_in", "MODEL AM rain (in)", 2, 12),
             ("model_pm_rain_in", "MODEL PM rain (in)", 2, 12)]


def col_letter(i):
    return chr(ord("A") + i)


def sheet_xml(rows):
    def cell(ref, v, style):
        if v is None or v == "":
            return ""
        if style == 3:
            v = (datetime.strptime(v, "%Y-%m-%d") - datetime(1899, 12, 30)).days
        if isinstance(v, (int, float)):
            return '<c r="%s" s="%d"><v>%s</v></c>' % (ref, style, v)
        return '<c r="%s" t="inlineStr"><is><t>%s</t></is></c>' % (ref, xesc(str(v)))
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
           '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
           "<cols>" + "".join('<col min="%d" max="%d" width="%s" customWidth="1"/>' % (i + 1, i + 1, c[3]) for i, c in enumerate(XLSX_COLS)) + "</cols><sheetData>"]
    out.append('<row r="1">' + "".join(cell("%s1" % col_letter(i), c[1], 1) for i, c in enumerate(XLSX_COLS)) + "</row>")
    for n, rec in enumerate(rows, start=2):
        out.append('<row r="%d">' % n + "".join(cell("%s%d" % (col_letter(i), n), rec.get(c[0]) if c[0] else None, c[2])
                                                   for i, c in enumerate(XLSX_COLS)) + "</row>")
    out.append("</sheetData></worksheet>")
    return "".join(out)


def write_xlsx(recs, order):
    """One sheet per site, oldest day first. Built with the standard library so nothing needs installing."""
    names = [n for n in order if any(r["site"] == n for r in recs)]
    names += sorted({r["site"] for r in recs} - set(names))
    used, sheets = set(), []
    for n in names:
        base = re.sub(r"[\\/*?:\[\]]", "-", n)[:31] or "Site"
        t, k = base, 2
        while t.lower() in used:
            t = base[:28] + " %d" % k
            k += 1
        used.add(t.lower())
        sheets.append((t, [r for r in recs if r["site"] == n]))
    if not sheets:
        return
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
              '<numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy\\-mm\\-dd"/></numFmts>'
              '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
              '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
              '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
              '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
              '<cellXfs count="4"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
              '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
              '<xf numFmtId="2" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
              '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>'
              '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
    wb = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>' +
          "".join('<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (xesc(t, {'"': "&quot;"}), i + 1, i + 1) for i, (t, _) in enumerate(sheets)) + "</sheets></workbook>")
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
            "".join('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet%d.xml"/>' % (i + 1, i + 1) for i in range(len(sheets))) +
            '<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>' % (len(sheets) + 1))
    ctypes = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
              '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
              '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
              '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>' +
              "".join('<Override PartName="/xl/worksheets/sheet%d.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' % (i + 1) for i in range(len(sheets))) + "</Types>")
    root = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
    with zipfile.ZipFile(XLSX_PATH, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ctypes)
        z.writestr("_rels/.rels", root)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        z.writestr("xl/styles.xml", styles)
        for i, (_, rows) in enumerate(sheets):
            z.writestr("xl/worksheets/sheet%d.xml" % (i + 1), sheet_xml(rows))


def save_records(recs):
    os.makedirs(DATA_DIR, exist_ok=True)
    recs.sort(key=lambda r: (r["date"], r["site"]))
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(recs, f, indent=0)
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, restval="")
        w.writeheader()
        w.writerows(recs)
    try:
        write_xlsx(recs, [x["name"] for x in load_sites() if x["history"]])
    except Exception as e:   # the Excel copy is a convenience; never let it stop the log from saving
        print("Could not write daily.xlsx: %s" % e, file=sys.stderr)


def main(now=None, backfill=None):
    now = now or datetime.now(TZ)
    local = now.replace(tzinfo=None)
    if backfill is None:
        try:
            backfill = int(os.environ.get("BACKFILL_DAYS", "0") or 0)
        except ValueError:
            backfill = 0
    n = max(RECHECK_DAYS, min(backfill + 1, 91))   # number of days to (re)compute, ending today

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
        R = fetch_radar(site, n, local)
        for i in range(n):
            d = (local - timedelta(days=i)).date()
            rec = compute_day(H, R, d, local)
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
