"""Milestone 1: download Catarroja building footprints from OpenStreetMap (Overpass API).

Writes web/data/buildings.geojson with one feature per building:
  - geometry: footprint polygon (lon/lat, EPSG:4326)
  - area_m2: footprint area in square metres (computed in a projected CRS)
  - levels: building:levels tag if present, else None
  - building_type: the OSM `building` tag value (e.g. "yes", "residential", "house")

Raw Overpass responses are cached in prep/cache/ so re-runs don't hit the API.
Also writes web/data/buildings_preview.png, a quick matplotlib plot for the milestone check.
"""

import json
import sys
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests
import shapely
from shapely.geometry import Polygon
from shapely.ops import linemerge, polygonize, unary_union
from sklearn.cluster import DBSCAN

from common import CONFIG

HERE = Path(__file__).resolve().parent
CACHE_DIR = HERE / "cache"
WEB_DATA_DIR = HERE.parent / "web" / "data"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
HEADERS = {"User-Agent": "everyray-demo/0.1 (student pitch demo; contact: yanis.blin.45@gmail.com)"}

# Catarroja municipality boundary (Valencia province, Spain).
AREA_FILTER = 'area["name"="Catarroja"]["boundary"="administrative"]["admin_level"="8"]->.a;'

WAYS_QUERY = f"""
[out:json][timeout:180];
{AREA_FILTER}
way["building"](area.a);
out geom;
"""

RELATIONS_QUERY = f"""
[out:json][timeout:180];
{AREA_FILTER}
relation["building"](area.a);
out body;
>;
out geom qt;
"""

BOUNDARY_QUERY = """
[out:json][timeout:90];
relation["name"="Catarroja"]["boundary"="administrative"]["admin_level"="8"];
out body;
>;
out geom qt;
"""


def build_boundary_line(data: dict):
    """Reconstruct the municipal boundary ring from the admin relation's
    outer member ways (same merge technique as multipolygon buildings)."""
    ways_by_id = {el["id"]: el for el in data.get("elements", []) if el["type"] == "way"}
    rel = next((el for el in data.get("elements", []) if el["type"] == "relation"), None)
    if rel is None:
        return None

    lines = []
    for member in rel.get("members", []):
        if member.get("type") != "way":
            continue
        way = ways_by_id.get(member["ref"])
        if not way or not way.get("geometry"):
            continue
        coords = [(pt["lon"], pt["lat"]) for pt in way["geometry"]]
        if len(coords) >= 2:
            lines.append(shapely.geometry.LineString(coords))

    if not lines:
        return None
    merged = linemerge(lines)
    polys = list(polygonize([merged] if merged.geom_type == "LineString" else list(merged.geoms)))
    if not polys:
        return None
    return unary_union(polys)


def fetch_cached(query: str, cache_name: str) -> dict:
    """Run an Overpass query, caching the raw JSON response under prep/cache/."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / cache_name
    if cache_path.exists():
        print(f"  using cached {cache_path.name}")
        return json.loads(cache_path.read_text())

    print(f"  requesting {cache_name} from Overpass...")
    resp = requests.post(OVERPASS_URL, data={"data": query}, headers=HEADERS, timeout=300)
    resp.raise_for_status()
    data = resp.json()
    cache_path.write_text(json.dumps(data))
    print(f"  cached to {cache_path.name} ({len(data.get('elements', []))} elements)")
    return data


def way_to_polygon(way: dict) -> Polygon | None:
    """Build a polygon from a way's `geometry` field (list of {lat, lon})."""
    geom = way.get("geometry")
    if not geom or len(geom) < 4:
        return None
    coords = [(pt["lon"], pt["lat"]) for pt in geom]
    if coords[0] != coords[-1]:
        # not a closed ring; skip (shouldn't happen for building ways)
        return None
    poly = Polygon(coords)
    if not poly.is_valid or poly.is_empty:
        poly = poly.buffer(0)
    return poly if (not poly.is_empty and poly.is_valid) else None


