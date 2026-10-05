# PROMPT: Generate a one-click PyQGIS watershed delineation + map script

Copy everything below the line and paste it into any AI model.

---

You are an expert in PyQGIS, GRASS GIS and hydrology. Write **one complete, ready-to-run Python script** that runs inside the **QGIS Python Console editor** (QGIS 3.x, including 3.34+ where GRASS algorithm ids start with `grass:` and older versions where they start with `grass7:`).

The script must work for **any location in the world**. Follow every requirement below exactly. Do not skip steps, do not leave TODOs, do not invent QGIS functions. Where I give an exact algorithm id or parameter name, use it exactly.

## 1. Goal

With one click, the script must:
1. Download a DEM for the area the user is looking at in QGIS.
2. Delineate the watershed(s) upstream of the user's outlet (pour) point(s).
3. Compute Strahler stream order.
4. Produce a finished map: watershed boundary, streams clipped to the watershed with line width by stream order, DEM clipped to the watershed with a terrain colour palette and a hillshade effect.
5. Hide every other layer in the project and zoom to the watershed.

## 2. What the user already has
- QGIS with the GRASS processing provider enabled (standard QGIS installs include it).
- A free OpenTopography API key (from portal.opentopography.org).
- A **point layer** in the project containing one or more outlet points (any CRS).
- The map canvas zoomed roughly to the area of interest.

## 3. Configuration block (top of the script)

Put ALL user settings at the top, in a clearly marked block, with a comment on each line. Use exactly these names and defaults:

```python
# ============================ USER INPUTS ============================
OPENTOPO_API_KEY  = "PASTE_YOUR_OPENTOPOGRAPHY_API_KEY_HERE"
OUTLET_LAYER_NAME = "outlets"      # name of the point layer with pour points
OUTPUT_FOLDER     = os.path.join(os.path.expanduser("~"), "watershed_output")

# ---- stream display ----
MIN_STREAM_ORDER = 1       # lowest Strahler order to show
MAX_STREAM_ORDER = None    # highest order to show; None = all
MIN_WIDTH_MM     = 0.12    # line width of the lowest shown order
WIDTH_STEP_MM    = 0.15    # extra width per higher order

# ---- optional ----
EXISTING_DEM         = ""        # path to a DEM already on disk -> skip download
DEM_TYPE             = "SRTMGL1" # SRTMGL1, SRTMGL3, NASADEM, COP30, COP90, AW3D30
EXTENT_BUFFER_PCT    = 30        # enlarge download box by this % on each side
SNAP_DISTANCE_M      = 150       # radius to snap outlets to the highest-flow cell
STREAM_THRESHOLD_KM2 = 1.0       # min upstream area for a cell to be a stream
WATERSHED_ID         = None      # None = map all watersheds; or one outlet_id
HILLSHADE_Z          = 1.5       # hillshade vertical exaggeration
GROUP_NAME           = "Watershed Map"
# =====================================================================
```

Resolution per DEM type (metres): SRTMGL1 30, NASADEM 30, COP30 30, AW3D30 30, SRTMGL3 90, COP90 90. If `EXISTING_DEM` is used, read its pixel size; if it is in degrees, convert with `size_deg * 111320` and round to the nearest metre.
Note in a comment: SRTM/NASADEM only cover 60°N–56°S; for areas outside that use COP30.

## 4. Imports

Use only what ships with QGIS: `os`, `time`, `urllib.request`, `urllib.parse`, `collections.deque`, `numpy`, `osgeo.gdal`, `processing`, `qgis.PyQt.QtCore.QVariant`, `qgis.PyQt.QtGui.QColor, QPainter`, needed classes from `qgis.core`, and `from qgis.utils import iface`. No pip installs.

## 5. Run folder (IMPORTANT – prevents mixing old files)

