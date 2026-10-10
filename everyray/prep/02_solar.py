"""Milestone 2: get PVGIS hourly PV yield for Catarroja (1 kWp reference system).

Queries the PVGIS (EU JRC) seriescalc API for hourly PV power output of a
1 kWp system at lat 39.40, lon -0.40 (Catarroja), tilt 30 deg, south-facing,
using PVGIS's PVGIS-SARAH2 satellite year 2020 (most recent year available
in API v5.2) as the "typical year" reference.

Raw response is cached in prep/cache/pvgis_hourly.json.
Writes web/data/solar_hourly.json: one row per hour of the year with
  - hour_of_year (0-8783 for the 2020 leap year)
  - month, day, hour
  - kwh_per_kwp: yield for that hour, per kWp installed
  - temp_c: ambient temperature (T2m), reused later for load simulation
Also writes web/data/solar_monthly_preview.png, a quick bar chart of monthly
yield per kWp, to visually check the summer/winter difference.
"""

import json
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import requests

HERE = Path(__file__).resolve().parent
CACHE_DIR = HERE / "cache"
WEB_DATA_DIR = HERE.parent / "web" / "data"

PVGIS_URL = "https://re.jrc.ec.europa.eu/api/v5_2/seriescalc"
HEADERS = {"User-Agent": "everyray-demo/0.1 (student pitch demo; contact: yanis.blin.45@gmail.com)"}

LAT, LON = 39.40, -0.40
TILT_DEG = 30
AZIMUTH_DEG = 0  # PVGIS convention: 0 = south
PVGIS_YEAR = 2020  # latest year available in PVGIS-SARAH2 for API v5.2


def fetch_cached() -> dict:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / "pvgis_hourly.json"
    if cache_path.exists():
        print(f"  using cached {cache_path.name}")
        return json.loads(cache_path.read_text())

    print("  requesting hourly PV series from PVGIS...")
    params = dict(
        lat=LAT, lon=LON,
        startyear=PVGIS_YEAR, endyear=PVGIS_YEAR,
        pvcalculation=1, peakpower=1, loss=14,
        angle=TILT_DEG, aspect=AZIMUTH_DEG,
        outputformat="json",
    )
    resp = requests.get(PVGIS_URL, params=params, headers=HEADERS, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    cache_path.write_text(json.dumps(data))
    print(f"  cached to {cache_path.name} ({len(data['outputs']['hourly'])} hourly rows)")
    return data


def main():
    print("Fetching PVGIS hourly PV yield (1 kWp reference, 30 deg tilt, south)...")
    data = fetch_cached()
    rows = data["outputs"]["hourly"]

    df = pd.DataFrame(rows)
    # time format: "YYYYMMDD:HHMM" (always HH10, i.e. the reading represents hour HH)
    ts = pd.to_datetime(df["time"], format="%Y%m%d:%H%M")
    df["month"] = ts.dt.month
    df["day"] = ts.dt.day
    df["hour"] = ts.dt.hour
    df["hour_of_year"] = range(len(df))
    # P is average power in W for a 1 kWp system over the hour -> Wh = P * 1h -> kWh = P/1000
    df["kwh_per_kwp"] = df["P"] / 1000.0
    df["temp_c"] = df["T2m"]

    out = df[["hour_of_year", "month", "day", "hour", "kwh_per_kwp", "temp_c"]]

    WEB_DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = WEB_DATA_DIR / "solar_hourly.json"
    out.to_json(out_path, orient="records")

    annual_kwh_per_kwp = out["kwh_per_kwp"].sum()
    monthly = out.groupby("month")["kwh_per_kwp"].sum()
    summer_months = [6, 7, 8]
    winter_months = [12, 1, 2]
    summer_avg = monthly.loc[summer_months].mean()
    winter_avg = monthly.loc[winter_months].mean()

    # --- quick monthly preview plot ---
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.bar(monthly.index, monthly.values, color="#7DBE82")
    ax.set_xticks(range(1, 13))
    ax.set_xlabel("month")
    ax.set_ylabel("kWh / kWp")
    ax.set_title(f"Catarroja PVGIS monthly PV yield per kWp — {PVGIS_YEAR}, annual={annual_kwh_per_kwp:.0f} kWh/kWp")
    preview_path = WEB_DATA_DIR / "solar_monthly_preview.png"
    fig.savefig(preview_path, dpi=150, bbox_inches="tight")
    plt.close(fig)

    print("\n=== Milestone 2 check ===")
    print(f"Hourly rows: {len(out)} (year {PVGIS_YEAR})")
    print(f"Annual yield: {annual_kwh_per_kwp:.0f} kWh/kWp (spec expects ~1,500 kWh/kWp for Valencia area)")
    print(f"Summer (Jun-Aug) monthly avg: {summer_avg:.1f} kWh/kWp")
    print(f"Winter (Dec-Feb) monthly avg: {winter_avg:.1f} kWh/kWp")
    print(f"Summer/winter ratio: {summer_avg / winter_avg:.2f}x")
    print("\nMonthly yield (kWh/kWp):")
    print(monthly.round(1).to_string())
    print(f"\nWrote {out_path}")
    print(f"Wrote {preview_path}")


if __name__ == "__main__":
    main()
