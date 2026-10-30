"""
Map tab for the AiM Field Assistant.

- Satellite map (Esri World Imagery) with street and topo layers
- Search a lat/long or an ATS location and fly there
- Drag in a shapefile (.zip), GeoJSON, KML, KMZ, GPX or GeoTIFF and zoom to it
- Click the map to get lat/long and ATS of that point
- ATS <-> lat/long converter, single and batch

ATS conversion is a calculated approximation of the Alberta Township System
grid (no survey data), typically within a few hundred metres. Fine for field
navigation and loadforms; confirm against the official ATS grid for legal work.
"""

import io
import math
import re
import zipfile
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

import folium
from folium import plugins
from streamlit_folium import st_folium

try:
    import geopandas as gpd
    GEO_OK = True
except Exception:
    GEO_OK = False

try:
    import gpxpy
    GPX_OK = True
except Exception:
    GPX_OK = False

try:
    import rasterio
    from rasterio.warp import transform_bounds
    RASTER_OK = True
except Exception:
    RASTER_OK = False

# ---------------------------------------------------------------------------
# ATS math (approximate)
# ---------------------------------------------------------------------------
BASE_LAT = 49.0
TWP_H = 11.0 / 126.0                 # degrees latitude per township (49 to 60 N)
TWP_W_M = 9776.8                     # township width in metres (6 sections + road allowances)
MERIDIANS = {4: 110.0, 5: 114.0, 6: 118.0}
QUARTERS = {"SE": (0, 0), "SW": (0, 1), "NE": (1, 0), "NW": (1, 1)}   # (row from S, col from E)

ATS_RE = re.compile(
    r"^\s*(?:(?:(NE|NW|SE|SW)|(?:LSD\s*)?(\d{1,2}))[\s\-]+)?(\d{1,2})[\s\-]+(\d{1,3})[\s\-]+(\d{1,2})"
    r"[\s\-]*W\s*([4-6])\s*M?\s*$", re.I)


def _m_per_deg_lon(lat):
    p = math.radians(lat)
    return 111412.84 * math.cos(p) - 93.5 * math.cos(3 * p)


def parse_ats_any(text):
    """NE-16-75-10-W6M, 12-16-75-10-W6M, 16-75-10-W6 (section only). Returns dict or None."""
    m = ATS_RE.match(str(text or "").replace("\u2013", "-"))
    if not m:
        return None
    q, lsd, sec, twp, rge, mer = m.groups()
    sec, twp, rge, mer = int(sec), int(twp), int(rge), int(mer)
    if not (1 <= sec <= 36 and 1 <= twp <= 126 and 1 <= rge <= 30):
        return None
    lsd = int(lsd) if lsd else None
    if lsd is not None and not (1 <= lsd <= 16):
        return None
    return {"quarter": q.upper() if q else None, "lsd": lsd, "section": sec,
            "township": twp, "range": rge, "meridian": mer}


def ats_label(a):
    head = a["quarter"] or (str(a["lsd"]) if a.get("lsd") else None)
    core = f"{a['section']}-{a['township']}-{a['range']}-W{a['meridian']}M"
    return f"{head}-{core}" if head else core


def _cell_to_lonlat(a, x_frac, y_frac):
    """x_frac from township east edge (0) to west edge (1), y_frac from south (0) to north (1)."""
    s_lat = BASE_LAT + (a["township"] - 1) * TWP_H
    w_deg = TWP_W_M / _m_per_deg_lon(s_lat + TWP_H / 2)
    east_lonw = MERIDIANS[a["meridian"]] + (a["range"] - 1) * w_deg
    return -(east_lonw + x_frac * w_deg), s_lat + y_frac * TWP_H


