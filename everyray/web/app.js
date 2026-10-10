/* EveryRay - Catarroja demo app. No build step: plain fetch + Leaflet + Chart.js. */

const DATA_DIR = "data";
const STATE = {
  config: null,
  buildingsGeo: null,
  buildingsEcon: new Map(),     // osm_id -> economics row
  boundaryGeo: null,
  zonesGeo: null,
  zoneEcon: new Map(),          // zone_id -> economics row
  zoneHourly: null,             // zone_id -> [8784] actual load kWh
  zoneForecast: null,           // zone_id -> {hour_of_year, actual, predicted, r2, mae_kwh}
  solarHourly: null,            // [8784] {hour_of_year, month, day, hour, kwh_per_kwp, temp_c}
  solarSeries: null,            // Float64Array [8784] kwh_per_kwp
  map: null,
  buildingsLayer: null,
  zonesLayer: null,
  selectedZoneId: null,
  zoneSim: null,                // cached full-year simulation for the open zone
  chart24h: null,
  chartForecast: null,
  chartPlanner: null,
};

// ---------------------------------------------------------------------------
// Data loading
// ---------------------------------------------------------------------------

async function loadAll() {
  const [config, buildingsGeo, buildingsEconArr, boundaryGeo, zonesGeo, zoneEconArr,
         zoneHourly, zoneForecast, solarHourly] = await Promise.all([
    fetch("config.json").then(r => r.json()),
    fetch(`${DATA_DIR}/buildings.geojson`).then(r => r.json()),
    fetch(`${DATA_DIR}/buildings_economics.json`).then(r => r.json()),
    fetch(`${DATA_DIR}/boundary.geojson`).then(r => r.json()),
    fetch(`${DATA_DIR}/zones.geojson`).then(r => r.json()),
    fetch(`${DATA_DIR}/zone_economics.json`).then(r => r.json()),
    fetch(`${DATA_DIR}/zone_loads_hourly.json`).then(r => r.json()),
    fetch(`${DATA_DIR}/zone_forecast.json`).then(r => r.json()),
    fetch(`${DATA_DIR}/solar_hourly.json`).then(r => r.json()),
  ]);

  STATE.config = config;
  STATE.buildingsGeo = buildingsGeo;
  STATE.boundaryGeo = boundaryGeo;
  STATE.zonesGeo = zonesGeo;
  STATE.zoneHourly = zoneHourly;
  STATE.zoneForecast = zoneForecast;
  STATE.solarHourly = solarHourly;
  STATE.solarSeries = Float64Array.from(solarHourly.map(d => d.kwh_per_kwp));

  for (const row of buildingsEconArr) STATE.buildingsEcon.set(row.osm_id, row);
  for (const row of zoneEconArr) STATE.zoneEcon.set(row.zone_id, row);
}

// ---------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------

function showScreen(name) {
  document.querySelectorAll(".screen").forEach(s => s.classList.remove("active"));
  document.getElementById(`screen-${name}`).classList.add("active");
  document.querySelectorAll(".tab-btn").forEach(b => b.classList.toggle("active", b.dataset.screen === name));
  // charts/map created or updated while their screen was hidden (display:none)
  // get laid out against a zero-size canvas -- force a relayout now that the
  // screen is actually visible
  if (name === "map" && STATE.map) setTimeout(() => STATE.map.invalidateSize(), 50);
  if (name === "zone") { STATE.chart24h?.resize(); STATE.chartForecast?.resize(); }
  if (name === "planner") { STATE.chartPlanner?.resize(); }
}

function initNavigation() {
  document.querySelectorAll(".tab-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      if (btn.disabled) return;
      showScreen(btn.dataset.screen);
    });
  });
  document.querySelectorAll("[data-back]").forEach(btn => {
    btn.addEventListener("click", () => showScreen(btn.dataset.back));
  });
}

// ---------------------------------------------------------------------------
// Map screen
// ---------------------------------------------------------------------------

