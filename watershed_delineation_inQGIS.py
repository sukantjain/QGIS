"""
Watershed delineation + map styling - all in one (run inside QGIS)
===================================================================
Plugins > Python Console > Show Editor > open this file > Run.

Steps:
  1. Download SRTM DEM for current map view + outlet points (OpenTopography)
  2. Reproject DEM to UTM
  3. GRASS r.watershed -> flow direction, accumulation, streams
  4. Snap outlets to streams, GRASS r.water.outlet -> watershed polygons
  5. Clip DEM to watershed, compute Strahler stream order, clip streams
  6. Style: boundary outline, streams (width by order), DEM colours + hillshade
  7. Hide every other layer and zoom to the watershed
"""

import os
import time
import urllib.request
import urllib.parse
from collections import deque

import numpy as np
from osgeo import gdal
import processing
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtGui import QColor, QPainter
from qgis.core import (
    QgsApplication, QgsProject, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsRectangle, QgsVectorLayer, QgsRasterLayer, QgsFeature, QgsField, QgsGeometry,
    QgsPointXY, QgsVectorFileWriter, QgsFillSymbol, QgsLineSymbol, QgsRendererCategory,
    QgsCategorizedSymbolRenderer, QgsColorRampShader, QgsRasterShader,
    QgsSingleBandPseudoColorRenderer, QgsHillshadeRenderer,
)
from qgis.utils import iface

# ============================ USER INPUTS ============================
OPENTOPO_API_KEY  = "PASTE_YOUR_OPENTOPOGRAPHY_API_KEY_HERE"
OUTLET_LAYER_NAME = "outlets"   # point layer with your pour points
OUTPUT_FOLDER     = os.path.join(os.path.expanduser("~"), "watershed_output")

# ---- stream display ----
MIN_STREAM_ORDER = 1       # lowest order to show (raise to hide small tributaries)
MAX_STREAM_ORDER = None    # highest order to show; None = up to the main river
MIN_WIDTH_MM     = 0.12    # line width of the lowest shown order
WIDTH_STEP_MM    = 0.15    # extra width per higher order

# ---- optional (defaults are fine for most cases) ----
EXISTING_DEM         = ""        # path to a DEM you already have -> skips download
DEM_TYPE             = "SRTMGL1" # SRTMGL1 = 30 m, SRTMGL3 = 90 m
EXTENT_BUFFER_PCT    = 30        # enlarge download area by this % on each side
SNAP_DISTANCE_M      = 150       # search radius to snap outlets onto the stream
STREAM_THRESHOLD_KM2 = 1.0       # min upstream area for a cell to be a stream
WATERSHED_ID         = None      # None = map all watersheds; or one outlet_id
HILLSHADE_Z          = 1.5       # vertical exaggeration of hillshade
GROUP_NAME           = "Watershed Map"
# =====================================================================

# every run gets its own folder, so old/locked files from earlier runs are never mixed in
RUN_FOLDER = os.path.join(OUTPUT_FOLDER, time.strftime("run_%Y%m%d_%H%M%S"))
WORK = os.path.join(RUN_FOLDER, "work")

# GRASS r.watershed drainage codes (code * 45 deg CCW from East) -> (row, col) offset
OFFSETS = {1: (-1, 1), 2: (-1, 0), 3: (-1, -1), 4: (0, -1),
           5: (1, -1), 6: (1, 0), 7: (1, 1), 8: (0, 1)}


def log(msg):
    print(f"[watershed] {msg}")


# =====================================================================
#  PART 1 - DEM download and watershed delineation
# =====================================================================

def grass_alg(name):
    reg = QgsApplication.processingRegistry()
    for prefix in ("grass", "grass7"):
        if reg.algorithmById(f"{prefix}:{name}"):
            return f"{prefix}:{name}"
    raise RuntimeError(
        f"GRASS algorithm {name} not found. Enable the GRASS provider under "
        "Settings > Options > Processing > Providers (or install QGIS with GRASS).")


