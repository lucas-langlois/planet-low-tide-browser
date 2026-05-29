# Planet Low Tide Browser Cloud User Guide

This guide is for people using the hosted Cloud Run version of the Planet Low
Tide Browser.

Cloud app:

<https://planet-low-tide-browser-1083872359479.australia-southeast1.run.app>

If you are on Windows, you can also double-click:

`Open_Planet_Low_Tide_Browser_Cloud.bat`

That launcher only opens the website. It does not install Python or run the
local VM version.

## What You Need

Before starting, you need:

- A Planet account.
- Your own Planet API key.
- An area of interest, either drawn on the map, uploaded as a file, or entered
  as centre coordinates plus area size.

The cloud app does not use a shared Planet API key. Paste your own key each
time you use the app.

## Get Your Planet API Key

1. Sign in to Planet.
2. Open your Planet account settings.
3. Find your API key.
4. Copy the key.

Do not send your API key by email or paste it into shared documents.

## Open The App

Open:

<https://planet-low-tide-browser-1083872359479.australia-southeast1.run.app>

If the app has been idle, the first load can take a short moment because Cloud
Run starts the service only when someone uses it.

## Enter Your Planet API Key

1. Paste your Planet API key into the `API key` box.
2. Wait for the status message to confirm that the key is valid.

If validation fails, check that:

- The key was copied completely.
- There are no extra spaces before or after it.
- Your internet connection can reach Planet.
- Your Planet account has access to PlanetScope imagery.

## Choose An Area

Use one of the three area options.

### Draw

1. Click `Draw polygon`.
2. Click around the area on the map to add vertices.
3. Double-click to finish, or click `Save drawn AOI`.

### Center

1. Choose `Center`.
2. Enter latitude and longitude.
3. Enter an area size in square kilometres.
4. Click `Create square AOI`.

### Upload

1. Choose `Upload`.
2. Select a GeoJSON, KML, SHP, or zipped shapefile.
3. Click `Upload AOI`.

For shapefiles, use a `.zip` when possible so the `.shp`, `.dbf`, `.shx`, and
`.prj` files stay together.

## Search Planet Imagery

1. Choose a start date and end date.
2. Set maximum cloud cover.
3. Set minimum AOI coverage.
4. Set the maximum number of scenes to return.
5. Leave `Predict tide and sort lowest first` checked.
6. Click `Query Planet`.

The app searches PlanetScope daily imagery only. It predicts tide height for
candidate acquisition times and sorts the candidates from lowest tide to
highest tide.

## Review Candidate Images

After the search finishes:

1. Click a candidate row to inspect it on the map.
2. Use the Planet overlay controls to view the selected scene over your AOI.
3. Use the row decision buttons:
   - `?` leaves a scene pending.
   - `Keep` retains a good scene.
   - `Reject` removes a bad scene from the active review list.
4. Use `Reject rest` after you have selected the good scenes.

For large AOIs, one scene may not cover the whole selected area. As you mark
scenes as `Keep`, the app reports how much of the AOI is covered by the union
of all kept scene footprints. If the summary says coverage is incomplete, keep
reviewing and selecting more scenes until the AOI is covered.

Use `Gap only` to show only scenes that still cover part of the AOI not already
covered by kept scenes. This is useful for large AOIs where several scenes are
needed. The list updates as you keep or reject scenes.

Use `Kept only` for a second-pass review after the AOI is covered. This helps
compare retained scenes and demote weaker overlaps before ordering.

Use `Show kept images` on the map to display all kept scenes at once before
ordering. The map adds a Leaflet layer checklist so overlapping kept images can
be switched on and off individually for a final visual check. Use `All` and
`None` in that Leaflet checklist to quickly show or hide every kept image layer.

You can export retained scene lists at any time:

- `CSV`
- `GeoJSON`
- `Copy IDs`

Use CSV or GeoJSON for QGIS workflows. Use copied IDs when another Planet tool
needs a comma-separated scene list.

## Order Kept Images

1. Mark the scenes you want as `Keep`.
2. Click `Order kept`.
3. Enter an order name.
4. Choose an asset type:
   - `Visual`
   - `Surface reflectance 4-band`
   - `Surface reflectance 8-band`
5. Choose tools such as clipping to AOI, compositing, or harmonizing.
6. Review the order estimate.
7. Click `Place order`.

Planet processes the order. This may take a while.

## Download Completed Orders

1. Click `Show orders`.
2. Click `Refresh` if needed.
3. When an order is complete, use the direct Planet file links.
4. If there are multiple files, click `Download all`.

Downloads go directly from Planet to your browser. The cloud app does not store
the imagery files.

Your browser may ask whether to allow multiple downloads from the site. Allow
multiple downloads if you clicked `Download all`.

## Common Issues

### The App Is Slow To Open

Cloud Run scales to zero when idle. The first user after an idle period may
wait briefly while the service starts.

### API Key Fails

Paste the key again and check for missing characters or extra spaces. If the
problem continues, confirm that your Planet account can access the imagery you
are requesting.

### No Candidate Images Found

Try:

- Expanding the date range.
- Increasing maximum cloud cover.
- Lowering minimum AOI coverage.
- Checking that the AOI is in the correct location.

### Tide Prediction Fails

Tell the project maintainer. The cloud deployment needs the CSIRO model file in
the container for tide prediction.

### Download All Does Not Start Every File

Browsers can block multiple automatic downloads. Use the browser prompt to
allow multiple downloads, or click the individual file links one by one.

## Privacy And API Keys

The public cloud app does not have a shared Planet API key configured. Each user
enters their own key for their session.

Do not paste Planet API keys into shared notes, screenshots, GitHub issues, or
email threads.
