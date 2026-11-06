"""Download the public data needed for one forecast and build an hourly panel (Europe/Madrid local time).

Sources
- OMIE: day-ahead marginal price for Spain (marginalpdbc daily files, hourly or 15-min).
- Energy-Charts (Fraunhofer ISE): realized generation by technology and day-ahead forecasts
  of wind, solar and load for Spain.
- MIBGAS: PVB day-ahead average gas price index.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import time
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

TZ = ZoneInfo("Europe/Madrid")
UA = {"User-Agent": "Mozilla/5.0 (timestral-luz research; github actions)"}
S = requests.Session()
S.headers.update(UA)


def _get(url: str, tries: int = 4, timeout: int = 60) -> requests.Response | None:
    for i in range(tries):
        try:
            r = S.get(url, timeout=timeout)
            if r.status_code == 200:
                return r
        except requests.RequestException:
            pass
        time.sleep(2 * (i + 1))
    return None


# ---------------------------------------------------------------- OMIE
def omie_day(day: dt.date) -> list[tuple[str, int, float]] | None:
    url = ("https://www.omie.es/es/file-download?parents=marginalpdbc"
           f"&filename=marginalpdbc_{day:%Y%m%d}.1")
    r = _get(url)
    if r is None or not r.text.startswith("MARGINALPDBC"):
        return None
    recs = [l.split(";") for l in r.text.splitlines()[1:] if l[:2] == "20" and l.count(";") >= 5]
    per_hour = 4 if len(recs) > 30 else 1
    out = []
    for rec in recs:
        hour = (int(rec[3]) - 1) // per_hour
        out.append((f"{rec[0]}-{rec[1]}-{rec[2]}", hour, float(rec[4])))
    return out


def omie_range(start: dt.date, end: dt.date) -> pd.Series:
    days = [start + dt.timedelta(d) for d in range((end - start).days + 1)]
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(omie_day, days))
    rows = [x for r in res if r for x in r]
    df = pd.DataFrame(rows, columns=["date", "hour", "price"])
    return df.groupby(["date", "hour"]).price.mean()


# ---------------------------------------------------------------- Energy-Charts
def _hourly(unix, vals, name) -> pd.Series:
    ts = pd.to_datetime(np.array(unix), unit="s", utc=True).tz_convert("Europe/Madrid")
    s = pd.DataFrame({"date": ts.strftime("%Y-%m-%d"), "hour": ts.hour,
                      name: np.array([np.nan if v is None else v for v in vals], dtype=float)})
    return s.groupby(["date", "hour"])[name].mean()


def _months(start: dt.date, end: dt.date):
    d = start.replace(day=1)
    while d <= end:
        e = (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
        yield d, min(e, end)
        d = e + dt.timedelta(days=1)


GEN = {"Fossil gas": "gen_gas", "Nuclear": "gen_nuclear", "Hydro water reservoir": "gen_hydro",
       "Cross border electricity trading": "gen_imports"}


def ec_actuals(start: dt.date, end: dt.date) -> pd.DataFrame:
    parts = {v: [] for v in GEN.values()}
    for a, b in _months(start, end):
        r = _get(f"https://api.energy-charts.info/public_power?country=es&start={a}&end={b}")
        if r is None:
            continue
        d = r.json()
        for p in d.get("production_types", []):
            if p["name"] in GEN:
                parts[GEN[p["name"]]].append(_hourly(d["unix_seconds"], p["data"], GEN[p["name"]]))
        time.sleep(0.5)
    cols = {}
    for k, v in parts.items():
        if v:
            s = pd.concat(v)
            cols[k] = s[~s.index.duplicated(keep="last")]
    return pd.DataFrame(cols)


FC = {"wind_onshore": "fc_wind", "solar": "fc_solar", "load": "fc_load"}


def ec_forecasts(start: dt.date, end: dt.date) -> pd.DataFrame:
    cols = {}
    for pt, name in FC.items():
        parts = []
        for a, b in _months(start, end):
            r = _get("https://api.energy-charts.info/public_power_forecast?country=es"
                     f"&production_type={pt}&forecast_type=day-ahead&start={a}&end={b}")
            if r is None:
                continue
            try:
                d = r.json()
            except json.JSONDecodeError:
                continue
            if "unix_seconds" in d:
                parts.append(_hourly(d["unix_seconds"], d["forecast_values"], name))
            time.sleep(0.5)
        if parts:
            s = pd.concat(parts)
            cols[name] = s[~s.index.duplicated(keep="last")]
    return pd.DataFrame(cols)


# ---------------------------------------------------------------- MIBGAS
def mibgas(years: list[int]) -> pd.Series:
    out = []
    for y in years:
        r = _get(f"https://www.mibgas.es/es/file-access/MIBGAS_Data_{y}.xlsx?path=AGNO_{y}/XLS")
        if r is None:
            continue
        d = pd.read_excel(io.BytesIO(r.content), "MIBGAS Indexes")
        col = [c for c in d.columns if "PVB Average Price Index Day-Ahead" in c][0]
        out.append(pd.DataFrame({"date": pd.to_datetime(d["Delivery day"]).dt.strftime("%Y-%m-%d"),
                                 "gas": d[col]}))
    g = pd.concat(out).dropna().drop_duplicates("date", keep="first")
    return g.set_index("date").gas


# ---------------------------------------------------------------- Open-Meteo weather forecast
# Energy-Charts publishes the day-ahead wind/solar forecast for Spain too late to use before the
# auction, so the live run also builds its own proxy from a public weather forecast.
WIND_SITES = [(43.0, -7.9), (42.3, -3.7), (41.6, -1.0), (42.7, -1.6), (36.2, -5.7)]   # Galicia, Burgos, Zaragoza, Navarra, Cádiz
SOLAR_SITES = [(38.9, -6.9), (39.0, -3.9), (37.4, -6.0), (38.0, -1.1)]               # Badajoz, Ciudad Real, Sevilla, Murcia
TEMP_SITES = [(40.4, -3.7), (41.4, 2.2), (37.4, -6.0), (39.5, -0.4)]                # Madrid, Barcelona, Sevilla, Valencia


def openmeteo(past_days: int = 92) -> pd.DataFrame:
    """Hourly site-averaged weather (local time) for the last `past_days` days plus the next 2 days."""
    out = {}
    for name, var, sites in [("wx_wind100", "wind_speed_100m", WIND_SITES),
                             ("wx_radiation", "shortwave_radiation", SOLAR_SITES),
                             ("wx_temp", "temperature_2m", TEMP_SITES)]:
        lat = ",".join(str(a) for a, _ in sites); lon = ",".join(str(b) for _, b in sites)
        r = _get(f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&hourly={var}"
                 f"&past_days={past_days}&forecast_days=3&timezone=Europe%2FMadrid")
        if r is None:
            continue
        data = r.json()
        data = data if isinstance(data, list) else [data]
        series = []
        for loc in data:
            t = pd.to_datetime(loc["hourly"]["time"])
            series.append(pd.Series(np.array(loc["hourly"][var], dtype=float), index=t))
        s = pd.concat(series, axis=1).mean(axis=1)
        if var == "wind_speed_100m":
            s = s.clip(upper=90) ** 3  # turbine output grows ~ with the cube of wind speed (capped)
        df = pd.DataFrame({"date": s.index.strftime("%Y-%m-%d"), "hour": s.index.hour, name: s.values})
        out[name] = df.groupby(["date", "hour"])[name].mean()
        time.sleep(1)
    return pd.DataFrame(out)


# ---------------------------------------------------------------- panel
def build_panel(first: dt.date, last: dt.date, weather: bool = True) -> pd.DataFrame:
    """Hourly panel for [first, last] (local days). Missing values are NaN."""
    dates = pd.date_range(first, last, freq="D").strftime("%Y-%m-%d")
    idx = pd.MultiIndex.from_product([dates, range(24)], names=["date", "hour"])
    P = pd.DataFrame(index=idx)
    P["price"] = omie_range(first, last).reindex(idx)
    P = P.join(ec_forecasts(first, last).reindex(idx))
    P = P.join(ec_actuals(first, last).reindex(idx))
    g = mibgas(sorted({first.year, last.year}))
    P["gas"] = P.index.get_level_values(0).map(g).astype(float)
    P["gas"] = P["gas"].ffill()
    for c in ["fc_wind", "fc_solar", "fc_load", "gen_gas", "gen_nuclear", "gen_hydro", "gen_imports"]:
        if c not in P:
            P[c] = np.nan
    P["fc_resid"] = P.fc_load - P.fc_wind - P.fc_solar
    if weather:
        try:
            P = P.join(openmeteo().reindex(P.index))
        except Exception as e:  # weather is optional
            print("weather skipped:", e)
    for c in ["wx_wind100", "wx_radiation", "wx_temp"]:
        if c not in P:
            P[c] = np.nan
    return P


def today_madrid() -> dt.date:
    return dt.datetime.now(TZ).date()