def get_outlet_layer():
    layers = QgsProject.instance().mapLayersByName(OUTLET_LAYER_NAME)
    if not layers:
        raise RuntimeError(f"No layer named '{OUTLET_LAYER_NAME}' in the project.")
    lyr = layers[0]
    if lyr.featureCount() == 0:
        raise RuntimeError(f"Layer '{OUTLET_LAYER_NAME}' has no points.")
    return lyr


def download_extent_wgs84(outlet_layer):
    wgs = QgsCoordinateReferenceSystem("EPSG:4326")
    proj = QgsProject.instance()
    canvas = iface.mapCanvas()
    bb = QgsCoordinateTransform(canvas.mapSettings().destinationCrs(), wgs,
                                proj).transformBoundingBox(canvas.extent())
    bb.combineExtentWith(QgsCoordinateTransform(outlet_layer.crs(), wgs, proj)
                         .transformBoundingBox(outlet_layer.extent()))
    dx = bb.width() * EXTENT_BUFFER_PCT / 100.0
    dy = bb.height() * EXTENT_BUFFER_PCT / 100.0
    return QgsRectangle(max(-180, bb.xMinimum() - dx), max(-60, bb.yMinimum() - dy),
                        min(180, bb.xMaximum() + dx), min(60, bb.yMaximum() + dy))


def download_dem(bb, out_path):
    params = {
        "demtype": DEM_TYPE,
        "south": bb.yMinimum(), "north": bb.yMaximum(),
        "west": bb.xMinimum(), "east": bb.xMaximum(),
        "outputFormat": "GTiff", "API_Key": OPENTOPO_API_KEY,
    }
    url = "https://portal.opentopography.org/API/globaldem?" + urllib.parse.urlencode(params)
    log(f"Downloading {DEM_TYPE} ...")
    with urllib.request.urlopen(url, timeout=600) as resp:
        data = resp.read()
    if data[:4] not in (b"II*\x00", b"MM\x00*"):
        raise RuntimeError("DEM download failed. Server said:\n" + data[:500].decode(errors="ignore"))
    with open(out_path, "wb") as f:
        f.write(data)
    log(f"DEM saved ({len(data) / 1e6:.1f} MB)")


def utm_crs_for_outlets(outlet_layer):
    """UTM zone chosen from the outlet points themselves, so it never changes with
    the map view or buffer (Roorkee sits right at the 78 deg E zone boundary)."""
    wgs = QgsCoordinateReferenceSystem("EPSG:4326")
    ext = QgsCoordinateTransform(outlet_layer.crs(), wgs, QgsProject.instance()) \
        .transformBoundingBox(outlet_layer.extent())
    lon, lat = ext.center().x(), ext.center().y()
    zone = int((lon + 180) / 6) + 1
    crs = QgsCoordinateReferenceSystem(f"EPSG:{(32600 if lat >= 0 else 32700) + zone}")
    log(f"Outlets centre: {lon:.4f} E, {lat:.4f} N -> {crs.authid()}")
    return crs


def snap_to_max_accumulation(x, y, acc_arr, gt):
    col = int((x - gt[0]) / gt[1])
    row = int((y - gt[3]) / gt[5])
    nrows, ncols = acc_arr.shape
    if not (0 <= row < nrows and 0 <= col < ncols):
        return None
    r = max(1, int(round(SNAP_DISTANCE_M / gt[1])))
    r0, r1 = max(0, row - r), min(nrows, row + r + 1)
    c0, c1 = max(0, col - r), min(ncols, col + r + 1)
    win = np.abs(np.nan_to_num(acc_arr[r0:r1, c0:c1]))
    wr, wc = np.unravel_index(np.argmax(win), win.shape)
    rr, cc = r0 + wr, c0 + wc
    return (gt[0] + (cc + 0.5) * gt[1], gt[3] + (rr + 0.5) * gt[5], float(win[wr, wc]))


def save_layer(layer, path):
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    opts.layerName = layer.name().replace(" ", "_")
    res = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, path, QgsProject.instance().transformContext(), opts)
    if res[0] != QgsVectorFileWriter.NoError:
        raise RuntimeError(f"Could not write {path}: {res[1]}")


