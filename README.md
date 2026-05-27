# Planet Low Tide Browser

Plain Python web UI for finding PlanetScope daily imagery over an AOI, sorting
candidates by CSIRO-predicted tide height, visually reviewing scenes, and
exporting kept image IDs for QGIS or Planet API ordering.

## Quick Start

1. Install Python 3.11 on the VM if it is not already available.
2. Put `CSIRO_tidal_const_v12.nc` in the app's `tide/` folder.
3. Optional: copy `config.example.py` to `config.py` and add your Planet API key.
4. Double-click `Launch_Planet_Low_Tide_Browser.bat`.

The launcher creates a local `.venv` environment on first run, then starts the
app at <http://127.0.0.1:5050>.

## Important Runtime Requirement

This tool will not run tide predictions unless the CSIRO model file is present:

- Required file: `CSIRO_tidal_const_v12.nc`
- Download: <https://data.csiro.au/collection/csiro:45584>
- Location for this app: `tide/CSIRO_tidal_const_v12.nc`
- Note: the `.nc` file is intentionally not stored in this repository.

## Environment

Planet MCP requires Python 3.11 or higher. On shared VMs, install Python 3.11
side-by-side with any existing Python versions and run this app with the Windows
Python launcher:

```cmd
py -3.11 --version
```

It is fine if `python --version` points to another Python version. The launcher
uses `py -3.11` explicitly so existing Python 3.10 workflows are not changed.

Recommended Windows install command, when `winget` is available:

```cmd
winget install -e --id Python.Python.3.11 --scope user
```

The local `.venv` installs:

- `planet` for Planet Data and Orders API access
- `planet-mcp` for agent/tool integration
- Flask for the local web UI
- `timezonefinder` for converting Planet UTC acquisition times to AOI-local time
- raster/xarray/UTide dependencies required by `tide/Tide_predictions.py`

Manual setup, if you do not use the launcher:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app\web_app.py
```

Do not commit `.venv`, `.conda`, `Planet_download`, or the CSIRO `.nc` model
file. They are local runtime files and are ignored by git.

## Notes

- The app uses PlanetScope daily imagery only (`PSScene`).
- Candidate acquisition times are displayed in the local timezone resolved from
  the AOI centre. If timezone lookup is unavailable, the app falls back to an
  approximate UTC offset from longitude.
- Same-time Planet scenes are collapsed before review: if multiple scenes share
  the same UTC acquisition minute, only the scene with the highest AOI coverage
  is shown. Lower cloud cover is used as the tie-breaker.
- Tide prediction is sourced from `tide/Tide_predictions.py`.
- `CSIRO_tidal_const_v12.nc` is intentionally ignored by git because it is a
  large local model file.
- Kept images can be exported as CSV or GeoJSON.
- Kept images can also be ordered through a review panel that asks for an order
  name, product bundle, and optional tools before submitting to Planet.

## Planet Ordering

The order panel is modelled on Planet Explorer's basic order flow:

1. Keep the candidate scenes you want to download.
2. Click `Order kept`.
3. Enter an order name.
4. Choose an asset type: visual, surface reflectance 4-band, or surface
   reflectance 8-band.
5. Choose tools: clip to AOI, composite, and/or harmonize to Sentinel-2.
6. Review the estimate before clicking `Place order`.

The PlanetScope scene asset choices use these Orders API bundles:

- `visual`: RGB visual imagery.
- `analytic_sr_udm2`: 4-band surface reflectance with UDM2.
- `analytic_8b_sr_udm2`: 8-band surface reflectance with UDM2.

The estimate reports item count, expected output image count, AOI area,
AOI-intersection area, and processed-area estimate. The app also compares the
processed-area estimate against the standard 3,000 km²/month education-account
quota used by this project. Planet calculates the final quota when the order
runs, so the estimate is a guardrail rather than a guarantee.

## AOI Tide Prediction

Tide prediction is run directly from the CSIRO model rather than loading a
precomputed tide CSV.

For each Planet candidate image:

1. The app reads the image acquisition timestamp from Planet metadata and
   converts it to UTC.
2. The drawn or uploaded AOI is normalised to lon/lat polygon geometry.
3. The app loads `tide/Tide_predictions.py` and opens
   `tide/CSIRO_tidal_const_v12.nc` through `CsiROModel`.
4. CSIRO model mesh-face centroids inside the AOI are selected using the AOI
   polygon.
5. Tide height is reconstructed at each image acquisition time for every
   selected mesh face using the harmonic reconstruction logic in
   `Tide_predictions.py`.
6. If multiple model faces fall inside the AOI, their predicted tide heights are
   averaged to produce an AOI-average tide height for that image.
7. If no model face centroid falls inside the AOI, the app falls back to the
   nearest model face to the AOI centre.
8. Candidate images are sorted by predicted tide height, lowest first.

The app stores `tide_height`, `tide_method`, and `tide_faces` on each candidate
item so exports can include the predicted tide and the number of CSIRO model
faces used. The current web app calls the reconstruction helper in
`Tide_predictions.py` directly for AOI averaging; a future cleanup should expose
a public AOI/face-based wrapper in that tide module.
