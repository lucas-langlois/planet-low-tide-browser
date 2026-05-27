let map;
let aoiLayer;
let drawLayer;
let satelliteLayer;
let streetLayer;
let planetOverlayLayer = null;
let drawing = false;
let drawPoints = [];
let currentAoi = null;
let results = [];
let statuses = {};
let selectedId = null;
let apiValidationTimer = null;
let apiValidationRequestId = 0;

const $ = (id) => document.getElementById(id);

function log(message) {
  $("messageLog").textContent = `${new Date().toLocaleTimeString()}  ${message}\n${$("messageLog").textContent}`.trim();
}

function initMap() {
  map = L.map("map", { preferCanvas: true }).setView([-19.183638, 146.682512], 10);

  satelliteLayer = L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    {
      maxZoom: 19,
      attribution: "Tiles &copy; Esri"
    }
  ).addTo(map);
  streetLayer = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap"
  });
  addBasemapControl();

  drawLayer = L.layerGroup().addTo(map);

  map.on("click", (event) => {
    if (!drawing) return;
    event.originalEvent.preventDefault();
    drawPoints.push([event.latlng.lng, event.latlng.lat]);
    drawAoiPreview();
    log(`${drawPoints.length} AOI ${drawPoints.length === 1 ? "vertex" : "vertices"} added.`);
  });

  map.on("dblclick", (event) => {
    event.originalEvent.preventDefault();
    if (drawing) saveDrawnAoi();
  });
}

function addBasemapControl() {
  const BasemapControl = L.Control.extend({
    options: { position: "topright" },
    onAdd: () => {
      const container = L.DomUtil.create("div", "basemap-control");
      container.innerHTML = `
        <button type="button" class="basemap-btn active" data-layer="satellite">Satellite</button>
        <button type="button" class="basemap-btn" data-layer="street">Street</button>
      `;
      L.DomEvent.disableClickPropagation(container);
      L.DomEvent.disableScrollPropagation(container);
      container.querySelectorAll("button").forEach((button) => {
        button.addEventListener("click", () => switchBasemap(button.dataset.layer));
      });
      return container;
    }
  });
  map.addControl(new BasemapControl());
}

function switchBasemap(layerName) {
  if (layerName === "street") {
    if (map.hasLayer(satelliteLayer)) map.removeLayer(satelliteLayer);
    if (!map.hasLayer(streetLayer)) streetLayer.addTo(map);
  } else {
    if (map.hasLayer(streetLayer)) map.removeLayer(streetLayer);
    if (!map.hasLayer(satelliteLayer)) satelliteLayer.addTo(map);
  }
  document.querySelectorAll(".basemap-btn").forEach((button) => {
    button.classList.toggle("active", button.dataset.layer === layerName);
  });
}

function setAoi(aoi) {
  currentAoi = aoi;
  if (aoiLayer) map.removeLayer(aoiLayer);
  if (drawLayer) drawLayer.clearLayers();
  aoiLayer = L.geoJSON(aoi, {
    style: { color: "#f05a28", weight: 3, fillOpacity: 0.12 }
  }).addTo(map);
  map.fitBounds(aoiLayer.getBounds(), { padding: [30, 30] });
}

function updatePlanetOverlay() {
  const item = selectedItem();
  const showOverlay = $("showPlanetOverlay")?.checked ?? true;

  if (planetOverlayLayer) {
    map.removeLayer(planetOverlayLayer);
    planetOverlayLayer = null;
  }

  if (!item || !showOverlay) {
    $("mapOverlayLabel").textContent = item ? "Selected image hidden." : "Select a candidate to show it on the map.";
    return;
  }

  const itemType = item.item_type || "PSScene";
  const opacity = Number($("planetOpacity")?.value ?? 70) / 100;
  planetOverlayLayer = L.tileLayer(
    `/planet-tiles/${encodeURIComponent(itemType)}/${encodeURIComponent(item.id)}/{z}/{x}/{y}.png`,
    {
      maxZoom: 19,
      opacity,
      attribution: "Planet preview"
    }
  ).addTo(map);

  if (aoiLayer) {
    aoiLayer.bringToFront();
    map.fitBounds(aoiLayer.getBounds(), { padding: [30, 30] });
  }
  $("mapOverlayLabel").textContent = `${item.id} on map`;
}