def delineate():
    """Returns dict of output paths."""
    proj = QgsProject.instance()
    outlets = get_outlet_layer()
    log(f"Outlet layer '{OUTLET_LAYER_NAME}': {outlets.featureCount()} point(s), "
        f"CRS {outlets.crs().authid()}")
    bb = download_extent_wgs84(outlets)

    # ---- DEM ----
    if EXISTING_DEM:
        dem_raw = EXISTING_DEM
        log(f"Using existing DEM: {dem_raw}")
    else:
        if "PASTE_YOUR" in OPENTOPO_API_KEY:
            raise RuntimeError("Set OPENTOPO_API_KEY (or EXISTING_DEM) at the top of the script.")
        dem_raw = os.path.join(RUN_FOLDER, "dem_srtm_wgs84.tif")
        download_dem(bb, dem_raw)

    utm = utm_crs_for_outlets(outlets)
    res = 90 if DEM_TYPE == "SRTMGL3" and not EXISTING_DEM else 30
    dem_utm = os.path.join(RUN_FOLDER, "dem_utm.tif")
    log(f"Reprojecting DEM to {utm.authid()} at {res} m ...")
    processing.run("gdal:warpreproject", {
        "INPUT": dem_raw, "SOURCE_CRS": None, "TARGET_CRS": utm,
        "RESAMPLING": 1, "NODATA": -32768, "TARGET_RESOLUTION": res,
        "DATA_TYPE": 6, "OUTPUT": dem_utm})

    # ---- hydrology ----
    acc_path = os.path.join(WORK, "flow_acc.tif")
    dir_path = os.path.join(WORK, "flow_dir.tif")
    str_path = os.path.join(RUN_FOLDER, "streams.tif")
    thr_cells = max(1, int(STREAM_THRESHOLD_KM2 * 1e6 / (res * res)))
    log(f"Running r.watershed (stream threshold = {thr_cells} cells) ...")
    processing.run(grass_alg("r.watershed"), {
        "elevation": dem_utm, "threshold": thr_cells, "-s": True,
        "accumulation": acc_path, "drainage": dir_path, "stream": str_path,
        "GRASS_REGION_CELLSIZE_PARAMETER": 0})

    ds = gdal.Open(acc_path)
    gt = ds.GetGeoTransform()
    acc_arr = ds.GetRasterBand(1).ReadAsArray().astype("float64")
    nd = ds.GetRasterBand(1).GetNoDataValue()
    if nd is not None:
        acc_arr[acc_arr == nd] = 0
    dem_xmin, dem_ymax = gt[0], gt[3]
    dem_xmax = gt[0] + gt[1] * ds.RasterXSize
    dem_ymin = gt[3] + gt[5] * ds.RasterYSize
    cell = gt[1]
    ds = None

    ws_layer = QgsVectorLayer(f"Polygon?crs={utm.authid()}", "Watersheds", "memory")
    pt_layer = QgsVectorLayer(f"Point?crs={utm.authid()}", "Snapped outlets", "memory")
    fields = [QgsField("outlet_id", QVariant.Int), QgsField("area_km2", QVariant.Double),
              QgsField("up_cells", QVariant.Double), QgsField("edge_warn", QVariant.String)]
    for l in (ws_layer, pt_layer):
        l.dataProvider().addAttributes(fields)
        l.updateFields()

    tr = QgsCoordinateTransform(outlets.crs(), utm, proj)
    alg_outlet = grass_alg("r.water.outlet")

    for f in outlets.getFeatures():
        oid = f.id()
        g = QgsGeometry(f.geometry())
        g.transform(tr)
        p = g.asPoint() if not g.isMultipart() else g.asMultiPoint()[0]
        snapped = snap_to_max_accumulation(p.x(), p.y(), acc_arr, gt)
        if snapped is None:
            log(f"Outlet {oid}: outside DEM, skipped.")
            continue
        sx, sy, upc = snapped
        log(f"Outlet {oid}: snapped {np.hypot(sx - p.x(), sy - p.y()):.0f} m")

        basin_r = os.path.join(WORK, f"basin_{oid}.tif")
        basin_v = os.path.join(WORK, f"basin_{oid}.gpkg")
        processing.run(alg_outlet, {
            "input": dir_path, "coordinates": f"{sx},{sy} [{utm.authid()}]",
            "output": basin_r, "GRASS_REGION_CELLSIZE_PARAMETER": 0})
        processing.run("gdal:polygonize", {
            "INPUT": basin_r, "BAND": 1, "FIELD": "DN",
            "EIGHT_CONNECTEDNESS": False, "OUTPUT": basin_v})
        vl = QgsVectorLayer(basin_v, "tmp", "ogr")
        geoms = [ft.geometry() for ft in vl.getFeatures() if ft["DN"] not in (0, None)]
        if not geoms:
            log(f"Outlet {oid}: empty basin, skipped.")
            continue
        geom = QgsGeometry.unaryUnion(geoms)
        if geom.distance(QgsGeometry.fromPointXY(QgsPointXY(sx, sy))) > 2 * cell:
            log(f"Outlet {oid}: resulting polygon is not at the outlet - discarded.")
            continue

        b, m = geom.boundingBox(), 2 * cell
        touches = (b.xMinimum() <= dem_xmin + m or b.xMaximum() >= dem_xmax - m or
                   b.yMinimum() <= dem_ymin + m or b.yMaximum() >= dem_ymax - m)
        warn = "TOUCHES DEM EDGE - enlarge extent" if touches else ""
        if touches:
            log(f"  WARNING: watershed {oid} reaches the DEM edge; zoom out or raise "
                "EXTENT_BUFFER_PCT and re-run.")

        attrs = [oid, round(geom.area() / 1e6, 3), upc, warn]
        wf = QgsFeature(ws_layer.fields()); wf.setGeometry(geom); wf.setAttributes(attrs)
        ws_layer.dataProvider().addFeature(wf)
        pf = QgsFeature(pt_layer.fields())
        pf.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(sx, sy))); pf.setAttributes(attrs)
        pt_layer.dataProvider().addFeature(pf)
        log(f"  area = {attrs[1]} km2")

    if ws_layer.featureCount() == 0:
        raise RuntimeError("No watershed could be delineated.")

    ws_path = os.path.join(RUN_FOLDER, "watersheds.gpkg")
    save_layer(ws_layer, ws_path)
    save_layer(pt_layer, os.path.join(RUN_FOLDER, "snapped_outlets.gpkg"))
    return {"dem": dem_utm, "streams": str_path, "fdir": dir_path, "watersheds": ws_path,
            "crs": utm}