def ats_to_latlon(a):
    """Centre point and outline polygon [(lat, lon), ...] of the ATS unit."""
    r = (a["section"] - 1) // 6
    idx = (a["section"] - 1) % 6
    c = idx if r % 2 == 0 else 5 - idx
    x0, y0, size = c / 6, r / 6, 1 / 6
    if a.get("lsd"):
        lr = (a["lsd"] - 1) // 4
        li = (a["lsd"] - 1) % 4
        lc = li if lr % 2 == 0 else 3 - li
        x0, y0, size = x0 + lc * size / 4, y0 + lr * size / 4, size / 4
    elif a.get("quarter"):
        qr, qc = QUARTERS[a["quarter"]]
        x0, y0, size = x0 + qc * size / 2, y0 + qr * size / 2, size / 2
    corners = [(x0, y0), (x0 + size, y0), (x0 + size, y0 + size), (x0, y0 + size)]
    poly = []
    for x, y in corners:
        lon, lat = _cell_to_lonlat(a, x, y)
        poly.append((lat, lon))
    lon, lat = _cell_to_lonlat(a, x0 + size / 2, y0 + size / 2)
    return (lat, lon), poly


def latlon_to_ats(lat, lon):
    """Returns dict with quarter and lsd filled, or None if outside the ATS grid."""
    lonw = -lon if lon < 0 else lon
    if not (BASE_LAT <= lat < 60.0):
        return None
    if 110.0 <= lonw < 114.0:
        mer = 4
    elif 114.0 <= lonw < 118.0:
        mer = 5
    elif 118.0 <= lonw <= 120.0:
        mer = 6
    else:
        return None
    twp = int((lat - BASE_LAT) // TWP_H) + 1
    s_lat = BASE_LAT + (twp - 1) * TWP_H
    w_deg = TWP_W_M / _m_per_deg_lon(s_lat + TWP_H / 2)
    off = lonw - MERIDIANS[mer]
    rge = int(off // w_deg) + 1
    x = (off - (rge - 1) * w_deg) / w_deg
    y = (lat - s_lat) / TWP_H
    c, r = min(int(x * 6), 5), min(int(y * 6), 5)
    sec = r * 6 + (c + 1 if r % 2 == 0 else 6 - c)
    xs, ys = x * 6 - c, y * 6 - r
    lc, lr = min(int(xs * 4), 3), min(int(ys * 4), 3)
    lsd = lr * 4 + (lc + 1 if lr % 2 == 0 else 4 - lc)
    q = ("N" if ys >= 0.5 else "S") + ("W" if xs >= 0.5 else "E")
    return {"quarter": q, "lsd": lsd, "section": sec, "township": twp,
            "range": min(rge, 30), "meridian": mer}


def parse_latlon_any(text):
    """'55.1234, -118.5678' (either order, minus optional). Returns (lat, lon) or None."""
    nums = re.findall(r"-?\d+(?:\.\d+)?", str(text or ""))
    if len(nums) != 2:
        return None
    a, b = float(nums[0]), float(nums[1])
    if abs(a) > abs(b):
        a, b = b, a
    if b > 0:
        b = -b
    if not (-90 <= a <= 90 and -180 <= b <= 180):
        return None
    return a, b


def convert_one(text):
    """Auto-detect lat/long or ATS. Returns a result dict or None."""
    a = parse_ats_any(text)
    if a:
        (lat, lon), poly = ats_to_latlon(a)
        zoom = 15 if (a["quarter"] or a["lsd"]) else 14
        return {"input": text.strip(), "kind": "ATS", "lat": lat, "lon": lon, "ats": ats_label(a),
                "poly": poly, "zoom": zoom}
    ll = parse_latlon_any(text) if not re.search(r"W\s*[4-6]", str(text), re.I) else None
    if ll:
        lat, lon = ll
        a = latlon_to_ats(lat, lon)
        if a:
            q = dict(a, lsd=None)
            l = dict(a, quarter=None)
            return {"input": text.strip(), "kind": "Lat, Long", "lat": lat, "lon": lon,
                    "ats": ats_label(q), "lsd": ats_label(l), "poly": ats_to_latlon(q)[1], "zoom": 16}
        return {"input": text.strip(), "kind": "Lat, Long", "lat": lat, "lon": lon,
                "ats": "Outside Alberta ATS grid", "poly": None, "zoom": 13}
    return None


# ---------------------------------------------------------------------------
# Reading dropped files
# ---------------------------------------------------------------------------
def _read_vector(upload):
    """Return a GeoDataFrame in EPSG:4326, or raise."""
    name = upload.name.lower()
    data = upload.getvalue()
    if name.endswith(".gpx"):
        if not GPX_OK:
            raise ValueError("gpxpy not installed")
        g = gpxpy.parse(io.StringIO(data.decode("utf-8", "ignore")))
        feats = []
        for w in g.waypoints:
            feats.append({"type": "Feature", "properties": {"name": w.name or ""},
                          "geometry": {"type": "Point", "coordinates": [w.longitude, w.latitude]}})
        for t in g.tracks:
            for s in t.segments:
                pts = [[p.longitude, p.latitude] for p in s.points]
                if len(pts) > 1:
                    feats.append({"type": "Feature", "properties": {"name": t.name or ""},
                                  "geometry": {"type": "LineString", "coordinates": pts}})
        if not feats:
            raise ValueError("no waypoints or tracks in GPX")
        return gpd.GeoDataFrame.from_features(feats, crs="EPSG:4326")

    if not GEO_OK:
        raise ValueError("geopandas not installed")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        if name.endswith(".zip"):
            zipfile.ZipFile(io.BytesIO(data)).extractall(tmp)
            shp = next(tmp.rglob("*.shp"), None)
            if shp is None:
                raise ValueError("no .shp inside the zip")
            gdf = gpd.read_file(shp)
        elif name.endswith(".kmz"):
            zipfile.ZipFile(io.BytesIO(data)).extractall(tmp)
            kml = next(tmp.rglob("*.kml"), None)
            if kml is None:
                raise ValueError("no .kml inside the kmz")
            gdf = _read_kml(kml)
        elif name.endswith(".kml"):
            p = tmp / "in.kml"
            p.write_bytes(data)
            gdf = _read_kml(p)
        else:
            p = tmp / ("in" + Path(name).suffix)
            p.write_bytes(data)
            gdf = gpd.read_file(p)
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    return gdf.to_crs("EPSG:4326")


def _read_kml(path):
    try:
        return gpd.read_file(path, driver="KML")
    except Exception:
        pass
    try:
        return gpd.read_file(path)
    except Exception:
        pass
    # fallback: pull coordinates out by hand
    txt = Path(path).read_text(encoding="utf-8", errors="ignore")
    feats = []
    for block in re.findall(r"<coordinates>(.*?)</coordinates>", txt, re.S):
        pts = []
        for tok in block.split():
            parts = tok.split(",")
            if len(parts) >= 2:
                pts.append([float(parts[0]), float(parts[1])])
        if len(pts) == 1:
            geom = {"type": "Point", "coordinates": pts[0]}
        elif len(pts) > 1:
            closed = pts[0] == pts[-1] and len(pts) > 3
            geom = {"type": "Polygon", "coordinates": [pts]} if closed else {"type": "LineString", "coordinates": pts}
        else:
            continue
        feats.append({"type": "Feature", "properties": {}, "geometry": geom})
    if not feats:
        raise ValueError("could not read KML")
    return gpd.GeoDataFrame.from_features(feats, crs="EPSG:4326")


def _read_raster(upload):
    """GeoTIFF -> (png_bytes, [[south, west], [north, east]])."""
    if not RASTER_OK:
        raise ValueError("rasterio not installed, so GeoTIFF imagery can't be shown")
    from PIL import Image
    import numpy as np
    with rasterio.MemoryFile(upload.getvalue()) as mem, mem.open() as src:
        if src.crs is None:
            raise ValueError("this image has no coordinate system (not georeferenced)")
        w, s, e, n = transform_bounds(src.crs, "EPSG:4326", *src.bounds)
        scale = max(src.width, src.height) / 2000
        out_w = max(1, int(src.width / max(scale, 1)))
        out_h = max(1, int(src.height / max(scale, 1)))
        bands = [1, 2, 3] if src.count >= 3 else [1]
        arr = src.read(bands, out_shape=(len(bands), out_h, out_w)).astype("float32")
    lo, hi = np.nanpercentile(arr, 2), np.nanpercentile(arr, 98)
    arr = np.clip((arr - lo) / max(hi - lo, 1e-6) * 255, 0, 255).astype("uint8")
    img = arr[0] if len(bands) == 1 else np.moveaxis(arr, 0, -1)
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, format="PNG")
    return buf.getvalue(), [[s, w], [n, e]]


# ---------------------------------------------------------------------------
# Map
# ---------------------------------------------------------------------------
def _base_map(center, zoom):
    m = folium.Map(location=center, zoom_start=zoom, tiles=None, control_scale=True)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri, Maxar, Earthstar Geographics", name="Satellite", max_zoom=19).add_to(m)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
        attr="Esri", name="Topo", max_zoom=19).add_to(m)
    folium.TileLayer("OpenStreetMap", name="Streets").add_to(m)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
        attr="Esri", name="Labels", overlay=True, control=True).add_to(m)
    try:
        plugins.Fullscreen().add_to(m)
        plugins.MousePosition(position="bottomleft", separator=", ", num_digits=6, prefix="").add_to(m)
        plugins.LocateControl(flyTo=True).add_to(m)
        plugins.MeasureControl(primary_length_unit="meters", primary_area_unit="hectares").add_to(m)
    except Exception:
        pass
    return m