function setPlanetOverlayOpacity() {
  if (!planetOverlayLayer) return;
  planetOverlayLayer.setOpacity(Number($("planetOpacity").value) / 100);
}

function drawAoiPreview() {
  if (aoiLayer) {
    map.removeLayer(aoiLayer);
    aoiLayer = null;
  }
  drawLayer.clearLayers();
  if (drawPoints.length === 0) return;

  const latLngs = drawPoints.map((point) => [point[1], point[0]]);
  latLngs.forEach((latLng, index) => {
    L.circleMarker(latLng, {
      radius: 5,
      color: "#f05a28",
      fillColor: "#ffffff",
      fillOpacity: 1,
      weight: 2
    }).bindTooltip(`${index + 1}`, { permanent: false }).addTo(drawLayer);
  });

  if (latLngs.length >= 2) {
    L.polyline(latLngs, { color: "#f05a28", weight: 3, dashArray: "6 5" }).addTo(drawLayer);
  }

  if (drawPoints.length >= 3) {
    const coords = [...drawPoints, drawPoints[0]];
    currentAoi = { type: "Polygon", coordinates: [coords] };
    L.polygon(latLngs, {
      color: "#f05a28",
      weight: 3,
      fillOpacity: 0.12
    }).addTo(drawLayer);
  } else {
    currentAoi = null;
  }
}

function switchMode(mode) {
  document.querySelectorAll(".mode-btn").forEach((btn) => btn.classList.toggle("active", btn.dataset.mode === mode));
  $("drawPanel").classList.toggle("hidden", mode !== "draw");
  $("centerPanel").classList.toggle("hidden", mode !== "center");
  $("uploadPanel").classList.toggle("hidden", mode !== "upload");
}

