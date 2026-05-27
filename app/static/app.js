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

function setAoiSummary(summary) {
  if (!summary) return;
  $("aoiSummary").classList.remove("hidden");
  $("aoiSummaryName").textContent = summary.name || "AOI";
  $("aoiSummaryArea").textContent = `${summary.area_km2 ?? 0} km²`;
}

function clearAoiSummary() {
  $("aoiSummary").classList.add("hidden");
  $("aoiSummaryName").textContent = "Not set";
  $("aoiSummaryArea").textContent = "0 km²";
}

function setAoi(aoi, summary = null) {
  currentAoi = aoi;
  if (aoiLayer) map.removeLayer(aoiLayer);
  if (drawLayer) drawLayer.clearLayers();
  aoiLayer = L.geoJSON(aoi, {
    style: { color: "#f05a28", weight: 3, fillOpacity: 0.12 }
  }).addTo(map);
  map.fitBounds(aoiLayer.getBounds(), { padding: [30, 30] });
  setAoiSummary(summary);
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

async function getJson(url) {
  const response = await fetch(url);
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
  setAoi(data.aoi, data.summary);
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
  setAoi(data.aoi, data.summary);
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
  setAoi(data.aoi, data.summary);
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
    const hiddenText = data.dedupe?.hidden ? ` ${data.dedupe.hidden} same-time scene(s) hidden by keeping best AOI coverage.` : "";
    $("resultSummary").textContent = `${results.length} candidates. Tide method: ${data.tide.method}, faces: ${data.tide.n_faces}.${timezoneText}${hiddenText}`;
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

function keptItemIds() {
  return results.filter((item) => statuses[item.id] === "keep").map((item) => item.id);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#39;"
  })[char]);
}

function defaultOrderName() {
  const now = new Date();
  const stamp = [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, "0"),
    String(now.getDate()).padStart(2, "0"),
    String(now.getHours()).padStart(2, "0"),
    String(now.getMinutes()).padStart(2, "0")
  ].join("");
  return `planet_low_tide_${stamp}`;
}

function orderOptions() {
  const asset = document.querySelector("input[name='orderAsset']:checked")?.value || "visual";
  return {
    order_name: $("orderName").value.trim(),
    asset_key: asset,
    clip_to_aoi: $("orderClip").checked,
    composite: $("orderComposite").checked,
    harmonize: $("orderHarmonize").checked
  };
}

function updateOrderToolAvailability() {
  const asset = document.querySelector("input[name='orderAsset']:checked")?.value || "visual";
  const harmonize = $("orderHarmonize");
  if (asset === "visual") {
    harmonize.checked = false;
    harmonize.disabled = true;
  } else {
    harmonize.disabled = false;
  }
}

function renderOrderSummary(estimate) {
  const tools = [];
  if (estimate.tools?.clip_to_aoi) tools.push("Clip to AOI");
  if (estimate.tools?.composite) tools.push("Composite");
  if (estimate.tools?.harmonize) tools.push("Harmonize to Sentinel-2");
  const warnings = (estimate.warnings || []).map((warning) => `<li>${escapeHtml(warning)}</li>`).join("");
  $("orderSummary").innerHTML = `
    <div class="summary-list">
      <div class="summary-line"><span>Items</span><strong>${estimate.item_count}</strong></div>
      <div class="summary-line"><span>Output images</span><strong>${estimate.output_images}</strong></div>
      <div class="summary-line"><span>Asset type</span><strong>${escapeHtml(estimate.asset_label)}</strong></div>
      <div class="summary-line"><span>Bundle</span><strong>${escapeHtml(estimate.product_bundle)}</strong></div>
      <div class="summary-line"><span>AOI area</span><strong>${estimate.aoi_area_km2} km²</strong></div>
      <div class="summary-line"><span>AOI-intersection area</span><strong>${estimate.estimated_aoi_intersection_km2} km²</strong></div>
      <div class="summary-line"><span>Processed area estimate</span><strong>${estimate.estimated_processed_area_km2} km²</strong></div>
      <div class="summary-line"><span>Education quota use</span><strong>${estimate.education_quota_percent}% of ${estimate.education_monthly_quota_km2} km²/month</strong></div>
      <div class="summary-line"><span>Tools</span><strong>${tools.length ? escapeHtml(tools.join(", ")) : "None"}</strong></div>
    </div>
    ${warnings ? `<ul class="warning-list">${warnings}</ul>` : ""}
    <p class="summary-note">Estimate uses AOI geometry and kept-scene coverage to compare expected quota use with the standard 3,000 km²/month education-account quota. Planet calculates the final quota when the order runs.</p>
  `;
  $("submitOrder").disabled = !estimate.can_order;
}