# =====================================================================
#  PART 2 - clipping, stream order, styling
# =====================================================================

def extract_watershed(ws_path):
    out = os.path.join(RUN_FOLDER, "watershed_selected.gpkg")
    expr = "TRUE" if WATERSHED_ID is None else f'"outlet_id" = {int(WATERSHED_ID)}'
    processing.run("native:extractbyexpression", {
        "INPUT": ws_path, "EXPRESSION": expr, "OUTPUT": out})
    lyr = QgsVectorLayer(out, "Watershed boundary", "ogr")
    if lyr.featureCount() == 0:
        raise RuntimeError(f"No watershed matches outlet_id = {WATERSHED_ID}")
    return out, lyr


def clip_dem(dem_path, mask_path):
    out = os.path.join(RUN_FOLDER, "dem_clipped.tif")
    processing.run("gdal:cliprasterbymasklayer", {
        "INPUT": dem_path, "MASK": mask_path, "NODATA": -32768,
        "CROP_TO_CUTLINE": True, "KEEP_RESOLUTION": True, "OUTPUT": out})
    return out


def strahler_stream_lines(str_path, fdir_path, crs_authid):
    s_ds, f_ds = gdal.Open(str_path), gdal.Open(fdir_path)
    gt = s_ds.GetGeoTransform()
    sb = s_ds.GetRasterBand(1)
    streams = sb.ReadAsArray()
    nd = sb.GetNoDataValue()
    fdir = f_ds.GetRasterBand(1).ReadAsArray()
    s_ds = f_ds = None

    mask = (streams != 0) & np.isfinite(streams)
    if nd is not None:
        mask &= streams != nd
    rows, cols = np.nonzero(mask)
    n = len(rows)
    log(f"{n} stream cells, computing Strahler order ...")

    idx = -np.ones(streams.shape, dtype=np.int64)
    idx[rows, cols] = np.arange(n)
    nr, nc = streams.shape
    down = -np.ones(n, dtype=np.int64)
    for i in range(n):
        d = abs(int(fdir[rows[i], cols[i]]))
        if d in OFFSETS:
            dr, dc = OFFSETS[d]
            rr, cc = rows[i] + dr, cols[i] + dc
            if 0 <= rr < nr and 0 <= cc < nc and idx[rr, cc] >= 0:
                down[i] = idx[rr, cc]

    valid = down >= 0
    n_in = np.bincount(down[valid], minlength=n)
    remaining = n_in.copy()
    order = np.zeros(n, dtype=np.int32)
    max_in = np.zeros(n, dtype=np.int32)
    cnt_max = np.zeros(n, dtype=np.int32)
    q = deque(np.nonzero(remaining == 0)[0].tolist())
    while q:
        i = q.popleft()
        order[i] = 1 if n_in[i] == 0 else max_in[i] + (1 if cnt_max[i] > 1 else 0)
        j = down[i]
        if j >= 0:
            if order[i] > max_in[j]:
                max_in[j], cnt_max[j] = order[i], 1
            elif order[i] == max_in[j]:
                cnt_max[j] += 1
            remaining[j] -= 1
            if remaining[j] == 0:
                q.append(j)
    log(f"Max stream order in network = {order.max() if n else 0}")

    lo = MIN_STREAM_ORDER
    hi = MAX_STREAM_ORDER if MAX_STREAM_ORDER is not None else 10 ** 6
    lyr = QgsVectorLayer(f"LineString?crs={crs_authid}", "streams_tmp", "memory")
    lyr.dataProvider().addAttributes([QgsField("order", QVariant.Int)])
    lyr.updateFields()
    cx = lambda c: gt[0] + (c + 0.5) * gt[1]
    cy = lambda r: gt[3] + (r + 0.5) * gt[5]
    feats = []
    for i in np.nonzero(valid)[0]:
        if not (lo <= order[i] <= hi):
            continue
        j = down[i]
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPolylineXY([
            QgsPointXY(cx(cols[i]), cy(rows[i])), QgsPointXY(cx(cols[j]), cy(rows[j]))]))
        f.setAttributes([int(order[i])])
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    return lyr