Every run writes to a NEW folder:
```python
RUN_FOLDER = os.path.join(OUTPUT_FOLDER, time.strftime("run_%Y%m%d_%H%M%S"))
WORK = os.path.join(RUN_FOLDER, "work")
```
All outputs go into `RUN_FOLDER`; temporary rasters into `WORK`. Never reuse fixed filenames across runs.

## 6. Step-by-step requirements

### Step 0 – Clean start
Before any processing, if a layer-tree group named `GROUP_NAME` exists, remove all its layers from the project and remove the group (this releases file locks from earlier runs).

### Step 1 – Outlet layer
- Find the layer with `QgsProject.instance().mapLayersByName(OUTLET_LAYER_NAME)`. Raise a clear `RuntimeError` if missing or empty.
- Print the number of points and the layer CRS.

### Step 2 – Download extent
- Take `iface.mapCanvas().extent()` in the canvas CRS (`iface.mapCanvas().mapSettings().destinationCrs()`), transform to EPSG:4326 with `QgsCoordinateTransform(...).transformBoundingBox()`.
- Combine it with the outlet layer extent (also transformed to EPSG:4326) using `combineExtentWith`.
- Enlarge by `EXTENT_BUFFER_PCT` of width/height on each side. Clamp to lon −180..180, lat −90..90.

### Step 3 – Download DEM (skip if `EXISTING_DEM`)
- If the API key still contains "PASTE_YOUR", raise an error asking the user to set it.
- URL: `https://portal.opentopography.org/API/globaldem?` + `urllib.parse.urlencode({demtype, south, north, west, east, outputFormat: "GTiff", API_Key})`.
- Use `urllib.request.urlopen(url, timeout=600)`.
- Check the first 4 bytes are a TIFF header (`b"II*\x00"` or `b"MM\x00*"`). If not, raise an error containing the first 500 characters of the server response (it contains the real reason, e.g. bad key or area too large).
- Save to `RUN_FOLDER/dem_wgs84.tif`.

### Step 4 – Choose the projected CRS (CRITICAL)
- Choose the UTM zone from the **centre of the outlet points** (transformed to EPSG:4326), **NOT** from the centre of the download box or the canvas. Reason: if an area sits near a zone boundary, the box centre can fall in a different zone between runs, and a watershed then appears shifted ~6° (hundreds of km) away.
- `zone = int((lon + 180) / 6) + 1`; EPSG = `32600 + zone` if lat ≥ 0 else `32700 + zone`.
- If |lat| > 80, use EPSG:3413 (north) or EPSG:3031 (south) instead.
- Print the chosen CRS. Keep this CRS object and pass it through the whole script; later, call `.setCrs(crs)` on every output layer you load.

### Step 5 – Reproject DEM
`processing.run("gdal:warpreproject", {"INPUT": dem_raw, "SOURCE_CRS": None, "TARGET_CRS": crs, "RESAMPLING": 1, "NODATA": -32768, "TARGET_RESOLUTION": res, "DATA_TYPE": 6, "OUTPUT": RUN_FOLDER/dem_proj.tif})`

### Step 6 – Hydrology with GRASS
Write a helper that returns the right algorithm id:
```python
def grass_alg(name):
    reg = QgsApplication.processingRegistry()
    for prefix in ("grass", "grass7"):
        if reg.algorithmById(f"{prefix}:{name}"):
            return f"{prefix}:{name}"
    raise RuntimeError("GRASS provider not available - enable it in Settings > Options > Processing > Providers")
```
Run `r.watershed`:
```python
processing.run(grass_alg("r.watershed"), {
    "elevation": dem_proj,
    "threshold": max(1, int(STREAM_THRESHOLD_KM2 * 1e6 / (res * res))),
    "-s": True,                        # single flow direction
    "accumulation": WORK/flow_acc.tif,
    "drainage": WORK/flow_dir.tif,
    "stream": RUN_FOLDER/streams.tif,
    "GRASS_REGION_CELLSIZE_PARAMETER": 0})
```
r.watershed handles depressions itself — do NOT add a separate fill-sinks step.