const EXCLUDED_COLOR = "#C7C7CC";

function computePaybackDomain() {
  const vals = [...STATE.buildingsEcon.values()]
    .map(b => b.payback_years).filter(v => isFinite(v)).sort((a, b) => a - b);
  if (!vals.length) return { lo: 5, hi: 13 };
  const pct = (q) => vals[Math.max(0, Math.min(vals.length - 1, Math.floor(q * (vals.length - 1))))];
  return { lo: pct(0.02), hi: pct(0.98) };
}

// Single-hue heat scale: red = fastest payback (best), fading to white =
// slowest payback (worst). The map's only real color, so it reads clearly
// against the muted, near-monochrome basemap and building outlines.
function paybackColor(years) {
  if (years === null || years === undefined || !isFinite(years)) return EXCLUDED_COLOR;
  const { lo, hi } = STATE.paybackDomain;
  const t = Math.max(0, Math.min(1, (years - lo) / (hi - lo || 1)));
  const red = [179, 38, 30];
  const white = [255, 255, 255];
  const rgb = red.map((c, i) => Math.round(c + (white[i] - c) * t));
  return `rgb(${rgb[0]},${rgb[1]},${rgb[2]})`;
}

function updateLegendLabels() {
  const { lo, hi } = STATE.paybackDomain;
  document.getElementById("legend-lo").textContent = `${lo.toFixed(1)} yr (fastest)`;
  document.getElementById("legend-hi").textContent = `${hi.toFixed(1)} yr (slowest)`;
}


function buildingStyle(feature) {
  const econ = STATE.buildingsEcon.get(feature.properties.osm_id);
  const color = econ ? paybackColor(econ.payback_years) : EXCLUDED_COLOR;
  // Public institutions still show their payback color here; the deep-blue
  // hatch overlay (added separately) marks them as direct-action candidates.
  return { fillColor: color, fillOpacity: 0.92, color: "rgba(0,0,0,0.25)", weight: 0.3 };
}

function buildingPopupHtml(feature) {
  const p = feature.properties;
  const econ = STATE.buildingsEcon.get(p.osm_id);
  const label = econ && econ.name ? econ.name
    : (econ && econ.amenity ? econ.amenity.replace(/_/g, " ") : p.building_type);

  if (!econ) {
    return `
      <div class="popup-title">${label}</div>
      <div class="popup-subtitle">${p.area_m2.toFixed(0)} m² footprint</div>
      <div class="popup-excluded">No clear solar use case — too small, or no consumer to self-consume the production.</div>
    `;
  }

  const badge = econ.is_public_institution
    ? `<div class="popup-badge">Public institution</div>` : "";

  return `
    <div class="popup-title">${label}</div>
    <div class="popup-subtitle">${econ.archetype}</div>
    <div class="popup-headline">
      <span class="popup-headline-value">${econ.payback_years.toFixed(1)}</span>
      <span class="popup-headline-unit">years payback</span>
    </div>
    <div class="popup-grid">
      <div><div class="popup-label">Rooftop PV</div><div class="popup-value">${econ.kwp.toFixed(1)} kWp</div></div>
      <div><div class="popup-label">Annual savings</div><div class="popup-value">€${econ.annual_savings_eur.toFixed(0)}</div></div>
      <div><div class="popup-label">CO₂ avoided/yr</div><div class="popup-value">${(econ.co2_avoided_kg_per_year / 1000).toFixed(2)} t</div></div>
      <div><div class="popup-label">PV capex</div><div class="popup-value">€${econ.pv_capex_eur.toLocaleString()}</div></div>
    </div>
    ${badge}
  `;
}