def render_map():
    st.subheader("Map")
    ss = st.session_state
    ss.setdefault("map_center", [53.5, -114.5])
    ss.setdefault("map_zoom", 6)
    ss.setdefault("map_target", None)       # dict from convert_one
    ss.setdefault("map_bounds", None)

    c1, c2 = st.columns([3, 1])
    q = c1.text_input("Search lat/long or ATS", key="map_q",
                      placeholder="55.1234, -118.5678   or   NE-16-75-10-W6M   or   12-16-75-10-W6M",
                      help="Lat/long in decimal degrees (minus sign optional). ATS: quarter or LSD, "
                           "section, township, range, meridian. Section only also works (16-75-10-W6M).")
    c2.write("")
    c2.write("")
    if c2.button("Go", key="map_go", use_container_width=True) or (q and q != ss.get("map_last_q")):
        ss["map_last_q"] = q
        res = convert_one(q) if q else None
        if res:
            ss["map_target"] = res
            ss["map_bounds"] = None
            ss["map_center"] = [res["lat"], res["lon"]]
            ss["map_zoom"] = res.get("zoom", 14)
        elif q:
            st.warning("Couldn't read that. Try 55.1234, -118.5678 or NE-16-75-10-W6M.")

    ups = st.file_uploader(
        "Drag in GIS files or imagery to zoom to them",
        type=["zip", "geojson", "json", "kml", "kmz", "gpx", "tif", "tiff"],
        accept_multiple_files=True, key="map_files",
        help="Shapefile as a .zip, GeoJSON, KML, KMZ, GPX, or a georeferenced GeoTIFF.")

    m = _base_map(ss["map_center"], ss["map_zoom"])
    fit = None
    colors = ["#ffcc00", "#00e5ff", "#ff4081", "#76ff03", "#ff9100", "#e040fb"]

    for i, up in enumerate(ups or []):
        name = up.name
        try:
            if name.lower().endswith((".tif", ".tiff")):
                png, bounds = _read_raster(up)
                import base64
                url = "data:image/png;base64," + base64.b64encode(png).decode()
                folium.raster_layers.ImageOverlay(url, bounds=bounds, name=name, opacity=0.85).add_to(m)
                b = bounds
            else:
                gdf = _read_vector(up)
                if len(gdf) > 5000:
                    gdf = gdf.iloc[:5000]
                    st.caption(f"{name}: showing the first 5000 features.")
                color = colors[i % len(colors)]
                fields = [c for c in gdf.columns if c != "geometry"][:5]
                folium.GeoJson(
                    gdf.__geo_interface__, name=name,
                    style_function=lambda f, c=color: {"color": c, "weight": 3, "fillOpacity": 0.15},
                    marker=folium.CircleMarker(radius=6, color=color, fill=True, fill_opacity=0.9),
                    tooltip=folium.GeoJsonTooltip(fields=fields) if fields else None,
                ).add_to(m)
                minx, miny, maxx, maxy = gdf.total_bounds
                b = [[miny, minx], [maxy, maxx]]
            fit = b if fit is None else [[min(fit[0][0], b[0][0]), min(fit[0][1], b[0][1])],
                                         [max(fit[1][0], b[1][0]), max(fit[1][1], b[1][1])]]
        except Exception as e:
            st.warning(f"{name}: {e}")

    t = ss["map_target"]
    if t:
        tip = f"{t['lat']:.6f}, {t['lon']:.6f}<br>{t['ats']}"
        folium.Marker([t["lat"], t["lon"]], tooltip=tip, popup=tip,
                      icon=folium.Icon(color="red", icon="map-marker")).add_to(m)
        if t.get("poly"):
            folium.Polygon(t["poly"], color="#ff1744", weight=2, fill=True, fill_opacity=0.08,
                           tooltip=f"{t['ats']} (approx.)").add_to(m)

    if fit:
        m.fit_bounds(fit)
    folium.LayerControl(collapsed=True).add_to(m)

    out = st_folium(m, height=600, use_container_width=True, returned_objects=["last_clicked"], key="map_view")

    clicked = (out or {}).get("last_clicked")
    if clicked:
        lat, lon = clicked["lat"], clicked["lng"]
        a = latlon_to_ats(lat, lon)
        if a:
            st.info(f"Clicked: {lat:.6f}, {lon:.6f}   |   {ats_label(dict(a, lsd=None))}   |   "
                    f"LSD {ats_label(dict(a, quarter=None))}")
        else:
            st.info(f"Clicked: {lat:.6f}, {lon:.6f}   |   outside the Alberta ATS grid")

    if t:
        st.caption(f"Search result: {t['lat']:.6f}, {t['lon']:.6f}  |  {t['ats']}"
                   + (f"  |  LSD {t['lsd']}" if t.get("lsd") else ""))

    # --- Converter ----------------------------------------------------------
    st.markdown("---")
    st.subheader("ATS / Lat, Long converter")
    st.caption("One per line, mix and match. ATS is a calculated approximation, usually within a few "
               "hundred metres. Confirm against the official ATS grid for legal descriptions.")
    lines = st.text_area("Locations", height=130, key="conv_in",
                         placeholder="NE-16-75-10-W6M\n55.1234, -118.5678\n12-11-44-5-W5M")
    if lines.strip():
        recs = []
        for ln in lines.splitlines():
            if not ln.strip():
                continue
            r = convert_one(ln)
            if not r:
                recs.append({"Input": ln.strip(), "Type": "?", "Lat": None, "Long": None,
                             "Quarter": "Couldn't read", "LSD": ""})
                continue
            if r["kind"] == "ATS":
                a = latlon_to_ats(r["lat"], r["lon"])
                recs.append({"Input": r["input"], "Type": "ATS", "Lat": round(r["lat"], 6),
                             "Long": round(r["lon"], 6),
                             "Quarter": ats_label(dict(a, lsd=None)) if a else "",
                             "LSD": ats_label(dict(a, quarter=None)) if a else ""})
            else:
                recs.append({"Input": r["input"], "Type": "Lat, Long", "Lat": round(r["lat"], 6),
                             "Long": round(r["lon"], 6), "Quarter": r["ats"], "LSD": r.get("lsd", "")})
        df = pd.DataFrame(recs)
        st.dataframe(df, hide_index=True, use_container_width=True)
        st.download_button("Download CSV", df.to_csv(index=False).encode(), "ats_latlong.csv",
                           "text/csv", key="conv_dl")