### Step 7 – Snap outlets
Read `flow_acc.tif` with GDAL into a float64 numpy array, set nodata to 0. For each outlet point (transformed to the projected CRS):
- convert x,y to row/col using the geotransform; skip with a message if outside the raster;
- search a square window of radius `round(SNAP_DISTANCE_M / cell_size)` cells for the cell with the largest `abs(accumulation)` (values can be negative near the DEM edge, so use abs);
- the snapped point is that cell's centre: `x = gt[0] + (col + 0.5) * gt[1]`, `y = gt[3] + (row + 0.5) * gt[5]`.
- Print the snap distance in metres.

### Step 8 – Delineate each watershed
For each snapped outlet:
```python
processing.run(grass_alg("r.water.outlet"), {
    "input": WORK/flow_dir.tif,
    "coordinates": f"{sx},{sy} [{crs.authid()}]",
    "output": WORK/basin_<id>.tif,
    "GRASS_REGION_CELLSIZE_PARAMETER": 0})
processing.run("gdal:polygonize", {"INPUT": basin_tif, "BAND": 1, "FIELD": "DN",
    "EIGHT_CONNECTEDNESS": False, "OUTPUT": WORK/basin_<id>.gpkg})
```
- Keep only features whose `DN` is not 0 or None; merge them with `QgsGeometry.unaryUnion`.
- **Sanity check:** if the polygon's distance to the snapped point is more than 2 cells, discard it and print a message.
- **Edge check:** if the polygon's bounding box is within 2 cells of any DEM edge, set field `edge_warn = "TOUCHES DEM EDGE - enlarge extent"` and print a warning (the upstream area is probably cut off).
- Store in a memory polygon layer with fields: `outlet_id` (Int, the feature id), `area_km2` (Double, area/1e6 rounded to 3), `up_cells` (Double), `edge_warn` (String). Also store snapped points in a memory point layer with the same fields.
- If no watershed was produced, raise an error.
- Save both layers to GeoPackage with `QgsVectorFileWriter.writeAsVectorFormatV3` (driver "GPKG"). **Check the returned error code**; if it is not `QgsVectorFileWriter.NoError`, raise an error with the message.

### Step 9 – Select and clip
- Extract the watershed(s) with `native:extractbyexpression` (expression `TRUE`, or `"outlet_id" = WATERSHED_ID`) to `RUN_FOLDER/watershed_selected.gpkg`.
- Clip the projected DEM: `gdal:cliprasterbymasklayer` with `MASK` = that file, `NODATA` −32768, `CROP_TO_CUTLINE` True, `KEEP_RESOLUTION` True → `RUN_FOLDER/dem_clipped.tif`.