// Public institutions get a diagonal-hatch overlay instead of a solid
// outline: a separate non-interactive SVG layer (pattern fills need SVG,
// not canvas) drawn on top of the main canvas-rendered buildings, so clicks
// still pass through to the building underneath for its popup.
function addPublicInstitutionHatch(map) {
  const renderer = L.svg({ padding: 0.5 }).addTo(map);
  const svgEl = renderer._container;
  if (svgEl && !svgEl.querySelector("#hatch-public")) {
    const svgNS = "http://www.w3.org/2000/svg";
    const defs = document.createElementNS(svgNS, "defs");
    defs.innerHTML = `
      <pattern id="hatch-public" width="5" height="5" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
        <rect width="5" height="5" fill="transparent"></rect>
        <line x1="0" y1="0" x2="0" y2="5" stroke="#1D3461" stroke-width="1.5"></line>
      </pattern>
    `;
    svgEl.insertBefore(defs, svgEl.firstChild);
  }

  const publicFeatures = STATE.buildingsGeo.features.filter(f => {
    const econ = STATE.buildingsEcon.get(f.properties.osm_id);
    return econ && econ.is_public_institution;
  });

  L.geoJSON({ type: "FeatureCollection", features: publicFeatures }, {
    renderer,
    interactive: false,
    style: () => ({ fill: true, fillColor: "url(#hatch-public)", fillOpacity: 0.8, stroke: false }),
  }).addTo(map);
}

function initMap() {
  const map = L.map("map", { preferCanvas: true, zoomControl: true }).setView([39.401, -0.402], 15);
  STATE.map = map;

  // Standard OSM tiles, heavily muted via CSS filter so the basemap recedes
  // (near-white, faint green/blue hints only) and the payback color-grading
  // on buildings stays the clear focal point.
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "© OpenStreetMap contributors",
    maxZoom: 19,
    className: "basemap-muted",
  }).addTo(map);

  // Municipal boundary: a thin delimiting line, no fill, sitting below
  // everything else.
  if (STATE.boundaryGeo) {
    L.geoJSON(STATE.boundaryGeo, {
      style: () => ({ fill: false, color: "#1D1D1F", weight: 1.3, opacity: 0.45, dashArray: "1 5", lineCap: "round" }),
      interactive: false,
    }).addTo(map);
  }

  // Zone layer is added first so the building layer renders (and hit-tests)
  // on top of it: clicking a building opens its popup, clicking the gaps
  // between buildings (most of a hex's area) opens the Zone screen.
  STATE.zonesLayer = L.geoJSON(STATE.zonesGeo, {
    style: () => ({ fillOpacity: 0, color: "#000000", weight: 0.6, opacity: 0.16 }),
    onEachFeature: (feature, layer) => {
      layer.on("click", () => openZone(feature.properties.zone_id));
      layer.on("mouseover", () => layer.setStyle({ weight: 1.4, opacity: 0.4, fillOpacity: 0.04 }));
      layer.on("mouseout", () => layer.setStyle({ weight: 0.6, opacity: 0.16, fillOpacity: 0 }));
    },
  }).addTo(map);

  STATE.buildingsLayer = L.geoJSON(STATE.buildingsGeo, {
    style: buildingStyle,
    onEachFeature: (feature, layer) => layer.bindPopup(buildingPopupHtml(feature)),
  }).addTo(map);

  addPublicInstitutionHatch(map);

  // deferred, with retries: at init time the map container's layout isn't
  // always finalized yet (varies by embedding context), which makes
  // fitBounds miscompute the zoom level. Retrying a few times at increasing
  // delays is cheap and guarantees correctness regardless of timing.
  const fitToBuildings = () => {
    map.invalidateSize();
    map.fitBounds(STATE.buildingsLayer.getBounds(), { padding: [20, 20] });
  };
  [0, 100, 400].forEach(delay => setTimeout(fitToBuildings, delay));

  document.getElementById("toggle-zones").addEventListener("change", (e) => {
    if (e.target.checked) { STATE.zonesLayer.addTo(map); STATE.zonesLayer.bringToBack(); }
    else map.removeLayer(STATE.zonesLayer);
  });
}

