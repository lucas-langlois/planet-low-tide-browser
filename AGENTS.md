# AGENTS.md

## Goal

Filter PlanetScope daily imagery for a specified area based on date range and
low tide. Users must visually confirm whether each candidate image is good,
then save the retained image list for QGIS viewing or Planet API download to
support composite creation.

The app should run as a simple local Python web UI on a shared VM. Beginner
users should be able to click a launcher file and open the app without using
Streamlit.

## Workflow

1. Define an area by drawing on a map, uploading GeoJSON/KML/SHP, or entering
   center coordinates plus area size in square kilometres.
2. Choose a date range and Planet query filters, including maximum cloud cover
   and minimum selected-area coverage by each image scene.
3. Query Planet for PlanetScope daily imagery only.
4. Predict tide for candidate image acquisition times using the CSIRO model.
5. Display candidates sorted by lowest predicted tide first.
6. Let users review each image preview and mark it as keep or reject.
7. Export kept images to CSV and GeoJSON for QGIS, and support ordering or
   downloading through the Planet API.

## Tools

- Install Planet MCP: [planetlabs/planet-mcp](https://github.com/planetlabs/planet-mcp)
  - Requires Python 3.11 or higher.
  - Install with `pip install planet-mcp`.
- Follow the Planet SDK for Python documentation:
  <https://planet-sdk-for-python.readthedocs.io/en/stable/>
- Use the Planet Python library for Data API searches and Orders API downloads.
- Use `tide/Tide_predictions.py` as the source for CSIRO tide prediction logic.
- Keep `CSIRO_tidal_const_v12.nc` beside `tide/Tide_predictions.py`, but do not
  commit the NetCDF model file to git.
