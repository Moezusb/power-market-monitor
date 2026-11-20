#!/usr/bin/env python3
"""Fetch grid data for North American power markets and write data/power.json.

US ISOs   EIA Open Data API v2 (Hourly Electric Grid Monitor, form EIA-930).
          Needs EIA_API_KEY in the environment (a GitHub Actions secret).
Alberta   AESO public ETS reports (no key):
          - Current Supply Demand report: live generation by fuel, AIL, interchange
          - Actual/Forecast report: hourly pool price and Alberta Internal Load
          Fuel-mix history for Alberta is built up from hourly snapshots.

All timestamps are written as UTC hours "YYYY-MM-DDTHH".
Stdlib only.
"""
import csv
import html
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

OUT = os.path.join(os.path.dirname(__file__), "..", "data", "power.json")
UA = "Mozilla/5.0 (power-market-monitor; +https://github.com/Moezusb/power-market-monitor)"
KEY = os.environ.get("EIA_API_KEY", "").strip()
DEBUG = os.environ.get("DEBUG") == "1"

MIX_DAYS = 8       # hourly fuel mix kept
DEMAND_DAYS = 31   # hourly demand kept (for 30-day peak)

US_MARKETS = [
    {"id": "ERCO", "label": "ERCOT", "name": "Electric Reliability Council of Texas", "region": "Texas", "tz": "America/Chicago"},
    {"id": "CISO", "label": "CAISO", "name": "California ISO", "region": "California", "tz": "America/Los_Angeles"},
    {"id": "PJM", "label": "PJM", "name": "PJM Interconnection", "region": "Mid-Atlantic and Midwest", "tz": "America/New_York"},
    {"id": "MISO", "label": "MISO", "name": "Midcontinent ISO", "region": "Central US", "tz": "America/Chicago"},
    {"id": "SWPP", "label": "SPP", "name": "Southwest Power Pool", "region": "Great Plains", "tz": "America/Chicago"},
    {"id": "NYIS", "label": "NYISO", "name": "New York ISO", "region": "New York", "tz": "America/New_York"},
    {"id": "ISNE", "label": "ISO-NE", "name": "ISO New England", "region": "New England", "tz": "America/New_York"},
]

# EIA fuel codes -> display groups
FUEL_GROUP = {
    "NG": "gas", "NUC": "nuclear", "WND": "wind", "SUN": "solar", "SNB": "solar",
    "WAT": "hydro", "COL": "coal", "OIL": "oil",
    "BAT": "storage", "PS": "storage", "OES": "storage",
    "GEO": "other", "OTH": "other", "UNK": "other", "UES": "storage",
}
GROUPS = ["gas", "nuclear", "coal", "wind", "solar", "hydro", "storage", "oil", "other"]


def get(url, tries=4, timeout=60):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8-sig", errors="replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"GET failed {re.sub(r'api_key=[^&]+', 'api_key=***', url)}: {last}")


