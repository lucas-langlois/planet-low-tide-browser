let map;
let aoiLayer;
let drawLayer;
let drawing = false;
let drawPoints = [];
let currentAoi = null;
let results = [];
let statuses = {};
let selectedId = null;
let previewMode = "aoi";

const $ = (id) => document.getElementById(id);

function log(message) {
  $("messageLog").textContent = `${new Date().toLocaleTimeString()}  ${message}\n${$("messageLog").textContent}`.trim();
}

function initMap() {
  map = L.map("map", { preferCanvas: true }).setView([-19.183638, 146.682512], 10);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap"
  }).addTo(map);
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

function setAoi(aoi) {
  currentAoi = aoi;
  if (aoiLayer) map.removeLayer(aoiLayer);
  if (drawLayer) drawLayer.clearLayers();
  aoiLayer = L.geoJSON(aoi, {
    style: { color: "#f05a28", weight: 3, fillOpacity: 0.12 }
  }).addTo(map);
  map.fitBounds(aoiLayer.getBounds(), { padding: [30, 30] });
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
  }
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
    updatePreview();
    $("resultSummary").textContent = `${results.length} candidates. Tide method: ${data.tide.method}, faces: ${data.tide.n_faces}.`;
    log(`Search complete: ${results.length} candidates.`);
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
      <td><span class="pill ${status}">${status}</span></td>
      <td class="item-id">${item.id}</td>
      <td>${(item.acquired || "").replace("T", " ").slice(0, 16)}</td>
      <td>${item.tide_height ?? ""}</td>
      <td>${item.cloud_cover ?? ""}</td>
      <td>${item.aoi_coverage_percent == null ? "" : item.aoi_coverage_percent.toFixed(1)}</td>
    `;
    row.addEventListener("click", () => {
      selectedId = item.id;
      renderResults();
      updatePreview();
    });
    body.appendChild(row);
  });
}

function selectedItem() {
  return results.find((item) => item.id === selectedId);
}

function updatePreview() {
  const item = selectedItem();
  if (!item) {
    $("previewImage").style.display = "none";
    $("selectedMeta").textContent = "Select a candidate.";
    return;
  }
  $("selectedMeta").textContent = `${item.id} | tide ${item.tide_height ?? "NA"} m | cloud ${item.cloud_cover ?? "NA"}%`;
  $("previewImage").src = `/api/preview/${encodeURIComponent(item.id)}.png?mode=${previewMode}&t=${Date.now()}`;
  $("previewImage").style.display = "block";
}

async function markSelected(status) {
  const item = selectedItem();
  if (!item) return;
  await postJson("/api/status", { item_id: item.id, status });
  statuses[item.id] = status;
  renderResults();
  log(`${item.id} marked ${status}.`);
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
  $("previewAoi").addEventListener("click", () => {
    previewMode = "aoi";
    updatePreview();
  });
  $("previewFull").addEventListener("click", () => {
    previewMode = "full";
    updatePreview();
  });
  $("keepSelected").addEventListener("click", () => markSelected("keep").catch((error) => log(error.message)));
  $("rejectSelected").addEventListener("click", () => markSelected("reject").catch((error) => log(error.message)));
  $("pendingSelected").addEventListener("click", () => markSelected("pending").catch((error) => log(error.message)));
  $("orderSelected").addEventListener("click", () => {
    const item = selectedItem();
    orderItems(item ? [item.id] : []).catch((error) => log(error.message));
  });
  $("orderKept").addEventListener("click", () => {
    const keptIds = Object.entries(statuses).filter((entry) => entry[1] === "keep").map((entry) => entry[0]);
    orderItems(keptIds).catch((error) => log(error.message));
  });
}

initMap();
bindEvents();
loadConfig().catch((error) => log(error.message));
