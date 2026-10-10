"""Milestone 4: per-building and per-zone solar + battery economics.

Per building (load-bearing buildings only, i.e. the 3,187 from milestone 3 --
PV without a resident/business to self-consume it doesn't make sense to rank):
  - usable roof area = footprint x 0.5 (pitched, single-family house types)
                                x 0.6 (flat roof, everything else -- assumption,
                                  since the Overpass query didn't fetch roof:shape)
  - kWp = usable area / 5.5 m2/kWp; PV capex = kWp x 1200 EUR/kWp
  - hourly production (kWp x PVGIS yield) vs. hourly load (archetype shape x
    annual kWh) -> self-consumption, export, avoided-import revenue, export
    revenue (export price drops near zero at midday, the paper's "duck curve")
  - annual net savings (minus 1%/year O&M) -> simple payback = capex / savings

Per zone: total PV capacity, and a battery sized by a small search over 0-4
kWh/kWp that maximises (savings with battery - savings PV-only) per euro of
battery capex, charging from PV surplus and discharging 18:00-23:00 at 90%
round-trip efficiency.

Outputs (web/data/):
  - buildings_economics.json  per osm_id: kWp, capex, production/load/self-use/
                               export kWh, annual_savings, payback_years, co2_avoided_kg
  - zone_economics.json       per zone_id: kWp_total, recommended battery kWh,
                               PV-only vs with-battery savings, midday surplus
                               absorbed %, payback (PV-only and whole zone programme)
"""

import json

import geopandas as gpd
import numpy as np
import pandas as pd

from common import CONFIG, SEED, L, WEB_DATA_DIR, load_calendar, hour_weight_vectors, is_public_institution

E = CONFIG["economics"]


def roof_factor(building_type: str) -> float:
    if building_type in L["single_family_types"]:
        return E["pitched_roof_usable_fraction"]
    return E["flat_roof_usable_fraction"]


def simulate_battery(production, load, capacity_kwh, discharge_mask, midday_mask,
                      export_price_hourly, eff):
    """Hour-by-hour simulation (sequential battery state). Returns
    (avoided_import_kwh, export_kwh, export_revenue, midday_export_kwh)."""
    n = len(production)
    level = 0.0
    avoided_import = 0.0
    export_total = 0.0
    export_revenue = 0.0
    midday_export = 0.0
    has_battery = capacity_kwh > 1e-9
    for h in range(n):
        prod = production[h]
        ld = load[h]
        if prod >= ld:
            direct = ld
            surplus = prod - ld
            unmet = 0.0
        else:
            direct = prod
            surplus = 0.0
            unmet = ld - prod

        if has_battery and surplus > 0:
            space = capacity_kwh - level
            charge = surplus if surplus < space else space
            level += charge
            leftover = surplus - charge
        else:
            leftover = surplus

        discharge_delivered = 0.0
        if has_battery and unmet > 0 and discharge_mask[h] and level > 0:
            max_deliverable = level * eff
            discharge_delivered = unmet if unmet < max_deliverable else max_deliverable
            level -= discharge_delivered / eff

        avoided_import += direct + discharge_delivered
        export_total += leftover
        price = export_price_hourly[h]
        export_revenue += leftover * price
        if midday_mask[h]:
            midday_export += leftover
    return avoided_import, export_total, export_revenue, midday_export


