"""Milestone 3: simulate hourly electricity loads, cluster them, and train a
gradient-boosting forecast model.

Pipeline:
1. Classify each building into a load archetype (residential / commercial /
   industrial / public) from its OSM building type (see config.json).
2. Estimate households (residential) and annual kWh per building from the
   archetypes/intensities in config.json, with +/-15% random variation per
   building (fixed seed).
3. Build a 250 m hexagonal zone grid over the built-up area and assign every
   building to a zone.
4. Build hourly (8784-hour, matching the PVGIS 2020 calendar) load shapes per
   archetype (weekday/weekend, with a summer afternoon A/C bump for
   residential), add small per-hour noise, and aggregate to zone-hourly load.
5. K-means cluster the simulated per-building daily load shapes into
   archetypes (the "AI" component #1) and report cluster purity against the
   known archetype labels.
6. Train one gradient-boosting model per zone to forecast hourly zone load
   from hour/weekday/temperature (the "AI" component #2), trained on
   Jan-Oct, tested on Nov-Dec.

Outputs (all under web/data/):
  - buildings_loads.json   per-building osm_id -> archetype, households, annual_kwh, zone_id
  - zones.geojson          250 m hex zones with archetype mix + annual kWh totals
  - zone_loads_hourly.json zone_id -> 8784 hourly kWh values (with noise, "actual")
  - load_clusters.json     k-means cluster centers (24h shapes) + purity vs. known archetypes
  - zone_forecast.json     per zone: test-period actual vs. predicted hourly load, R2, MAE

Everything here is SIMULATED (Spanish typical load profiles), not real
smart-meter data -- see the honesty rule in the spec / UI footer.

Shared archetype/config logic lives in prep/common.py (also used by 04_economics.py).
"""

import json

import geopandas as gpd
import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, r2_score
from shapely.geometry import Polygon

from common import (CONFIG, SEED, L, Z, WEB_DATA_DIR, SHAPES,
                     classify_archetype, default_levels,
                     load_calendar, hour_weight_vectors)

K = CONFIG["clustering"]["n_clusters"]


