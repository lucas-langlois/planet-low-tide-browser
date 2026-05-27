from __future__ import annotations

import csv
import importlib
import io
import json
import math
import os
import sys
import tempfile
import time
import uuid
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pandas as pd
import requests
from flask import Flask, Response, jsonify, render_template, request, send_file, send_from_directory, session
from PIL import Image, ImageDraw
from requests.auth import HTTPBasicAuth

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
TIDE_DIR = ROOT_DIR / "tide"
MODEL_PATH = TIDE_DIR / "CSIRO_tidal_const_v12.nc"
LEAFLET_ASSETS = ROOT_DIR / "assets"

ITEM_TYPE = "PSScene"
PRODUCT_BUNDLE = "visual"
DEFAULT_TIMEZONE = "Australia/Brisbane"

app = Flask(__name__)
app.secret_key = os.environ.get("PLANET_BROWSER_SECRET_KEY", uuid.uuid4().hex)

APP_STATE: dict[str, dict[str, Any]] = {}
TIDE_MODEL_CACHE: dict[str, Any] = {}


def get_state() -> dict[str, Any]:
    sid = session.setdefault("sid", uuid.uuid4().hex)
    return APP_STATE.setdefault(
        sid,
        {
            "api_key": "",
            "aoi": None,
            "aoi_name": "",
            "items": [],
            "statuses": {},
            "last_order": None,
        },
    )


def load_default_api_key() -> str:
    for key_name in ("PL_API_KEY", "PLANET_API_KEY"):
        value = os.environ.get(key_name)
        if value:
            return normalise_api_key(value)

    config_path = ROOT_DIR / "config.py"
    if config_path.exists():
        namespace: dict[str, Any] = {}
        exec(config_path.read_text(encoding="utf-8"), namespace)
        value = namespace.get("PLANET_API_KEY")
        if value and value != "your_api_key_here":
            return normalise_api_key(str(value))
    return ""


def import_planet_sdk():
    try:
        from planet import Auth, Planet, Session, data_filter
        from planet.order_request import build_request, clip_tool, product
    except Exception as exc:
        raise RuntimeError(
            "Planet SDK is not installed in this Python environment. "
            "Create/activate the local .venv first."
        ) from exc
    return Auth, Planet, Session, data_filter, build_request, clip_tool, product


def normalise_api_key(api_key: str) -> str:
    return str(api_key or "").strip().strip("\"'")


def mask_api_key(api_key: str) -> str:
    key = normalise_api_key(api_key)
    if len(key) <= 12:
        return "***" if key else ""
    return f"{key[:8]}...{key[-4:]}"


def planet_client_from_key(api_key: str):
    Auth, Planet, Session, _data_filter, _build_request, _clip_tool, _product = import_planet_sdk()
    key = normalise_api_key(api_key)
    if not key:
        raise ValueError("Planet API key is required.")
    os.environ["PL_API_KEY"] = key
    return Planet(session=Session(auth=Auth.from_key(key)))


def import_tide_predictions():
    if str(TIDE_DIR) not in sys.path:
        sys.path.insert(0, str(TIDE_DIR))
    try:
        return importlib.import_module("Tide_predictions")
    except Exception as exc:
        raise RuntimeError(
            "Could not import tide/Tide_predictions.py. "
            "Install the tide dependencies in the local conda environment."
        ) from exc


def get_tide_model():
    if not MODEL_PATH.exists():
        raise FileNotFoundError(f"CSIRO model file not found: {MODEL_PATH}")
    cache_key = str(MODEL_PATH.resolve())
    if cache_key not in TIDE_MODEL_CACHE:
        tide_predictions = import_tide_predictions()
        TIDE_MODEL_CACHE[cache_key] = tide_predictions.CsiROModel(MODEL_PATH)
    return TIDE_MODEL_CACHE[cache_key]


def close_ring(coords: list[list[float]]) -> list[list[float]]:
    if coords and coords[0] != coords[-1]:
        return coords + [coords[0]]
    return coords