def main():
    print("Loading buildings, loads, zones, solar calendar...")
    buildings = gpd.read_file(WEB_DATA_DIR / "buildings.geojson")
    loads = pd.read_json(WEB_DATA_DIR / "buildings_loads.json")
    buildings = buildings.merge(loads, on="osm_id", how="left")
    zones = gpd.read_file(WEB_DATA_DIR / "zones.geojson")
    zone_hourly = json.loads((WEB_DATA_DIR / "zone_loads_hourly.json").read_text())

    calendar = load_calendar()
    n_hours = len(calendar)
    solar_series = calendar["kwh_per_kwp"].to_numpy()
    hour_weight = hour_weight_vectors(calendar)

    discharge_mask_arr = calendar["hour"].isin(E["battery_discharge_hours"]).to_numpy()
    midday_mask_arr = calendar["hour"].isin(E["export_midday_hours"]).to_numpy()
    export_price_hourly = np.where(midday_mask_arr, E["export_price_midday_eur_per_kwh"],
                                    E["export_price_eur_per_kwh"])
    # plain python lists: scalar indexing in the per-hour battery loop below
    # is much faster on lists than on numpy arrays
    discharge_mask = discharge_mask_arr.tolist()
    midday_mask = midday_mask_arr.tolist()
    export_price_list = export_price_hourly.tolist()

    # --- per-building PV sizing ---------------------------------------------
    print("Sizing rooftop PV per building...")
    load_bearing = buildings[buildings["archetype"].notna()].copy().reset_index(drop=True)
    load_bearing["roof_factor"] = load_bearing["building_type"].apply(roof_factor)
    load_bearing["usable_area_m2"] = (load_bearing["area_m2"] * load_bearing["roof_factor"]).round(1)
    load_bearing["kwp_roof_max"] = (load_bearing["usable_area_m2"] / E["m2_per_kwp"]).round(3)

    # Residential systems are capped so they aren't built far beyond what the
    # household can plausibly use: a single house's roof can often host far
    # more kWp than its ~3,000 kWh/year demand would ever self-consume, which
    # in reality nobody sizes that way without a strong export incentive.
    # Commercial/industrial/public already self-consume most of their own
    # production (shared buildings, daytime use) so the roof-based figure is
    # left as-is for those.
    annual_yield_per_kwp = solar_series.sum()
    # not every household/installer sizes to the exact same target ratio --
    # add modest per-building variation (fixed seed) so otherwise-identical
    # small houses don't all land on literally the same payback
    sizing_rng = np.random.default_rng(SEED + 3)
    ratio_variation = sizing_rng.uniform(-E["residential_sizing_ratio_variation_pct"],
                                         E["residential_sizing_ratio_variation_pct"],
                                         size=len(load_bearing))
    sizing_ratio = E["residential_max_production_to_load_ratio"] * (1 + ratio_variation)
    load_cap_kwp = (load_bearing["annual_kwh"] * sizing_ratio) / annual_yield_per_kwp
    is_residential = load_bearing["archetype"] == "residential"
    load_bearing["kwp"] = load_bearing["kwp_roof_max"]
    load_bearing.loc[is_residential, "kwp"] = np.minimum(
        load_bearing.loc[is_residential, "kwp_roof_max"], load_cap_kwp[is_residential])
    load_bearing["kwp"] = load_bearing["kwp"].round(3)
    load_bearing["pv_capex_eur"] = (load_bearing["kwp"] * E["pv_capex_eur_per_kwp"]).round(0)

    # --- per-building hourly production/load, vectorized per archetype group
    print("Simulating per-building hourly production vs. load...")
    annual_production = np.zeros(len(load_bearing))
    self_consumption = np.zeros(len(load_bearing))
    export_kwh = np.zeros(len(load_bearing))
    export_revenue = np.zeros(len(load_bearing))

    for arch, group in load_bearing.groupby("archetype"):
        idx = group.index.to_numpy()
        kwp = group["kwp"].to_numpy()
        annual_kwh = group["annual_kwh"].to_numpy()
        production = np.outer(kwp, solar_series)            # (n_buildings, n_hours)
        load_mat = np.outer(annual_kwh, hour_weight[arch])   # (n_buildings, n_hours)
        self_use = np.minimum(production, load_mat)
        export_mat = production - self_use
        annual_production[idx] = production.sum(axis=1)
        self_consumption[idx] = self_use.sum(axis=1)
        export_kwh[idx] = export_mat.sum(axis=1)
        export_revenue[idx] = export_mat @ export_price_hourly
        del production, load_mat, self_use, export_mat

    load_bearing["annual_production_kwh"] = annual_production.round(1)
    load_bearing["self_consumption_kwh"] = self_consumption.round(1)
    load_bearing["export_kwh"] = export_kwh.round(1)
    avoided_import_revenue = self_consumption * E["retail_import_price_eur_per_kwh"]
    om_cost = load_bearing["pv_capex_eur"].to_numpy() * E["om_pct_of_capex_per_year"]
    net_savings = avoided_import_revenue + export_revenue - om_cost
    load_bearing["annual_savings_eur"] = net_savings.round(1)
    with np.errstate(divide="ignore"):
        payback = np.where(net_savings > 0, load_bearing["pv_capex_eur"].to_numpy() / net_savings, np.inf)
    load_bearing["payback_years"] = np.round(payback, 2)
    load_bearing["co2_avoided_kg_per_year"] = (self_consumption * E["co2_factor_kg_per_kwh"]).round(1)

    # Flag buildings the municipality itself plausibly owns/operates (town
    # hall, public school, health centre, police...) -- independent of the
    # load archetype (e.g. a church is archetype "public" but not town
    # property). Drives the Planner's direct-action vs. private-subsidy split.
    load_bearing["is_public_institution"] = [
        is_public_institution(bt, am, le, of) for bt, am, le, of in
        zip(load_bearing["building_type"], load_bearing["amenity"],
            load_bearing["leisure"], load_bearing["office"])
    ]

    buildings_economics = load_bearing[[
        "osm_id", "zone_id", "archetype", "building_type", "area_m2", "roof_factor",
        "usable_area_m2", "kwp_roof_max", "kwp", "pv_capex_eur", "annual_production_kwh",
        "self_consumption_kwh", "export_kwh", "annual_savings_eur", "payback_years",
        "co2_avoided_kg_per_year", "is_public_institution", "amenity", "name",
    ]]
    buildings_economics.to_json(WEB_DATA_DIR / "buildings_economics.json", orient="records")

    n_public_institutions = int(load_bearing["is_public_institution"].sum())

    # --- per-zone PV + battery -------------------------------------------------
    print("Sizing battery per zone (search 0-4 kWh/kWp)...")
    multipliers = np.arange(0, E["battery_search_max_kwh_per_kwp"] + 1e-9,
                             E["battery_search_step_kwh_per_kwp"])

    def zone_battery_economics(kwp_total: float, load_list: list[float]) -> dict | None:
        """Battery search + PV-only/with-battery economics for one zone's
        aggregate kWp and hourly load. Reused for the whole zone and for the
        private-only (non-municipal) subset."""
        if kwp_total <= 0:
            return None
        production = (kwp_total * solar_series).tolist()
        pv_capex = kwp_total * E["pv_capex_eur_per_kwp"]

        baseline = simulate_battery(production, load_list, 0.0, discharge_mask, midday_mask,
                                     export_price_list, E["battery_round_trip_efficiency"])
        base_avoided, base_export, base_export_rev, base_midday_export = baseline
        base_om = pv_capex * E["om_pct_of_capex_per_year"]
        base_savings = base_avoided * E["retail_import_price_eur_per_kwh"] + base_export_rev - base_om

        best = None
        for mult in multipliers:
            if mult == 0:
                continue
            capacity = mult * kwp_total
            battery_capex = capacity * E["battery_capex_eur_per_kwh"]
            avoided, export_tot, export_rev, midday_export = simulate_battery(
                production, load_list, capacity, discharge_mask, midday_mask,
                export_price_list, E["battery_round_trip_efficiency"])
            om = base_om + battery_capex * E["om_pct_of_capex_per_year"]
            savings = avoided * E["retail_import_price_eur_per_kwh"] + export_rev - om
            marginal_savings = savings - base_savings
            ratio = marginal_savings / battery_capex if battery_capex > 0 else -np.inf
            if best is None or ratio > best["ratio"]:
                best = dict(mult=mult, capacity=capacity, battery_capex=battery_capex,
                            avoided=avoided, export_tot=export_tot, export_rev=export_rev,
                            midday_export=midday_export, savings=savings, ratio=ratio)

        total_capex = pv_capex + best["battery_capex"]
        with np.errstate(divide="ignore"):
            payback_pv_only = pv_capex / base_savings if base_savings > 0 else float("inf")
            payback_programme = total_capex / best["savings"] if best["savings"] > 0 else float("inf")
        midday_absorbed_pct = (1 - best["midday_export"] / base_midday_export) * 100 if base_midday_export > 0 else 0.0

        return {
            "kwp_total": round(kwp_total, 2),
            "pv_capex_eur": round(pv_capex, 0),
            "recommended_battery_kwh": round(best["capacity"], 1),
            "battery_kwh_per_kwp": round(best["mult"], 2),
            "battery_capex_eur": round(best["battery_capex"], 0),
            "annual_savings_pv_only_eur": round(base_savings, 1),
            "annual_savings_with_battery_eur": round(best["savings"], 1),
            "midday_export_pv_only_kwh": round(base_midday_export, 1),
            "midday_export_with_battery_kwh": round(best["midday_export"], 1),
            "midday_surplus_absorbed_pct": round(midday_absorbed_pct, 1),
            "payback_years_pv_only": round(payback_pv_only, 2),
            "payback_years_programme": round(payback_programme, 2),
            "co2_avoided_kg_pv_only": round(base_avoided * E["co2_factor_kg_per_kwh"], 1),
            "co2_avoided_kg_with_battery": round(best["avoided"] * E["co2_factor_kg_per_kwh"], 1),
        }

    # private-only (non-municipal) rollup, for the Planner's subsidy bucket --
    # recomputed from scratch (deterministic, no noise) so excluding public
    # buildings doesn't require un-mixing noise already baked into
    # zone_loads_hourly.json
    private_bearing = load_bearing[~load_bearing["is_public_institution"]]
    private_zone_arch_kwh = private_bearing.groupby(["zone_id", "archetype"])["annual_kwh"].sum()
    private_zone_kwp = private_bearing.groupby("zone_id")["kwp"].sum()

    zone_kwp = load_bearing.groupby("zone_id")["kwp"].sum()
    zone_rows = []
    for zid in zones["zone_id"]:
        kwp_total = float(zone_kwp.get(zid, 0.0))
        load_list = list(zone_hourly.get(zid, [0.0] * n_hours))
        econ = zone_battery_economics(kwp_total, load_list)
        if econ is None:
            continue

        kwp_private = float(private_zone_kwp.get(zid, 0.0))
        load_private = np.zeros(n_hours)
        for arch in hour_weight:
            kwh = private_zone_arch_kwh.get((zid, arch), 0.0)
            if kwh:
                load_private += kwh * hour_weight[arch]
        econ_private = zone_battery_economics(kwp_private, load_private.tolist())

        row = {"zone_id": zid, **econ}
        if econ_private is not None:
            row.update({f"{k}_private": v for k, v in econ_private.items()})
        zone_rows.append(row)

    with open(WEB_DATA_DIR / "zone_economics.json", "w") as f:
        json.dump(zone_rows, f)

    # --- milestone check summary ----------------------------------------------
    finite = load_bearing[np.isfinite(load_bearing["payback_years"])]
    in_range = finite[(finite["payback_years"] >= 5) & (finite["payback_years"] <= 12)]
    n_no_savings = int((~np.isfinite(load_bearing["payback_years"])).sum())

    print("\n=== Milestone 4 check ===")
    print(f"Buildings with PV sizing: {len(load_bearing)} ({n_no_savings} with no positive net savings)")
    print(f"Payback stats (years), finite only (n={len(finite)}):")
    print(finite["payback_years"].describe().round(2).to_string())
    print(f"\nShare of buildings with payback in 5-12 years: {len(in_range) / len(finite):.1%}")

    print("\n10 best paybacks:")
    best10 = finite.nsmallest(10, "payback_years")[
        ["osm_id", "building_type", "archetype", "area_m2", "kwp", "annual_savings_eur", "payback_years"]]
    print(best10.to_string(index=False))

    print("\n10 worst (finite) paybacks:")
    worst10 = finite.nlargest(10, "payback_years")[
        ["osm_id", "building_type", "archetype", "area_m2", "kwp", "annual_savings_eur", "payback_years"]]
    print(worst10.to_string(index=False))

    zdf = pd.DataFrame(zone_rows)
    print(f"\nZones with battery sizing: {len(zdf)}")
    print(f"Mean battery size: {zdf['battery_kwh_per_kwp'].mean():.2f} kWh/kWp "
          f"(min {zdf['battery_kwh_per_kwp'].min():.2f}, max {zdf['battery_kwh_per_kwp'].max():.2f})")
    print(f"Mean midday surplus absorbed by battery: {zdf['midday_surplus_absorbed_pct'].mean():.1f}%")
    print(f"\nPublic institutions identified (direct-action candidates): {n_public_institutions}")
    public_df = load_bearing[load_bearing["is_public_institution"]][
        ["name", "building_type", "amenity", "kwp", "payback_years"]].sort_values("payback_years")
    print(public_df.to_string(index=False))

    print(f"\nWrote buildings_economics.json, zone_economics.json to {WEB_DATA_DIR}")


if __name__ == "__main__":
    main()
