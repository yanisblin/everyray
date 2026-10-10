"""Shared config, archetype classification and load-shape logic used by both
03_loads.py and 04_economics.py, so the two milestones can't silently diverge."""

import json
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
WEB_DATA_DIR = HERE.parent / "web" / "data"
CONFIG_PATH = HERE.parent / "web" / "config.json"

CONFIG = json.loads(CONFIG_PATH.read_text())
SEED = CONFIG["random_seed"]
L = CONFIG["load"]
Z = CONFIG["zones"]
PS = CONFIG["public_sector"]

RESIDENTIAL_TYPES = {"house", "apartments", "semidetached_house", "terrace", "detached",
                      "residential", "dormitory", "bungalow", "static_caravan"}
COMMERCIAL_TYPES = {"retail", "commercial", "supermarket", "office", "hotel", "kiosk"}
INDUSTRIAL_TYPES = {"industrial", "warehouse", "manufacture", "factory"}
PUBLIC_TYPES = {"school", "university", "hospital", "public", "government", "civic",
                 "college", "church", "chapel", "cathedral", "train_station",
                 "fire_station", "sports_hall"}
EXCLUDED_TYPES = {"roof", "garage", "carport", "shed", "ruins", "construction",
                   "greenhouse", "service", "parking", "bunker", "silo"}


def is_public_institution(building_type: str, amenity: str | None, leisure: str | None,
                           office: str | None) -> bool:
    """True for buildings the municipality itself plausibly owns/operates
    (town hall, public schools, health center, police...) -- used only to
    decide the Planner's direct-action-vs-subsidy split, independent of the
    load archetype. A church can be archetype 'public' (office-hours-like
    load shape) without being town property, for example."""
    if amenity in PS["amenities"]:
        return True
    if leisure in PS["leisure"]:
        return True
    if office in PS["offices"]:
        return True
    return building_type in PS["building_types"]


def classify_archetype(building_type: str, area_m2: float, amenity: str | None = None,
                        leisure: str | None = None, office: str | None = None) -> str | None:
    # amenity/leisure/office can override a generic `building=yes` tag: a
    # building tagged only building=yes but amenity=townhall (as OSM often
    # does for Catarroja's own city hall) must not fall through to the
    # residential/industrial area-based guess below.
    if is_public_institution(building_type, amenity, leisure, office):
        return "public"
    if building_type in RESIDENTIAL_TYPES:
        return "residential"
    if building_type in COMMERCIAL_TYPES:
        return "commercial"
    if building_type in INDUSTRIAL_TYPES:
        return "industrial"
    if building_type in PUBLIC_TYPES:
        return "public"
    if building_type in EXCLUDED_TYPES:
        return None
    # ambiguous / untyped ("yes", etc.)
    if area_m2 < L["min_building_area_m2"]:
        return None
    if area_m2 > L["large_untyped_building_m2"]:
        return "industrial"
    return "residential"


def default_levels(building_type: str) -> float:
    defaults = L["default_levels_by_type"]
    return defaults.get(building_type, defaults["default"])


def _normalize(raw: list[float]) -> np.ndarray:
    arr = np.array(raw, dtype=float)
    return arr / arr.mean()