// ---------------------------------------------------------------------------
// Zone screen
// ---------------------------------------------------------------------------

function simulateZoneYear(zoneId) {
  const econ = STATE.zoneEcon.get(zoneId);
  const load = STATE.zoneHourly[zoneId] || new Array(STATE.solarSeries.length).fill(0);
  const n = load.length;
  const kwpTotal = econ ? econ.kwp_total : 0;
  const batteryKwh = econ ? econ.recommended_battery_kwh : 0;
  const dischargeHours = new Set(STATE.config.economics.battery_discharge_hours);
  const eff = STATE.config.economics.battery_round_trip_efficiency;

  const production = new Float64Array(n);
  const netNoBattery = new Float64Array(n);
  const netWithBattery = new Float64Array(n);
  let level = 0;

  for (let h = 0; h < n; h++) {
    const prod = STATE.solarSeries[h] * kwpTotal;
    const ld = load[h];
    production[h] = prod;
    let direct, surplus, unmet;
    if (prod >= ld) { direct = ld; surplus = prod - ld; unmet = 0; }
    else { direct = prod; surplus = 0; unmet = ld - prod; }
    netNoBattery[h] = prod - ld;

    let charge = 0;
    if (batteryKwh > 0 && surplus > 0) {
      charge = Math.min(surplus, batteryKwh - level);
      level += charge;
    }
    const leftover = surplus - charge;

    let dischargeDelivered = 0;
    const hourOfDay = STATE.solarHourly[h].hour;
    if (batteryKwh > 0 && unmet > 0 && dischargeHours.has(hourOfDay) && level > 0) {
      const maxDeliverable = level * eff;
      dischargeDelivered = Math.min(unmet, maxDeliverable);
      level -= dischargeDelivered / eff;
    }
    const importAfter = ld - direct - dischargeDelivered;
    netWithBattery[h] = leftover - importAfter;
  }

  return { load, production, netNoBattery, netWithBattery };
}

function fmtKwh(v) { return `${v.toLocaleString(undefined, { maximumFractionDigits: 0 })} kWh`; }

function openZone(zoneId) {
  STATE.selectedZoneId = zoneId;
  const econ = STATE.zoneEcon.get(zoneId);
  const zoneProps = STATE.zonesGeo.features.find(f => f.properties.zone_id === zoneId)?.properties;

  document.getElementById("tab-zone").disabled = false;
  document.getElementById("zone-title").textContent = `Zone ${zoneId.replace("hex_", "")}`;
  document.getElementById("zone-n-buildings").textContent = zoneProps ? zoneProps.n_buildings : "–";

  if (econ) {
    document.getElementById("zone-kwp").textContent = `${econ.kwp_total.toFixed(0)} kWp`;
    document.getElementById("zone-battery").textContent = `${econ.recommended_battery_kwh.toFixed(0)} kWh`;
    document.getElementById("zone-payback-pv").textContent = `${econ.payback_years_pv_only.toFixed(1)} yr`;
    document.getElementById("zone-payback-batt").textContent = `${econ.payback_years_programme.toFixed(1)} yr`;
    document.getElementById("zone-absorbed").textContent = `${econ.midday_surplus_absorbed_pct.toFixed(1)}%`;
  } else {
    ["zone-kwp", "zone-battery", "zone-payback-pv", "zone-payback-batt", "zone-absorbed"]
      .forEach(id => document.getElementById(id).textContent = "n/a");
  }

  STATE.zoneSim = simulateZoneYear(zoneId);
  showScreen("zone");
  renderZoneDayChart();
  renderZoneForecastChart();
}

