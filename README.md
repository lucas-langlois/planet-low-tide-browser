# Planet Low Tide Browser

Plain Python web UI for finding PlanetScope daily imagery over an AOI, sorting
candidates by CSIRO-predicted tide height, visually reviewing scenes, and
exporting kept image IDs for QGIS or Planet API ordering.

## Quick Start

1. Put `CSIRO_tidal_const_v12.nc` in `tide/`.
2. Optional: copy `config.example.py` to `config.py` and add your Planet API key.
3. Double-click `Launch_Planet_Low_Tide_Browser.bat`.

The launcher creates a local `.conda` environment on first run, then starts the
app at <http://127.0.0.1:5050>.

## Environment

Planet MCP requires Python 3.11 or higher, so the app environment uses Python
3.11. The environment installs:

- `planet` for Planet Data and Orders API access
- `planet-mcp` for agent/tool integration
- Flask for the local web UI
- raster/xarray/UTide dependencies required by `tide/Tide_predictions.py`

Manual setup:

```powershell
conda --no-plugins env create --prefix .conda --file environment.yml
.\.conda\python.exe app\web_app.py
```

## Notes

- The app uses PlanetScope daily imagery only (`PSScene`).
- Tide prediction is sourced from `tide/Tide_predictions.py`.
- `CSIRO_tidal_const_v12.nc` is intentionally ignored by git because it is a
  large local model file.
- Kept images can be exported as CSV or GeoJSON.
