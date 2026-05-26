from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import signal
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Sequence

def _signal_handler(signum, frame):
  """Catch SIGTERM / SIGHUP so SLURM-killed jobs leave a trace."""
  name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
  print(f"\nCaught signal {name} ({signum}) — terminating.", file=sys.stderr, flush=True)
  sys.exit(128 + signum)

signal.signal(signal.SIGTERM, _signal_handler)
if hasattr(signal, "SIGHUP"):
  signal.signal(signal.SIGHUP, _signal_handler)

import numpy as np
import pandas as pd
import rasterio
import utide
import xarray as xr
from netCDF4 import Dataset, date2num
from rasterio.warp import transform as rio_transform
from rasterio.windows import Window
from scipy.spatial import cKDTree
from utide import ut_constants
from utide.utilities import Bunch

DEFAULT_MODEL_NAME = "CSIRO_tidal_const_v12"
DEFAULT_CONSTITUENTS = ["Q1", "O1", "P1", "K1", "2N2", "N2", "M2", "S2", "K2", "M4", "MS4", "M6", "2MS6"]
UTIDE_NAMES_UPPER = [str(name).strip().upper() for name in ut_constants["const"]["name"]]
UTIDE_NAME_TO_INDEX = {name: idx for idx, name in enumerate(UTIDE_NAMES_UPPER)}
UTIDE_NAME_SET = set(UTIDE_NAME_TO_INDEX.keys())
UTIDE_FREQ = ut_constants["const"]["freq"]

TIDE_CLASS_LABELS = {
  0: "neap",
  1: "moderate",
  2: "spring",
  3: "king",
}

GRID_FLOAT_VARS: dict[str, tuple[str, str]] = {
  "min_tide_height": ("Lowest tide height during daytime", "m"),
  "max_tide_height": ("Highest tide height during daytime", "m"),
  "tide_range": ("Daytime tidal range (max - min)", "m"),
  "mean_tide_height": ("Mean tide height during daytime", "m"),
  "midday_tide_height": ("Tide height at solar noon (12:00)", "m"),
  "hours_exposed": ("Hours tide is below pixel elevation", "hours"),
  "low_tide_solar_offset": ("Hours between min-tide and solar noon", "hours"),
  "exposure_duration_max": ("Longest continuous exposure period", "hours"),
  "tide_position_midday": ("Normalized tide position at solar noon (0=low, 1=high)", "1"),
  "tide_zscore_midday": ("Standardized tide height at solar noon (z-score)", "1"),
  "time_from_low_abs_h": ("Absolute hours between daytime low tide and solar noon", "hours"),
}

GRID_HHMM_VARS: dict[str, str] = {
  "min_tide_time": "Time of lowest tide (HHMM local)",
  "max_tide_time": "Time of highest tide (HHMM local)",
}

GRID_INT8_VARS: dict[str, str] = {
  "tide_class": "Tide classification code",
  "n_tide_cycles": "Number of low-to-high-to-low cycles",
  "low_tide_midday_flag": "Solar noon is near daytime low tide (tide_position_midday <= 0.2)",
}

CLI_HELP_NOTES = """
Timezone handling:
  --utc-offset controls local time zone (default 10 = AEST/Queensland).
  --day-start-hour and --day-end-hour are in LOCAL time.
  Tide model predictions are computed in UTC then converted.
  HHMM output times are reported in local time.

Output variables (12 total):
  min_tide_height        f32  Lowest tide height during daytime (m)
  min_tide_time          i16  Time of lowest tide (HHMM, local)
  max_tide_height        f32  Highest tide height during daytime (m)
  max_tide_time          i16  Time of highest tide (HHMM, local)
  tide_range             f32  Daytime tidal range (max - min) (m)
  mean_tide_height       f32  Mean tide height during daytime (m)
  midday_tide_height     f32  Tide height at solar noon (12:00) (m)
  hours_exposed          f32  Hours tide < pixel elevation (requires --elevation-raster)
  low_tide_solar_offset  f32  Hours between min-tide and solar noon
  tide_class             i8   0=neap 1=moderate 2=spring 3=king
  exposure_duration_max  f32  Longest continuous exposure period (requires --elevation-raster)
  n_tide_cycles          i8   Number of low-to-high-to-low cycles

  tide_class thresholds:
    With --tide-range-raster (ratio = daytime_range / local_range):
      ratio < 0.30  -> 0 neap
      0.30 <= ratio < 0.55 -> 1 moderate
      0.55 <= ratio < 0.80 -> 2 spring
      ratio >= 0.80 -> 3 king
    Without --tide-range-raster (absolute):
      range < 1.0 m -> 0 neap
      1.0 <= range < 2.0 m -> 1 moderate
      2.0 <= range < 3.5 m -> 2 spring
      range >= 3.5 m -> 3 king
"""


class CsiROModel:
  def __init__(self, model_path: str | Path, model_name: str = DEFAULT_MODEL_NAME):
    self.model_path = Path(model_path)
    self.model_name = model_name
    self.ds = xr.open_dataset(self.model_path)
    self.face_lon = np.asarray(self.ds["Mesh2_face_x"].values)
    self.face_lat = np.asarray(self.ds["Mesh2_face_y"].values)
    self.tree = _build_face_tree(self.face_lon, self.face_lat)
    self.constituents = _read_constituent_names(self.ds)

  def list_models(self) -> pd.DataFrame:
    return pd.DataFrame(
      {
        "model": [self.model_name],
        "path": [str(self.model_path)],
        "status": ["available"],
      }
    )

  def nearest_face(self, x: float, y: float) -> tuple[int, float]:
    return _query_face_tree(self.tree, x, y)

  def predict(self, x: float, y: float, time: Sequence[pd.Timestamp]) -> pd.DataFrame:
    iface, distance_km = self.nearest_face(float(x), float(y))
    tide_height = _reconstruct_tides(
      ds=self.ds,
      iface=iface,
      lat=float(y),
      times=pd.DatetimeIndex(time),
      constituents=self.constituents,
    )
    out = pd.DataFrame({
      "time": pd.DatetimeIndex(time),
      "x": float(x),
      "y": float(y),
      "tide_model": self.model_name,
      "tide_height": tide_height,
      "distance_km": float(distance_km),
    })
    return out


def list_models(model_path: str | Path) -> pd.DataFrame:
  model = CsiROModel(model_path=model_path)
  return model.list_models()


def model_tides(
  x: float | Iterable[float],
  y: float | Iterable[float],
  time: Sequence[pd.Timestamp] | pd.DatetimeIndex,
  model_path: str | Path,
  model: str = DEFAULT_MODEL_NAME,
  mode: str = "one-to-many",
) -> pd.DataFrame:
  x_arr = _to_1d_float(x)
  y_arr = _to_1d_float(y)
  t_idx = pd.DatetimeIndex(pd.to_datetime(time))

  if len(t_idx) == 0:
    raise ValueError("`time` must contain at least one timestamp.")

  if mode == "one-to-many":
    request_df = (
      pd.MultiIndex.from_product([x_arr, y_arr, t_idx], names=["x", "y", "time"])
      .to_frame(index=False)
      .loc[:, ["time", "x", "y"]]
    )
  elif mode == "one-to-one":
    if not (len(x_arr) == len(y_arr) == len(t_idx)):
      raise ValueError("For `one-to-one` mode, `x`, `y`, and `time` must have the same length.")
    request_df = pd.DataFrame({"time": t_idx, "x": x_arr, "y": y_arr})
  else:
    raise ValueError("`mode` must be either 'one-to-many' or 'one-to-one'.")

  model_obj = CsiROModel(model_path=model_path, model_name=model)

  outputs: list[pd.DataFrame] = []
  for (x_val, y_val), group in request_df.groupby(["x", "y"], sort=False):
    pred = model_obj.predict(float(x_val), float(y_val), pd.DatetimeIndex(group["time"]))
    outputs.append(pred)

  tide_df = pd.concat(outputs, ignore_index=True)
  tide_df = tide_df.sort_values(["time", "x", "y"]).reset_index(drop=True)
  return tide_df


def _to_1d_float(value: float | Iterable[float]) -> np.ndarray:
  if np.isscalar(value):
    return np.asarray([float(value)], dtype=float)
  return np.asarray(list(value), dtype=float)


def _build_face_tree(lon_deg: np.ndarray, lat_deg: np.ndarray) -> cKDTree:
  lon = np.deg2rad(np.asarray(lon_deg))
  lat = np.deg2rad(np.asarray(lat_deg))
  xyz = np.column_stack([
    np.cos(lat) * np.cos(lon),
    np.cos(lat) * np.sin(lon),
    np.sin(lat),
  ])
  return cKDTree(xyz)


def _query_face_tree(tree: cKDTree, qlon_deg: float, qlat_deg: float) -> tuple[int, float]:
  lon = np.deg2rad(float(qlon_deg))
  lat = np.deg2rad(float(qlat_deg))
  q = np.array([[np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)]])

  chord, idx = tree.query(q, k=1)
  theta = 2 * np.arcsin(np.clip(chord / 2, 0, 1))
  km = 6371.0 * theta
  return int(idx), float(km)