function renderZoneDayChart() {
  const day = parseInt(document.getElementById("day-slider").value, 10);
  const showBattery = document.getElementById("toggle-battery").checked;
  const sim = STATE.zoneSim;
  if (!sim) return;

  const start = day * 24;
  const hours = Array.from({ length: 24 }, (_, i) => i);
  const dayInfo = STATE.solarHourly[start];
  document.getElementById("day-of-year-label").textContent =
    dayInfo ? `${dayInfo.day}/${dayInfo.month}` : `day ${day}`;

  const consumption = Array.from(sim.load.slice(start, start + 24));
  const production = Array.from(sim.production.slice(start, start + 24));
  const net = Array.from((showBattery ? sim.netWithBattery : sim.netNoBattery).slice(start, start + 24));

  const ctx = document.getElementById("chart-24h");
  const datasets = [
    { label: "Consumption (kWh)", data: consumption, borderColor: "#1D1D1F", borderWidth: 2.2, backgroundColor: "transparent", tension: 0.3 },
    { label: "Solar production (kWh)", data: production, borderColor: "#AEAEB2", backgroundColor: "rgba(142,142,147,0.16)", fill: true, tension: 0.3 },
    { label: showBattery ? "Net grid, with battery (+export/-import)" : "Net grid, PV only (+export/-import)",
      data: net, borderColor: "#58181C", borderDash: [4, 3], tension: 0.3 },
  ];

  if (STATE.chart24h) {
    STATE.chart24h.data.labels = hours;
    STATE.chart24h.data.datasets = datasets;
    STATE.chart24h.update();
  } else {
    STATE.chart24h = new Chart(ctx, {
      type: "line",
      data: { labels: hours, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        scales: { x: { title: { display: true, text: "hour of day" } }, y: { title: { display: true, text: "kWh" } } },
        plugins: { legend: { position: "bottom" } },
      },
    });
  }
}

function renderZoneForecastChart() {
  const fc = STATE.zoneForecast[STATE.selectedZoneId];
  const ctx = document.getElementById("chart-forecast");
  if (!fc) {
    if (STATE.chartForecast) { STATE.chartForecast.destroy(); STATE.chartForecast = null; }
    return;
  }
  const n = Math.min(336, fc.actual.length); // first 2 weeks of the Nov-Dec test period
  const labels = fc.hour_of_year.slice(0, n);
  const datasets = [
    { label: "Simulated actual", data: fc.actual.slice(0, n), borderColor: "#1D1D1F", borderWidth: 2, pointRadius: 0, tension: 0.2 },
    { label: `AI forecast (R²=${fc.r2.toFixed(2)})`, data: fc.predicted.slice(0, n), borderColor: "#AEAEB2", pointRadius: 0, borderDash: [3, 2], tension: 0.2 },
  ];
  if (STATE.chartForecast) {
    STATE.chartForecast.data.labels = labels;
    STATE.chartForecast.data.datasets = datasets;
    STATE.chartForecast.update();
  } else {
    STATE.chartForecast = new Chart(ctx, {
      type: "line",
      data: { labels, datasets },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        scales: { x: { title: { display: true, text: "hour of year (test period)" } }, y: { title: { display: true, text: "kWh" } } },
        plugins: { legend: { position: "bottom" } },
      },
    });
  }
}

function initZoneControls() {
  document.getElementById("day-slider").addEventListener("input", renderZoneDayChart);
  document.getElementById("toggle-battery").addEventListener("change", renderZoneDayChart);
}

// ---------------------------------------------------------------------------
// Planner screen
// ---------------------------------------------------------------------------

function greedyFill(items, budget, rankKey, costFn) {
  const sorted = [...items].sort((a, b) => a[rankKey] - b[rankKey]);
  let spent = 0;
  const picked = [];
  for (const item of sorted) {
    if (spent >= budget) break;
    const cost = costFn(item);
    if (cost <= 0) continue;
    const remaining = budget - spent;
    const frac = remaining >= cost ? 1 : remaining / cost;
    picked.push({ item, frac });
    spent += cost * frac;
    if (frac < 1) break;
  }
  return picked;
}

function aggregate(picked, fields) {
  const out = {};
  for (const key of Object.keys(fields)) out[key] = 0;
  for (const { item, frac } of picked) {
    for (const [key, field] of Object.entries(fields)) out[key] += item[field] * frac;
  }
  return out;
}

