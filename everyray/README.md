# EveryRay – Catarroja demo

A 2-minute interactive demo for a municipality planner: where do solar panels
and batteries pay back in Catarroja (Valencia, Spain)?

Buildings, sun and weather are **real open data** (OpenStreetMap, PVGIS).
Household consumption is **simulated** (typical Spanish load profiles) —
costs and tariffs are **indicative assumptions**, editable in `web/config.json`
and visible in the app's "Assumptions" drawer. A pilot would replace the
simulated loads with the town's own smart-meter data.

## Running the demo

Everything is pre-computed into static files under `web/data/`, so the app
works fully offline. From the project root:

```bash
cd web
python -m http.server 8000
```

Then open <http://localhost:8000> in a browser.

The app is a single `index.html` + `app.js` + `style.css`, using
[Leaflet](https://leafletjs.com/) for the map and
[Chart.js](https://www.chartjs.org/) for charts (both loaded from CDN, no
build step, no backend).

## Regenerating the data

The `prep/` pipeline is Python 3 (see `prep/requirements.txt` / the project's
`.venv`). Run the milestones in order from the project root:

```bash
source .venv/bin/activate
python prep/01_buildings.py   # OSM building footprints -> web/data/buildings.geojson
python prep/02_solar.py       # PVGIS hourly PV yield -> web/data/solar_hourly.json
python prep/03_loads.py       # simulated loads, zones, clustering, forecast
python prep/04_economics.py   # per-building & per-zone solar + battery economics
```

Each script caches its raw API responses under `prep/cache/`, so re-running
is fast and works offline once the cache is populated. All model parameters
(capex, tariffs, load assumptions, battery rules, etc.) live in
`web/config.json`.

## Demo script (~2 minutes)

1. **Map**: "This is Catarroja. Each building is coloured by how fast solar
   pays back."
2. Click a zone: "Here is the problem: at midday, production far exceeds
   local use."
3. Toggle the battery: "Our model sizes it so the surplus is used locally in
   the evening."
4. **Planner** slider: "With €2M, the municipality gets X kWp, saves €Y per
   year and avoids Z tonnes of CO₂ — instead of spreading the money blindly."
5. Close: "With real smart-meter data in a pilot, these simulated profiles
   become real forecasts."

## Known limitations / future work

- Household counts come from OSM footprint × levels, calibrated against the
  town's population — a few OSM building ways merge whole city blocks and
  needed a documented guard (see `web/config.json`'s `max_households_per_building`
  and milestone 3/4 notes).
- Roof pitch (flat vs. pitched) is inferred from building type, not from an
  OSM `roof:shape` tag (not fetched in this pass).
- PVGIS's hourly series uses 2020 (the latest year in the public API) as a
  stand-in for a "typical year", rather than a true multi-year TMY.
- Residential PV sizing is capped to roughly match household demand rather
  than maxing out the technical roof potential — see the Assumptions drawer.
- Electricity price shape uses a synthetic duck-curve (cheap midday, no real
  OMIE/ESIOS day-ahead series) — out of scope for this pass.