def build_relation_polygons(data: dict) -> list[dict]:
    """Reconstruct multipolygon buildings from relations + their recursed member ways."""
    ways_by_id = {}
    relations = []
    for el in data.get("elements", []):
        if el["type"] == "way":
            ways_by_id[el["id"]] = el
        elif el["type"] == "relation":
            relations.append(el)

    records = []
    for rel in relations:
        outer_lines, inner_lines = [], []
        for member in rel.get("members", []):
            if member.get("type") != "way":
                continue
            way = ways_by_id.get(member["ref"])
            if not way or not way.get("geometry"):
                continue
            coords = [(pt["lon"], pt["lat"]) for pt in way["geometry"]]
            if len(coords) < 2:
                continue
            line = shapely.geometry.LineString(coords)
            if member.get("role") == "inner":
                inner_lines.append(line)
            else:
                outer_lines.append(line)

        if not outer_lines:
            continue

        try:
            merged = linemerge(outer_lines)
            outer_polys = list(polygonize([merged] if merged.geom_type == "LineString" else list(merged.geoms)))
            if not outer_polys:
                outer_polys = list(polygonize(outer_lines))
            outer_union = unary_union(outer_polys)
        except Exception:
            continue

        if outer_union.is_empty:
            continue

        if inner_lines:
            try:
                inner_merged = linemerge(inner_lines)
                inner_polys = list(polygonize([inner_merged] if inner_merged.geom_type == "LineString" else list(inner_merged.geoms)))
                if inner_polys:
                    outer_union = outer_union.difference(unary_union(inner_polys))
            except Exception:
                pass

        if outer_union.is_empty or not outer_union.is_valid:
            continue

        tags = rel.get("tags", {})
        records.append({
            "osm_id": f"relation/{rel['id']}",
            "geometry": outer_union,
            "building_type": tags.get("building", "yes"),
            "levels_raw": tags.get("building:levels"),
            "amenity": tags.get("amenity"),
            "leisure": tags.get("leisure"),
            "office": tags.get("office"),
            "name": tags.get("name"),
        })
    return records


def build_way_polygons(data: dict) -> list[dict]:
    records = []
    for el in data.get("elements", []):
        if el["type"] != "way":
            continue
        poly = way_to_polygon(el)
        if poly is None:
            continue
        tags = el.get("tags", {})
        records.append({
            "osm_id": f"way/{el['id']}",
            "geometry": poly,
            "building_type": tags.get("building", "yes"),
            "levels_raw": tags.get("building:levels"),
            "amenity": tags.get("amenity"),
            "leisure": tags.get("leisure"),
            "office": tags.get("office"),
            "name": tags.get("name"),
        })
    return records