function fmtEur(v) { return `€${Math.round(v).toLocaleString()}`; }

function renderPlanner() {
  const budget = parseInt(document.getElementById("budget-slider").value, 10);
  const publicPct = parseInt(document.getElementById("split-slider").value, 10);
  document.getElementById("budget-label").textContent = fmtEur(budget);
  const publicBudget = budget * publicPct / 100;
  const privateBudget = budget - publicBudget;
  document.getElementById("split-label").textContent =
    `${fmtEur(publicBudget)} direct / ${fmtEur(privateBudget)} subsidies`;
  const subsidyRate = STATE.config.economics.subsidy_rate_private;
  document.getElementById("subsidy-rate-note").textContent = `${Math.round(subsidyRate * 100)}%`;

  // --- Direct action: the town's own buildings, funded at full capex -------
  const publicBuildings = [...STATE.buildingsEcon.values()].filter(b => b.is_public_institution);
  const pubPicked = greedyFill(publicBuildings, publicBudget, "payback_years", b => b.pv_capex_eur);
  const pub = aggregate(pubPicked, {
    kwp: "kwp", capex: "pv_capex_eur", savings: "annual_savings_eur", co2: "co2_avoided_kg_per_year",
  });
  document.getElementById("pub-kwp").textContent = `${pub.kwp.toFixed(0)} kWp`;
  document.getElementById("pub-capex").textContent = fmtEur(pub.capex);
  document.getElementById("pub-savings").textContent = fmtEur(pub.savings) + "/yr";
  document.getElementById("pub-co2").textContent = `${(pub.co2 / 1000).toFixed(1)} t/yr`;
  document.getElementById("pub-payback").textContent = pub.savings > 0 ? `${(pub.capex / pub.savings).toFixed(1)} yr` : "n/a";
  document.getElementById("pub-count").textContent = pubPicked.length;
  const topNames = pubPicked.slice(0, 4).map(({ item }) => item.name || item.building_type).join(", ");
  document.getElementById("pub-detail").textContent = topNames ? `Funded: ${topNames}${pubPicked.length > 4 ? "…" : ""}` : "";

  // --- Subsidies: private zones (residential/commercial/industrial only,   -
  // public buildings excluded so the two budgets never double-fund the same
  // building), town pays subsidy_rate of PV+battery cost, owner pays the rest
  const privateZones = [...STATE.zoneEcon.values()].filter(z => (z.kwp_total_private || 0) > 0);
  const privPicked = greedyFill(privateZones, privateBudget, "payback_years_programme_private",
    z => (z.pv_capex_eur_private + z.battery_capex_eur_private) * subsidyRate);
  const priv = aggregate(privPicked, {
    kwp: "kwp_total_private", capex_pv: "pv_capex_eur_private", capex_batt: "battery_capex_eur_private",
    savings: "annual_savings_with_battery_eur_private", co2: "co2_avoided_kg_with_battery_private",
    midday_pv: "midday_export_pv_only_kwh_private", midday_batt: "midday_export_with_battery_kwh_private",
  });
  const privFullCapex = priv.capex_pv + priv.capex_batt;
  const privSubsidy = privFullCapex * subsidyRate;
  const privOwnerCapex = privFullCapex - privSubsidy;
  document.getElementById("priv-kwp").textContent = `${priv.kwp.toFixed(0)} kWp`;
  document.getElementById("priv-subsidy").textContent = fmtEur(privSubsidy);
  document.getElementById("priv-leveraged").textContent = fmtEur(privFullCapex);
  document.getElementById("priv-savings").textContent = fmtEur(priv.savings) + "/yr";
  document.getElementById("priv-co2").textContent = `${(priv.co2 / 1000).toFixed(1)} t/yr`;
  document.getElementById("priv-payback").textContent =
    priv.savings > 0 ? `${(privOwnerCapex / priv.savings).toFixed(1)} yr` : "n/a";
  document.getElementById("priv-zones").textContent = privPicked.length;
  const absorbed = priv.midday_pv > 0 ? (1 - priv.midday_batt / priv.midday_pv) * 100 : 0;
  document.getElementById("priv-absorbed").textContent = `${absorbed.toFixed(1)}%`;

  renderPlannerChart(pub, priv);
}