def square_aoi(center_lat: float, center_lon: float, area_km2: float) -> dict[str, Any]:
    side_m = math.sqrt(max(float(area_km2), 0.000001) * 1_000_000.0)
    half_side_m = side_m / 2.0
    meters_per_deg_lat = 111_320.0
    meters_per_deg_lon = 111_320.0 * math.cos(math.radians(center_lat))
    half_lat = half_side_m / meters_per_deg_lat
    half_lon = half_side_m / meters_per_deg_lon
    coords = [
        [center_lon - half_lon, center_lat - half_lat],
        [center_lon + half_lon, center_lat - half_lat],
        [center_lon + half_lon, center_lat + half_lat],
        [center_lon - half_lon, center_lat + half_lat],
        [center_lon - half_lon, center_lat - half_lat],
    ]
    return {"type": "Polygon", "coordinates": [coords]}


def aoi_polygons(aoi: dict[str, Any]) -> list[list[list[float]]]:
    if aoi.get("type") == "Feature":
        return aoi_polygons(aoi.get("geometry") or {})
    if aoi.get("type") == "FeatureCollection":
        polys: list[list[list[float]]] = []
        for feature in aoi.get("features", []):
            polys.extend(aoi_polygons(feature))
        return polys
    if aoi.get("type") == "Polygon":
        coords = aoi.get("coordinates") or []
        return [close_ring([[float(x), float(y)] for x, y, *_ in coords[0]])] if coords else []
    if aoi.get("type") == "MultiPolygon":
        polys = []
        for coords in aoi.get("coordinates") or []:
            if coords:
                polys.append(close_ring([[float(x), float(y)] for x, y, *_ in coords[0]]))
        return polys
    return []


def polygons_to_geojson(polygons: list[list[list[float]]]) -> dict[str, Any]:
    if len(polygons) == 1:
        return {"type": "Polygon", "coordinates": [close_ring(polygons[0])]}
    return {"type": "MultiPolygon", "coordinates": [[close_ring(poly)] for poly in polygons]}


