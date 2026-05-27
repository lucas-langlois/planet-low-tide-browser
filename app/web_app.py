from __future__ import annotations

import csv
import importlib
import io
import json
import math
import os
import re
import sys
import tempfile
import uuid
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse
from zoneinfo import ZoneInfo
from zipfile import ZipFile

import pandas as pd
import requests
from flask import Flask, Response, jsonify, render_template, request, send_file, send_from_directory, session
from PIL import Image, ImageDraw
from requests.auth import HTTPBasicAuth

IS_FROZEN = bool(getattr(sys, "frozen", False))
ROOT_DIR = Path(sys.executable).resolve().parent if IS_FROZEN else Path(__file__).resolve().parent.parent
APP_DIR = ROOT_DIR / "app"
TIDE_DIR = ROOT_DIR / "tide"
MODEL_CANDIDATES = [TIDE_DIR / "CSIRO_tidal_const_v12.nc", ROOT_DIR / "CSIRO_tidal_const_v12.nc"]
MODEL_PATH = next((path for path in MODEL_CANDIDATES if path.exists()), MODEL_CANDIDATES[0])
LEAFLET_ASSETS = ROOT_DIR / "assets"

ITEM_TYPE = "PSScene"
PRODUCT_BUNDLE = "visual"
PLANET_DATA_BASE_URL = "https://api.planet.com/data/v1"
PLANET_ORDERS_BASE_URL = "https://api.planet.com/compute/ops/orders/v2"
PLANET_DOWNLOAD_DIR = ROOT_DIR / "Planet_download"
DEFAULT_TIMEZONE = "Australia/Brisbane"
EDUCATION_MONTHLY_QUOTA_KM2 = 3000.0
ORDER_ASSET_OPTIONS = {
    "visual": {
        "label": "Visual",
        "product_bundle": "visual",
        "bands": 3,
        "bytes_per_sample": 1,
        "description": "RGB visual imagery.",
    },
    "sr_4b": {
        "label": "Surface reflectance 4-band",
        "product_bundle": "analytic_sr_udm2",
        "bands": 4,
        "bytes_per_sample": 2,
        "description": "4-band surface reflectance bundle with UDM2.",
    },
    "sr_8b": {
        "label": "Surface reflectance 8-band",
        "product_bundle": "analytic_8b_sr_udm2",
        "bands": 8,
        "bytes_per_sample": 2,
        "description": "8-band surface reflectance bundle with UDM2.",
    },
}

app = Flask(__name__, template_folder=str(APP_DIR / "templates"), static_folder=str(APP_DIR / "static"))
app.secret_key = os.environ.get("PLANET_BROWSER_SECRET_KEY", uuid.uuid4().hex)

APP_STATE: dict[str, dict[str, Any]] = {}
TIDE_MODEL_CACHE: dict[str, Any] = {}
TIMEZONE_FINDER: Any | None = None


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


def planet_client_from_key(api_key: str, read_timeout_secs: float | None = None):
    Auth, Planet, Session, _data_filter, _build_request, _clip_tool, _product = import_planet_sdk()
    key = normalise_api_key(api_key)
    if not key:
        raise ValueError("Planet API key is required.")
    os.environ["PL_API_KEY"] = key
    return Planet(session=Session(auth=Auth.from_key(key), read_timeout_secs=read_timeout_secs))


def user_facing_error(exc: Exception) -> str:
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.ProxyError, requests.exceptions.Timeout)):
        return "Could not reach the Planet API. Check internet/proxy access, then try again."

    text = str(exc)
    network_markers = (
        "Failed to establish a new connection",
        "Max retries exceeded",
        "actively refused",
        "Unable to connect to proxy",
        "Read timed out",
    )
    if any(marker in text for marker in network_markers):
        return "Could not reach the Planet API. Check internet/proxy access, then try again."
    if "Please provide valid credentials" in text:
        return "Planet rejected this API key. Check the key and try again."

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and parsed.get("message"):
            message = str(parsed["message"])
            if "Please provide valid credentials" in message:
                return "Planet rejected this API key. Check the key and try again."
            return message
    except Exception:
        pass
    return text


def raise_for_planet_response(response: requests.Response) -> None:
    if response.ok:
        return
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("error")
        if message:
            raise RuntimeError(str(message))
    raise RuntimeError(f"Planet API returned HTTP {response.status_code}.")