function renderPlannerChart(pub, priv) {
  const ctx = document.getElementById("chart-planner");
  const data = {
    labels: ["kWp installed", "Annual savings (€)", "CO₂ avoided (t/yr)"],
    datasets: [
      {
        label: "Direct (public)",
        data: [pub.kwp, pub.savings, pub.co2 / 1000],
        backgroundColor: "#1D1D1F",
      },
      {
        label: "Subsidized (private)",
        data: [priv.kwp, priv.savings, priv.co2 / 1000],
        backgroundColor: "#AEAEB2",
      },
    ],
  };
  const opts = {
    responsive: true, maintainAspectRatio: false, animation: false,
    scales: {
      ykwp: { type: "linear", position: "left", display: false },
    },
    plugins: { legend: { position: "bottom" }, title: { display: true, text: "Each metric scaled to its own max (bar height is comparative, not absolute)" } },
  };
  // normalize each metric 0-1 across both scenarios so wildly different units
  // (kWp, euros, tonnes) are visually comparable bar heights
  const maxes = data.labels.map((_, i) => Math.max(data.datasets[0].data[i], data.datasets[1].data[i], 1e-9));
  const normalized = {
    labels: data.labels,
    datasets: data.datasets.map(ds => ({ ...ds, data: ds.data.map((v, i) => v / maxes[i]) })),
  };

  if (STATE.chartPlanner) {
    STATE.chartPlanner.data = normalized;
    STATE.chartPlanner.update();
  } else {
    STATE.chartPlanner = new Chart(ctx, {
      type: "bar",
      data: normalized,
      options: {
        ...opts,
        scales: { y: { display: false, max: 1.15 } },
        plugins: {
          ...opts.plugins,
          tooltip: {
            callbacks: {
              label: (item) => {
                const raw = data.datasets[item.datasetIndex].data[item.dataIndex];
                const label = data.labels[item.dataIndex];
                if (label.includes("€")) return `${item.dataset.label}: €${Math.round(raw).toLocaleString()}`;
                if (label.includes("CO")) return `${item.dataset.label}: ${raw.toFixed(1)} t/yr`;
                return `${item.dataset.label}: ${raw.toFixed(0)} kWp`;
              },
            },
          },
        },
      },
    });
  }
}

function initPlannerControls() {
  document.getElementById("budget-slider").addEventListener("input", renderPlanner);
  document.getElementById("split-slider").addEventListener("input", renderPlanner);
}

// ---------------------------------------------------------------------------
// Assumptions drawer
// ---------------------------------------------------------------------------