def parse_uploaded_aoi(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix in (".geojson", ".json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        polygons = aoi_polygons(data)
    elif suffix == ".kml":
        polygons = parse_kml_polygons(path)
    elif suffix == ".zip":
        polygons = parse_shapefile_zip(path)
    elif suffix == ".shp":
        polygons = parse_shp_polygons(path)
    else:
        raise ValueError("Upload a GeoJSON, KML, SHP, or zipped shapefile.")

    if not polygons:
        raise ValueError("No polygon geometry found in uploaded AOI.")
    return polygons_to_geojson(polygons)


def parse_kml_polygons(path: Path) -> list[list[list[float]]]:
    import xml.etree.ElementTree as ET

    tree = ET.parse(path)
    root = tree.getroot()
    ns = {"kml": "http://www.opengis.net/kml/2.2"}
    polygons: list[list[list[float]]] = []
    for elem in root.findall(".//kml:Polygon", ns):
        coord_el = elem.find(".//kml:outerBoundaryIs/kml:LinearRing/kml:coordinates", ns)
        if coord_el is None or not coord_el.text:
            continue
        pts = []
        for token in coord_el.text.strip().split():
            parts = token.split(",")
            if len(parts) >= 2:
                pts.append([float(parts[0]), float(parts[1])])
        if len(pts) >= 3:
            polygons.append(close_ring(pts))
    return polygons


def parse_shp_polygons(path: Path) -> list[list[list[float]]]:
    try:
        import shapefile
    except Exception as exc:
        raise RuntimeError("Shapefile support requires pyshp.") from exc

    reader = shapefile.Reader(str(path))
    polygons: list[list[list[float]]] = []
    for shape in reader.shapes():
        if shape.shapeType not in (shapefile.POLYGON, shapefile.POLYGONM, shapefile.POLYGONZ):
            continue
        points = shape.points
        parts = list(shape.parts) + [len(points)]
        for idx in range(len(parts) - 1):
            ring = points[parts[idx] : parts[idx + 1]]
            if len(ring) >= 3:
                polygons.append(close_ring([[float(p[0]), float(p[1])] for p in ring]))

    prj_path = path.with_suffix(".prj")
    if prj_path.exists():
        from pyproj import CRS, Transformer

        src_crs = CRS.from_wkt(prj_path.read_text(encoding="utf-8", errors="ignore"))
        dst_crs = CRS.from_epsg(4326)
        if src_crs != dst_crs:
            transformer = Transformer.from_crs(src_crs, dst_crs, always_xy=True)
            transformed = []
            for poly in polygons:
                lon, lat = transformer.transform([p[0] for p in poly], [p[1] for p in poly])
                transformed.append(close_ring([[float(x), float(y)] for x, y in zip(lon, lat)]))
            polygons = transformed
    elif polygons:
        xs = [p[0] for poly in polygons for p in poly]
        ys = [p[1] for poly in polygons for p in poly]
        if min(xs) < -180 or max(xs) > 180 or min(ys) < -90 or max(ys) > 90:
            raise ValueError("Projected shapefiles need a matching .prj file.")

    return polygons


def parse_shapefile_zip(path: Path) -> list[list[list[float]]]:
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp_dir = Path(tmp_name)
        with ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                name = Path(info.filename).name
                if Path(name).suffix.lower() in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
                    (tmp_dir / name).write_bytes(archive.read(info))
        shp_files = list(tmp_dir.glob("*.shp"))
        if not shp_files:
            raise ValueError("The zip file does not contain a .shp file.")
        return parse_shp_polygons(shp_files[0])


def aoi_center(aoi: dict[str, Any]) -> tuple[float, float]:
    pts = [pt for poly in aoi_polygons(aoi) for pt in poly]
    if not pts:
        return -19.1836382, 146.6825115
    lon = sum(pt[0] for pt in pts) / len(pts)
    lat = sum(pt[1] for pt in pts) / len(pts)
    return lat, lon


def item_aoi_coverage(item: dict[str, Any], aoi: dict[str, Any]) -> float | None:
    try:
        from shapely.geometry import shape

        aoi_shape = shape(aoi)
        item_shape = shape(item.get("geometry") or {})
        if aoi_shape.is_empty or item_shape.is_empty or aoi_shape.area == 0:
            return None
        return min(100.0, max(0.0, item_shape.intersection(aoi_shape).area / aoi_shape.area * 100.0))
    except Exception:
        return None


def acquired_timestamp(item: dict[str, Any]) -> pd.Timestamp:
    acquired = item.get("properties", {}).get("acquired")
    if not acquired:
        raise ValueError(f"Item {item.get('id', '')} has no acquired timestamp.")
    ts = pd.Timestamp(acquired)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.tz_localize(None)


def predict_tides_for_items(items: list[dict[str, Any]], aoi: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not items:
        return items, {"n_faces": 0, "method": "none"}

    tide_predictions = import_tide_predictions()
    model = get_tide_model()
    times = pd.DatetimeIndex([acquired_timestamp(item) for item in items])
    polygons = aoi_polygons(aoi)

    import numpy as np
    from matplotlib.path import Path as MplPath

    points = np.column_stack([model.face_lon, model.face_lat])
    mask = np.zeros(points.shape[0], dtype=bool)
    for poly in polygons:
        if len(poly) >= 3:
            mask |= MplPath(poly).contains_points(points)

    face_indices = np.where(mask)[0]
    method = "area-average"
    if face_indices.size == 0:
        lat, lon = aoi_center(aoi)
        iface, _distance_km = model.nearest_face(lon, lat)
        face_indices = np.asarray([iface], dtype=int)
        method = "nearest-face-fallback"

    total = np.zeros(len(times), dtype=float)
    for iface in face_indices:
        total += tide_predictions._reconstruct_tides(
            ds=model.ds,
            iface=int(iface),
            lat=float(model.face_lat[int(iface)]),
            times=times,
            constituents=model.constituents,
        )
    tide_values = total / float(face_indices.size)

    for item, tide_height in zip(items, tide_values):
        item["tide_height"] = round(float(tide_height), 4)
        item["tide_method"] = method
        item["tide_faces"] = int(face_indices.size)

    items.sort(key=lambda item: (item.get("tide_height") is None, item.get("tide_height", float("inf"))))
    return items, {"n_faces": int(face_indices.size), "method": method}


def normalise_item(item: dict[str, Any], aoi: dict[str, Any]) -> dict[str, Any]:
    props = item.get("properties", {})
    out = {
        "id": item.get("id"),
        "geometry": item.get("geometry"),
        "properties": props,
        "acquired": props.get("acquired", ""),
        "cloud_cover": round(float(props.get("cloud_cover", 0.0)) * 100.0, 3),
        "visible_percent": props.get("visible_percent"),
        "clear_percent": props.get("clear_percent"),
        "gsd": props.get("gsd"),
        "satellite_id": props.get("satellite_id"),
        "item_type": props.get("item_type", ITEM_TYPE),
        "aoi_coverage_percent": item_aoi_coverage(item, aoi),
    }
    return out


def search_planet(
    api_key: str,
    aoi: dict[str, Any],
    start_date: str,
    end_date: str,
    max_cloud: float,
    min_aoi_coverage: float,
    max_results: int,
) -> list[dict[str, Any]]:
    _Auth, _Planet, _Session, data_filter, _build_request, _clip_tool, _product = import_planet_sdk()
    api_key = normalise_api_key(api_key)
    pl = planet_client_from_key(api_key)

    start_dt = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
    end_dt = datetime.fromisoformat(end_date).replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)

    combined_filter = data_filter.and_filter(
        [
            data_filter.date_range_filter("acquired", gte=start_dt, lte=end_dt),
            data_filter.range_filter("cloud_cover", lte=float(max_cloud) / 100.0),
            data_filter.geometry_filter(aoi),
        ]
    )

    raw_results = pl.data.search(item_types=[ITEM_TYPE], search_filter=combined_filter, limit=int(max_results))
    items: list[dict[str, Any]] = []
    for item in raw_results:
        normalised = normalise_item(item, aoi)
        coverage = normalised.get("aoi_coverage_percent")
        if coverage is not None and coverage < float(min_aoi_coverage):
            continue
        items.append(normalised)
    return items


def load_preview_tiles(api_key: str, item_id: str, item_type: str, center_lat: float, center_lon: float, mode: str) -> Image.Image | None:
    zoom = 17
    display_grid_size = 5 if mode == "full" else 3
    n = 2**zoom
    x_tile = int((center_lon + 180.0) / 360.0 * n)
    y_tile = int((1.0 - math.log(math.tan(math.radians(center_lat)) + 1 / math.cos(math.radians(center_lat))) / math.pi) / 2.0 * n)
    offset = display_grid_size // 2
    tiles_to_fetch = [
        (x_tile + dx, y_tile + dy)
        for dy in range(-offset, offset + 1)
        for dx in range(-offset, offset + 1)
    ]

    tile_images = []
    auth = HTTPBasicAuth(api_key, "")
    for tx, ty in tiles_to_fetch:
        tile_url = f"https://tiles0.planet.com/data/v1/{item_type}/{item_id}/{zoom}/{tx}/{ty}.png"
        try:
            response = requests.get(tile_url, auth=auth, timeout=12)
            tile_images.append(Image.open(io.BytesIO(response.content)).convert("RGB") if response.status_code == 200 else None)
        except Exception:
            tile_images.append(None)

    if not any(tile_images):
        return None

    tile_size = 256
    mosaic = Image.new("RGB", (tile_size * display_grid_size, tile_size * display_grid_size), (220, 224, 230))
    for idx, tile_img in enumerate(tile_images):
        if tile_img:
            mosaic.paste(tile_img, ((idx % display_grid_size) * tile_size, (idx // display_grid_size) * tile_size))

    if mode == "aoi":
        draw = ImageDraw.Draw(mosaic)
        margin = tile_size // 2
        draw.rectangle(
            [margin, margin, mosaic.width - margin, mosaic.height - margin],
            outline=(255, 80, 40),
            width=4,
        )
    return mosaic


def make_csv(items: list[dict[str, Any]], statuses: dict[str, str]) -> str:
    buf = io.StringIO()
    fields = [
        "item_id",
        "status",
        "acquired",
        "tide_height_m",
        "cloud_cover_percent",
        "aoi_coverage_percent",
        "visible_percent",
        "clear_percent",
        "satellite_id",
        "item_type",
        "planet_item_url",
    ]
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    for item in items:
        item_id = item["id"]
        writer.writerow(
            {
                "item_id": item_id,
                "status": statuses.get(item_id, "pending"),
                "acquired": item.get("acquired", ""),
                "tide_height_m": item.get("tide_height", ""),
                "cloud_cover_percent": item.get("cloud_cover", ""),
                "aoi_coverage_percent": item.get("aoi_coverage_percent", ""),
                "visible_percent": item.get("visible_percent", ""),
                "clear_percent": item.get("clear_percent", ""),
                "satellite_id": item.get("satellite_id", ""),
                "item_type": item.get("item_type", ""),
                "planet_item_url": f"https://www.planet.com/explorer/?item={item_id}",
            }
        )
    return buf.getvalue()


def order_items(api_key: str, item_ids: list[str], aoi: dict[str, Any], clip_to_aoi: bool) -> dict[str, Any]:
    _Auth, _Planet, _Session, _data_filter, build_request, clip_tool, product = import_planet_sdk()
    api_key = normalise_api_key(api_key)
    pl = planet_client_from_key(api_key)
    tools = [clip_tool(aoi)] if clip_to_aoi else []
    order_name = f"planet_browser_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    order_request = build_request(
        name=order_name,
        products=[product(item_ids=item_ids, product_bundle=PRODUCT_BUNDLE, item_type=ITEM_TYPE)],
        tools=tools or None,
    )
    order = pl.orders.create_order(order_request)
    order_id = order.get("id")
    if not order_id:
        raise RuntimeError("Planet did not return an order id.")

    deadline = time.time() + 600
    while time.time() < deadline:
        status = pl.orders.get_order(order_id)
        state = status.get("state", "unknown")
        if state == "success":
            return {"order_id": order_id, "state": state, "results": status.get("results", [])}
        if state in ("failed", "cancelled", "partial"):
            return {"order_id": order_id, "state": state, "error": status.get("error")}
        time.sleep(10)
    return {"order_id": order_id, "state": "timeout", "results": []}


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/vendor/leaflet/<path:name>")
def leaflet_asset(name: str):
    return send_from_directory(LEAFLET_ASSETS, name)


@app.get("/api/config")
def config():
    key = load_default_api_key()
    return jsonify(
        {
            "has_api_key": bool(key),
            "masked_api_key": mask_api_key(key),
            "model_exists": MODEL_PATH.exists(),
            "model_path": str(MODEL_PATH),
            "item_type": ITEM_TYPE,
        }
    )


@app.post("/api/aoi/square")
def api_square_aoi():
    payload = request.get_json(force=True)
    aoi = square_aoi(float(payload["lat"]), float(payload["lon"]), float(payload["area_km2"]))
    state = get_state()
    state["aoi"] = aoi
    state["aoi_name"] = "center square"
    return jsonify({"aoi": aoi})


@app.post("/api/aoi/upload")
def api_upload_aoi():
    uploaded = request.files.get("file")
    if not uploaded:
        return jsonify({"error": "No file uploaded."}), 400
    suffix = Path(uploaded.filename or "").suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        uploaded.save(tmp.name)
        tmp_path = Path(tmp.name)
    try:
        aoi = parse_uploaded_aoi(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    state = get_state()
    state["aoi"] = aoi
    state["aoi_name"] = uploaded.filename
    return jsonify({"aoi": aoi})


@app.post("/api/aoi/drawn")
def api_drawn_aoi():
    payload = request.get_json(force=True)
    aoi = payload.get("aoi")
    polygons = aoi_polygons(aoi or {})
    if not polygons:
        return jsonify({"error": "Draw or provide a valid polygon first."}), 400
    state = get_state()
    state["aoi"] = polygons_to_geojson(polygons)
    state["aoi_name"] = "drawn polygon"
    return jsonify({"aoi": state["aoi"]})


@app.post("/api/search")
def api_search():
    payload = request.get_json(force=True)
    submitted_key = normalise_api_key(payload.get("api_key"))
    default_key = load_default_api_key()
    api_key = submitted_key or default_key
    key_source = "typed key" if submitted_key else "configured key"
    if not api_key:
        return jsonify({"error": "Planet API key is required."}), 400

    aoi = payload.get("aoi") or get_state().get("aoi")
    if not aoi:
        return jsonify({"error": "Set an AOI first."}), 400

    try:
        items = search_planet(
            api_key=api_key,
            aoi=aoi,
            start_date=payload["start_date"],
            end_date=payload["end_date"],
            max_cloud=float(payload.get("max_cloud", 5)),
            min_aoi_coverage=float(payload.get("min_aoi_coverage", 80)),
            max_results=int(payload.get("max_results", 100)),
        )
        tide_info = {"n_faces": 0, "method": "not-run"}
        if payload.get("predict_tides", True):
            items, tide_info = predict_tides_for_items(items, aoi)
    except Exception as exc:
        return jsonify({"error": f"{str(exc)}\n\nKey source: {key_source} ({mask_api_key(api_key)})"}), 500

    state = get_state()
    state["api_key"] = api_key
    state["aoi"] = aoi
    state["items"] = items
    state["statuses"] = {item["id"]: "pending" for item in items}
    return jsonify({"items": items, "tide": tide_info, "key_source": key_source, "masked_api_key": mask_api_key(api_key)})


@app.post("/api/status")
def api_status():
    payload = request.get_json(force=True)
    item_id = payload.get("item_id")
    status = payload.get("status")
    if status not in ("pending", "keep", "reject"):
        return jsonify({"error": "Unknown status."}), 400
    state = get_state()
    state["statuses"][item_id] = status
    return jsonify({"ok": True, "statuses": state["statuses"]})


@app.get("/api/preview/<item_id>.png")
def api_preview(item_id: str):
    state = get_state()
    item = next((candidate for candidate in state["items"] if candidate["id"] == item_id), None)
    if not item:
        return jsonify({"error": "Item not found."}), 404
    if not state.get("api_key"):
        return jsonify({"error": "Planet API key missing."}), 400
    lat, lon = aoi_center(state.get("aoi") or {})
    image = load_preview_tiles(
        state["api_key"],
        item_id,
        item.get("item_type") or ITEM_TYPE,
        lat,
        lon,
        request.args.get("mode", "aoi"),
    )
    if image is None:
        return jsonify({"error": "Preview tiles could not be loaded."}), 502
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@app.get("/export/kept.csv")
def export_kept_csv():
    state = get_state()
    kept = [item for item in state["items"] if state["statuses"].get(item["id"]) == "keep"]
    csv_text = make_csv(kept, state["statuses"])
    filename = f"planet_kept_images_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/export/kept.geojson")
def export_kept_geojson():
    state = get_state()
    features = []
    for item in state["items"]:
        if state["statuses"].get(item["id"]) != "keep":
            continue
        props = {key: value for key, value in item.items() if key not in ("geometry", "properties")}
        features.append({"type": "Feature", "geometry": item.get("geometry"), "properties": props})
    body = json.dumps({"type": "FeatureCollection", "features": features}, indent=2)
    filename = f"planet_kept_images_{datetime.now().strftime('%Y%m%d_%H%M%S')}.geojson"
    return Response(
        body,
        mimetype="application/geo+json",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.post("/api/order")
def api_order():
    payload = request.get_json(force=True)
    state = get_state()
    api_key = normalise_api_key(state.get("api_key")) or normalise_api_key(payload.get("api_key")) or load_default_api_key()
    if not api_key:
        return jsonify({"error": "Planet API key is required."}), 400
    item_ids = payload.get("item_ids") or []
    if not item_ids:
        item_ids = [item["id"] for item in state["items"] if state["statuses"].get(item["id"]) == "keep"]
    if not item_ids:
        return jsonify({"error": "No kept items to order."}), 400
    try:
        result = order_items(api_key, item_ids, state["aoi"], bool(payload.get("clip_to_aoi", True)))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    state["last_order"] = result
    return jsonify(result)


def main() -> None:
    url = "http://127.0.0.1:5050"
    if os.environ.get("PLANET_BROWSER_NO_OPEN") != "1":
        webbrowser.open(url)
    app.run(host="127.0.0.1", port=5050, debug=False)


if __name__ == "__main__":
    main()