def parse_levels(raw) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def main():
    print("Fetching building ways...")
    ways_data = fetch_cached(WAYS_QUERY, "buildings_ways.json")
    print("Fetching building relations (multipolygons)...")
    rel_data = fetch_cached(RELATIONS_QUERY, "buildings_relations.json")
    print("Fetching municipal boundary...")
    boundary_data = fetch_cached(BOUNDARY_QUERY, "boundary_raw.json")
    boundary_geom = build_boundary_line(boundary_data)
    if boundary_geom is not None:
        WEB_DATA_DIR.mkdir(parents=True, exist_ok=True)
        gpd.GeoDataFrame([{"name": "Catarroja"}], geometry=[boundary_geom], crs="EPSG:4326") \
            .to_file(WEB_DATA_DIR / "boundary.geojson", driver="GeoJSON")
        print(f"  wrote boundary.geojson")
    else:
        print("  WARNING: could not reconstruct municipal boundary", file=sys.stderr)

    way_records = build_way_polygons(ways_data)
    rel_records = build_relation_polygons(rel_data)
    print(f"  {len(way_records)} way footprints, {len(rel_records)} relation (multipolygon) footprints")

    all_records = way_records + rel_records
    if not all_records:
        print("ERROR: no building footprints retrieved.", file=sys.stderr)
        sys.exit(1)

    gdf = gpd.GeoDataFrame(all_records, geometry="geometry", crs="EPSG:4326")

    n_before = len(gdf)
    empty_mask = gdf.geometry.is_empty | gdf.geometry.isna()
    n_empty = int(empty_mask.sum())
    if n_empty:
        print(f"  dropping {n_empty} empty/invalid geometries")
        gdf = gdf[~empty_mask].copy()

    dup_mask = gdf["osm_id"].duplicated()
    n_dup = int(dup_mask.sum())
    if n_dup:
        print(f"  dropping {n_dup} duplicate osm_id entries")
        gdf = gdf[~dup_mask].copy()

    # area in m2: project to a metric CRS (ETRS89 / UTM zone 30N covers Valencia)
    gdf_m = gdf.to_crs(epsg=25830)
    gdf["area_m2"] = gdf_m.geometry.area.round(1)
    gdf["levels"] = gdf["levels_raw"].apply(parse_levels)
    gdf = gdf.drop(columns=["levels_raw"])

    n_zero_area = int((gdf["area_m2"] <= 0).sum())
    if n_zero_area:
        print(f"  dropping {n_zero_area} zero/negative-area footprints")
        gdf = gdf[gdf["area_m2"] > 0].copy()

    n_no_levels = int(gdf["levels"].isna().sum())

    # Drop buildings outside the town's built-up clusters: isolated farmhouses
    # scattered across the surrounding huerta (agricultural land) are real OSM
    # data but outside the scope of a municipal solar/battery plan for the
    # town itself. DBSCAN groups buildings by footprint-centroid proximity;
    # we keep clusters of a meaningful size (the town core, any adjoining
    # neighbourhood, the industrial estate) and drop noise points plus tiny
    # clusters of a handful of remote buildings.
    gdf_m = gdf.to_crs(epsg=25830)
    coords = np.column_stack([gdf_m.geometry.centroid.x, gdf_m.geometry.centroid.y])
    DC = CONFIG["data_cleaning"]
    cluster_labels = DBSCAN(eps=DC["dbscan_eps_m"], min_samples=DC["dbscan_min_samples"]).fit_predict(coords)
    cluster_sizes = pd.Series(cluster_labels).value_counts()
    keep_clusters = set(cluster_sizes[cluster_sizes >= DC["min_cluster_size"]].index) - {-1}
    is_outlier = ~pd.Series(cluster_labels).isin(keep_clusters)
    n_outliers = int(is_outlier.sum())
    if n_outliers:
        print(f"  dropping {n_outliers} buildings outside the town's built-up clusters "
              f"(isolated farmhouses, DBSCAN noise/tiny clusters)")
        gdf = gdf[~is_outlier.to_numpy()].copy()

    gdf = gdf[["osm_id", "building_type", "levels", "area_m2", "amenity", "leisure",
               "office", "name", "geometry"]].reset_index(drop=True)

    WEB_DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = WEB_DATA_DIR / "buildings.geojson"
    gdf.to_file(out_path, driver="GeoJSON")

    # --- quick preview plot ---
    fig, ax = plt.subplots(figsize=(10, 10))
    gdf.plot(ax=ax, column="area_m2", cmap="viridis", linewidth=0.1, edgecolor="black", legend=True,
              legend_kwds={"label": "footprint area (m²)", "shrink": 0.6})
    ax.set_title(f"Catarroja buildings (OSM) — n={len(gdf)}")
    ax.set_xlabel("lon")
    ax.set_ylabel("lat")
    preview_path = WEB_DATA_DIR / "buildings_preview.png"
    fig.savefig(preview_path, dpi=150, bbox_inches="tight")
    plt.close(fig)

    # --- milestone check summary ---
    print("\n=== Milestone 1 check ===")
    print(f"Buildings written: {len(gdf)} (from {n_before} raw footprints)")
    print(f"Empty/invalid geometries dropped: {n_empty}")
    print(f"Duplicate osm_id dropped: {n_dup}")
    print(f"Zero/negative area dropped: {n_zero_area}")
    print(f"Outlier buildings dropped (outside town clusters): {n_outliers}")
    print(f"Missing building:levels: {n_no_levels} ({100 * n_no_levels / len(gdf):.1f}%)")
    print(f"Area stats (m2): min={gdf['area_m2'].min():.1f} median={gdf['area_m2'].median():.1f} "
          f"mean={gdf['area_m2'].mean():.1f} max={gdf['area_m2'].max():.1f}")
    print("\nTop building_type counts:")
    print(gdf["building_type"].value_counts().head(10).to_string())
    print(f"\nWrote {out_path}")
    print(f"Wrote {preview_path}")


if __name__ == "__main__":
    main()