function renderAssumptions() {
  const c = STATE.config;
  const sections = [
    ["Load simulation", c.load, {
      residential_kwh_per_household: "Residential kWh / household / year",
      commercial_kwh_per_m2: "Commercial kWh / m² / year",
      industrial_kwh_per_m2: "Industrial kWh / m² / year",
      public_kwh_per_m2: "Public kWh / m² / year",
      building_variation_pct: "Per-building random variation",
      household_m2_per_unit: "m² per household (non single-family)",
      max_households_per_building: "Max households per building (OSM merged-block guard)",
      assumed_persons_per_household: "Assumed persons / household",
      min_building_area_m2: "Ignore buildings under (m²)",
    }],
    ["Solar", c.solar, { lat: "Latitude", lon: "Longitude", tilt_deg: "Tilt (deg)", azimuth_deg: "Azimuth (0=south)", pvgis_year: "PVGIS reference year" }],
    ["Zones", c.zones, { hex_spacing_m: "Hex zone spacing (m)" }],
    ["Economics", c.economics, {
      m2_per_kwp: "m² of usable roof per kWp",
      pitched_roof_usable_fraction: "Usable roof fraction (pitched / single-family)",
      flat_roof_usable_fraction: "Usable roof fraction (flat / other)",
      pv_capex_eur_per_kwp: "PV capex (€/kWp)",
      battery_capex_eur_per_kwh: "Battery capex (€/kWh)",
      retail_import_price_eur_per_kwh: "Retail import price (€/kWh)",
      export_price_eur_per_kwh: "Export price, off-peak (€/kWh)",
      export_price_midday_eur_per_kwh: "Export price, midday (€/kWh)",
      om_pct_of_capex_per_year: "O&M (% of capex / year)",
      project_lifetime_years: "Project lifetime (years)",
      co2_factor_kg_per_kwh: "Grid CO₂ factor (kg/kWh)",
      battery_round_trip_efficiency: "Battery round-trip efficiency",
      residential_max_production_to_load_ratio: "Residential PV sizing cap (x annual load)",
      subsidy_rate_private: "Private subsidy rate (town share of PV+battery cost)",
    }],
    ["Public sector (direct-action eligibility)", c.public_sector, {
      amenities: "OSM amenity tags counted as town property",
      leisure: "OSM leisure tags counted as town property",
      offices: "OSM office tags counted as town property",
      building_types: "OSM building tags counted as town property (no amenity needed)",
    }],
  ];

  const html = sections.map(([title, obj, labels]) => {
    const rows = Object.entries(labels).map(([key, label]) => {
      let v = obj[key];
      if (Array.isArray(v)) v = v.join(", ");
      return `<tr><td>${label}</td><td>${v}</td></tr>`;
    }).join("");
    return `<section><h4>${title}</h4><table>${rows}</table></section>`;
  }).join("");

  document.getElementById("assumptions-content").innerHTML =
    `<p style="font-size:0.85rem;color:#555">Random seed: <b>${c.random_seed}</b> (every simulated number is reproducible). ` +
    `All consumption is simulated; PVGIS solar and OSM buildings are real open data.</p>` + html;
}

function initAssumptionsDrawer() {
  document.getElementById("btn-assumptions").addEventListener("click", () => {
    renderAssumptions();
    document.getElementById("assumptions-overlay").hidden = false;
  });
  document.getElementById("btn-close-assumptions").addEventListener("click", () => {
    document.getElementById("assumptions-overlay").hidden = true;
  });
  document.getElementById("assumptions-overlay").addEventListener("click", (e) => {
    if (e.target.id === "assumptions-overlay") e.target.hidden = true;
  });
}

// ---------------------------------------------------------------------------
// Screenshot export
// ---------------------------------------------------------------------------

function initScreenshot() {
  document.getElementById("btn-screenshot").addEventListener("click", async () => {
    const active = document.querySelector(".screen.active");
    const canvas = await html2canvas(active, { backgroundColor: "#F5F5F7" });
    const link = document.createElement("a");
    link.download = `everyray-${active.id}-${Date.now()}.png`;
    link.href = canvas.toDataURL("image/png");
    link.click();
  });
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

async function main() {
  await loadAll();
  STATE.paybackDomain = computePaybackDomain();
  updateLegendLabels();
  initNavigation();
  initMap();
  initZoneControls();
  initPlannerControls();
  initAssumptionsDrawer();
  initScreenshot();
  renderPlanner();
}

main().catch(err => {
  console.error(err);
  document.body.innerHTML = `<div style="padding:2rem;font-family:sans-serif">
    <h2>EveryRay failed to load</h2><pre>${err.stack || err}</pre>
    <p>Make sure you're running this through a local server (e.g. <code>python -m http.server</code>) from the <code>web/</code> folder, not opening index.html directly as a file.</p></div>`;
});
