# EveryRay – Catarroja demo (spec for Claude Code)

Purpose: a 2-minute interactive demo for a class pitch. A municipality planner sees where solar + batteries pay back in Catarroja (Valencia, Spain, ~29–30k inhabitants), and how much midday surplus batteries can absorb.

**Honesty rule (put it in the UI footer):** buildings, sun and weather are real open data. Household consumption is *simulated* (typical Spanish load profiles). Costs and tariffs are *indicative assumptions* in `config.json`. A pilot would replace simulated loads with the town's smart-meter data.

---

## 1. Scope (do NOT exceed)

One town, three screens, one slider. Everything is pre-computed into static files, so the demo works offline on presentation day.

1. **Map screen**: buildings coloured by estimated payback (green = short, red = long / not worth it).
2. **Zone screen**: click a zone, see a 24h chart (consumption, solar production, with/without battery), grid export at midday, recommended kWp and kWh.
3. **Planner screen**: budget slider (€0 – €5M). The app selects the best buildings/zones greedily by payback and shows: kWp installed, annual savings, CO₂ avoided, midday surplus absorbed (%), simple payback of the whole programme.

## 2. Stack

- `prep/` – Python 3 scripts (requests, pandas, geopandas, shapely, scikit-learn, numpy). Output to `web/data/*.json|geojson`.
- `web/` – a single `index.html` + `app.js` + `style.css`, using MapLibre GL JS (or Leaflet) and Chart.js loaded from a CDN. No build step, no backend.
- Palette: blue `#3F5FA8`, green `#7DBE82`, lime `#B7D95A`, beige background `#EFEBDD` (matches the pitch deck). Title: "EveryRay – Catarroja".

## 3. Data sources (check availability and licences first; keep a cached copy of every download in `prep/cache/`)

| Need | Source | Fallback |
|---|---|---|
| Building footprints, levels, use | OpenStreetMap via Overpass API (area: Catarroja municipality) | Spanish Catastro INSPIRE buildings (WFS) |
| Solar yield per kWp, hourly | PVGIS (EU JRC) API, lat 39.40, lon -0.40, typical year, tilt ~30°, south | Generate a clear-sky profile with `pvlib` |
| Temperature | Open-Meteo or PVGIS TMY | Monthly averages |
| Electricity price shape (duck curve) | OMIE / ESIOS day-ahead prices for Spain | Synthetic curve: cheap 10:00–17:00, expensive 19:00–22:00 |

## 4. Model (all parameters in `config.json`, visible in the UI "assumptions" drawer)

- **Usable roof area** = footprint × 0.5 (pitched) or × 0.6 (flat). Ignore buildings under 40 m².
- **kWp** = usable area ÷ 5.5 m² per kWp. **Annual yield** from PVGIS (about 1,500 kWh/kWp expected for the Valencia area; use the API value).
- **Households** per building = footprint × levels ÷ 90 m² (residential); commercial/industrial get a different profile.
- **Load**: about 3,000 kWh/year per household, shaped by a profile (morning peak, evening peak, higher in summer afternoons for A/C). 3 to 4 archetypes: residential, commercial, industrial, public. Add ±15% random variation per building (fixed seed).
- **"AI" component** (keep it simple and label it honestly):
  1. K-means clustering of simulated hourly profiles into load archetypes per zone.
  2. A gradient-boosting model forecasting hourly zone load from hour, weekday, temperature (trained on the simulated data). Show forecast vs. actual in the zone screen.
- **Zones**: 250 m hexagonal grid over the built-up area (or the town's barrios if clean polygons are found).
- **Battery rule** (per zone): charge from surplus PV, discharge from 18:00 to 23:00, round-trip efficiency 90%, size = the kWh that maximises annual savings per € of battery capex (search over 0 to 4 kWh per kWp).
- **Economics (assumptions, editable)**: PV capex €1,200/kWp, battery capex €450/kWh, retail import price €0.18/kWh, surplus export price €0.05/kWh (midday reduced to near 0), O&M 1%/year, 25-year life, CO₂ factor 0.15 kg/kWh (grid average, indicative).
- **Savings** = avoided imports × retail price + exports × export price. **Payback** = capex ÷ annual savings.
- **Planner**: rank zones by payback, fill the budget greedily, report totals. Show the effect of "with batteries" vs "PV only" side by side (this is the core message: batteries absorb the midday surplus).

## 5. Milestones (stop and check after each)

1. `prep/01_buildings.py` downloads buildings and writes `buildings.geojson`. **Check:** count, a map plot, no empty geometries.
2. `prep/02_solar.py` gets PVGIS hourly yield. **Check:** annual kWh/kWp is plausible and there is a summer/winter difference.
3. `prep/03_loads.py` simulates loads, clusters, trains the forecast. **Check:** the total town consumption is about 29k inhabitants × ~1,100 kWh/capita/year (indicative).
4. `prep/04_economics.py` computes per-building and per-zone results. **Check:** paybacks mostly in the 5–12 year range; print the 10 best and 10 worst buildings.
5. `web/` map screen, then zone screen, then planner screen. **Check:** the slider updates the numbers in under 200 ms.
6. Polish: assumptions drawer, footer note, screenshot export, README with how to run (`python -m http.server`).

## 6. First prompt to paste into Claude Code

> I'm building a demo called EveryRay for a startup pitch: a tool that helps a municipality decide where solar panels and batteries pay back, shown for the town of Catarroja (Valencia, Spain). Read `everyray_catarroja_demo_spec.md` in the project root and follow it exactly. Start only with milestone 1 (download Catarroja buildings from OpenStreetMap via Overpass, cache the result, write `web/data/buildings.geojson` with footprint area, levels and building type, and plot a quick preview PNG). Do not build anything else yet. Ask me before adding any dependency that is not listed in the spec. When milestone 1 passes its check, summarise what you found (number of buildings, any data quality issues) and wait for my go-ahead.

## 7. Demo script (about 2 minutes)

1. "This is Catarroja. Each building is coloured by how fast solar pays back."
2. Click a zone: "Here is the problem: at midday, production far exceeds local use."
3. Toggle the battery: "Our model sizes it so the surplus is used locally in the evening."
4. Planner slider: "With €2M, the municipality gets X kWp, saves €Y per year and avoids Z tonnes of CO₂, instead of spreading the money blindly."
5. Close: "With real smart-meter data in a pilot, these simulated profiles become real forecasts."

## 8. Risks and mitigations

- **Credibility:** every number labelled "indicative"; show the assumptions drawer.
- **Live failure:** record a 60-second backup video and keep screenshots in the deck.
- **Sensitivity:** Catarroja was one of the towns hit by the October 2024 floods. If you mention it, treat it respectfully (resilience and rebuilding), not as a sales hook.
- **Scope creep:** anything not in sections 1 to 4 goes into a "future work" list, not the demo.