def main():
    print("Loading buildings and solar calendar...")
    buildings = gpd.read_file(WEB_DATA_DIR / "buildings.geojson")
    calendar = load_calendar()
    n_hours = len(calendar)

    # --- 1. classify + estimate annual kWh per building --------------------
    rng = np.random.default_rng(SEED)
    buildings["archetype"] = [classify_archetype(t, a, am, le, of) for t, a, am, le, of in
                               zip(buildings["building_type"], buildings["area_m2"],
                                   buildings["amenity"], buildings["leisure"], buildings["office"])]
    buildings["levels_filled"] = buildings["levels"].fillna(
        buildings["building_type"].apply(default_levels))

    def annual_kwh_and_households(row):
        if pd.isna(row["archetype"]):
            return 0.0, 0
        if row["archetype"] == "residential":
            if row["building_type"] in L["single_family_types"]:
                # single-dwelling types: multiple floors of the same house, not
                # multiple households -- footprint x levels would double-count.
                households = 1
            else:
                raw = row["area_m2"] * row["levels_filled"] / L["household_m2_per_unit"]
                # cap guards against a handful of OSM ways that merge a whole
                # city block (courtyard + several real buildings) into one
                # "apartments" polygon, which would otherwise imply 100+ units
                # in a single building -- see milestone 3 data-quality note.
                households = min(L["max_households_per_building"], max(1, round(raw)))
            kwh = households * L["residential_kwh_per_household"]
            return kwh, households
        intensity_key = f"{row['archetype']}_kwh_per_m2"
        kwh = row["area_m2"] * L[intensity_key]
        return kwh, 0

    results = buildings.apply(annual_kwh_and_households, axis=1, result_type="expand")
    buildings["annual_kwh_base"], buildings["households"] = results[0], results[1]
    variation = rng.uniform(-L["building_variation_pct"], L["building_variation_pct"], size=len(buildings))
    buildings["annual_kwh"] = (buildings["annual_kwh_base"] * (1 + variation)).round(1)
    buildings.loc[buildings["archetype"].isna(), "annual_kwh"] = 0.0

    n_excluded = int(buildings["archetype"].isna().sum())
    print(f"  {len(buildings) - n_excluded} load-bearing buildings, {n_excluded} excluded "
          f"(no clear archetype / under {L['min_building_area_m2']} m2)")

    # --- 2. 250 m hex zone grid + assignment --------------------------------
    print("Building 250 m hex zone grid...")
    buildings_m = buildings.to_crs(epsg=25830)
    centroids = buildings_m.geometry.centroid
    size = Z["hex_spacing_m"] / np.sqrt(3)  # circumradius for 250 m center spacing

    def pixel_to_axial(x, y):
        q = (np.sqrt(3) / 3 * x - 1 / 3 * y) / size
        r = (2 / 3 * y) / size
        return q, r

    def axial_round(q, r):
        x, z = q, r
        y = -x - z
        rx, ry, rz = round(x), round(y), round(z)
        dx, dy, dz = abs(rx - x), abs(ry - y), abs(rz - z)
        if dx > dy and dx > dz:
            rx = -ry - rz
        elif dy > dz:
            ry = -rx - rz
        else:
            rz = -rx - ry
        return int(rx), int(rz)

    qs, rs = pixel_to_axial(centroids.x.values, centroids.y.values)
    axial = [axial_round(q, r) for q, r in zip(qs, rs)]
    buildings["zone_id"] = [f"hex_{q}_{r}" for q, r in axial]

    def axial_to_pixel(q, r):
        x = size * np.sqrt(3) * (q + r / 2)
        y = size * 1.5 * r
        return x, y

    def hex_polygon(cx, cy):
        pts = []
        for i in range(6):
            angle = np.radians(60 * i - 30)
            pts.append((cx + size * np.cos(angle), cy + size * np.sin(angle)))
        return Polygon(pts)

    unique_axial = sorted(set(axial))
    zone_geoms = {}
    for q, r in unique_axial:
        cx, cy = axial_to_pixel(q, r)
        zone_geoms[f"hex_{q}_{r}"] = hex_polygon(cx, cy)

    print(f"  {len(zone_geoms)} occupied zones (of {len(buildings)} buildings)")

    # --- 3. hourly load per archetype, aggregated to zones ------------------
    print("Simulating hourly load shapes and aggregating to zones...")
    hour_weight = hour_weight_vectors(calendar)

    zone_arch_kwh = buildings[buildings["archetype"].notna()].groupby(
        ["zone_id", "archetype"])["annual_kwh"].sum()

    zone_ids = list(zone_geoms.keys())
    noise_rng = np.random.default_rng(SEED + 1)
    zone_hourly = {}
    for zid in zone_ids:
        series = np.zeros(n_hours)
        for arch in SHAPES:
            kwh = zone_arch_kwh.get((zid, arch), 0.0)
            if kwh:
                series += kwh * hour_weight[arch]
        noise = noise_rng.normal(1.0, 0.05, size=n_hours)
        zone_hourly[zid] = np.clip(series * noise, 0, None)

    # --- 4. k-means clustering of simulated building daily shapes -----------
    print("Clustering per-building daily load shapes (k-means)...")
    load_buildings = buildings[buildings["archetype"].notna()].reset_index(drop=True)
    shape_rng = np.random.default_rng(SEED + 2)
    daily_shapes = np.zeros((len(load_buildings), 24))
    for i, arch in enumerate(load_buildings["archetype"]):
        base = SHAPES[arch]["weekday"]
        daily_shapes[i] = base * shape_rng.normal(1.0, 0.03, size=24)

    kmeans = KMeans(n_clusters=K, random_state=SEED, n_init=10)
    cluster_labels = kmeans.fit_predict(daily_shapes)
    load_buildings["cluster"] = cluster_labels

    # map each cluster to its majority true archetype, compute purity
    cluster_to_archetype = {}
    for c in range(K):
        mask = cluster_labels == c
        majority = load_buildings.loc[mask, "archetype"].mode().iloc[0]
        cluster_to_archetype[c] = majority
    purity = (load_buildings["archetype"] == load_buildings["cluster"].map(cluster_to_archetype)).mean()

    load_clusters_out = {
        "n_clusters": K,
        "purity_vs_known_archetypes": round(float(purity), 4),
        "clusters": [
            {
                "cluster": c,
                "label_guess": cluster_to_archetype[c],
                "n_buildings": int((cluster_labels == c).sum()),
                "center_24h_shape": kmeans.cluster_centers_[c].round(3).tolist(),
            }
            for c in range(K)
        ],
    }

    # --- 5. gradient boosting forecast per zone ------------------------------
    print("Training gradient-boosting hourly forecast per zone...")
    feat = calendar[["hour", "dayofweek", "temp_c", "month"]].copy()
    train_mask = calendar["month"] <= 10
    test_mask = ~train_mask

    forecast_out = {}
    r2s, maes = [], []
    for zid in zone_ids:
        y = zone_hourly[zid]
        model = GradientBoostingRegressor(n_estimators=150, max_depth=3, learning_rate=0.1,
                                           random_state=SEED)
        model.fit(feat[train_mask], y[train_mask.values])
        pred_test = model.predict(feat[test_mask])
        r2 = r2_score(y[test_mask.values], pred_test)
        mae = mean_absolute_error(y[test_mask.values], pred_test)
        r2s.append(r2)
        maes.append(mae)
        forecast_out[zid] = {
            "hour_of_year": calendar.loc[test_mask, "hour_of_year"].tolist(),
            "actual": y[test_mask.values].round(3).tolist(),
            "predicted": pred_test.round(3).tolist(),
            "r2": round(float(r2), 4),
            "mae_kwh": round(float(mae), 4),
        }

    # --- write outputs --------------------------------------------------------
    WEB_DATA_DIR.mkdir(parents=True, exist_ok=True)

    buildings_loads = buildings[["osm_id", "archetype", "households", "annual_kwh", "zone_id"]]
    buildings_loads.to_json(WEB_DATA_DIR / "buildings_loads.json", orient="records")

    zones_rows = []
    for zid, geom in zone_geoms.items():
        sub = buildings[buildings["zone_id"] == zid]
        archetype_counts = sub["archetype"].value_counts().to_dict()
        zones_rows.append({
            "zone_id": zid,
            "n_buildings": len(sub),
            "annual_kwh_total": round(float(sub["annual_kwh"].sum()), 1),
            "n_residential": archetype_counts.get("residential", 0),
            "n_commercial": archetype_counts.get("commercial", 0),
            "n_industrial": archetype_counts.get("industrial", 0),
            "n_public": archetype_counts.get("public", 0),
            "geometry": geom,
        })
    zones_gdf = gpd.GeoDataFrame(zones_rows, geometry="geometry", crs="EPSG:25830").to_crs(epsg=4326)
    zones_gdf.to_file(WEB_DATA_DIR / "zones.geojson", driver="GeoJSON")

    with open(WEB_DATA_DIR / "zone_loads_hourly.json", "w") as f:
        json.dump({zid: series.round(3).tolist() for zid, series in zone_hourly.items()}, f)

    with open(WEB_DATA_DIR / "load_clusters.json", "w") as f:
        json.dump(load_clusters_out, f)

    with open(WEB_DATA_DIR / "zone_forecast.json", "w") as f:
        json.dump(forecast_out, f)

    # --- milestone check summary ----------------------------------------------
    total_kwh = buildings["annual_kwh"].sum()
    residential_kwh = buildings.loc[buildings["archetype"] == "residential", "annual_kwh"].sum()
    total_households = int(buildings["households"].sum())
    target_population = 29000
    target_kwh_per_capita = 1100
    target_total = target_population * target_kwh_per_capita
    implied_population = total_households * L["assumed_persons_per_household"]

    print("\n=== Milestone 3 check ===")
    print(f"Zones: {len(zone_ids)} occupied 250 m hex cells")
    print(f"Load-bearing buildings: {len(load_buildings)} / {len(buildings)} total "
          f"({n_excluded} excluded: too small or no clear archetype)")
    print(f"Total households (residential): {total_households} "
          f"(implied population at {L['assumed_persons_per_household']} persons/household: {implied_population:,.0f})")
    print(f"Total simulated annual consumption (all archetypes): {total_kwh / 1e6:.2f} GWh/year")
    print(f"  of which residential: {residential_kwh / 1e6:.2f} GWh/year")
    print(f"Spec reference check: {target_population:,} inhabitants x {target_kwh_per_capita} kWh/capita "
          f"= {target_total / 1e6:.2f} GWh/year")
    print(f"Ratio total simulated / reference: {total_kwh / target_total:.2f}x")
    print(f"\nK-means cluster purity vs. known archetypes: {purity:.1%} (k={K})")
    for c in load_clusters_out["clusters"]:
        print(f"  cluster {c['cluster']}: label~{c['label_guess']:<11} n={c['n_buildings']}")
    print(f"\nGradient boosting forecast (Nov-Dec test period), across {len(zone_ids)} zones:")
    print(f"  mean R2: {np.mean(r2s):.3f}  (min {np.min(r2s):.3f}, max {np.max(r2s):.3f})")
    print(f"  mean MAE: {np.mean(maes):.3f} kWh/hour")
    print(f"\nWrote buildings_loads.json, zones.geojson, zone_loads_hourly.json, "
          f"load_clusters.json, zone_forecast.json to {WEB_DATA_DIR}")


if __name__ == "__main__":
    main()