async function refreshOrderEstimate() {
  const ids = keptItemIds();
  const options = orderOptions();
  if (!ids.length) {
    $("orderSummary").textContent = "Keep at least one candidate before ordering.";
    $("submitOrder").disabled = true;
    return;
  }
  if (!options.order_name) {
    $("orderSummary").textContent = "Enter an order name to estimate and place the order.";
    $("submitOrder").disabled = true;
    return;
  }
  $("orderSummary").textContent = "Estimating order...";
  const estimate = await postJson("/api/order/estimate", { item_ids: ids, ...options });
  renderOrderSummary(estimate);
}

async function openOrderModal() {
  const ids = keptItemIds();
  if (!ids.length) {
    log("No kept items selected for order.");
    return;
  }
  if (!$("orderName").value.trim()) {
    $("orderName").value = defaultOrderName();
  }
  updateOrderToolAvailability();
  $("orderModal").classList.remove("hidden");
  await refreshOrderEstimate();
}

function closeOrderModal() {
  $("orderModal").classList.add("hidden");
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

async function copyKeptIds() {
  const ids = keptItemIds();
  if (!ids.length) {
    log("No kept item IDs to copy.");
    return;
  }
  const text = ids.join(",");
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const textArea = document.createElement("textarea");
      textArea.value = text;
      textArea.setAttribute("readonly", "");
      textArea.style.position = "fixed";
      textArea.style.left = "-9999px";
      document.body.appendChild(textArea);
      textArea.select();
      document.execCommand("copy");
      textArea.remove();
    }
    log(`Copied ${ids.length} kept item ID(s) for QGIS Planet Explorer.`);
  } catch (error) {
    log(`Copy failed. Kept item IDs: ${text}`);
  }
}

async function orderItems(itemIds) {
  if (!itemIds.length) {
    log("No items selected for order.");
    return;
  }
  const options = orderOptions();
  if (!options.order_name) {
    log("Enter an order name first.");
    return;
  }
  $("submitOrder").disabled = true;
  log(`Creating Planet order "${options.order_name}" for ${itemIds.length} item(s). This can take several minutes.`);
  closeOrderModal();
  try {
    const result = await postJson("/api/order", {
      item_ids: itemIds,
      api_key: $("apiKey").value,
      ...options
    });
    log(`Order ${result.order_id} submitted. Polling Planet for status.`);
    pollOrderStatus(result.order_id).catch((error) => log(`Order polling stopped: ${error.message}`));
  } finally {
    $("submitOrder").disabled = false;
  }
}

function orderResultLines(results) {
  return (results || []).map((entry) => {
    const name = entry.name || entry.id || "download";
    const location = entry.location || entry.url || "";
    return location ? `${name}: ${location}` : name;
  });
}

async function pollOrderStatus(orderId) {
  let lastState = "";
  for (let attempt = 0; attempt < 180; attempt += 1) {
    if (attempt > 0) {
      await new Promise((resolve) => setTimeout(resolve, 10000));
    }
    const status = await getJson(`/api/order/${encodeURIComponent(orderId)}/status`);
    const state = status.state || "unknown";
    if (state !== lastState) {
      log(`Order ${orderId} state: ${state}.`);
      lastState = state;
    }
    if (state === "success") {
      const lines = orderResultLines(status.results);
      log(`Order ${orderId} complete.${lines.length ? `\n${lines.join("\n")}` : ""}`);
      return;
    }
    if (["failed", "cancelled", "partial"].includes(state)) {
      log(`Order ${orderId} finished with state: ${state}.${status.error ? ` ${status.error}` : ""}`);
      return;
    }
  }
  log(`Order ${orderId} is still processing. You can check it later in Planet Orders.`);
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
    clearAoiSummary();
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
  $("copyKeptIds").addEventListener("click", () => copyKeptIds().catch((error) => log(error.message)));
  $("rejectUnkept").addEventListener("click", () => rejectUnkeptItems().catch((error) => log(error.message)));
  $("orderKept").addEventListener("click", () => openOrderModal().catch((error) => log(error.message)));
  $("closeOrderModal").addEventListener("click", closeOrderModal);
  $("cancelOrderModal").addEventListener("click", closeOrderModal);
  $("submitOrder").addEventListener("click", () => orderItems(keptItemIds()).catch((error) => log(error.message)));
  $("orderName").addEventListener("input", () => refreshOrderEstimate().catch(() => {}));
  document.querySelectorAll("input[name='orderAsset'], #orderClip, #orderComposite, #orderHarmonize").forEach((input) => {
    input.addEventListener("change", () => {
      updateOrderToolAvailability();
      refreshOrderEstimate().catch((error) => {
        $("submitOrder").disabled = true;
        $("orderSummary").textContent = error.message;
      });
    });
  });
}

initMap();
bindEvents();
loadConfig().catch((error) => log(error.message));