SHAPES = {
    "residential": {
        "weekday": _normalize([0.40, 0.35, 0.30, 0.30, 0.35, 0.50, 0.90, 1.30, 1.10, 0.80,
                                0.70, 0.70, 0.75, 0.75, 0.80, 0.85, 0.95, 1.10, 1.40, 1.70,
                                1.60, 1.30, 0.90, 0.60]),
        "weekend": _normalize([0.50, 0.45, 0.40, 0.35, 0.35, 0.40, 0.55, 0.80, 1.00, 1.10,
                                1.15, 1.20, 1.25, 1.20, 1.15, 1.10, 1.10, 1.20, 1.40, 1.60,
                                1.50, 1.20, 0.85, 0.60]),
    },
    "commercial": {
        "weekday": _normalize([0.20, 0.15, 0.15, 0.15, 0.15, 0.20, 0.30, 0.60, 1.00, 1.30,
                                1.40, 1.40, 1.30, 1.30, 1.40, 1.40, 1.30, 1.20, 1.10, 0.90,
                                0.60, 0.40, 0.30, 0.25]),
    },
    "industrial": {
        "weekday": _normalize([0.30, 0.30, 0.30, 0.30, 0.40, 0.60, 1.00, 1.30, 1.40, 1.40,
                                1.40, 1.30, 1.20, 1.30, 1.40, 1.40, 1.30, 1.20, 1.00, 0.70,
                                0.50, 0.40, 0.35, 0.30]),
    },
    "public": {
        "weekday": _normalize([0.20, 0.20, 0.20, 0.20, 0.20, 0.20, 0.30, 0.60, 1.10, 1.40,
                                1.50, 1.50, 1.30, 1.40, 1.50, 1.40, 1.10, 0.70, 0.40, 0.30,
                                0.25, 0.20, 0.20, 0.20]),
    },
}
WEEKEND_SCALE = {"commercial": 0.6, "industrial": 0.3, "public": 0.15}

MONTHLY_RAW = {
    "residential": [1.08, 1.03, 0.95, 0.90, 0.92, 1.05, 1.20, 1.22, 1.05, 0.92, 0.98, 1.10],
    "commercial": [1.02, 1.02, 1.00, 1.00, 1.00, 0.98, 0.95, 0.70, 1.00, 1.02, 1.02, 1.05],
    "industrial": [1.02, 1.02, 1.00, 1.00, 1.00, 0.98, 0.90, 0.60, 1.02, 1.04, 1.04, 1.04],
    "public": [1.00, 1.00, 1.00, 1.00, 1.00, 0.95, 0.70, 0.60, 1.00, 1.05, 1.05, 1.05],
}
MONTHLY = {k: _normalize(v) for k, v in MONTHLY_RAW.items()}
AC_HOURS = list(range(14, 19))
AC_MONTHS = {6, 7, 8, 9}
AC_BUMP = 1.35


def archetype_hour_weight(archetype: str, month: int, hour: int, is_weekend: bool) -> float:
    shapes = SHAPES[archetype]
    if is_weekend:
        shape = shapes.get("weekend")
        base = shape[hour] if shape is not None else shapes["weekday"][hour] * WEEKEND_SCALE.get(archetype, 1.0)
    else:
        base = shapes["weekday"][hour]
    monthly = MONTHLY[archetype][month - 1]
    w = base * monthly
    if archetype == "residential" and month in AC_MONTHS and hour in AC_HOURS:
        w *= AC_BUMP
    return w


def load_calendar() -> pd.DataFrame:
    """Read the PVGIS hourly calendar (2020) and annotate weekday/weekend."""
    solar = pd.read_json(WEB_DATA_DIR / "solar_hourly.json")
    calendar = solar[["hour_of_year", "month", "day", "hour", "temp_c", "kwh_per_kwp"]].copy()
    ts = pd.to_datetime(dict(year=2020, month=calendar["month"], day=calendar["day"], hour=calendar["hour"]))
    calendar["is_weekend"] = ts.dt.dayofweek >= 5
    calendar["dayofweek"] = ts.dt.dayofweek
    return calendar


def hour_weight_vectors(calendar: pd.DataFrame) -> dict[str, np.ndarray]:
    """Per archetype, a vector over every hour of the year that sums to 1 --
    multiply by a building's/zone's annual kWh to get its hourly load."""
    weights = {}
    for arch in SHAPES:
        w = np.array([archetype_hour_weight(arch, m, h, wk) for m, h, wk in
                       zip(calendar["month"], calendar["hour"], calendar["is_weekend"])])
        weights[arch] = w / w.sum()
    return weights