async function postJson(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed: ${response.status}`);
  return data;
}

async function loadConfig() {
  const response = await fetch("/api/config");
  const config = await response.json();
  $("runtimeStatus").textContent = config.model_exists
    ? `CSIRO model found. Item type: ${config.item_type}`
    : `CSIRO model missing: ${config.model_path}`;
  if (config.has_api_key) {
    $("apiHint").textContent = `Using configured API key ${config.masked_api_key} unless you paste another one.`;
    setApiStatus("idle", "○", "Paste a key to validate it.");
  }
}

function setApiStatus(state, icon, text) {
  const status = $("apiStatus");
  status.dataset.state = state;
  status.querySelector(".api-status-icon").textContent = icon;
  status.querySelector(".api-status-text").textContent = text;
}

function scheduleApiValidation() {
  const key = $("apiKey").value.trim();
  apiValidationRequestId += 1;
  const requestId = apiValidationRequestId;
  clearTimeout(apiValidationTimer);

  if (!key) {
    setApiStatus("idle", "○", "Paste a key to validate it.");
    return;
  }

  if (key.length < 16) {
    setApiStatus("invalid", "!", "Key looks too short.");
    return;
  }

  setApiStatus("checking", "…", "Checking Planet key...");
  apiValidationTimer = setTimeout(async () => {
    try {
      const data = await postJson("/api/validate-key", { api_key: key });
      if (requestId !== apiValidationRequestId) return;
      setApiStatus("valid", "✓", `Valid and ready: ${data.masked_api_key}`);
    } catch (error) {
      if (requestId !== apiValidationRequestId) return;
      setApiStatus("invalid", "!", error.message);
    }
  }, 600);
}

async function createSquareAoi() {
  const data = await postJson("/api/aoi/square", {
    lat: $("centerLat").value,
    lon: $("centerLon").value,
    area_km2: $("areaKm2").value
  });
  setAoi(data.aoi);
  log("Square AOI saved.");
}

async function uploadAoi() {
  const file = $("aoiFile").files[0];
  if (!file) {
    log("Choose an AOI file first.");
    return;
  }
  const form = new FormData();
  form.append("file", file);
  const response = await fetch("/api/aoi/upload", { method: "POST", body: form });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "AOI upload failed.");
  setAoi(data.aoi);
  log("AOI uploaded.");
}

async function saveDrawnAoi() {
  if (!currentAoi || drawPoints.length < 3) {
    log("Draw at least three vertices.");
    return;
  }
  drawing = false;
  map.doubleClickZoom.enable();
  const data = await postJson("/api/aoi/drawn", { aoi: currentAoi });
  setAoi(data.aoi);
  log("Drawn AOI saved.");
}

async function queryPlanet() {
  if (!currentAoi) {
    log("Set an AOI before searching.");
    return;
  }
  $("searchPlanet").disabled = true;
  $("resultSummary").textContent = "Searching Planet and predicting tides...";
  try {
    const data = await postJson("/api/search", {
      api_key: $("apiKey").value,
      aoi: currentAoi,
      start_date: $("startDate").value,
      end_date: $("endDate").value,
      max_cloud: $("maxCloud").value,
      min_aoi_coverage: $("minCoverage").value,
      max_results: $("maxResults").value,
      predict_tides: $("predictTides").checked
    });
    results = data.items || [];
    statuses = Object.fromEntries(results.map((item) => [item.id, "pending"]));
    selectedId = results.length ? results[0].id : null;
    renderResults();
    updatePlanetOverlay();
    const timezoneText = data.time?.timezone ? ` Local time: ${data.time.timezone}.` : "";
    $("resultSummary").textContent = `${results.length} candidates. Tide method: ${data.tide.method}, faces: ${data.tide.n_faces}.${timezoneText}`;
    log(`Search complete: ${results.length} candidates. Planet auth: ${data.key_source} ${data.masked_api_key}.`);
  } catch (error) {
    $("resultSummary").textContent = "Search failed.";
    log(error.message);
  } finally {
    $("searchPlanet").disabled = false;
  }
}

function renderResults() {
  const body = $("resultsBody");
  body.innerHTML = "";
  results.forEach((item, index) => {
    const status = statuses[item.id] || "pending";
    const row = document.createElement("tr");
    row.classList.toggle("selected", item.id === selectedId);
    row.innerHTML = `
      <td>${index + 1}</td>
      <td class="decision-cell">
        <label class="keep-toggle">
          <input type="checkbox" data-item-id="${item.id}" ${status === "keep" ? "checked" : ""}>
          Keep
        </label>
        <span class="pill ${status}">${status}</span>
      </td>
      <td class="item-id">${item.id}</td>
      <td title="UTC: ${(item.acquired_utc || item.acquired || "").replace("T", " ")}">${item.acquired_local || (item.acquired || "").replace("T", " ").slice(0, 16)}</td>
      <td>${item.tide_height ?? ""}</td>
      <td>${item.cloud_cover ?? ""}</td>
      <td>${item.aoi_coverage_percent == null ? "" : item.aoi_coverage_percent.toFixed(1)}</td>
    `;
    row.querySelector("input").addEventListener("click", (event) => event.stopPropagation());
    row.querySelector("input").addEventListener("change", (event) => {
      const nextStatus = event.target.checked ? "keep" : "pending";
      setItemStatus(item.id, nextStatus).catch((error) => log(error.message));
    });
    row.addEventListener("click", () => {
      selectedId = item.id;
      renderResults();
      updatePlanetOverlay();
    });
    body.appendChild(row);
  });
  updateDecisionSummary();
}

function selectedItem() {
  return results.find((item) => item.id === selectedId);
}

function updateDecisionSummary() {
  const counts = { keep: 0, reject: 0, pending: 0 };
  results.forEach((item) => {
    counts[statuses[item.id] || "pending"] += 1;
  });
  $("decisionSummary").textContent = `${counts.keep} kept, ${counts.reject} rejected, ${counts.pending} pending.`;
}

async function setItemStatus(itemId, status) {
  await postJson("/api/status", { item_id: itemId, status });
  statuses[itemId] = status;
  renderResults();
  if (itemId === selectedId) updatePlanetOverlay();
  log(`${itemId} marked ${status}.`);
}

async function rejectUnkeptItems() {
  const unkeptIds = results.filter((item) => (statuses[item.id] || "pending") !== "keep").map((item) => item.id);
  if (!unkeptIds.length) {
    log("No unkept items to reject.");
    return;
  }
  const data = await postJson("/api/status/bulk", { item_ids: unkeptIds, status: "reject" });
  statuses = data.statuses || statuses;
  renderResults();
  updatePlanetOverlay();
  log(`${unkeptIds.length} unkept item(s) marked reject.`);
}

async function orderItems(itemIds) {
  if (!itemIds.length) {
    log("No items selected for order.");
    return;
  }
  log(`Creating Planet order for ${itemIds.length} item(s). This can take several minutes.`);
  const result = await postJson("/api/order", {
    item_ids: itemIds,
    clip_to_aoi: true,
    api_key: $("apiKey").value
  });
  if (result.state === "success") {
    const lines = (result.results || []).map((entry) => `${entry.name || "download"}: ${entry.location || ""}`);
    log(`Order ${result.order_id} complete.\n${lines.join("\n")}`);
  } else {
    log(`Order ${result.order_id || ""} state: ${result.state || "unknown"}`);
  }
}

function bindEvents() {
  document.querySelectorAll(".mode-btn").forEach((btn) => btn.addEventListener("click", () => switchMode(btn.dataset.mode)));
  $("startDraw").addEventListener("click", () => {
    drawing = true;
    drawPoints = [];
    currentAoi = null;
    map.doubleClickZoom.disable();
    if (aoiLayer) {
      map.removeLayer(aoiLayer);
      aoiLayer = null;
    }
    drawLayer.clearLayers();
    log("Drawing started.");
  });
  $("undoPoint").addEventListener("click", () => {
    drawPoints.pop();
    drawAoiPreview();
  });
  $("clearAoi").addEventListener("click", () => {
    drawing = false;
    map.doubleClickZoom.enable();
    drawPoints = [];
    currentAoi = null;
    if (aoiLayer) map.removeLayer(aoiLayer);
    drawLayer.clearLayers();
    log("AOI cleared.");
  });
  $("saveDrawnAoi").addEventListener("click", () => saveDrawnAoi().catch((error) => log(error.message)));
  $("createSquareAoi").addEventListener("click", () => createSquareAoi().catch((error) => log(error.message)));
  $("uploadAoi").addEventListener("click", () => uploadAoi().catch((error) => log(error.message)));
  $("searchPlanet").addEventListener("click", queryPlanet);
  $("showPlanetOverlay").addEventListener("change", updatePlanetOverlay);
  $("planetOpacity").addEventListener("input", setPlanetOverlayOpacity);
  $("apiKey").addEventListener("input", scheduleApiValidation);
  $("apiKey").addEventListener("paste", () => setTimeout(scheduleApiValidation, 0));
  $("rejectUnkept").addEventListener("click", () => rejectUnkeptItems().catch((error) => log(error.message)));
  $("orderKept").addEventListener("click", () => {
    const keptIds = Object.entries(statuses).filter((entry) => entry[1] === "keep").map((entry) => entry[0]);
    orderItems(keptIds).catch((error) => log(error.message));
  });
}

initMap();
bindEvents();
loadConfig().catch((error) => log(error.message));