def build_stream_layer(str_path, fdir_path, mask_path, crs_authid):
    tmp = strahler_stream_lines(str_path, fdir_path, crs_authid)
    if tmp.featureCount() == 0:
        log("No streams in the chosen order range.")
    clipped = processing.run("native:clip", {
        "INPUT": tmp, "OVERLAY": mask_path, "OUTPUT": "TEMPORARY_OUTPUT"})["OUTPUT"]
    out = os.path.join(RUN_FOLDER, "streams_order.gpkg")
    processing.run("native:dissolve", {
        "INPUT": clipped, "FIELD": ["order"], "OUTPUT": out})
    return QgsVectorLayer(out, "Streams (Strahler order)", "ogr")


def style_boundary(lyr):
    lyr.renderer().setSymbol(QgsFillSymbol.createSimple({
        "style": "no", "outline_color": "20,20,20,255",
        "outline_width": "0.7", "joinstyle": "round"}))


def style_streams(lyr):
    orders = sorted({f["order"] for f in lyr.getFeatures()})
    if not orders:
        return
    light, dark = QColor("#6baed6"), QColor("#08306b")
    cats = []
    for k, o in enumerate(orders):
        t = k / max(1, len(orders) - 1)
        col = QColor(int(light.red() + t * (dark.red() - light.red())),
                     int(light.green() + t * (dark.green() - light.green())),
                     int(light.blue() + t * (dark.blue() - light.blue())))
        width = MIN_WIDTH_MM + WIDTH_STEP_MM * (o - orders[0])
        sym = QgsLineSymbol.createSimple({
            "line_color": col.name(), "line_width": f"{width:.3f}",
            "capstyle": "round", "joinstyle": "round"})
        cats.append(QgsRendererCategory(o, sym, f"Order {o}"))
    lyr.setRenderer(QgsCategorizedSymbolRenderer("order", cats))