### Step 10 – Strahler stream order (implement in numpy, no add-ons)
Do NOT use `r.stream.order` (it is a GRASS add-on most users don't have). Implement this:
1. Read `streams.tif` and `flow_dir.tif` with GDAL. Stream cells = value ≠ 0, finite, and ≠ nodata.
2. GRASS drainage codes: direction = code × 45° counter-clockwise from East. Use `abs(code)` (negative means flow leaves the region). Offsets (row, col):
   `{1: (-1, 1), 2: (-1, 0), 3: (-1, -1), 4: (0, -1), 5: (1, -1), 6: (1, 0), 7: (1, 1), 8: (0, 1)}`
3. Give each stream cell an index; for each, find the downstream **stream** cell index (or −1).
4. Count inflows per cell (`np.bincount`). Process cells in topological order with a `deque` starting from cells with 0 inflows (Kahn's algorithm):
   - no inflows → order 1;
   - otherwise order = highest incoming order, **+1 only if two or more inflows share that highest order**.
   - Pass the order to the downstream cell by tracking `max_in` and `count_of_max`.
5. Print the maximum order found.
6. Build a memory LineString layer (field `order`, Int): for each cell that has a downstream cell and whose order is within `MIN_STREAM_ORDER..MAX_STREAM_ORDER`, add a 2-point line from its cell centre to the downstream cell centre, with that cell's order. Add features in one batch (`addFeatures`).
7. `native:clip` it to the selected watershed, then `native:dissolve` with `FIELD ["order"]` → `RUN_FOLDER/streams_order.gpkg`.

### Step 11 – Styling
- **Watershed boundary:** `QgsFillSymbol.createSimple({"style": "no", "outline_color": "20,20,20,255", "outline_width": "0.7", "joinstyle": "round"})`.
- **Streams:** `QgsCategorizedSymbolRenderer` on `order`, one category per order present, label `"Order N"`. Colour interpolated from `#6baed6` (lowest) to `#08306b` (highest). Width = `MIN_WIDTH_MM + WIDTH_STEP_MM * (order - lowest_shown_order)`. Round caps and joins.
- **DEM colours:** read the clipped DEM with GDAL, nodata → NaN, take the 1st and 99th percentiles as min/max (if equal, max = min + 1). `QgsColorRampShader`, type Interpolated, stops:
  `0.00 #1a7837, 0.20 #7fbf7b, 0.40 #f6e8a6, 0.60 #e0a960, 0.80 #a0643c, 1.00 #f5f5f5`, labels like `"245 m"`. Use `QgsSingleBandPseudoColorRenderer` and set classification min/max.
- **Hillshade:** a second `QgsRasterLayer` of the same clipped DEM named "Hillshade" with `QgsHillshadeRenderer(provider, 1, 315, 45)`, `setZFactor(HILLSHADE_Z)`, `setMultiDirectional(True)`, `setOpacity(0.55)`, and layer blend mode `QPainter.CompositionMode_Multiply`.

### Step 12 – Show only the new map
- Uncheck visibility of every existing layer (`root.findLayers()` → `setItemVisibilityChecked(False)`).
- Insert a new group `GROUP_NAME` at position 0. Add layers with `QgsProject.instance().addMapLayer(layer, False)` then `group.addLayer(layer)` in this order (top to bottom): Watershed boundary, Streams (Strahler order), Hillshade, Elevation (m). Make the group and its layers visible.
- Zoom the canvas to the watershed extent (transformed to the canvas CRS), scaled by 1.08, then `refresh()`.

## 7. Code quality rules
- Organise into small functions; a `main()` at the bottom, called directly with `main()` (NOT inside `if __name__ == "__main__":` — the QGIS console does not set that reliably).
- Print progress with a `log()` helper using a prefix like `[watershed]`.
- Every failure must raise `RuntimeError` with a message telling the user exactly what to fix.
- No placeholder values except the API key. No pseudo-code.
- Use `QVariant` types for fields (works in all QGIS 3 versions).
- A docstring at the top explaining how to run: *Plugins > Python Console > Show Editor > Open script > Run*.

## 8. Final self-check before answering
Confirm in your head that:
- [ ] The UTM/projected CRS comes from the outlet points, not the download box.
- [ ] Every run writes to a new timestamped folder.
- [ ] Both `grass:` and `grass7:` prefixes are handled.
- [ ] Snapping uses `abs()` of accumulation.
- [ ] Watershed polygons that are not at their outlet are discarded.
- [ ] Stream order is Strahler (not Shreve), computed in numpy.
- [ ] Only the new group is visible at the end.
- [ ] The script compiles and every parameter name matches this prompt.

Then output the full script in a single code block, followed by a short list of what the user must set (API key, outlet layer name) and 3 tips: enlarge the extent if `edge_warn` appears; reduce `SNAP_DISTANCE_M` if an outlet jumps to the wrong tributary; raise `STREAM_THRESHOLD_KM2` or `MIN_STREAM_ORDER` if there are too many small streams.