def validate_planet_key(api_key: str) -> None:
    api_key = normalise_api_key(api_key)
    if not api_key:
        raise ValueError("Planet API key is required.")
    response = requests.post(
        f"{PLANET_DATA_BASE_URL}/quick-search",
        json={
            "item_types": [ITEM_TYPE],
            "filter": {
                "type": "DateRangeFilter",
                "field_name": "acquired",
                "config": {
                    "gte": "2024-01-01T00:00:00+00:00",
                    "lte": "2024-01-02T00:00:00+00:00",
                },
            },
        },
        params={"_page_size": 1},
        auth=HTTPBasicAuth(api_key, ""),
        timeout=12,
    )
    raise_for_planet_response(response)


def import_tide_predictions():
    if str(TIDE_DIR) not in sys.path:
        sys.path.insert(0, str(TIDE_DIR))
    try:
        return importlib.import_module("Tide_predictions")
    except Exception as exc:
        raise RuntimeError(
            "Could not import tide/Tide_predictions.py. "
            f"Install the tide dependencies in the local .venv first. Details: {exc}"
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


def aoi_area_km2(aoi: dict[str, Any]) -> float:
    try:
        from pyproj import Geod

        geod = Geod(ellps="WGS84")
        total_area_m2 = 0.0
        for poly in aoi_polygons(aoi):
            if len(poly) < 4:
                continue
            lons = [point[0] for point in poly]
            lats = [point[1] for point in poly]
            area_m2, _perimeter_m = geod.polygon_area_perimeter(lons, lats)
            total_area_m2 += abs(area_m2)
        return total_area_m2 / 1_000_000.0
    except Exception:
        return 0.0


def aoi_summary(aoi: dict[str, Any], name: str) -> dict[str, Any]:
    area = aoi_area_km2(aoi)
    return {
        "name": name or "AOI",
        "area_km2": round(area),
        "area_km2_precise": round(area, 3),
    }


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


def timezone_from_aoi(aoi: dict[str, Any]) -> tuple[Any, str, str]:
    lat, lon = aoi_center(aoi)
    try:
        global TIMEZONE_FINDER
        from timezonefinder import TimezoneFinder

        if TIMEZONE_FINDER is None:
            TIMEZONE_FINDER = TimezoneFinder()
        finder = TIMEZONE_FINDER
        tz_name = finder.timezone_at(lng=lon, lat=lat)
        if not tz_name and hasattr(finder, "closest_timezone_at"):
            tz_name = finder.closest_timezone_at(lng=lon, lat=lat)
        if tz_name:
            return ZoneInfo(tz_name), tz_name, "timezonefinder"
    except Exception:
        pass

    offset_hours = max(-12, min(14, round(lon / 15.0)))
    label = f"UTC{offset_hours:+03d}:00"
    return timezone(timedelta(hours=offset_hours)), label, "longitude-offset"


def add_local_acquired_times(items: list[dict[str, Any]], aoi: dict[str, Any]) -> dict[str, Any]:
    tzinfo, tz_label, method = timezone_from_aoi(aoi)
    for item in items:
        acquired = item.get("acquired")
        if not acquired:
            continue
        ts = pd.Timestamp(acquired)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        local = ts.tz_convert(tzinfo)
        item["acquired_utc"] = ts.isoformat()
        item["acquired_local"] = local.strftime("%Y-%m-%d %H:%M")
        item["acquired_local_iso"] = local.isoformat()
        item["acquired_timezone"] = tz_label
    return {"timezone": tz_label, "method": method}


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


def acquisition_minute_key(item: dict[str, Any]) -> str:
    acquired = item.get("acquired") or item.get("properties", {}).get("acquired")
    if not acquired:
        return f"missing:{item.get('id', '')}"
    ts = pd.Timestamp(acquired)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.floor("min").isoformat()


def deduplicate_same_time_scenes(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for item in items:
        key = acquisition_minute_key(item)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(item)

    deduped: list[dict[str, Any]] = []
    hidden_count = 0
    duplicate_groups = 0
    for key in order:
        group = buckets[key]
        if len(group) > 1:
            duplicate_groups += 1
            hidden_count += len(group) - 1
        best = max(
            group,
            key=lambda item: (
                item.get("aoi_coverage_percent") if item.get("aoi_coverage_percent") is not None else -1.0,
                -(item.get("cloud_cover") if item.get("cloud_cover") is not None else 100.0),
                str(item.get("id") or ""),
            ),
        )
        best["same_time_scene_count"] = len(group)
        best["same_time_hidden_count"] = len(group) - 1
        deduped.append(best)

    return deduped, {
        "hidden": hidden_count,
        "groups": duplicate_groups,
        "before": len(items),
        "after": len(deduped),
    }


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

    start_dt = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
    end_dt = datetime.fromisoformat(end_date).replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)

    combined_filter = data_filter.and_filter(
        [
            data_filter.date_range_filter("acquired", gte=start_dt, lte=end_dt),
            data_filter.range_filter("cloud_cover", lte=float(max_cloud) / 100.0),
            data_filter.geometry_filter(aoi),
        ]
    )

    items: list[dict[str, Any]] = []
    next_url: str | None = f"{PLANET_DATA_BASE_URL}/quick-search"
    payload = {"item_types": [ITEM_TYPE], "filter": combined_filter}
    params: dict[str, Any] | None = {"_page_size": min(max(int(max_results), 1), 250), "_sort": "acquired asc"}

    while next_url and len(items) < int(max_results):
        if payload is None:
            response = requests.get(next_url, auth=HTTPBasicAuth(api_key, ""), timeout=60)
        else:
            response = requests.post(
                next_url,
                json=payload,
                params=params,
                auth=HTTPBasicAuth(api_key, ""),
                timeout=60,
            )
        raise_for_planet_response(response)
        body = response.json()
        for item in body.get("features", []):
            normalised = normalise_item(item, aoi)
            coverage = normalised.get("aoi_coverage_percent")
            if coverage is not None and coverage < float(min_aoi_coverage):
                continue
            items.append(normalised)
            if len(items) >= int(max_results):
                break
        links = body.get("_links") or {}
        next_url = links.get("_next")
        if next_url:
            next_url = urljoin(f"{PLANET_DATA_BASE_URL}/", next_url)
        payload = None
        params = None
    return items


def lonlat_to_tile_xy(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    lat = max(min(float(lat), 85.05112878), -85.05112878)
    n = 2**zoom
    x_tile = (float(lon) + 180.0) / 360.0 * n
    lat_rad = math.radians(lat)
    y_tile = (1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n
    return x_tile, y_tile


def aoi_bounds(aoi: dict[str, Any]) -> tuple[float, float, float, float]:
    pts = [pt for poly in aoi_polygons(aoi) for pt in poly]
    if not pts:
        raise ValueError("AOI geometry is empty.")
    lon_values = [pt[0] for pt in pts]
    lat_values = [pt[1] for pt in pts]
    return min(lon_values), min(lat_values), max(lon_values), max(lat_values)


def choose_preview_tile_range(aoi: dict[str, Any], preferred_zoom: int = 17, max_axis_tiles: int = 8) -> tuple[int, int, int, int, int]:
    min_lon, min_lat, max_lon, max_lat = aoi_bounds(aoi)

    for zoom in range(preferred_zoom, 9, -1):
        x0, y_bottom = lonlat_to_tile_xy(min_lon, min_lat, zoom)
        x1, y_top = lonlat_to_tile_xy(max_lon, max_lat, zoom)
        x_min = math.floor(min(x0, x1))
        x_max = math.floor(max(x0, x1))
        y_min = math.floor(min(y_top, y_bottom))
        y_max = math.floor(max(y_top, y_bottom))
        if (x_max - x_min + 1) <= max_axis_tiles and (y_max - y_min + 1) <= max_axis_tiles:
            return zoom, x_min, x_max, y_min, y_max

    return 10, x_min, x_max, y_min, y_max


def draw_aoi_overlay(mosaic: Image.Image, aoi: dict[str, Any], zoom: int, x_min: int, y_min: int) -> None:
    tile_size = 256
    overlay = Image.new("RGBA", mosaic.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    for poly in aoi_polygons(aoi):
        pixels = []
        for lon, lat in poly:
            x_tile, y_tile = lonlat_to_tile_xy(lon, lat, zoom)
            pixels.append(((x_tile - x_min) * tile_size, (y_tile - y_min) * tile_size))
        if len(pixels) >= 3:
            draw.polygon(pixels, fill=(255, 80, 40, 32))
            draw.line(pixels, fill=(255, 80, 40, 255), width=4, joint="curve")

    mosaic.paste(Image.alpha_composite(mosaic.convert("RGBA"), overlay).convert("RGB"))


def load_aoi_preview_tiles(api_key: str, item_id: str, item_type: str, aoi: dict[str, Any]) -> Image.Image | None:
    zoom, x_min, x_max, y_min, y_max = choose_preview_tile_range(aoi)
    tile_size = 256
    auth = HTTPBasicAuth(api_key, "")
    mosaic = Image.new(
        "RGB",
        (tile_size * (x_max - x_min + 1), tile_size * (y_max - y_min + 1)),
        (230, 234, 238),
    )
    any_tiles = False

    for ty in range(y_min, y_max + 1):
        for tx in range(x_min, x_max + 1):
            tile_url = f"https://tiles0.planet.com/data/v1/{item_type}/{item_id}/{zoom}/{tx}/{ty}.png"
            try:
                response = requests.get(tile_url, auth=auth, timeout=12)
                if response.status_code == 200:
                    tile_img = Image.open(io.BytesIO(response.content)).convert("RGB")
                    mosaic.paste(tile_img, ((tx - x_min) * tile_size, (ty - y_min) * tile_size))
                    any_tiles = True
            except Exception:
                continue

    if not any_tiles:
        return None

    draw_aoi_overlay(mosaic, aoi, zoom, x_min, y_min)
    return mosaic


def load_center_preview_tiles(api_key: str, item_id: str, item_type: str, center_lat: float, center_lon: float, mode: str) -> Image.Image | None:
    zoom = 17
    display_grid_size = 5 if mode == "full" else 3
    x_float, y_float = lonlat_to_tile_xy(center_lon, center_lat, zoom)
    x_tile = int(x_float)
    y_tile = int(y_float)
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

    return mosaic


def load_preview_tiles(api_key: str, item_id: str, item_type: str, aoi: dict[str, Any], mode: str) -> Image.Image | None:
    if mode == "aoi":
        return load_aoi_preview_tiles(api_key, item_id, item_type, aoi)

    center_lat, center_lon = aoi_center(aoi)
    return load_center_preview_tiles(api_key, item_id, item_type, center_lat, center_lon, mode)


def make_csv(items: list[dict[str, Any]], statuses: dict[str, str]) -> str:
    buf = io.StringIO()
    fields = [
        "item_id",
        "status",
        "acquired_utc",
        "acquired_local",
        "acquired_timezone",
        "tide_height_m",
        "cloud_cover_percent",
        "aoi_coverage_percent",
        "same_time_scene_count",
        "same_time_hidden_count",
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
                "acquired_utc": item.get("acquired_utc") or item.get("acquired", ""),
                "acquired_local": item.get("acquired_local", ""),
                "acquired_timezone": item.get("acquired_timezone", ""),
                "tide_height_m": item.get("tide_height", ""),
                "cloud_cover_percent": item.get("cloud_cover", ""),
                "aoi_coverage_percent": item.get("aoi_coverage_percent", ""),
                "same_time_scene_count": item.get("same_time_scene_count", 1),
                "same_time_hidden_count": item.get("same_time_hidden_count", 0),
                "visible_percent": item.get("visible_percent", ""),
                "clear_percent": item.get("clear_percent", ""),
                "satellite_id": item.get("satellite_id", ""),
                "item_type": item.get("item_type", ""),
                "planet_item_url": f"https://www.planet.com/explorer/?item={item_id}",
            }
        )
    return buf.getvalue()


def order_asset_option(asset_key: str) -> dict[str, Any]:
    if asset_key not in ORDER_ASSET_OPTIONS:
        raise ValueError("Choose a valid order asset type.")
    return ORDER_ASSET_OPTIONS[asset_key]


def selected_items_by_id(state: dict[str, Any], item_ids: list[str]) -> list[dict[str, Any]]:
    wanted = set(item_ids)
    return [item for item in state.get("items", []) if item.get("id") in wanted]


def build_order_tools(aoi: dict[str, Any], clip_to_aoi: bool, composite: bool, harmonize: bool, asset_key: str) -> list[dict[str, Any]]:
    if harmonize and asset_key == "visual":
        raise ValueError("Harmonize requires a surface reflectance asset type.")
    tools: list[dict[str, Any]] = []
    if clip_to_aoi:
        tools.append({"clip": {"aoi": aoi}})
    if harmonize:
        tools.append({"harmonize": {"target_sensor": "Sentinel-2"}})
    if composite:
        tools.append({"composite": {"group_by": "order"}})
    return tools


def estimate_order(item_ids: list[str], items: list[dict[str, Any]], aoi: dict[str, Any], asset_key: str, clip_to_aoi: bool, composite: bool, harmonize: bool) -> dict[str, Any]:
    asset = order_asset_option(asset_key)
    item_count = len(item_ids)
    aoi_area = aoi_area_km2(aoi)
    intersection_area = 0.0
    for item in items:
        coverage = item.get("aoi_coverage_percent")
        if coverage is None:
            coverage = 100.0
        intersection_area += aoi_area * max(0.0, min(float(coverage), 100.0)) / 100.0

    processed_area = aoi_area if composite and clip_to_aoi and item_count else intersection_area
    if not clip_to_aoi:
        processed_area = intersection_area

    output_images = 1 if composite and item_count else item_count
    pixel_area_m2 = 3.0 * 3.0
    quota_percent = processed_area / EDUCATION_MONTHLY_QUOTA_KM2 * 100.0 if EDUCATION_MONTHLY_QUOTA_KM2 else 0.0
    warnings = []
    if item_count > 500:
        warnings.append("Orders API scenes orders are limited to 500 items per request.")
    if quota_percent >= 99:
        warnings.append("This estimate exceeds the standard 3,000 km2 monthly education quota.")
    elif quota_percent >= 80:
        warnings.append("This estimate uses more than 80% of the standard 3,000 km2 monthly education quota.")
    if not clip_to_aoi:
        warnings.append("Clip is off. Delivered files and quota use may be much larger than the AOI-intersection estimate.")
    if composite and aoi_area > 1500:
        warnings.append("Planet documents a 1,500 km2 PSScene composite output limit.")
    if harmonize and asset_key == "visual":
        warnings.append("Harmonize is only available for surface reflectance assets.")

    return {
        "item_count": item_count,
        "output_images": output_images,
        "asset_label": asset["label"],
        "product_bundle": asset["product_bundle"],
        "aoi_area_km2": round(aoi_area),
        "estimated_aoi_intersection_km2": round(intersection_area),
        "estimated_processed_area_km2": round(processed_area),
        "education_monthly_quota_km2": EDUCATION_MONTHLY_QUOTA_KM2,
        "education_quota_percent": round(quota_percent, 1),
        "tools": {
            "clip_to_aoi": clip_to_aoi,
            "composite": composite,
            "harmonize": harmonize,
        },
        "warnings": warnings,
        "can_order": bool(item_count) and item_count <= 500 and not (harmonize and asset_key == "visual"),
    }


def submit_order(
    api_key: str,
    item_ids: list[str],
    aoi: dict[str, Any],
    order_name: str,
    asset_key: str,
    clip_to_aoi: bool,
    composite: bool,
    harmonize: bool,
) -> dict[str, Any]:
    _Auth, _Planet, _Session, _data_filter, _build_request, _clip_tool, _product = import_planet_sdk()
    api_key = normalise_api_key(api_key)
    pl = planet_client_from_key(api_key)
    asset = order_asset_option(asset_key)
    if not order_name.strip():
        raise ValueError("Order name is required.")
    if not item_ids:
        raise ValueError("No kept items to order.")
    if len(item_ids) > 500:
        raise ValueError("Orders API scenes orders are limited to 500 items per request.")
    tools = build_order_tools(aoi, clip_to_aoi, composite, harmonize, asset_key)
    order_request = {
        "name": order_name.strip(),
        "source_type": "scenes",
        "order_type": "partial",
        "products": [
            {
                "item_ids": item_ids,
                "item_type": ITEM_TYPE,
                "product_bundle": asset["product_bundle"],
            }
        ],
    }
    if tools:
        order_request["tools"] = tools
    order = pl.orders.create_order(order_request)
    order_id = order.get("id")
    if not order_id:
        raise RuntimeError("Planet did not return an order id.")
    return {"order_id": order_id, "state": order.get("state", "submitted"), "order": order}


def get_order_status(api_key: str, order_id: str) -> dict[str, Any]:
    status = fetch_order_detail(api_key, order_id)
    return {
        "order_id": order_id,
        "state": status.get("state", "unknown"),
        "results": order_download_results(status),
        "error": status.get("error"),
        "name": status.get("name"),
    }


def fetch_order_detail(api_key: str, order_id: str) -> dict[str, Any]:
    response = requests.get(
        f"{PLANET_ORDERS_BASE_URL}/{order_id}",
        auth=HTTPBasicAuth(normalise_api_key(api_key), ""),
        timeout=30,
    )
    raise_for_planet_response(response)
    return response.json()


def order_download_results(order: dict[str, Any]) -> list[dict[str, Any]]:
    links = order.get("_links") or {}
    raw_results: list[Any] = []
    for results in (order.get("results"), links.get("results")):
        if isinstance(results, list):
            raw_results.extend(results)
        elif results:
            raw_results.append(results)
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for result in raw_results:
        if isinstance(result, str):
            result = {"name": "download", "location": result}
        if not isinstance(result, dict):
            continue
        location = result.get("location") or result.get("url") or result.get("href")
        name = result.get("name") or result.get("id") or "download"
        dedupe_key = (str(name), str(location or ""))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        output.append(
            {
                "name": name,
                "location": location,
                "delivery": result.get("delivery"),
                "expires_at": result.get("expires_at"),
            }
        )
    return output


def simplify_order(order: dict[str, Any]) -> dict[str, Any]:
    return {
        "order_id": order.get("id"),
        "name": order.get("name", ""),
        "state": order.get("state", "unknown"),
        "created_on": order.get("created_on", ""),
        "last_modified": order.get("last_modified", ""),
        "source_type": order.get("source_type", ""),
        "results": order_download_results(order),
        "error": order.get("error"),
    }


def list_orders(api_key: str, limit: int = 25) -> list[dict[str, Any]]:
    response = requests.get(
        PLANET_ORDERS_BASE_URL,
        params={"source_type": "scenes", "sort_by": "created_on DESC"},
        auth=HTTPBasicAuth(normalise_api_key(api_key), ""),
        timeout=30,
    )
    raise_for_planet_response(response)
    body = response.json()
    orders = body.get("orders", [])[: max(1, min(int(limit), 100))]
    output = []
    for order in orders:
        if str(order.get("state", "")).lower() == "success" and not order_download_results(order) and order.get("id"):
            try:
                order = fetch_order_detail(api_key, order["id"])
            except Exception:
                pass
        output.append(simplify_order(order))
    return output


def safe_file_part(value: str, fallback: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value.strip())
    cleaned = re.sub(r"\s+", "_", cleaned).strip(" ._")
    return cleaned[:180] or fallback


def order_result_filename(result: dict[str, Any], index: int) -> str:
    name = str(result.get("name") or "").strip()
    if name:
        filename = Path(unquote(urlparse(name).path)).name
    else:
        location_path = unquote(urlparse(str(result.get("location") or "")).path)
        filename = Path(location_path).name
    return safe_file_part(filename, f"planet_order_file_{index:03d}")


def unique_path(folder: Path, filename: str) -> Path:
    candidate = folder / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    for counter in range(2, 10000):
        numbered = folder / f"{stem}_{counter}{suffix}"
        if not numbered.exists():
            return numbered
    raise RuntimeError(f"Could not make a unique filename for {filename}.")


def download_order_files(api_key: str, order_id: str) -> dict[str, Any]:
    order = fetch_order_detail(api_key, order_id)
    state = str(order.get("state", "")).lower()
    if state != "success":
        raise ValueError(f"Order is not ready yet. Current state: {state or 'unknown'}.")
    results = [result for result in order_download_results(order) if result.get("location")]
    if not results:
        raise ValueError("Planet has not returned download URLs for this order yet. Refresh orders in a moment.")

    order_name = safe_file_part(str(order.get("name") or order_id), order_id)
    folder = PLANET_DOWNLOAD_DIR / order_name
    folder.mkdir(parents=True, exist_ok=True)

    saved_files = []
    for index, result in enumerate(results, start=1):
        filename = order_result_filename(result, index)
        target = unique_path(folder, filename)
        response = requests.get(str(result["location"]), stream=True, timeout=(15, 300))
        response.raise_for_status()
        with target.open("wb") as file_handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    file_handle.write(chunk)
        saved_files.append(
            {
                "name": target.name,
                "path": str(target),
                "size_bytes": target.stat().st_size,
            }
        )

    return {
        "order_id": order_id,
        "order_name": order.get("name") or order_id,
        "folder": str(folder),
        "file_count": len(saved_files),
        "files": saved_files,
    }


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
    return jsonify({"aoi": aoi, "summary": aoi_summary(aoi, state["aoi_name"])})


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
    return jsonify({"aoi": aoi, "summary": aoi_summary(aoi, state["aoi_name"])})


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
    return jsonify({"aoi": state["aoi"], "summary": aoi_summary(state["aoi"], state["aoi_name"])})


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
        items, dedupe_info = deduplicate_same_time_scenes(items)
        tide_info = {"n_faces": 0, "method": "not-run"}
        if payload.get("predict_tides", True):
            items, tide_info = predict_tides_for_items(items, aoi)
        time_info = add_local_acquired_times(items, aoi)
    except Exception as exc:
        return jsonify({"error": f"{str(exc)}\n\nKey source: {key_source} ({mask_api_key(api_key)})"}), 500

    state = get_state()
    state["api_key"] = api_key
    state["aoi"] = aoi
    state["items"] = items
    state["statuses"] = {item["id"]: "pending" for item in items}
    return jsonify(
        {
            "items": items,
            "tide": tide_info,
            "time": time_info,
            "dedupe": dedupe_info,
            "key_source": key_source,
            "masked_api_key": mask_api_key(api_key),
        }
    )


@app.post("/api/validate-key")
def api_validate_key():
    payload = request.get_json(force=True)
    api_key = normalise_api_key(payload.get("api_key"))
    if not api_key:
        return jsonify({"valid": False, "error": "Paste a Planet API key first."}), 400

    try:
        validate_planet_key(api_key)
    except Exception as exc:
        status_code = 503 if isinstance(exc, requests.exceptions.RequestException) else 401
        return jsonify({"valid": False, "error": user_facing_error(exc)}), status_code

    return jsonify({"valid": True, "masked_api_key": mask_api_key(api_key)})


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


@app.post("/api/status/bulk")
def api_status_bulk():
    payload = request.get_json(force=True)
    item_ids = payload.get("item_ids") or []
    status = payload.get("status")
    if status not in ("pending", "keep", "reject"):
        return jsonify({"error": "Unknown status."}), 400
    state = get_state()
    known_ids = {item["id"] for item in state["items"]}
    for item_id in item_ids:
        if item_id in known_ids:
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
    image = load_preview_tiles(
        state["api_key"],
        item_id,
        item.get("item_type") or ITEM_TYPE,
        state.get("aoi") or {},
        request.args.get("mode", "aoi"),
    )
    if image is None:
        return jsonify({"error": "Preview tiles could not be loaded."}), 502
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@app.get("/planet-tiles/<item_type>/<item_id>/<int:zoom>/<int:x>/<int:y>.png")
def planet_tile_proxy(item_type: str, item_id: str, zoom: int, x: int, y: int):
    state = get_state()
    api_key = normalise_api_key(state.get("api_key"))
    if not api_key:
        return Response("Planet API key missing.", status=401)

    tile_url = f"https://tiles0.planet.com/data/v1/{item_type}/{item_id}/{zoom}/{x}/{y}.png"
    try:
        response = requests.get(tile_url, auth=HTTPBasicAuth(api_key, ""), timeout=15)
    except Exception as exc:
        return Response(str(exc), status=502)

    if response.status_code != 200:
        return Response(response.content, status=response.status_code, content_type=response.headers.get("content-type"))

    return Response(
        response.content,
        status=200,
        content_type=response.headers.get("content-type", "image/png"),
        headers={"Cache-Control": "private, max-age=3600"},
    )


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


@app.post("/api/order/estimate")
def api_order_estimate():
    payload = request.get_json(force=True)
    state = get_state()
    item_ids = payload.get("item_ids") or [item["id"] for item in state["items"] if state["statuses"].get(item["id"]) == "keep"]
    items = selected_items_by_id(state, item_ids)
    try:
        estimate = estimate_order(
            item_ids=item_ids,
            items=items,
            aoi=state.get("aoi") or {},
            asset_key=payload.get("asset_key", "visual"),
            clip_to_aoi=bool(payload.get("clip_to_aoi", True)),
            composite=bool(payload.get("composite", False)),
            harmonize=bool(payload.get("harmonize", False)),
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(estimate)


@app.post("/api/order")
def api_order():
    payload = request.get_json(force=True)
    state = get_state()
    api_key = normalise_api_key(payload.get("api_key")) or normalise_api_key(state.get("api_key")) or load_default_api_key()
    if not api_key:
        return jsonify({"error": "Planet API key is required."}), 400
    item_ids = payload.get("item_ids") or []
    if not item_ids:
        item_ids = [item["id"] for item in state["items"] if state["statuses"].get(item["id"]) == "keep"]
    if not item_ids:
        return jsonify({"error": "No kept items to order."}), 400
    try:
        estimate = estimate_order(
            item_ids=item_ids,
            items=selected_items_by_id(state, item_ids),
            aoi=state.get("aoi") or {},
            asset_key=payload.get("asset_key", "visual"),
            clip_to_aoi=bool(payload.get("clip_to_aoi", True)),
            composite=bool(payload.get("composite", False)),
            harmonize=bool(payload.get("harmonize", False)),
        )
        if not estimate.get("can_order"):
            return jsonify({"error": "Order estimate is not orderable. Check warnings before placing the order."}), 400
        result = submit_order(
            api_key=api_key,
            item_ids=item_ids,
            aoi=state["aoi"],
            order_name=payload.get("order_name") or "",
            asset_key=payload.get("asset_key", "visual"),
            clip_to_aoi=bool(payload.get("clip_to_aoi", True)),
            composite=bool(payload.get("composite", False)),
            harmonize=bool(payload.get("harmonize", False)),
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    state["api_key"] = api_key
    state["last_order"] = result
    return jsonify(result)


@app.get("/api/order/<order_id>/status")
def api_order_status(order_id: str):
    state = get_state()
    api_key = normalise_api_key(state.get("api_key")) or load_default_api_key()
    if not api_key:
        return jsonify({"error": "Planet API key is required."}), 400
    try:
        result = get_order_status(api_key, order_id)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    state["last_order"] = result
    return jsonify(result)


@app.post("/api/order/status")
def api_order_status_post():
    payload = request.get_json(force=True)
    order_id = str(payload.get("order_id") or "").strip()
    if not order_id:
        return jsonify({"error": "Order ID is required."}), 400
    state = get_state()
    api_key = normalise_api_key(payload.get("api_key")) or normalise_api_key(state.get("api_key")) or load_default_api_key()
    if not api_key:
        return jsonify({"error": "Planet API key is required."}), 400
    try:
        result = get_order_status(api_key, order_id)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    state["api_key"] = api_key
    state["last_order"] = result
    return jsonify(result)


@app.post("/api/orders")
def api_orders_list():
    payload = request.get_json(force=True)
    state = get_state()
    api_key = normalise_api_key(payload.get("api_key")) or normalise_api_key(state.get("api_key")) or load_default_api_key()
    if not api_key:
        return jsonify({"error": "Planet API key is required."}), 400
    try:
        orders = list_orders(api_key, int(payload.get("limit", 25)))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    state["api_key"] = api_key
    return jsonify({"orders": orders})


@app.post("/api/order/download")
def api_order_download():
    payload = request.get_json(force=True)
    order_id = str(payload.get("order_id") or "").strip()
    if not order_id:
        return jsonify({"error": "Order ID is required."}), 400
    state = get_state()
    api_key = normalise_api_key(payload.get("api_key")) or normalise_api_key(state.get("api_key")) or load_default_api_key()
    if not api_key:
        return jsonify({"error": "Planet API key is required."}), 400
    try:
        result = download_order_files(api_key, order_id)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    state["api_key"] = api_key
    return jsonify(result)


def main() -> None:
    cloud_run_service = bool(os.environ.get("K_SERVICE"))
    host = "0.0.0.0" if cloud_run_service else os.environ.get("PLANET_BROWSER_HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8080" if cloud_run_service else "5050"))
    browser_host = "127.0.0.1" if host == "0.0.0.0" else host
    url = f"http://{browser_host}:{port}"
    if not cloud_run_service and os.environ.get("PLANET_BROWSER_NO_OPEN") != "1":
        webbrowser.open(url)
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