def hour_str(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H")


# ------------------------------------------------------------------- EIA
def eia(route, facets, start, extra=None):
    params = [
        ("api_key", KEY), ("frequency", "hourly"), ("data[0]", "value"),
        ("start", start), ("sort[0][column]", "period"), ("sort[0][direction]", "asc"),
        ("length", "5000"),
    ]
    for k, vals in facets.items():
        for v in vals:
            params.append((f"facets[{k}][]", v))
    rows, offset = [], 0
    while True:
        q = urllib.parse.urlencode(params + [("offset", str(offset))])
        j = json.loads(get(f"https://api.eia.gov/v2/{route}/data/?{q}"))
        resp = j.get("response", {})
        batch = resp.get("data", [])
        rows.extend(batch)
        total = int(resp.get("total", len(rows)) or 0)
        offset += len(batch)
        if not batch or offset >= total:
            break
    return rows


def fetch_us_market(m, now):
    mix_start = hour_str(now - timedelta(days=MIX_DAYS))
    dem_start = hour_str(now - timedelta(days=DEMAND_DAYS))

    fuel_rows = eia("electricity/rto/fuel-type-data", {"respondent": [m["id"]]}, mix_start)
    region_rows = eia("electricity/rto/region-data",
                      {"respondent": [m["id"]], "type": ["D", "DF", "NG", "TI"]}, dem_start)
    if DEBUG:
        print("   fuel codes:", sorted({r.get("fueltype") for r in fuel_rows}))

    mix = {}  # period -> {group: MWh}
    for r in fuel_rows:
        v = r.get("value")
        if v is None:
            continue
        try:
            v = float(v)
        except ValueError:
            continue
        g = FUEL_GROUP.get(r.get("fueltype"), "other")
        p = r["period"]
        mix.setdefault(p, {})
        mix[p][g] = mix[p].get(g, 0.0) + v

    series = {"D": {}, "DF": {}, "NG": {}, "TI": {}}
    for r in region_rows:
        t = r.get("type")
        if t in series and r.get("value") is not None:
            try:
                series[t][r["period"]] = float(r["value"])
            except ValueError:
                pass

    print(f"   {m['label']}: {len(mix)} mix hours, {len(series['D'])} demand hours")
    return pack_market(m, mix, series)


def pack_market(m, mix, series, extra=None):
    mix_hours = sorted(mix)
    # drop trailing hours where the fuel mix is clearly incomplete (<60% of median total)
    totals = [sum(max(0, v) for v in mix[h].values()) for h in mix_hours]
    if totals:
        med = sorted(totals)[len(totals) // 2]
        while mix_hours and totals and totals[-1] < 0.6 * med:
            mix_hours.pop()
            totals.pop()
    dem_hours = sorted(set(series["D"]) | set(series["DF"]))
    out = {
        **{k: m[k] for k in ("id", "label", "name", "region", "tz") if k in m},
        "mix": {
            "hours": mix_hours,
            **{g: [round(mix[h].get(g, 0.0)) for h in mix_hours] for g in GROUPS},
        },
        "demand": {
            "hours": dem_hours,
            "actual": [rnd(series["D"].get(h)) for h in dem_hours],
            "forecast": [rnd(series["DF"].get(h)) for h in dem_hours],
            "netgen": [rnd(series["NG"].get(h)) for h in dem_hours],
            "interchange": [rnd(series["TI"].get(h)) for h in dem_hours],
        },
    }
    # drop fuel groups that are zero everywhere
    for g in GROUPS:
        if not any(out["mix"][g]):
            del out["mix"][g]
    if extra:
        out.update(extra)
    return out


def rnd(v):
    return None if v is None else round(v)


# ------------------------------------------------------------------ AESO
class Cells(HTMLParser):
    """Collect table rows as lists of cell text."""

    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell, self.in_cell = [], None, [], False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.in_cell, self.cell = True, []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.row is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.in_cell = False
        elif tag == "tr" and self.row is not None:
            if any(self.row):
                self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.in_cell:
            self.cell.append(data)


AESO_FUEL = {
    "GAS": "gas", "HYDRO": "hydro", "ENERGY STORAGE": "storage", "SOLAR": "solar",
    "WIND": "wind", "BIOMASS AND OTHER": "other", "OTHER": "other",
    "DUAL FUEL": "gas", "COAL": "coal",
}
EDMONTON = ZoneInfo("America/Edmonton")


def to_num(s):
    s = (s or "").replace(",", "").replace("$", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def fetch_aeso_csd():
    text = get("http://ets.aeso.ca/ets_web/ip/Market/Reports/CSDReportServlet")
    p = Cells()
    p.feed(text)
    rows = p.rows
    if DEBUG:
        for r in rows[:80]:
            print("   CSD:", r)
    summary, gen = {}, {}
    for r in rows:
        if len(r) == 2:
            v = to_num(r[1])
            if v is not None:
                summary[r[0].strip()] = v
        if len(r) >= 3:
            label = r[0].strip().upper()
            if label in AESO_FUEL:
                tng = to_num(r[2])  # columns: GROUP, MC, TNG, DCR
                if tng is not None:
                    g = AESO_FUEL[label]
                    gen[g] = gen.get(g, 0.0) + tng
    m = re.search(r"Last Update\s*:\s*([A-Za-z]{3} \d{1,2}, \d{4} \d{1,2}:\d{2})", html.unescape(text))
    stamp = None
    if m:
        try:
            stamp = datetime.strptime(m.group(1), "%b %d, %Y %H:%M").replace(tzinfo=EDMONTON)
        except ValueError:
            pass
    print(f"   AESO CSD: summary keys={list(summary)[:8]} gen={gen} stamp={stamp}")
    return summary, gen, stamp


def fetch_aeso_hourly(now):
    """Hourly pool price and AIL (actual + day-ahead forecast)."""
    local_now = now.astimezone(EDMONTON)
    begin = (local_now - timedelta(days=DEMAND_DAYS)).strftime("%m%d%Y")
    end = (local_now + timedelta(days=1)).strftime("%m%d%Y")
    url = ("http://ets.aeso.ca/ets_web/ip/Market/Reports/ActualForecastWMRQHReportServlet"
           f"?beginDate={begin}&endDate={end}&contentType=csv")
    text = get(url)
    lines = text.splitlines()
    hdr_i = next((i for i, l in enumerate(lines) if "Pool Price" in l and "Date" in l), None)
    if hdr_i is None:
        print("   AESO hourly: header not found; first lines:", lines[:6])
        return {}
    reader = csv.reader(io.StringIO("\n".join(lines[hdr_i:])))
    header = [h.strip() for h in next(reader)]
    if DEBUG:
        print("   AESO hourly header:", header)

    def col(*names):
        for n in names:
            for i, h in enumerate(header):
                if n.lower() in h.lower():
                    return i
        return None

    i_date = col("Date")
    i_price = col("Actual Posted Pool Price", "Actual Pool Price")
    i_fprice = col("Forecast Pool Price")
    i_ail = col("Actual AIL")
    i_fail = col("Day Ahead Forecasted AIL", "Forecasted AIL")
    out = {"D": {}, "DF": {}, "price": {}, "price_fc": {}}
    seen = set()
    for r in reader:
        if not r or i_date is None or len(r) <= i_date:
            continue
        m = re.match(r"\s*(\d{2})/(\d{2})/(\d{4}) (\d{2})(\*?)", r[i_date])
        if not m:
            continue
        mo, dd, yy, he, star = m.groups()
        base = datetime(int(yy), int(mo), int(dd), tzinfo=EDMONTON)
        # hour-ending HE -> interval start; EIA convention labels by hour ending,
        # so we label by hour ending too (HE01 = 00:00-01:00 local, label 01:00).
        local = base + timedelta(hours=int(he))
        utc = local.astimezone(timezone.utc)
        if star:  # repeated hour at DST fall-back
            utc += timedelta(hours=1)
        key = hour_str(utc)
        if key in seen:
            continue
        seen.add(key)
        for name, idx in (("D", i_ail), ("DF", i_fail), ("price", i_price), ("price_fc", i_fprice)):
            if idx is not None and idx < len(r):
                v = to_num(r[idx])
                if v is not None:
                    out[name][key] = v
    print(f"   AESO hourly: {len(out['D'])} AIL hours, {len(out['price'])} price hours")
    return out


def fetch_aeso(now, prev):
    m = {"id": "AESO", "label": "AESO", "name": "Alberta Electric System Operator",
         "region": "Alberta", "tz": "America/Edmonton"}
    summary, gen, stamp = fetch_aeso_csd()
    hourly = {}
    try:
        hourly = fetch_aeso_hourly(now)
    except Exception as e:  # noqa: BLE001
        print("   AESO hourly failed:", e)

    # Fuel-mix history: carry forward previous snapshots, add the current one
    prev_aeso = next((x for x in prev.get("markets", []) if x.get("id") == "AESO"), None)
    mix = {}
    if prev_aeso:
        ph = prev_aeso["mix"]["hours"]
        for i, h in enumerate(ph):
            mix[h] = {g: prev_aeso["mix"][g][i] for g in GROUPS if g in prev_aeso["mix"]}
    if gen:
        snap_hour = hour_str((stamp or now))
        mix[snap_hour] = gen
    cutoff = hour_str(now - timedelta(days=MIX_DAYS))
    mix = {h: v for h, v in mix.items() if h >= cutoff}

    series = {"D": hourly.get("D", {}), "DF": hourly.get("DF", {}), "NG": {}, "TI": {}}
    # The CSD gives live net generation and interchange; attach them to the snapshot hour
    def pick(*keys):
        for k in summary:
            if any(key.lower() in k.lower() for key in keys):
                return summary[k]
        return None

    live = {
        "ail": pick("Alberta Internal Load"),
        "netgen": pick("Total Net Generation"),
        "interchange": pick("Net Actual Interchange"),
        "asof": stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ") if stamp else None,
    }
    price_hours = sorted(set(hourly.get("price", {})) | set(hourly.get("price_fc", {})))
    extra = {
        "live": live,
        "price": {
            "hours": price_hours,
            "actual": [hourly["price"].get(h) for h in price_hours] if price_hours else [],
            "forecast": [hourly["price_fc"].get(h) for h in price_hours] if price_hours else [],
            "unit": "CAD/MWh",
        },
        "mixNote": "Alberta fuel mix is sampled hourly from the AESO Current Supply Demand report.",
    }
    return pack_market(m, mix, series, extra)


# ------------------------------------------------------------------ main
def main():
    now = datetime.now(timezone.utc)
    prev = {}
    if os.path.exists(OUT):
        with open(OUT) as f:
            prev = json.load(f)
    prev_by_id = {x["id"]: x for x in prev.get("markets", [])}

    markets, status = [], {}
    if not KEY:
        print("EIA_API_KEY not set: skipping US markets (keeping previous data if any)")
    for m in US_MARKETS:
        print("Fetching", m["label"])
        if not KEY:
            if m["id"] in prev_by_id:
                markets.append(prev_by_id[m["id"]])
            status[m["id"]] = "no-key"
            continue
        try:
            markets.append(fetch_us_market(m, now))
            status[m["id"]] = "ok"
        except Exception as e:  # noqa: BLE001
            print("   FAILED:", e)
            status[m["id"]] = "error"
            if m["id"] in prev_by_id:
                markets.append(prev_by_id[m["id"]])

    print("Fetching AESO")
    try:
        markets.append(fetch_aeso(now, prev))
        status["AESO"] = "ok"
    except Exception as e:  # noqa: BLE001
        print("   FAILED:", e)
        status["AESO"] = "error"
        if "AESO" in prev_by_id:
            markets.append(prev_by_id["AESO"])

    if not markets:
        print("No data; not writing")
        sys.exit(1)

    out = {
        "generated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": status,
        "groups": GROUPS,
        "markets": markets,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"Wrote {OUT} ({os.path.getsize(OUT)//1024} KB) status={status}")


if __name__ == "__main__":
    main()