def _query_face_tree_many(tree: cKDTree, qlon_deg: np.ndarray, qlat_deg: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
  lon = np.deg2rad(np.asarray(qlon_deg, dtype=float))
  lat = np.deg2rad(np.asarray(qlat_deg, dtype=float))
  q = np.column_stack([
    np.cos(lat) * np.cos(lon),
    np.cos(lat) * np.sin(lon),
    np.sin(lat),
  ])

  chord, idx = tree.query(q, k=1)
  theta = 2 * np.arcsin(np.clip(chord / 2, 0, 1))
  km = 6371.0 * theta
  return np.asarray(idx, dtype=np.int64), np.asarray(km, dtype=float)


def _iter_slices(length: int, chunk_size: int) -> Iterable[slice]:
  for start in range(0, int(length), int(chunk_size)):
    end = min(start + int(chunk_size), int(length))
    yield slice(start, end)


def _query_face_tree_many_chunked(
  tree: cKDTree,
  qlon_deg: np.ndarray,
  qlat_deg: np.ndarray,
  query_chunk_size: int,
  progress_every_chunks: int = 0,
) -> tuple[np.ndarray, np.ndarray]:
  n = len(qlon_deg)
  idx_out = np.empty(n, dtype=np.int64)
  dist_out = np.empty(n, dtype=float)
  total_chunks = (n + int(query_chunk_size) - 1) // int(query_chunk_size)
  for chunk_i, chunk in enumerate(_iter_slices(n, query_chunk_size), start=1):
    idx_c, dist_c = _query_face_tree_many(tree, qlon_deg[chunk], qlat_deg[chunk])
    idx_out[chunk] = idx_c
    dist_out[chunk] = dist_c
    if int(progress_every_chunks) > 0 and ((chunk_i % int(progress_every_chunks) == 0) or (chunk_i == total_chunks)):
      print(f"KD-tree matching chunks: {chunk_i}/{total_chunks}", flush=True)
  return idx_out, dist_out


def _read_constituent_names(ds: xr.Dataset) -> list[str]:
  for key in ["constituent", "constituents"]:
    if key in ds.variables:
      values = ds[key].values
      names = _decode_constituent_array(values)
      if names and _count_compatible_constituents(names) > 0:
        return names
  return DEFAULT_CONSTITUENTS.copy()


def _count_compatible_constituents(names: Iterable[str]) -> int:
  return sum(1 for name in names if str(name).strip().upper() in UTIDE_NAME_SET)


def _decode_constituent_array(values: np.ndarray) -> list[str]:
  arr = np.asarray(values)
  if arr.ndim == 1:
    names = [
      item.decode("utf-8").strip() if isinstance(item, (bytes, np.bytes_)) else str(item).strip()
      for item in arr
    ]
    return [name for name in names if name]

  if arr.ndim == 2:
    decoded: list[str] = []
    for row in arr:
      chars = [
        item.decode("utf-8") if isinstance(item, (bytes, np.bytes_)) else str(item)
        for item in row
      ]
      decoded.append("".join(chars).strip())
    return [name for name in decoded if name]

  return []


def _reconstruct_tides(
  ds: xr.Dataset,
  iface: int,
  lat: float,
  times: pd.DatetimeIndex,
  constituents: list[str],
) -> np.ndarray:
  amplitudes = np.asarray(ds["h_amp"].isel(nMesh2_face=iface).values, dtype=float)
  phases = np.asarray(ds["h_pha"].isel(nMesh2_face=iface).values, dtype=float)

  if (len(constituents) != len(amplitudes)) or (_count_compatible_constituents(constituents) == 0):
    constituents = DEFAULT_CONSTITUENTS[: len(amplitudes)]

  keep_idx: list[int] = []
  ut_idx: list[int] = []
  names_kept: list[str] = []
  for idx, name in enumerate(constituents):
    upper_name = name.strip().upper()
    ut_i = UTIDE_NAME_TO_INDEX.get(upper_name)
    if ut_i is not None:
      keep_idx.append(idx)
      ut_idx.append(ut_i)
      names_kept.append(upper_name)

  if not keep_idx:
    fallback = DEFAULT_CONSTITUENTS[: len(amplitudes)]
    keep_idx = []
    ut_idx = []
    names_kept = []
    for idx, name in enumerate(fallback):
      upper_name = name.strip().upper()
      ut_i = UTIDE_NAME_TO_INDEX.get(upper_name)
      if ut_i is not None:
        keep_idx.append(idx)
        ut_idx.append(ut_i)
        names_kept.append(upper_name)

  if not keep_idx:
    raise ValueError("No compatible harmonic constituents found after fallback to default constituent names.")

  a = amplitudes[np.asarray(keep_idx, dtype=int)]
  g = phases[np.asarray(keep_idx, dtype=int)]
  freq = UTIDE_FREQ[np.asarray(ut_idx, dtype=int)]

  reftime = _datetime_to_ordinal_float(times[0])

  coef = Bunch(name=names_kept, mean=0.0, slope=0.0)
  coef["A"] = np.asarray(a, dtype=float)
  coef["g"] = np.asarray(g, dtype=float)
  coef["A_ci"] = np.zeros_like(coef["A"])
  coef["g_ci"] = np.zeros_like(coef["g"])
  coef["aux"] = Bunch(reftime=reftime, lind=np.asarray(ut_idx, dtype=int), frq=freq, lat=float(lat))
  coef["aux"]["opt"] = Bunch(
    twodim=False,
    epoch="python",
    phase="Greenwich",
    nodal=True,
    trend=False,
    verbose=False,
    nodiagn=True,
    diagnminsnr=2,
    rmin=1,
    ordercnstit="PE",
    cnstit="auto",
    nodsatlint=False,
    nodsatnone=False,
    nodesatlint=False,
    nodesatnone=False,
    gwchlint=False,
    gwchnone=False,
    prefilt=[],
    white=False,
    RunTimeDisp=False,
    equi=False,
    infer=None,
    inferaprx=0,
    notrend=True,
    linci=True,
    lsfrqosmp=1,
    nrlzn=200,
    tunrdn=1,
  )

  tide = utide.reconstruct(times.to_pydatetime(), coef, verbose=False)
  return np.asarray(tide.h, dtype=float)


def _datetime_to_ordinal_float(ts: pd.Timestamp) -> float:
  ts = pd.Timestamp(ts)
  return float(ts.toordinal() + (ts.hour + ts.minute / 60 + ts.second / 3600) / 24.0)


def _extract_valid_pixel_points(
  raster_path: str | Path,
  valid_threshold: float = float('-inf'),
  raster_tile_size: int = 4096,
  progress_every_tiles: int = 0,
) -> dict[str, np.ndarray | tuple[int, int] | rasterio.Affine | str | None]:
  raster_path = Path(raster_path)
  if not raster_path.exists():
    raise FileNotFoundError(f"Raster not found: {raster_path}")

  with rasterio.open(raster_path) as src:
    row_parts: list[np.ndarray] = []
    col_parts: list[np.ndarray] = []

    if int(raster_tile_size) > 0:
      tile = int(raster_tile_size)
      windows = [
        Window(
          col_off=col_off,
          row_off=row_off,
          width=min(tile, src.width - col_off),
          height=min(tile, src.height - row_off),
        )
        for row_off in range(0, src.height, tile)
        for col_off in range(0, src.width, tile)
      ]
    else:
      windows = [window for _, window in src.block_windows(1)]

    total_tiles = len(windows)
    for tile_i, window in enumerate(windows, start=1):
      block = src.read(1, window=window, masked=True)
      data = np.asarray(block.data, dtype=float)
      mask = np.ma.getmaskarray(block)
      if mask is not np.ma.nomask:
        data[mask] = np.nan
      valid = np.isfinite(data) if not np.isfinite(float(valid_threshold)) else np.isfinite(data) & (data > float(valid_threshold))
      if not np.any(valid):
        if int(progress_every_tiles) > 0 and ((tile_i % int(progress_every_tiles) == 0) or (tile_i == total_tiles)):
          print(f"Raster tiles scanned: {tile_i}/{total_tiles}", flush=True)
        continue

      r_local, c_local = np.where(valid)
      row_parts.append(r_local.astype(np.int64) + int(window.row_off))
      col_parts.append(c_local.astype(np.int64) + int(window.col_off))

      if int(progress_every_tiles) > 0 and ((tile_i % int(progress_every_tiles) == 0) or (tile_i == total_tiles)):
        print(f"Raster tiles scanned: {tile_i}/{total_tiles}", flush=True)

    if not row_parts:
      raise ValueError("No valid pixels found in raster after applying threshold.")

    rows = np.concatenate(row_parts)
    cols = np.concatenate(col_parts)

    xs, ys = rasterio.transform.xy(src.transform, rows, cols, offset="center")
    xs = np.asarray(xs, dtype=float)
    ys = np.asarray(ys, dtype=float)

    if src.crs is not None and src.crs.to_epsg() != 4326:
      lon, lat = rio_transform(src.crs, "EPSG:4326", xs.tolist(), ys.tolist())
      lon = np.asarray(lon, dtype=float)
      lat = np.asarray(lat, dtype=float)
    else:
      lon = xs
      lat = ys

    x_coords, _ = rasterio.transform.xy(src.transform, np.zeros(src.width, dtype=int), np.arange(src.width), offset="center")
    _, y_coords = rasterio.transform.xy(src.transform, np.arange(src.height), np.zeros(src.height, dtype=int), offset="center")

    nodata = src.nodata
    nodata_float = np.nan if nodata is None else float(nodata)

    return {
      "rows": rows.astype(np.int64),
      "cols": cols.astype(np.int64),
      "x_valid": xs,
      "y_valid": ys,
      "lon": lon,
      "lat": lat,
      "shape": (src.height, src.width),
      "x_coords": np.asarray(x_coords, dtype=float),
      "y_coords": np.asarray(y_coords, dtype=float),
      "crs_wkt": None if src.crs is None else src.crs.to_wkt(),
      "transform": src.transform,
      "nodata": nodata_float,
    }


def _sample_raster_values_at_points(
  raster_path: str | Path,
  x: np.ndarray,
  y: np.ndarray,
  source_crs_wkt: str | None,
  query_chunk_size: int = 500_000,
  progress_every_chunks: int = 0,
) -> np.ndarray:
  raster_path = Path(raster_path)
  if not raster_path.exists():
    raise FileNotFoundError(f"Raster not found: {raster_path}")

  x_arr = np.asarray(x, dtype=float)
  y_arr = np.asarray(y, dtype=float)
  out = np.full(len(x_arr), np.nan, dtype=np.float32)

  with rasterio.open(raster_path) as src:
    target_x = x_arr
    target_y = y_arr
    if source_crs_wkt is not None and src.crs is not None and source_crs_wkt != src.crs.to_wkt():
      tx, ty = rio_transform(source_crs_wkt, src.crs, x_arr.tolist(), y_arr.tolist())
      target_x = np.asarray(tx, dtype=float)
      target_y = np.asarray(ty, dtype=float)

    total_chunks = (len(target_x) + int(query_chunk_size) - 1) // int(query_chunk_size)
    nodata = src.nodata
    for chunk_i, chunk in enumerate(_iter_slices(len(target_x), int(query_chunk_size)), start=1):
      coords = np.column_stack([target_x[chunk], target_y[chunk]])
      vals = np.fromiter((v[0] for v in src.sample(coords)), dtype=float, count=coords.shape[0])
      if nodata is not None:
        vals[np.isclose(vals, float(nodata), equal_nan=True)] = np.nan
      out[chunk] = vals.astype(np.float32)
      if int(progress_every_chunks) > 0 and ((chunk_i % int(progress_every_chunks) == 0) or (chunk_i == total_chunks)):
        print(f"Elevation sampling chunks: {chunk_i}/{total_chunks}", flush=True)

  return out


def _build_daytime_times(day: pd.Timestamp, freq: str, start_hour: int, end_hour: int, utc_offset_hours: float = 0.0) -> pd.DatetimeIndex:
  """Build daytime timestep index.  Hours are local; returned timestamps are UTC."""
  start_ts = pd.Timestamp(day).normalize() + pd.Timedelta(hours=int(start_hour) - utc_offset_hours)
  end_ts = pd.Timestamp(day).normalize() + pd.Timedelta(hours=int(end_hour) - utc_offset_hours)
  times = pd.date_range(start=start_ts, end=end_ts, freq=freq)
  if len(times) == 0:
    raise ValueError("No timesteps generated for daytime window. Check `freq`, `day_start_hour`, and `day_end_hour`.")
  return times


def _to_hhmm_int16(min_time_per_pixel: np.ndarray, utc_offset_hours: float = 0.0) -> np.ndarray:
  """Convert datetime64 UTC timestamps to HHMM int16 in *local* time."""
  t64 = np.asarray(min_time_per_pixel, dtype="datetime64[m]")
  out = np.full(t64.shape, -9999, dtype=np.int16)
  valid = ~np.isnat(t64)
  if np.any(valid):
    offset_min = np.int64(round(utc_offset_hours * 60))
    mins = (t64[valid].astype(np.int64) + offset_min) % np.int64(24 * 60)
    hhmm = (mins // 60) * 100 + (mins % 60)
    out[valid] = hhmm.astype(np.int16)
  return out


def _time_step_hours(daytime_times: pd.DatetimeIndex) -> float:
  if len(daytime_times) < 2:
    return 0.0
  dt = np.diff(daytime_times.values.astype("datetime64[s]")).astype(np.int64)
  if len(dt) == 0:
    return 0.0
  return float(np.median(dt) / 3600.0)


def _classify_daytime_pattern(
  range_m: float,
  local_range_m: float = np.nan,
) -> np.int8:
  """Classify daytime tide as 0=neap, 1=moderate, 2=spring, 3=king."""
  if not np.isfinite(range_m):
    return np.int8(-1)

  # Prefer ratio-based classification when local tidal range is available
  if np.isfinite(local_range_m) and local_range_m > 0.0:
    ratio = range_m / local_range_m
    if ratio < 0.30:
      return np.int8(0)   # neap
    if ratio < 0.55:
      return np.int8(1)   # moderate
    if ratio < 0.80:
      return np.int8(2)   # spring
    return np.int8(3)     # king

  # Fallback: absolute thresholds (no local range raster)
  if range_m < 1.0:
    return np.int8(0)     # neap
  if range_m < 2.0:
    return np.int8(1)     # moderate
  if range_m < 3.5:
    return np.int8(2)     # spring
  return np.int8(3)       # king


def _classify_daytime_pattern_vectorized(
  range_arr: np.ndarray,
  local_range_arr: np.ndarray,
) -> np.ndarray:
  """Vectorized classification: 0=neap, 1=moderate, 2=spring, 3=king, -1=invalid."""
  out = np.full(len(range_arr), -1, dtype=np.int8)
  valid = np.isfinite(range_arr)
  if not np.any(valid):
    return out

  r = range_arr[valid]
  lr = local_range_arr[valid]

  # Check if ratio-based classification applies
  use_ratio = np.isfinite(lr) & (lr > 0.0)
  ratio = np.where(use_ratio, r / np.where(lr > 0, lr, 1.0), 0.0)

  cls = np.full(len(r), np.int8(3), dtype=np.int8)  # default: king

  # Ratio-based branch
  m = use_ratio
  cls[m] = np.int8(3)
  cls[m & (ratio < 0.80)] = np.int8(2)
  cls[m & (ratio < 0.55)] = np.int8(1)
  cls[m & (ratio < 0.30)] = np.int8(0)

  # Absolute threshold branch (no valid local range)
  a = ~use_ratio
  cls[a] = np.int8(3)
  cls[a & (r < 3.5)] = np.int8(2)
  cls[a & (r < 2.0)] = np.int8(1)
  cls[a & (r < 1.0)] = np.int8(0)

  out[valid] = cls
  return out


def _summarise_tide_series_daytime(
  tide_vals: np.ndarray,
  daytime_times: pd.DatetimeIndex,
  step_hours: float,
  utc_offset_hours: float = 0.0,
) -> dict[str, float | int]:
  out: dict[str, float | int] = {
    "low": np.nan,
    "high": np.nan,
    "mean": np.nan,
    "std": np.nan,
    "range": np.nan,
    "midday": np.nan,
    "argmin": 0,
    "argmax": 0,
    "low_tide_solar_offset": np.nan,
    "n_tide_cycles": np.int8(0),
  }

  valid = np.isfinite(tide_vals)
  if not np.any(valid):
    return out

  low = float(np.nanmin(tide_vals))
  high = float(np.nanmax(tide_vals))
  argmin = int(np.nanargmin(tide_vals))
  argmax = int(np.nanargmax(tide_vals))
  mean = float(np.nanmean(tide_vals))
  std = float(np.nanstd(tide_vals))
  tide_range = high - low

  # Local noon in UTC: derive the local calendar day from the first timestamp,
  # then place noon at 12:00 local -> (12 - offset) hours UTC on that local day.
  local_start = daytime_times[0] + pd.Timedelta(hours=utc_offset_hours)
  local_day = pd.Timestamp(local_start).normalize()
  target_mid = local_day + pd.Timedelta(hours=12 - utc_offset_hours)
  mid_idx = int(np.argmin(np.abs((daytime_times - target_mid).asi8)))
  midday = float(tide_vals[mid_idx]) if np.isfinite(tide_vals[mid_idx]) else np.nan

  # Solar offset: hours between min-tide time and local solar noon
  min_tide_ts = pd.Timestamp(daytime_times[argmin])
  solar_offset = float((min_tide_ts - target_mid).total_seconds() / 3600.0)

  # Count tide cycles: number of local minima (sign change from falling to rising)
  n_cycles = np.int8(0)
  finite_vals = tide_vals[valid] if not np.all(valid) else tide_vals
  if len(finite_vals) >= 3:
    diffs = np.diff(finite_vals)
    sign_changes = (diffs[:-1] < 0) & (diffs[1:] > 0)
    n_cycles = np.int8(min(int(np.sum(sign_changes)), 127))

  out.update(
    {
      "low": low,
      "high": high,
      "mean": mean,
      "std": std,
      "range": tide_range,
      "midday": midday,
      "argmin": argmin,
      "argmax": argmax,
      "low_tide_solar_offset": solar_offset,
      "n_tide_cycles": n_cycles,
    }
  )
  return out


def _build_daily_grids(
  rows: np.ndarray,
  cols: np.ndarray,
  shape: tuple[int, int],
  predictor_pixel_data: dict[str, np.ndarray],
  utc_offset_hours: float = 0.0,
) -> dict[str, np.ndarray]:
  grids: dict[str, np.ndarray] = {}
  for var_name in GRID_FLOAT_VARS:
    grid = np.full(shape, np.nan, dtype=np.float32)
    grid[rows, cols] = np.asarray(predictor_pixel_data[var_name], dtype=np.float32)
    grids[var_name] = grid

  for var_name in GRID_HHMM_VARS:
    grid = np.full(shape, -9999, dtype=np.int16)
    grid[rows, cols] = _to_hhmm_int16(predictor_pixel_data[var_name], utc_offset_hours=utc_offset_hours)
    grids[var_name] = grid

  for var_name in GRID_INT8_VARS:
    grid = np.full(shape, np.int8(-1), dtype=np.int8)
    grid[rows, cols] = np.asarray(predictor_pixel_data[var_name], dtype=np.int8)
    grids[var_name] = grid

  return grids


def _assemble_daily_dataset(
  date_label: pd.Timestamp,
  rows: np.ndarray,
  cols: np.ndarray,
  shape: tuple[int, int],
  x_coords: np.ndarray,
  y_coords: np.ndarray,
  min_height_per_pixel: np.ndarray,
  min_time_per_pixel: np.ndarray,
  crs_wkt: str | None,
  transform,
  model_name: str,
  freq: str,
  day_start_hour: int,
  day_end_hour: int,
  nodata: float,
) -> xr.Dataset:
  height_grid = np.full(shape, np.nan, dtype=np.float32)
  time_hhmm_grid = np.full(shape, -9999, dtype=np.int16)

  min_time_idx = pd.DatetimeIndex(min_time_per_pixel)
  hhmm_vals = np.where(
    min_time_idx.notna(),
    (min_time_idx.hour * 100 + min_time_idx.minute).astype(np.int16),
    np.int16(-9999),
  )

  height_grid[rows, cols] = min_height_per_pixel.astype(np.float32)
  time_hhmm_grid[rows, cols] = hhmm_vals
  date_ts = pd.Timestamp(date_label).normalize()

  ds = xr.Dataset(
    data_vars={
      "daytime_low_tide_height": (("time", "lat", "lon"), height_grid[np.newaxis, :, :]),
      "daytime_low_tide_time": (("time", "lat", "lon"), time_hhmm_grid[np.newaxis, :, :]),
    },
    coords={
      "time": ("time", np.asarray([np.datetime64(date_ts)], dtype="datetime64[ns]")),
      "lat": ("lat", y_coords),
      "lon": ("lon", x_coords),
    },
    attrs={
      "Conventions": "CF-1.7",
      "title": "Daily daytime low tide summary",
      "date": str(pd.Timestamp(date_label).date()),
      "daytime_window": f"{int(day_start_hour):02d}:00-{int(day_end_hour):02d}:00",
      "modelled_frequency": freq,
      "tide_model": model_name,
      "crs_wkt": "" if crs_wkt is None else crs_wkt,
      "transform": ",".join(str(v) for v in transform[:6]),
    },
  )

  ds["daytime_low_tide_height"].attrs = {
    "long_name": "Lowest daytime tide height",
    "units": "m",
    "_FillValue": np.float32(nodata) if np.isfinite(nodata) else np.float32(np.nan),
  }
  ds["daytime_low_tide_time"].attrs = {
    "long_name": "Time of lowest daytime tide",
    "format": "HHMM",
    "missing_value": np.int16(-9999),
  }
  return ds


def _assemble_daily_dataset_sparse(
  date_label: pd.Timestamp,
  rows: np.ndarray,
  cols: np.ndarray,
  x_valid: np.ndarray,
  y_valid: np.ndarray,
  lon: np.ndarray,
  lat: np.ndarray,
  min_height_per_pixel: np.ndarray,
  min_time_per_pixel: np.ndarray,
  crs_wkt: str | None,
  model_name: str,
  freq: str,
  day_start_hour: int,
  day_end_hour: int,
  predictor_pixel_data: dict[str, np.ndarray],
  utc_offset_hours: float = 0.0,
) -> xr.Dataset:
  n = len(rows)
  data_vars: dict[str, tuple[str, np.ndarray]] = {}

  for var_name in GRID_FLOAT_VARS:
    data_vars[var_name] = ("pixel", np.asarray(predictor_pixel_data[var_name], dtype=np.float32))

  for var_name in GRID_HHMM_VARS:
    data_vars[var_name] = ("pixel", _to_hhmm_int16(predictor_pixel_data[var_name], utc_offset_hours=utc_offset_hours))

  for var_name in GRID_INT8_VARS:
    data_vars[var_name] = ("pixel", np.asarray(predictor_pixel_data[var_name], dtype=np.int8))

  ds = xr.Dataset(
    data_vars=data_vars,
    coords={
      "pixel": ("pixel", np.arange(n, dtype=np.int64)),
      "row": ("pixel", rows.astype(np.int32)),
      "col": ("pixel", cols.astype(np.int32)),
      "x": ("pixel", x_valid.astype(float)),
      "y": ("pixel", y_valid.astype(float)),
      "lon": ("pixel", lon.astype(float)),
      "lat": ("pixel", lat.astype(float)),
    },
    attrs={
      "title": "Daily daytime low tide summary (sparse valid pixels)",
      "date": str(pd.Timestamp(date_label).date()),
      "daytime_window": f"{int(day_start_hour):02d}:00-{int(day_end_hour):02d}:00",
      "modelled_frequency": freq,
      "tide_model": model_name,
      "crs_wkt": "" if crs_wkt is None else crs_wkt,
      "output_format": "sparse",
    },
  )

  for var_name, (long_name, units) in GRID_FLOAT_VARS.items():
    ds[var_name].attrs = {"long_name": long_name, "units": units}

  for var_name, long_name in GRID_HHMM_VARS.items():
    ds[var_name].attrs = {
      "long_name": long_name,
      "format": "HHMM",
      "missing_value": np.int16(-9999),
    }

  for var_name, long_name in GRID_INT8_VARS.items():
    attrs: dict = {"long_name": long_name, "missing_value": np.int8(-1)}
    if var_name == "tide_class":
      attrs["flag_values"] = np.array(sorted(TIDE_CLASS_LABELS.keys()), dtype=np.int8)
      attrs["flag_meanings"] = " ".join(TIDE_CLASS_LABELS[k] for k in sorted(TIDE_CLASS_LABELS.keys()))
    ds[var_name].attrs = attrs
  return ds


@lru_cache(maxsize=4)
def _open_model_dataset_cached(model_path: str) -> xr.Dataset:
  return xr.open_dataset(model_path)


def _compute_face_chunk_day(
  model_path: str,
  face_idx_chunk: np.ndarray,
  face_lat_chunk: np.ndarray,
  daytime_times: pd.DatetimeIndex,
  constituents: tuple[str, ...],
  utc_offset_hours: float = 0.0,
) -> dict[str, np.ndarray]:
  ds = _open_model_dataset_cached(model_path)
  n = len(face_idx_chunk)
  n_times = len(daytime_times)
  step_hours = _time_step_hours(daytime_times)
  out = {
    "low": np.full(n, np.nan, dtype=np.float32),
    "high": np.full(n, np.nan, dtype=np.float32),
    "mean": np.full(n, np.nan, dtype=np.float32),
    "std": np.full(n, np.nan, dtype=np.float32),
    "range": np.full(n, np.nan, dtype=np.float32),
    "midday": np.full(n, np.nan, dtype=np.float32),
    "argmin": np.zeros(n, dtype=np.int64),
    "argmax": np.zeros(n, dtype=np.int64),
    "low_tide_solar_offset": np.full(n, np.nan, dtype=np.float32),
    "n_tide_cycles": np.zeros(n, dtype=np.int8),
    "tide_series": np.full((n, n_times), np.nan, dtype=np.float32),
  }

  for i, iface in enumerate(face_idx_chunk):
    tide_vals = _reconstruct_tides(
      ds=ds,
      iface=int(iface),
      lat=float(face_lat_chunk[i]),
      times=daytime_times,
      constituents=list(constituents),
    )
    out["tide_series"][i, :] = tide_vals.astype(np.float32)
    summary = _summarise_tide_series_daytime(tide_vals=tide_vals, daytime_times=daytime_times, step_hours=step_hours, utc_offset_hours=utc_offset_hours)

    out["low"][i] = np.float32(summary["low"])
    out["high"][i] = np.float32(summary["high"])
    out["mean"][i] = np.float32(summary["mean"])
    out["std"][i] = np.float32(summary["std"])
    out["range"][i] = np.float32(summary["range"])
    out["midday"][i] = np.float32(summary["midday"])
    out["argmin"][i] = int(summary["argmin"])
    out["argmax"][i] = int(summary["argmax"])
    out["low_tide_solar_offset"][i] = np.float32(summary["low_tide_solar_offset"])
    out["n_tide_cycles"][i] = np.int8(summary["n_tide_cycles"])

  return out


def _select_netcdf_engine() -> str | None:
  if importlib.util.find_spec("h5netcdf") is not None:
    return "h5netcdf"
  if importlib.util.find_spec("netCDF4") is not None:
    return "netcdf4"
  return None


def _write_netcdf_robust(ds_out: xr.Dataset, out_path: Path, encoding: dict) -> None:
  engine = _select_netcdf_engine()
  if engine is None:
    safe_encoding = {
      k: {kk: vv for kk, vv in v.items() if kk in {"dtype", "_FillValue"}}
      for k, v in encoding.items()
    }
    ds_out.to_netcdf(out_path, encoding=safe_encoding)
  else:
    ds_out.to_netcdf(out_path, engine=engine, encoding=encoding)


def _init_combined_grid_netcdf(
  file_path: Path,
  lat: np.ndarray,
  lon: np.ndarray,
  global_attrs: dict,
  compression_level: int = 4,
) -> None:
  file_path.parent.mkdir(parents=True, exist_ok=True)
  with Dataset(file_path, mode="w", format="NETCDF4") as ds:
    ds.createDimension("time", None)
    ds.createDimension("lat", len(lat))
    ds.createDimension("lon", len(lon))

    time_var = ds.createVariable("time", "f8", ("time",))
    time_var.units = "days since 1970-01-01 00:00:00"
    time_var.calendar = "standard"
    time_var.long_name = "time"

    lat_var = ds.createVariable("lat", "f8", ("lat",))
    lat_var[:] = lat
    lat_var.standard_name = "latitude"
    lat_var.long_name = "latitude"
    lat_var.units = "degrees_north"

    lon_var = ds.createVariable("lon", "f8", ("lon",))
    lon_var[:] = lon
    lon_var.standard_name = "longitude"
    lon_var.long_name = "longitude"
    lon_var.units = "degrees_east"

    for var_name, (long_name, units) in GRID_FLOAT_VARS.items():
      var = ds.createVariable(
        var_name,
        "f4",
        ("time", "lat", "lon"),
        zlib=True,
        complevel=int(compression_level),
        chunksizes=(1, min(1024, len(lat)), min(1024, len(lon))),
        fill_value=np.float32(np.nan),
      )
      var.long_name = long_name
      if units is not None:
        var.units = units

    for var_name, long_name in GRID_HHMM_VARS.items():
      var = ds.createVariable(
        var_name,
        "i2",
        ("time", "lat", "lon"),
        zlib=True,
        complevel=int(compression_level),
        chunksizes=(1, min(1024, len(lat)), min(1024, len(lon))),
        fill_value=np.int16(-9999),
      )
      var.long_name = long_name
      var.format = "HHMM"
      var.missing_value = np.int16(-9999)

    for var_name, long_name in GRID_INT8_VARS.items():
      var = ds.createVariable(
        var_name,
        "i1",
        ("time", "lat", "lon"),
        zlib=True,
        complevel=int(compression_level),
        chunksizes=(1, min(1024, len(lat)), min(1024, len(lon))),
        fill_value=np.int8(-1),
      )
      var.long_name = long_name
      if var_name == "tide_class":
        var.flag_values = np.array(sorted(TIDE_CLASS_LABELS.keys()), dtype=np.int8)
        var.flag_meanings = " ".join(TIDE_CLASS_LABELS[k] for k in sorted(TIDE_CLASS_LABELS.keys()))

    for key, value in global_attrs.items():
      setattr(ds, key, value)


def _append_combined_grid_netcdf(
  file_path: Path,
  day: pd.Timestamp,
  grid_vars_2d: dict[str, np.ndarray],
) -> None:
  with Dataset(file_path, mode="a") as ds:
    idx = ds.dimensions["time"].size
    time_num = date2num(
      pd.Timestamp(day).to_pydatetime(),
      units=ds.variables["time"].units,
      calendar=getattr(ds.variables["time"], "calendar", "standard"),
    )
    ds.variables["time"][idx] = time_num
    for var_name in GRID_FLOAT_VARS:
      ds.variables[var_name][idx, :, :] = np.asarray(grid_vars_2d[var_name], dtype=np.float32)
    for var_name in GRID_HHMM_VARS:
      ds.variables[var_name][idx, :, :] = np.asarray(grid_vars_2d[var_name], dtype=np.int16)
    for var_name in GRID_INT8_VARS:
      ds.variables[var_name][idx, :, :] = np.asarray(grid_vars_2d[var_name], dtype=np.int8)


def _write_daily_grid_streaming(
  out_path: Path,
  day: pd.Timestamp,
  rows: np.ndarray,
  cols: np.ndarray,
  shape: tuple[int, int],
  predictor_pixel_data: dict[str, np.ndarray],
  y_coords: np.ndarray,
  x_coords: np.ndarray,
  global_attrs: dict,
  utc_offset_hours: float = 0.0,
  write_row_chunk: int = 1024,
  progress_every_write_blocks: int = 10,
  compression_level: int = 1,
  verbose: bool = True,
) -> None:
  """Write a daily grid NetCDF from sparse pixel data without allocating full grids.

  Memory usage: O(write_row_chunk * nlon * 12) instead of O(nlat * nlon * 12).
  """
  out_path.parent.mkdir(parents=True, exist_ok=True)
  nlat, nlon = shape
  row_chunk = max(1, min(int(write_row_chunk), nlat))
  block_total = (nlat + row_chunk - 1) // row_chunk
  use_compression = int(compression_level) > 0

  # Pre-compute HHMM values from datetime arrays (sparse — ~17M int16s, small)
  hhmm_pixel = {}
  for var_name in GRID_HHMM_VARS:
    hhmm_pixel[var_name] = _to_hhmm_int16(predictor_pixel_data[var_name], utc_offset_hours=utc_offset_hours)

  # Sort pixel indices by row for fast per-chunk extraction
  sort_idx = np.argsort(rows)
  sorted_rows = rows[sort_idx]

  # Build searchsorted boundaries per row-chunk (O(n_blocks) lookups)
  # sorted_rows is sorted, so we can use searchsorted to find pixel spans
  t0 = time.perf_counter()
  with Dataset(out_path, mode="w", format="NETCDF4") as ds:
    ds.createDimension("time", 1)
    ds.createDimension("lat", nlat)
    ds.createDimension("lon", nlon)

    time_var = ds.createVariable("time", "f8", ("time",))
    time_var.units = "days since 1970-01-01 00:00:00"
    time_var.calendar = "standard"
    time_var.long_name = "time"
    time_var[0] = date2num(
      pd.Timestamp(day).to_pydatetime(),
      units=time_var.units,
      calendar=time_var.calendar,
    )

    lat_var = ds.createVariable("lat", "f8", ("lat",))
    lat_var[:] = y_coords
    lat_var.standard_name = "latitude"
    lat_var.long_name = "latitude"
    lat_var.units = "degrees_north"

    lon_var = ds.createVariable("lon", "f8", ("lon",))
    lon_var[:] = x_coords
    lon_var.standard_name = "longitude"
    lon_var.long_name = "longitude"
    lon_var.units = "degrees_east"

    float_nc = {}
    for var_name, (long_name, units) in GRID_FLOAT_VARS.items():
      var = ds.createVariable(
        var_name, "f4", ("time", "lat", "lon"),
        zlib=use_compression,
        complevel=max(1, int(compression_level)) if use_compression else 0,
        chunksizes=(1, row_chunk, min(1024, nlon)),
        fill_value=np.float32(np.nan),
      )
      var.long_name = long_name
      if units is not None:
        var.units = units
      float_nc[var_name] = var

    hhmm_nc = {}
    for var_name, long_name in GRID_HHMM_VARS.items():
      var = ds.createVariable(
        var_name, "i2", ("time", "lat", "lon"),
        zlib=use_compression,
        complevel=max(1, int(compression_level)) if use_compression else 0,
        chunksizes=(1, row_chunk, min(1024, nlon)),
        fill_value=np.int16(-9999),
      )
      var.long_name = long_name
      var.format = "HHMM"
      var.missing_value = np.int16(-9999)
      hhmm_nc[var_name] = var

    int8_nc = {}
    for var_name, long_name in GRID_INT8_VARS.items():
      var = ds.createVariable(
        var_name, "i1", ("time", "lat", "lon"),
        zlib=use_compression,
        complevel=max(1, int(compression_level)) if use_compression else 0,
        chunksizes=(1, row_chunk, min(1024, nlon)),
        fill_value=np.int8(-1),
      )
      var.long_name = long_name
      if var_name == "tide_class":
        var.flag_values = np.array(sorted(TIDE_CLASS_LABELS.keys()), dtype=np.int8)
        var.flag_meanings = " ".join(TIDE_CLASS_LABELS[k] for k in sorted(TIDE_CLASS_LABELS.keys()))
      int8_nc[var_name] = var

    for key, value in global_attrs.items():
      setattr(ds, key, value)

    # Pre-sort column indices for per-tile grouping
    sorted_cols = cols[sort_idx]
    col_chunk = min(1024, nlon)  # match HDF5 chunk col size

    # Pre-allocate slab buffers — reused across all tiles to avoid
    # repeated allocation of large arrays (was ~3 GB/block previously)
    _f4_buf = np.empty((row_chunk, col_chunk), dtype=np.float32)
    _i2_buf = np.empty((row_chunk, col_chunk), dtype=np.int16)
    _i1_buf = np.empty((row_chunk, col_chunk), dtype=np.int8)

    # Stream 2D tiles: iterate row-blocks, then column-tiles within each block.
    # Only tiles containing actual pixels are written; all others are left
    # unallocated in HDF5 (auto-filled with the variable's fill_value on read).
    n_row_blocks_done = 0
    n_row_blocks_empty = 0
    n_tiles_written = 0
    for block_i, row0 in enumerate(range(0, nlat, row_chunk), start=1):
      row1 = min(row0 + row_chunk, nlat)
      chunk_h = row1 - row0

      # Find pixels in this row range using sorted array
      pix_lo = int(np.searchsorted(sorted_rows, row0, side="left"))
      pix_hi = int(np.searchsorted(sorted_rows, row1, side="left"))

      if pix_hi <= pix_lo:
        n_row_blocks_empty += 1
        n_row_blocks_done += 1
        if bool(verbose) and int(progress_every_write_blocks) > 0 and (
          (n_row_blocks_done % int(progress_every_write_blocks) == 0) or (block_i == block_total)
        ):
          print(f"Write progress: row-block {n_row_blocks_done}/{block_total} ({n_tiles_written} tiles written)", flush=True)
        continue

      idx = sort_idx[pix_lo:pix_hi]
      block_rows = rows[idx] - row0
      block_cols = cols[idx]

      # Group pixels by column tile (aligned to HDF5 chunk boundaries)
      col_tile_ids = block_cols // col_chunk
      unique_col_tiles = np.unique(col_tile_ids)

      for ct_id in unique_col_tiles:
        ct_mask = col_tile_ids == ct_id
        ct_c0 = int(ct_id) * col_chunk
        ct_c1 = min(ct_c0 + col_chunk, nlon)
        ct_w = ct_c1 - ct_c0
        ct_idx = idx[ct_mask]
        ct_rows = block_rows[ct_mask]
        ct_cols = block_cols[ct_mask] - ct_c0

        # Float variables — reuse pre-allocated buffer view
        for var_name in GRID_FLOAT_VARS:
          slab = _f4_buf[:chunk_h, :ct_w]
          slab[:] = np.nan
          slab[ct_rows, ct_cols] = np.asarray(predictor_pixel_data[var_name][ct_idx], dtype=np.float32)
          float_nc[var_name][0, row0:row1, ct_c0:ct_c1] = slab

        # HHMM variables
        for var_name in GRID_HHMM_VARS:
          slab = _i2_buf[:chunk_h, :ct_w]
          slab[:] = -9999
          slab[ct_rows, ct_cols] = hhmm_pixel[var_name][ct_idx]
          hhmm_nc[var_name][0, row0:row1, ct_c0:ct_c1] = slab

        # Int8 variables
        for var_name in GRID_INT8_VARS:
          slab = _i1_buf[:chunk_h, :ct_w]
          slab[:] = np.int8(-1)
          slab[ct_rows, ct_cols] = np.asarray(predictor_pixel_data[var_name][ct_idx], dtype=np.int8)
          int8_nc[var_name][0, row0:row1, ct_c0:ct_c1] = slab

        n_tiles_written += 1

      n_row_blocks_done += 1
      if bool(verbose) and int(progress_every_write_blocks) > 0 and (
        (n_row_blocks_done % int(progress_every_write_blocks) == 0) or (block_i == block_total)
      ):
        print(f"Write progress: row-block {n_row_blocks_done}/{block_total} ({n_tiles_written} tiles written)", flush=True)

  if bool(verbose):
    elapsed = time.perf_counter() - t0
    print(f"Write complete in {elapsed:.1f}s ({n_tiles_written} tiles, "
          f"{n_row_blocks_empty} empty row-blocks skipped): {out_path}", flush=True)


def _build_spatial_cache_key(
  raster_path: Path,
  model_path: Path,
  valid_threshold: float,
  raster_tile_size: int,
) -> str:
  raster_stat = raster_path.stat()
  model_stat = model_path.stat()
  payload = {
    "raster_path": str(raster_path.resolve()),
    "raster_mtime": raster_stat.st_mtime,
    "raster_size": raster_stat.st_size,
    "model_path": str(model_path.resolve()),
    "model_mtime": model_stat.st_mtime,
    "model_size": model_stat.st_size,
    "valid_threshold": float(valid_threshold),
    "raster_tile_size": int(raster_tile_size),
  }
  digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
  return digest[:24]


def _load_spatial_cache(cache_dir: Path, key: str) -> dict | None:
  npz_path = cache_dir / f"spatial_{key}.npz"
  meta_path = cache_dir / f"spatial_{key}.json"
  if not npz_path.exists() or not meta_path.exists():
    return None

  try:
    with np.load(npz_path, allow_pickle=False) as arr:
      with meta_path.open("r", encoding="utf-8") as f:
        meta = json.load(f)

      out = {
        "rows": arr["rows"],
        "cols": arr["cols"],
        "x_valid": arr["x_valid"],
        "y_valid": arr["y_valid"],
        "lon": arr["lon"],
        "lat": arr["lat"],
        "shape": tuple(meta["shape"]),
        "x_coords": arr["x_coords"],
        "y_coords": arr["y_coords"],
        "crs_wkt": meta.get("crs_wkt"),
        "transform": tuple(meta["transform"]),
        "nodata": float(meta["nodata"]),
        "pixel_face_idx": arr["pixel_face_idx"],
        "unique_faces": arr["unique_faces"],
        "inverse": arr["inverse"],
      }
      # Optional elevation / tidal-range arrays (added by --prep-only)
      if "pixel_elevation" in arr:
        out["pixel_elevation"] = arr["pixel_elevation"]
      if "pixel_local_tidal_range" in arr:
        out["pixel_local_tidal_range"] = arr["pixel_local_tidal_range"]
      return out
  except Exception:
    return None


def _save_spatial_cache(cache_dir: Path, key: str, payload: dict) -> None:
  cache_dir.mkdir(parents=True, exist_ok=True)
  npz_path = cache_dir / f"spatial_{key}.npz"
  meta_path = cache_dir / f"spatial_{key}.json"

  arrays_to_save = {
    "rows": np.asarray(payload["rows"]),
    "cols": np.asarray(payload["cols"]),
    "x_valid": np.asarray(payload["x_valid"]),
    "y_valid": np.asarray(payload["y_valid"]),
    "lon": np.asarray(payload["lon"]),
    "lat": np.asarray(payload["lat"]),
    "x_coords": np.asarray(payload["x_coords"]),
    "y_coords": np.asarray(payload["y_coords"]),
    "pixel_face_idx": np.asarray(payload["pixel_face_idx"]),
    "unique_faces": np.asarray(payload["unique_faces"]),
    "inverse": np.asarray(payload["inverse"]),
  }
  # Optional elevation / tidal-range arrays
  if "pixel_elevation" in payload:
    arrays_to_save["pixel_elevation"] = np.asarray(payload["pixel_elevation"], dtype=np.float32)
  if "pixel_local_tidal_range" in payload:
    arrays_to_save["pixel_local_tidal_range"] = np.asarray(payload["pixel_local_tidal_range"], dtype=np.float32)

  np.savez_compressed(npz_path, **arrays_to_save)

  meta = {
    "shape": list(payload["shape"]),
    "crs_wkt": payload["crs_wkt"],
    "transform": [float(v) for v in payload["transform"]],
    "nodata": float(payload["nodata"]),
  }
  with meta_path.open("w", encoding="utf-8") as f:
    json.dump(meta, f)


def generate_daily_daytime_low_tide_netcdf(
  raster_path: str | Path,
  model_path: str | Path,
  output_dir: str | Path,
  start_date: str,
  end_date: str,
  freq: str = "10min",
  day_start_hour: int = 6,
  day_end_hour: int = 18,
  valid_threshold: float = float('-inf'),
  model: str = DEFAULT_MODEL_NAME,
  output_format: str = "grid",
  raster_tile_size: int = 4096,
  query_chunk_size: int = 2_000_000,
  elevation_raster: str | Path | None = None,
  tide_range_raster: str | Path | None = None,
  face_chunk_size: int = 2_000,
  max_workers: int | None = None,
  progress_every_faces: int = 2000,
  progress_every_tiles: int = 200,
  progress_every_query_chunks: int = 1,
  progress_every_elevation_chunks: int = 1,
  verbose: bool = True,
  use_cache: bool = True,
  cache_dir: str | Path = "cache/tide_prediction",
  output_mode: str = "daily",
  combined_output_name: str | None = None,
  write_row_chunk: int = 1024,
  progress_every_write_blocks: int = 10,
  grid_compression_level: int = 1,
  overwrite: bool = False,
  utc_offset_hours: float = 10.0,
  prep_only: bool = False,
) -> list[Path]:
  if int(day_end_hour) <= int(day_start_hour):
    raise ValueError("`day_end_hour` must be greater than `day_start_hour`.")

  output_dir = Path(output_dir)
  output_dir.mkdir(parents=True, exist_ok=True)

  raster_path = Path(raster_path)
  model_path = Path(model_path)
  cache_base = Path(cache_dir)

  # --- early sanity checks ---
  if not raster_path.exists():
    raise FileNotFoundError(f"Raster not found: {raster_path}")
  if not model_path.exists():
    raise FileNotFoundError(f"Tide model not found: {model_path}")
  if elevation_raster is not None and not Path(elevation_raster).exists():
    raise FileNotFoundError(f"Elevation raster not found: {elevation_raster}")
  if tide_range_raster is not None and not Path(tide_range_raster).exists():
    raise FileNotFoundError(f"Tide range raster not found: {tide_range_raster}")

  if bool(verbose):
    print("[CHECKPOINT] Input files verified. Loading tide model...", flush=True)

  model_obj = CsiROModel(model_path=model_path, model_name=model)

  if bool(verbose):
    print(f"[CHECKPOINT] Tide model loaded: {len(model_obj.constituents)} constituents, "
          f"{len(model_obj.face_lon):,} faces", flush=True)

  spatial_cache = None
  cache_key = _build_spatial_cache_key(
    raster_path=raster_path,
    model_path=model_path,
    valid_threshold=valid_threshold,
    raster_tile_size=raster_tile_size,
  )
  if bool(use_cache):
    spatial_cache = _load_spatial_cache(cache_base, cache_key)
    if bool(verbose) and spatial_cache is not None:
      print(f"[CHECKPOINT] Spatial cache hit: {cache_base / f'spatial_{cache_key}.npz'}", flush=True)

  if spatial_cache is None:
    if bool(verbose) and bool(use_cache):
      print("[CHECKPOINT] Spatial cache miss; building cache...", flush=True)

    pixel_data = _extract_valid_pixel_points(
      raster_path=raster_path,
      valid_threshold=valid_threshold,
      raster_tile_size=int(raster_tile_size),
      progress_every_tiles=int(progress_every_tiles) if bool(verbose) else 0,
    )

    rows = np.asarray(pixel_data["rows"], dtype=np.int64)
    cols = np.asarray(pixel_data["cols"], dtype=np.int64)
    x_valid = np.asarray(pixel_data["x_valid"], dtype=float)
    y_valid = np.asarray(pixel_data["y_valid"], dtype=float)
    lon = np.asarray(pixel_data["lon"], dtype=float)
    lat = np.asarray(pixel_data["lat"], dtype=float)
    shape = tuple(pixel_data["shape"])
    x_coords = np.asarray(pixel_data["x_coords"], dtype=float)
    y_coords = np.asarray(pixel_data["y_coords"], dtype=float)
    crs_wkt = pixel_data["crs_wkt"]
    transform = pixel_data["transform"]
    nodata = float(pixel_data["nodata"])

    pixel_face_idx, _ = _query_face_tree_many_chunked(
      model_obj.tree,
      lon,
      lat,
      query_chunk_size=int(query_chunk_size),
      progress_every_chunks=int(progress_every_query_chunks) if bool(verbose) else 0,
    )
    unique_faces, inverse = np.unique(pixel_face_idx, return_inverse=True)

    if bool(use_cache):
      _save_spatial_cache(
        cache_dir=cache_base,
        key=cache_key,
        payload={
          "rows": rows,
          "cols": cols,
          "x_valid": x_valid,
          "y_valid": y_valid,
          "lon": lon,
          "lat": lat,
          "shape": shape,
          "x_coords": x_coords,
          "y_coords": y_coords,
          "crs_wkt": crs_wkt,
          "transform": transform,
          "nodata": nodata,
          "pixel_face_idx": pixel_face_idx,
          "unique_faces": unique_faces,
          "inverse": inverse,
        },
      )
      if bool(verbose):
        print(f"[CHECKPOINT] Spatial cache written: {cache_base / f'spatial_{cache_key}.npz'}", flush=True)
  else:
    rows = np.asarray(spatial_cache["rows"], dtype=np.int64)
    cols = np.asarray(spatial_cache["cols"], dtype=np.int64)
    x_valid = np.asarray(spatial_cache["x_valid"], dtype=float)
    y_valid = np.asarray(spatial_cache["y_valid"], dtype=float)
    lon = np.asarray(spatial_cache["lon"], dtype=float)
    lat = np.asarray(spatial_cache["lat"], dtype=float)
    shape = tuple(spatial_cache["shape"])
    x_coords = np.asarray(spatial_cache["x_coords"], dtype=float)
    y_coords = np.asarray(spatial_cache["y_coords"], dtype=float)
    crs_wkt = spatial_cache["crs_wkt"]
    transform = tuple(spatial_cache["transform"])
    nodata = float(spatial_cache["nodata"])
    pixel_face_idx = np.asarray(spatial_cache["pixel_face_idx"], dtype=np.int64)
    unique_faces = np.asarray(spatial_cache["unique_faces"], dtype=np.int64)
    inverse = np.asarray(spatial_cache["inverse"], dtype=np.int64)

  if bool(verbose):
    print(f"[CHECKPOINT] Valid pixels: {len(rows):,}; unique model faces: {len(unique_faces):,}", flush=True)

  # --- Elevation: prefer explicit raster, else fall back to cache ---
  _elev_in_cache = spatial_cache is not None and "pixel_elevation" in spatial_cache
  _range_in_cache = spatial_cache is not None and "pixel_local_tidal_range" in spatial_cache

  pixel_elevation = np.full(len(rows), np.nan, dtype=np.float32)
  if elevation_raster is not None:
    if bool(verbose):
      print(f"[CHECKPOINT] Sampling elevation values: {Path(elevation_raster)}", flush=True)
    pixel_elevation = _sample_raster_values_at_points(
      raster_path=elevation_raster,
      x=x_valid,
      y=y_valid,
      source_crs_wkt=crs_wkt,
      query_chunk_size=int(query_chunk_size),
      progress_every_chunks=int(progress_every_elevation_chunks) if bool(verbose) else 0,
    )
  elif _elev_in_cache:
    pixel_elevation = np.asarray(spatial_cache["pixel_elevation"], dtype=np.float32)
    if bool(verbose):
      _nv = int(np.sum(np.isfinite(pixel_elevation)))
      print(f"[CHECKPOINT] Elevation loaded from cache ({_nv:,} valid pixels)", flush=True)

  pixel_local_tidal_range = np.full(len(rows), np.nan, dtype=np.float32)
  if tide_range_raster is not None:
    if bool(verbose):
      print(f"[CHECKPOINT] Sampling local tidal range values: {Path(tide_range_raster)}", flush=True)
    pixel_local_tidal_range = _sample_raster_values_at_points(
      raster_path=tide_range_raster,
      x=x_valid,
      y=y_valid,
      source_crs_wkt=crs_wkt,
      query_chunk_size=int(query_chunk_size),
      progress_every_chunks=int(progress_every_elevation_chunks) if bool(verbose) else 0,
    )
  elif _range_in_cache:
    pixel_local_tidal_range = np.asarray(spatial_cache["pixel_local_tidal_range"], dtype=np.float32)
    if bool(verbose):
      _nv = int(np.sum(np.isfinite(pixel_local_tidal_range)))
      print(f"[CHECKPOINT] Tidal range loaded from cache ({_nv:,} valid pixels)", flush=True)

  # --- prep-only mode: save all cached data (incl. elevation) and exit ---
  if prep_only:
    _elev_sampled = np.any(np.isfinite(pixel_elevation))
    _range_sampled = np.any(np.isfinite(pixel_local_tidal_range))
    if bool(use_cache) and (_elev_sampled or _range_sampled):
      _payload = {
        "rows": rows, "cols": cols, "x_valid": x_valid, "y_valid": y_valid,
        "lon": lon, "lat": lat, "shape": shape, "x_coords": x_coords,
        "y_coords": y_coords, "crs_wkt": crs_wkt, "transform": transform,
        "nodata": nodata, "pixel_face_idx": pixel_face_idx,
        "unique_faces": unique_faces, "inverse": inverse,
      }
      if _elev_sampled:
        _payload["pixel_elevation"] = pixel_elevation
      if _range_sampled:
        _payload["pixel_local_tidal_range"] = pixel_local_tidal_range
      _save_spatial_cache(cache_dir=cache_base, key=cache_key, payload=_payload)
      if bool(verbose):
        print(f"[CHECKPOINT] Cache updated with elevation/tidal-range data", flush=True)
        print(f"  Cache file: {cache_base / f'spatial_{cache_key}.npz'}", flush=True)

    if bool(verbose):
      _ne = int(np.sum(np.isfinite(pixel_elevation)))
      _nr = int(np.sum(np.isfinite(pixel_local_tidal_range)))
      print(f"\n{'=' * 60}", flush=True)
      print(f"PREP-ONLY complete", flush=True)
      print(f"  Valid pixels:  {len(rows):,}", flush=True)
      print(f"  Unique faces:  {len(unique_faces):,}", flush=True)
      print(f"  Elevation:     {_ne:,} valid  ({_ne / max(1, len(rows)) * 100:.1f}%)", flush=True)
      print(f"  Tidal range:   {_nr:,} valid  ({_nr / max(1, len(rows)) * 100:.1f}%)", flush=True)
      print(f"{'=' * 60}", flush=True)
    return []

  if max_workers is None:
    max_workers = 1 if len(unique_faces) < 10_000 else max(1, (os.cpu_count() or 2) - 1)

  days = pd.date_range(start=pd.Timestamp(start_date).normalize(), end=pd.Timestamp(end_date).normalize(), freq="D")
  if len(days) == 0:
    raise ValueError("No days in requested date range.")

  outputs: list[Path] = []
  combined_path: Path | None = None

  if output_mode not in {"daily", "single"}:
    raise ValueError("`output_mode` must be either 'daily' or 'single'.")

  if output_mode == "single":
    if output_format != "grid":
      raise ValueError("`output_mode='single'` currently requires `output_format='grid'`.")

    if combined_output_name is None:
      combined_output_name = f"daytime_low_tide_{days[0].strftime('%Y%m%d')}_{days[-1].strftime('%Y%m%d')}.nc"

    combined_path = output_dir / combined_output_name
    if combined_path.exists() and overwrite:
      combined_path.unlink()

    if not combined_path.exists():
      _init_combined_grid_netcdf(
        file_path=combined_path,
        lat=y_coords,
        lon=x_coords,
        global_attrs={
          "Conventions": "CF-1.7",
          "title": "Daily daytime low tide summary",
          "daytime_window": f"{int(day_start_hour):02d}:00-{int(day_end_hour):02d}:00 local",
          "utc_offset_hours": float(utc_offset_hours),
          "modelled_frequency": freq,
          "tide_model": model_obj.model_name,
          "crs_wkt": "" if crs_wkt is None else crs_wkt,
          "transform": ",".join(str(v) for v in transform[:6]),
        },
      )
      if bool(verbose):
        print(f"[CHECKPOINT] Initialized combined NetCDF: {combined_path}", flush=True)

  n_processed = 0
  n_skipped = 0
  n_failed = 0
  run_t0 = time.time()

  for day_i, day in enumerate(days):
    if bool(verbose):
      print(f"[{day_i + 1}/{len(days)}] Processing day: {day.strftime('%Y-%m-%d')}", flush=True)

    out_path = output_dir / f"daytime_low_tide_{day.strftime('%Y%m%d')}.nc"
    if output_mode == "daily" and out_path.exists() and not overwrite:
      if bool(verbose):
        print(f"  Skipping existing file: {out_path}", flush=True)
      outputs.append(out_path)
      n_skipped += 1
      continue

    day_t0 = time.time()
    t_stage = time.time()

    daytime_times = _build_daytime_times(day=day, freq=freq, start_hour=day_start_hour, end_hour=day_end_hour, utc_offset_hours=utc_offset_hours)
    n_faces = unique_faces.shape[0]
    n_timesteps = len(daytime_times)
    step_hours = _time_step_hours(daytime_times)
    face_low = np.full(n_faces, np.nan, dtype=np.float32)
    face_high = np.full(n_faces, np.nan, dtype=np.float32)
    face_mean = np.full(n_faces, np.nan, dtype=np.float32)
    face_std = np.full(n_faces, np.nan, dtype=np.float32)
    face_range = np.full(n_faces, np.nan, dtype=np.float32)
    face_midday = np.full(n_faces, np.nan, dtype=np.float32)
    face_argmin = np.zeros(n_faces, dtype=np.int64)
    face_argmax = np.zeros(n_faces, dtype=np.int64)
    face_solar_offset = np.full(n_faces, np.nan, dtype=np.float32)
    face_n_cycles = np.zeros(n_faces, dtype=np.int8)
    face_tide_series = np.full((n_faces, n_timesteps), np.nan, dtype=np.float32)

    if int(max_workers) <= 1:
      for i, iface in enumerate(unique_faces):
        iface_int = int(iface)
        tide_vals = _reconstruct_tides(
          ds=model_obj.ds,
          iface=iface_int,
          lat=float(model_obj.face_lat[iface_int]),
          times=daytime_times,
          constituents=model_obj.constituents,
        )
        face_tide_series[i, :] = tide_vals.astype(np.float32)
        summary = _summarise_tide_series_daytime(tide_vals=tide_vals, daytime_times=daytime_times, step_hours=step_hours, utc_offset_hours=utc_offset_hours)
        face_low[i] = np.float32(summary["low"])
        face_high[i] = np.float32(summary["high"])
        face_mean[i] = np.float32(summary["mean"])
        face_std[i] = np.float32(summary["std"])
        face_range[i] = np.float32(summary["range"])
        face_midday[i] = np.float32(summary["midday"])
        face_argmin[i] = int(summary["argmin"])
        face_argmax[i] = int(summary["argmax"])
        face_solar_offset[i] = np.float32(summary["low_tide_solar_offset"])
        face_n_cycles[i] = np.int8(summary["n_tide_cycles"])

        if bool(verbose) and int(progress_every_faces) > 0 and (((i + 1) % int(progress_every_faces) == 0) or ((i + 1) == len(unique_faces))):
          print(f"Faces processed: {i + 1}/{len(unique_faces)}", flush=True)
    else:
      chunks = list(_iter_slices(len(unique_faces), int(face_chunk_size)))
      completed_faces = 0
      with ProcessPoolExecutor(max_workers=int(max_workers)) as executor:
        futures = [
          executor.submit(
            _compute_face_chunk_day,
            str(model_obj.model_path),
            unique_faces[chunk],
            model_obj.face_lat[unique_faces[chunk]],
            daytime_times,
            tuple(model_obj.constituents),
            float(utc_offset_hours),
          )
          for chunk in chunks
        ]

        for chunk, future in zip(chunks, futures):
          chunk_out = future.result()
          face_low[chunk] = chunk_out["low"]
          face_high[chunk] = chunk_out["high"]
          face_mean[chunk] = chunk_out["mean"]
          face_std[chunk] = chunk_out["std"]
          face_range[chunk] = chunk_out["range"]
          face_midday[chunk] = chunk_out["midday"]
          face_argmin[chunk] = chunk_out["argmin"]
          face_argmax[chunk] = chunk_out["argmax"]
          face_solar_offset[chunk] = chunk_out["low_tide_solar_offset"]
          face_n_cycles[chunk] = chunk_out["n_tide_cycles"]
          face_tide_series[chunk] = chunk_out["tide_series"]
          completed_faces += int(chunk.stop - chunk.start)
          if bool(verbose) and int(progress_every_faces) > 0 and ((completed_faces % int(progress_every_faces) == 0) or (completed_faces >= len(unique_faces))):
            print(f"Faces processed: {completed_faces}/{len(unique_faces)}", flush=True)

    # Broadcast face-level summaries to pixels
    t_faces = time.time() - t_stage
    t_stage = time.time()

    low_height_per_pixel = face_low[inverse]
    high_height_per_pixel = face_high[inverse]
    low_time_per_pixel = np.full(len(rows), np.datetime64("NaT"), dtype="datetime64[ns]")
    high_time_per_pixel = np.full(len(rows), np.datetime64("NaT"), dtype="datetime64[ns]")
    valid_low = np.isfinite(low_height_per_pixel)
    valid_high = np.isfinite(high_height_per_pixel)
    low_time_per_pixel[valid_low] = daytime_times.values[face_argmin[inverse][valid_low]]
    high_time_per_pixel[valid_high] = daytime_times.values[face_argmax[inverse][valid_high]]

    # Per-pixel exposure from face tide series + pixel elevation (chunked for memory)
    # NOTE: check pixel_elevation (not elevation_raster) so cached elevation works
    pixel_hours_exposed = np.full(len(rows), np.nan, dtype=np.float32)
    pixel_exposure_max = np.full(len(rows), np.nan, dtype=np.float32)
    _has_elev = np.isfinite(pixel_elevation)
    if np.any(_has_elev):
      _elev_idx = np.where(_has_elev)[0]
      _exp_chunk_sz = 500_000
      for _ci in range(0, len(_elev_idx), _exp_chunk_sz):
        _chunk_idx = _elev_idx[_ci:_ci + _exp_chunk_sz]
        _tide_chunk = face_tide_series[inverse[_chunk_idx]]
        _elev_chunk = pixel_elevation[_chunk_idx]
        _valid_t = np.isfinite(_tide_chunk)
        _exposed = (_tide_chunk < _elev_chunk[:, np.newaxis]) & _valid_t
        pixel_hours_exposed[_chunk_idx] = np.sum(_exposed, axis=1).astype(np.float32) * step_hours
        # Max contiguous exposure run (vectorised column scan)
        _running = np.zeros(len(_chunk_idx), dtype=np.int32)
        _best = np.zeros(len(_chunk_idx), dtype=np.int32)
        for _t in range(_exposed.shape[1]):
          _running = np.where(_exposed[:, _t], _running + 1, 0)
          _best = np.maximum(_best, _running)
        pixel_exposure_max[_chunk_idx] = _best.astype(np.float32) * step_hours
      if bool(verbose):
        n_elev = int(np.sum(_has_elev))
        print(f"[CHECKPOINT] Pixel exposure computed for {n_elev:,} pixels with elevation data", flush=True)

    t_exposure = time.time() - t_stage
    t_stage = time.time()

    # Free the per-face tide series — no longer needed after exposure
    del face_tide_series

    # Per-pixel tide classification (vectorized)
    pixel_range = face_range[inverse]
    pixel_tide_class = _classify_daytime_pattern_vectorized(pixel_range, pixel_local_tidal_range)
    if bool(verbose):
      print(f"[CHECKPOINT] Tide classification done for {len(pixel_tide_class):,} pixels", flush=True)

    t_classify = time.time() - t_stage
    t_stage = time.time()

    # Derived tide-position variables (vectorized, no new tidal simulation)
    _face_midday_px = face_midday[inverse]
    _face_std_px = face_std[inverse]
    _face_range_px = face_range[inverse]
    _face_mean_px = face_mean[inverse]

    _safe_range = np.where(_face_range_px > 0, _face_range_px, np.nan)
    pixel_tide_position_midday = np.where(
      np.isfinite(_safe_range),
      ((_face_midday_px - low_height_per_pixel) / _safe_range),
      np.nan,
    ).astype(np.float32)

    _safe_std = np.where(_face_std_px > 0, _face_std_px, np.nan)
    pixel_tide_zscore_midday = np.where(
      np.isfinite(_safe_std),
      ((_face_midday_px - _face_mean_px) / _safe_std),
      np.nan,
    ).astype(np.float32)

    pixel_time_from_low_abs_h = np.abs(face_solar_offset[inverse]).astype(np.float32)

    pixel_low_tide_flag = np.where(
      np.isfinite(pixel_tide_position_midday),
      (pixel_tide_position_midday <= 0.2).astype(np.int8),
      np.int8(-1),
    ).astype(np.int8)

    predictor_pixel_data = {
      "min_tide_height": low_height_per_pixel,
      "min_tide_time": low_time_per_pixel,
      "max_tide_height": high_height_per_pixel,
      "max_tide_time": high_time_per_pixel,
      "tide_range": face_range[inverse],
      "mean_tide_height": face_mean[inverse],
      "midday_tide_height": face_midday[inverse],
      "hours_exposed": pixel_hours_exposed,
      "low_tide_solar_offset": face_solar_offset[inverse].astype(np.float32),
      "tide_class": pixel_tide_class,
      "exposure_duration_max": pixel_exposure_max,
      "n_tide_cycles": face_n_cycles[inverse],
      "tide_position_midday": pixel_tide_position_midday,
      "tide_zscore_midday": pixel_tide_zscore_midday,
      "time_from_low_abs_h": pixel_time_from_low_abs_h,
      "low_tide_midday_flag": pixel_low_tide_flag,
    }

    if output_format == "grid":
      ds_out = None
    else:
      ds_out = _assemble_daily_dataset_sparse(
        date_label=day,
        rows=rows,
        cols=cols,
        x_valid=x_valid,
        y_valid=y_valid,
        lon=lon,
        lat=lat,
        min_height_per_pixel=low_height_per_pixel,
        min_time_per_pixel=low_time_per_pixel,
        crs_wkt=crs_wkt,
        model_name=model_obj.model_name,
        freq=freq,
        day_start_hour=day_start_hour,
        day_end_hour=day_end_hour,
        predictor_pixel_data=predictor_pixel_data,
        utc_offset_hours=utc_offset_hours,
      )
      sparse_chunk = min(1_000_000, max(10_000, len(rows)))
      encoding = {}
      for var_name in GRID_FLOAT_VARS:
        encoding[var_name] = {"zlib": True, "complevel": 4, "dtype": "float32", "chunksizes": (sparse_chunk,)}
      for var_name in GRID_HHMM_VARS:
        encoding[var_name] = {"zlib": True, "complevel": 4, "dtype": "int16", "chunksizes": (sparse_chunk,)}
      for var_name in GRID_INT8_VARS:
        encoding[var_name] = {"zlib": True, "complevel": 4, "dtype": "int8", "chunksizes": (sparse_chunk,)}

    if output_mode == "daily":
      if output_format == "grid":
        _write_daily_grid_streaming(
          out_path=out_path,
          day=day,
          rows=rows,
          cols=cols,
          shape=shape,
          predictor_pixel_data=predictor_pixel_data,
          y_coords=y_coords,
          x_coords=x_coords,
          global_attrs={
            "Conventions": "CF-1.7",
            "title": "Daily daytime low tide summary",
            "date": str(pd.Timestamp(day).date()),
            "daytime_window": f"{int(day_start_hour):02d}:00-{int(day_end_hour):02d}:00 local",
            "utc_offset_hours": float(utc_offset_hours),
            "modelled_frequency": freq,
            "tide_model": model_obj.model_name,
            "crs_wkt": "" if crs_wkt is None else crs_wkt,
            "transform": ",".join(str(v) for v in transform[:6]),
            "elevation_raster": "" if elevation_raster is None else str(Path(elevation_raster)),
          },
          utc_offset_hours=utc_offset_hours,
          write_row_chunk=int(write_row_chunk),
          progress_every_write_blocks=int(progress_every_write_blocks),
          compression_level=int(grid_compression_level),
          verbose=bool(verbose),
        )
      else:
        _write_netcdf_robust(ds_out, out_path=out_path, encoding=encoding)
        if bool(verbose):
          print(f"Wrote: {out_path}", flush=True)
      outputs.append(out_path)
    else:
      raise NotImplementedError(
        "output_mode='single' with grid format is not supported at GBR scale. "
        "Use output_mode='daily' instead."
      )
      if bool(verbose):
        print(f"Appended day to: {combined_path}", flush=True)

    n_processed += 1
    if bool(verbose):
      t_write = time.time() - t_stage
      elapsed = time.time() - day_t0
      print(f"  Day complete in {elapsed:.1f}s  [ faces={t_faces:.1f}s  exposure={t_exposure:.1f}s  classify={t_classify:.1f}s  write={t_write:.1f}s ]", flush=True)

  if output_mode == "single" and combined_path is not None:
    outputs = [combined_path]

  total_elapsed = time.time() - run_t0
  if bool(verbose):
    print(f"\n{'=' * 60}", flush=True)
    print(f"Run complete: {n_processed} processed, {n_skipped} skipped, {n_failed} failed", flush=True)
    print(f"Total days: {len(days)}  |  Wall time: {total_elapsed:.1f}s ({total_elapsed / 3600:.2f}h)", flush=True)
    if n_processed > 0:
      print(f"Avg per day: {total_elapsed / max(1, n_processed + n_skipped):.1f}s", flush=True)
    print(f"{'=' * 60}", flush=True)

  return outputs


def _build_cli_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(
    description="Generate daily daytime low-tide NetCDF files for valid intertidal pixels.",
    epilog=CLI_HELP_NOTES,
    formatter_class=argparse.RawDescriptionHelpFormatter,
  )
  parser.add_argument("--raster", required=True, help="Path to cleaned intertidal raster mask (GeoTIFF).")
  parser.add_argument("--model-path", required=True, help="Path to CSIRO harmonic model NetCDF file.")
  parser.add_argument("--output-dir", required=True, help="Directory for daily NetCDF outputs.")
  parser.add_argument("--start-date", required=True, help="Start date (YYYY-MM-DD).")
  parser.add_argument("--end-date", required=True, help="End date (YYYY-MM-DD).")
  parser.add_argument("--freq", default="10min", help="Timestep frequency (default: 10min).")
  parser.add_argument("--day-start-hour", type=int, default=6, help="Daytime start hour (default: 6).")
  parser.add_argument("--day-end-hour", type=int, default=18, help="Daytime end hour (default: 18).")
  parser.add_argument("--valid-threshold", type=float, default=float('-inf'), help="Valid pixel threshold applied to raster values (default: -inf, i.e. all non-NA pixels included).")
  parser.add_argument("--model-name", default=DEFAULT_MODEL_NAME, help="Model name label to write into outputs.")
  parser.add_argument("--output-format", choices=["sparse", "grid"], default="grid", help="Output layout. 'grid' writes template-like NetCDF structure with dimensions (time, lat, lon).")
  parser.add_argument("--raster-tile-size", type=int, default=4096, help="Tile size used when scanning valid raster pixels. Use <=0 to use source block windows.")
  parser.add_argument("--query-chunk-size", type=int, default=2_000_000, help="Chunk size for nearest-face KD-tree queries.")
  parser.add_argument("--elevation-raster", type=str, default=None, help="Optional elevation raster used to derive daytime exposure predictors per valid pixel.")
  parser.add_argument("--tide-range-raster", type=str, default=None, help="Optional local tidal range raster (m). Used for ratio-based tide pattern classification and output as local_tidal_range.")
  parser.add_argument("--face-chunk-size", type=int, default=2_000, help="Unique-face chunk size for parallel processing.")
  parser.add_argument("--max-workers", type=int, default=None, help="Number of worker processes for face-level tide modelling.")
  parser.add_argument("--progress-every-faces", type=int, default=2000, help="Print progress every N model faces processed.")
  parser.add_argument("--progress-every-tiles", type=int, default=200, help="Print progress every N raster tiles scanned.")
  parser.add_argument("--progress-every-query-chunks", type=int, default=1, help="Print progress every N KD-tree query chunks.")
  parser.add_argument("--progress-every-elevation-chunks", type=int, default=1, help="Print progress every N elevation sampling chunks.")
  parser.add_argument("--cache-dir", type=str, default="cache/tide_prediction", help="Directory used to persist/load spatial preprocessing cache.")
  parser.add_argument("--no-cache", action="store_true", help="Disable spatial preprocessing cache.")
  parser.add_argument("--output-mode", choices=["daily", "single"], default="daily", help="Write one file per day ('daily', default) or one combined file for full period ('single').")
  parser.add_argument("--combined-output-name", type=str, default=None, help="Output filename when --output-mode single (default auto-generated).")
  parser.add_argument("--write-row-chunk", type=int, default=1024, help="Row chunk size for daily grid NetCDF writing.")
  parser.add_argument("--progress-every-write-blocks", type=int, default=10, help="Print progress every N row blocks while writing daily grid files.")
  parser.add_argument("--grid-compression-level", type=int, default=1, help="Compression level for grid outputs (0 disables compression; faster writes with larger files).")
  parser.add_argument("--quiet", action="store_true", help="Disable progress output.")
  parser.add_argument("--overwrite", action="store_true", help="Overwrite existing daily files.")
  parser.add_argument("--utc-offset", type=float, default=10.0, help="UTC offset for local time zone (default: 10 = AEST/Queensland). Day-start/end hours are treated as local time; tide model runs in UTC.")
  parser.add_argument("--prep-only", action="store_true", help="Run spatial preprocessing and elevation sampling only. Builds/populates cache and exits without processing any days. Use in Phase 1 of a 2-phase HPC workflow.")
  return parser


if __name__ == "__main__":
  wall_start = time.time()
  args = _build_cli_parser().parse_args()

  print(f"Tide_predictions.py  |  start={args.start_date}  end={args.end_date}", flush=True)
  print(f"  raster : {args.raster}", flush=True)
  print(f"  output : {args.output_dir}", flush=True)
  print(f"  workers: {args.max_workers}  format: {args.output_format}  mode: {args.output_mode}", flush=True)
  print(f"  utc_off: {args.utc_offset}  elevation: {args.elevation_raster}", flush=True)
  if args.prep_only:
    print(f"  MODE:    prep-only (cache build, no prediction)", flush=True)
  print(flush=True)

  try:
    out_files = generate_daily_daytime_low_tide_netcdf(
      raster_path=args.raster,
      model_path=args.model_path,
      output_dir=args.output_dir,
      start_date=args.start_date,
      end_date=args.end_date,
      freq=args.freq,
      day_start_hour=args.day_start_hour,
      day_end_hour=args.day_end_hour,
      valid_threshold=args.valid_threshold,
      model=args.model_name,
      output_format=args.output_format,
      raster_tile_size=args.raster_tile_size,
      query_chunk_size=args.query_chunk_size,
      elevation_raster=args.elevation_raster,
      tide_range_raster=args.tide_range_raster,
      face_chunk_size=args.face_chunk_size,
      max_workers=args.max_workers,
      progress_every_faces=args.progress_every_faces,
      progress_every_tiles=args.progress_every_tiles,
      progress_every_query_chunks=args.progress_every_query_chunks,
      progress_every_elevation_chunks=args.progress_every_elevation_chunks,
      verbose=not bool(args.quiet),
      use_cache=not bool(args.no_cache),
      cache_dir=args.cache_dir,
      output_mode=args.output_mode,
      combined_output_name=args.combined_output_name,
      write_row_chunk=args.write_row_chunk,
      progress_every_write_blocks=args.progress_every_write_blocks,
      grid_compression_level=args.grid_compression_level,
      overwrite=bool(args.overwrite),
      utc_offset_hours=args.utc_offset,
      prep_only=bool(args.prep_only),
    )
    print(f"Generated {len(out_files)} files", flush=True)
    for p in out_files[:5]:
      print(p, flush=True)
    if len(out_files) > 5:
      print("...", flush=True)
    wall_secs = time.time() - wall_start
    print(f"\nTotal wall time: {wall_secs:.1f}s ({wall_secs / 3600:.2f}h)", flush=True)
    sys.exit(0)
  except Exception as exc:
    print(f"\nFATAL ERROR: {exc}", file=sys.stderr, flush=True)
    import traceback
    traceback.print_exc(file=sys.stderr)
    sys.exit(1)