def style_dem(lyr, path):
    ds = gdal.Open(path)
    b = ds.GetRasterBand(1)
    arr = b.ReadAsArray().astype("float64")
    nd = b.GetNoDataValue()
    if nd is not None:
        arr[arr == nd] = np.nan
    ds = None
    zmin, zmax = float(np.nanpercentile(arr, 1)), float(np.nanpercentile(arr, 99))
    if zmax <= zmin:
        zmax = zmin + 1

    stops = [(0.00, "#1a7837"), (0.20, "#7fbf7b"), (0.40, "#f6e8a6"),
             (0.60, "#e0a960"), (0.80, "#a0643c"), (1.00, "#f5f5f5")]
    items = [QgsColorRampShader.ColorRampItem(zmin + t * (zmax - zmin), QColor(c),
                                              f"{zmin + t * (zmax - zmin):.0f} m")
             for t, c in stops]
    ramp = QgsColorRampShader(zmin, zmax)
    ramp.setColorRampType(QgsColorRampShader.Interpolated)
    ramp.setColorRampItemList(items)
    shader = QgsRasterShader()
    shader.setRasterShaderFunction(ramp)
    r = QgsSingleBandPseudoColorRenderer(lyr.dataProvider(), 1, shader)
    r.setClassificationMin(zmin)
    r.setClassificationMax(zmax)
    lyr.setRenderer(r)


def style_hillshade(lyr):
    r = QgsHillshadeRenderer(lyr.dataProvider(), 1, 315, 45)
    r.setZFactor(HILLSHADE_Z)
    r.setMultiDirectional(True)
    r.setOpacity(0.55)
    lyr.setRenderer(r)
    lyr.setBlendMode(QPainter.CompositionMode_Multiply)


def remove_old_map():
    proj = QgsProject.instance()
    root = proj.layerTreeRoot()
    old = root.findGroup(GROUP_NAME)
    if old:
        for node in old.findLayers():
            proj.removeMapLayer(node.layerId())
        root.removeChildNode(old)


def show_only(layers_top_to_bottom):
    proj = QgsProject.instance()
    root = proj.layerTreeRoot()
    for node in root.findLayers():
        node.setItemVisibilityChecked(False)
    group = root.insertGroup(0, GROUP_NAME)
    for lyr in layers_top_to_bottom:
        proj.addMapLayer(lyr, False)
        group.addLayer(lyr)
    group.setItemVisibilityChecked(True)
    for node in group.findLayers():
        node.setItemVisibilityChecked(True)


def zoom_to(lyr):
    canvas = iface.mapCanvas()
    ext = QgsCoordinateTransform(lyr.crs(), canvas.mapSettings().destinationCrs(),
                                 QgsProject.instance()).transformBoundingBox(lyr.extent())
    ext.scale(1.08)
    canvas.setExtent(ext)
    canvas.refresh()


def make_map(paths):
    ws_path, ws_lyr = extract_watershed(paths["watersheds"])
    crs = paths["crs"]
    crs_authid = crs.authid()
    ws_lyr.setCrs(crs)
    log("Clipping DEM ...")
    dem_clip = clip_dem(paths["dem"], ws_path)
    streams = build_stream_layer(paths["streams"], paths["fdir"], ws_path, crs_authid)

    dem_lyr = QgsRasterLayer(dem_clip, "Elevation (m)")
    hs_lyr = QgsRasterLayer(dem_clip, "Hillshade")
    for l in (streams, dem_lyr, hs_lyr):
        l.setCrs(crs)
    style_boundary(ws_lyr)
    style_streams(streams)
    style_dem(dem_lyr, dem_clip)
    style_hillshade(hs_lyr)

    show_only([ws_lyr, streams, hs_lyr, dem_lyr])
    zoom_to(ws_lyr)


# =====================================================================

def main():
    os.makedirs(WORK, exist_ok=True)
    remove_old_map()
    paths = delineate()
    make_map(paths)
    log(f"All done. Outputs in: {RUN_FOLDER}")


main()
