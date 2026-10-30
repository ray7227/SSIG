"""
Map tab for the AiM Field Assistant.

- Satellite map (Esri World Imagery) with topo, streets and labels
- Search a lat/long or an ATS location and fly there
- Drag in a shapefile (.zip), GeoJSON, KML, KMZ, GPX or GeoTIFF and zoom to it
- Click anywhere for lat/long (popup on the map) plus its ATS
- Centerline length of dropped footprints: the longest path down the middle,
  ignoring side branches
- ATS <-> lat/long converter, single and batch

ATS lookups use ats_grid.npz (the official quarter-section grid, packed) when it
is in the app folder. Without it, a calculated grid is used instead
(usually within a few hundred metres).
"""

import base64
import heapq
import io
import math
import re
import tempfile
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

import folium
from folium import plugins
from streamlit_folium import st_folium

try:
    import geopandas as gpd
    import shapely
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

GRID_FILE = Path(__file__).with_name("ats_grid.npz")
Q_NAMES = ["SE", "SW", "NE", "NW"]                     # storage order in ats_grid.npz
QUARTERS = {"SE": (0, 0), "SW": (0, 1), "NE": (1, 0), "NW": (1, 1)}   # (row from S, col from E)
LSD_QUARTER = {}                                       # lsd -> quarter
for _lsd in range(1, 17):
    _lr = (_lsd - 1) // 4
    _li = (_lsd - 1) % 4
    _lc = _li if _lr % 2 == 0 else 3 - _li
    LSD_QUARTER[_lsd] = {(0, 0): "SE", (0, 1): "SW", (1, 0): "NE", (1, 1): "NW"}[(_lr // 2, _lc // 2)]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
ATS_RE = re.compile(
    r"^\s*(?:(?:(NE|NW|SE|SW)|(?:LSD\s*)?(\d{1,2}))[\s\-]+)?(\d{1,2})[\s\-]+(\d{1,3})[\s\-]+(\d{1,2})"
    r"[\s\-]*W\s*([4-6])\s*M?\s*$", re.I)


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
    head = a.get("quarter") or (str(a["lsd"]) if a.get("lsd") else None)
    core = f"{a['section']}-{a['township']}-{a['range']}-W{a['meridian']}M"
    return f"{head}-{core}" if head else core


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


# ---------------------------------------------------------------------------
# Calculated ATS (fallback when ats_grid.npz is missing or there is no survey)
# ---------------------------------------------------------------------------
BASE_LAT = 49.0
TWP_H = 11.0 / 126.0
TWP_W_M = 9776.8
MERIDIANS = {4: 110.0, 5: 114.0, 6: 118.0}


def _m_per_deg_lon(lat):
    p = math.radians(lat)
    return 111412.84 * math.cos(p) - 93.5 * math.cos(3 * p)


def _calc_cell_to_lonlat(a, x_frac, y_frac):
    s_lat = BASE_LAT + (a["township"] - 1) * TWP_H
    w_deg = TWP_W_M / _m_per_deg_lon(s_lat + TWP_H / 2)
    east_lonw = MERIDIANS[a["meridian"]] + (a["range"] - 1) * w_deg
    return -(east_lonw + x_frac * w_deg), s_lat + y_frac * TWP_H


def _calc_ats_to_latlon(a):
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
    poly = []
    for x, y in [(x0, y0), (x0 + size, y0), (x0 + size, y0 + size), (x0, y0 + size)]:
        lon, lat = _calc_cell_to_lonlat(a, x, y)
        poly.append((lat, lon))
    lon, lat = _calc_cell_to_lonlat(a, x0 + size / 2, y0 + size / 2)
    return (lat, lon), poly


def _calc_latlon_to_ats(lat, lon):
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


# ---------------------------------------------------------------------------
# Official ATS grid (ats_grid.npz)
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner="Loading ATS grid...")
def _grid():
    if not GRID_FILE.exists():
        return None
    z = np.load(GRID_FILE)
    keys = np.cumsum(z["kd"], dtype=np.int64)
    cen = np.cumsum(z["cd"], axis=0, dtype=np.int64)
    corners = ((cen[:, None, :] + z["off"].astype(np.int64)) / 1e5).astype(np.float64)  # (N,4,2) lon,lat
    cen = (cen / 1e5).astype(np.float64)
    return {"keys": keys, "lon": cen[:, 0].copy(), "lat": cen[:, 1].copy(), "corners": corners}


def _key(mer, rge, twp, sec, q=0):
    return (((mer * 100 + rge) * 1000 + twp) * 100 + sec) * 10 + q


def _decode(key):
    key = int(key)
    q = key % 10; key //= 10
    sec = key % 100; key //= 100
    twp = key % 1000; key //= 1000
    rge = key % 100; mer = key // 100
    return {"quarter": Q_NAMES[q], "section": sec, "township": twp, "range": rge, "meridian": mer}


def _bilinear(c, u, v):
    """c = corners (4,2) in SE, SW, NE, NW order. u from east (0) to west (1), v from south (0) to north (1)."""
    se, sw, ne, nw = c
    return (1 - u) * (1 - v) * se + u * (1 - v) * sw + (1 - u) * v * ne + u * v * nw


def _inverse_bilinear(c, lon, lat):
    k = math.cos(math.radians(lat))
    cc = c.copy()
    cc[:, 0] *= k
    p = np.array([lon * k, lat])
    u = v = 0.5
    for _ in range(8):
        f = _bilinear(cc, u, v) - p
        se, sw, ne, nw = cc
        du = -(1 - v) * se + (1 - v) * sw - v * ne + v * nw
        dv = -(1 - u) * se - u * sw + (1 - u) * ne + u * nw
        jac = np.array([du, dv]).T
        try:
            step = np.linalg.solve(jac, f)
        except np.linalg.LinAlgError:
            break
        u, v = u - step[0], v - step[1]
    return min(max(u, 0.0), 1.0), min(max(v, 0.0), 1.0)


def _in_quad(c, lon, lat):
    poly = c[[0, 1, 3, 2]]          # SE, SW, NW, NE around the ring
    inside = False
    j = 3
    for i in range(4):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-15) + xi:
            inside = not inside
        j = i
    return inside


def _ring(c):
    """Quad corners -> [(lat, lon), ...] for folium."""
    return [(float(p[1]), float(p[0])) for p in c[[0, 1, 3, 2]]]


def latlon_to_ats(lat, lon):
    """Returns (ats dict with quarter and lsd, source text) or (None, None)."""
    g = _grid()
    if g is not None:
        m = np.flatnonzero((np.abs(g["lat"] - lat) < 0.012) & (np.abs(g["lon"] - lon) < 0.02))
        hit = None
        for i in m:
            if _in_quad(g["corners"][i], lon, lat):
                hit = i
                break
        source = "ATS grid"
        if hit is None and len(m):
            d = (g["lat"][m] - lat) ** 2 + ((g["lon"][m] - lon) * math.cos(math.radians(lat))) ** 2
            hit = m[int(np.argmin(d))]
            source = "ATS grid (nearest quarter)"
        if hit is not None:
            a = _decode(g["keys"][hit])
            u, v = _inverse_bilinear(g["corners"][hit], lon, lat)
            qr, qc = QUARTERS[a["quarter"]]
            lc, lr = qc * 2 + (1 if u >= 0.5 else 0), qr * 2 + (1 if v >= 0.5 else 0)
            a["lsd"] = lr * 4 + (lc + 1 if lr % 2 == 0 else 4 - lc)
            return a, source
    a = _calc_latlon_to_ats(lat, lon)
    return (a, "calculated") if a else (None, None)


def ats_to_latlon(a):
    """Returns ((lat, lon), ring [(lat, lon), ...], source)."""
    g = _grid()
    if g is not None:
        base = _key(a["meridian"], a["range"], a["township"], a["section"])
        lo, hi = np.searchsorted(g["keys"], [base, base + 4])
        idx = {Q_NAMES[int(g["keys"][i] % 10)]: i for i in range(lo, hi)}
        want_q = a.get("quarter") or (LSD_QUARTER[a["lsd"]] if a.get("lsd") else None)
        if idx and (want_q is None or want_q in idx):
            if a.get("lsd"):
                c = g["corners"][idx[want_q]]
                lr = (a["lsd"] - 1) // 4
                li = (a["lsd"] - 1) % 4
                lc = li if lr % 2 == 0 else 3 - li
                u0, v0 = (lc % 2) * 0.5, (lr % 2) * 0.5
                sub = np.array([_bilinear(c, u0, v0), _bilinear(c, u0 + .5, v0),
                                _bilinear(c, u0, v0 + .5), _bilinear(c, u0 + .5, v0 + .5)])
            elif want_q:
                sub = g["corners"][idx[want_q]]
            else:
                pick = lambda q, k, fb: g["corners"][idx[q]][k] if q in idx else fb
                allc = np.concatenate([g["corners"][i] for i in idx.values()])
                sub = np.array([pick("SE", 0, allc[np.argmax(allc[:, 0] - allc[:, 1])]),
                                pick("SW", 1, allc[np.argmax(-allc[:, 0] - allc[:, 1])]),
                                pick("NE", 2, allc[np.argmax(allc[:, 0] + allc[:, 1])]),
                                pick("NW", 3, allc[np.argmax(-allc[:, 0] + allc[:, 1])])])
            ctr = sub.mean(axis=0)
            return (float(ctr[1]), float(ctr[0])), _ring(sub), "ATS grid"
    ctr, ring = _calc_ats_to_latlon(a)
    return ctr, ring, "calculated (not in surveyed grid)" if g is not None else "calculated"


def convert_one(text):
    """Auto-detect lat/long or ATS. Returns a result dict or None."""
    a = parse_ats_any(text)
    if a:
        (lat, lon), ring, src = ats_to_latlon(a)
        b, _ = latlon_to_ats(lat, lon)
        return {"input": text.strip(), "kind": "ATS", "lat": lat, "lon": lon, "ats": ats_label(a),
                "quarter": ats_label(dict(b, lsd=None)) if b else "",
                "lsd": ats_label(dict(b, quarter=None)) if b else "",
                "poly": ring, "source": src, "zoom": 15 if (a["quarter"] or a["lsd"]) else 14}
    if re.search(r"W\s*[4-6]", str(text), re.I):
        return None
    ll = parse_latlon_any(text)
    if not ll:
        return None
    lat, lon = ll
    a, src = latlon_to_ats(lat, lon)
    if a:
        q = dict(a, lsd=None)
        _, ring, _ = ats_to_latlon(q)
        return {"input": text.strip(), "kind": "Lat, Long", "lat": lat, "lon": lon,
                "ats": ats_label(q), "quarter": ats_label(q), "lsd": ats_label(dict(a, quarter=None)),
                "poly": ring, "source": src, "zoom": 16}
    return {"input": text.strip(), "kind": "Lat, Long", "lat": lat, "lon": lon,
            "ats": "Outside Alberta ATS grid", "quarter": "", "lsd": "", "poly": None,
            "source": "", "zoom": 13}


# ---------------------------------------------------------------------------
# Centerline length (longest path down the middle, ignoring branches)
# ---------------------------------------------------------------------------
def _longest_paths(edges):
    """edges: list of (node_a, node_b, length). One longest path per connected piece."""
    adj = {}
    for a, b, w in edges:
        if a == b:
            continue
        adj.setdefault(a, []).append((b, w))
        adj.setdefault(b, []).append((a, w))

    def far(src):
        dist, prev = {src: 0.0}, {src: None}
        pq = [(0.0, src)]
        while pq:
            d, n = heapq.heappop(pq)
            if d > dist[n]:
                continue
            for m, w in adj[n]:
                nd = d + w
                if nd < dist.get(m, float("inf")):
                    dist[m], prev[m] = nd, n
                    heapq.heappush(pq, (nd, m))
        end = max(dist, key=dist.get)
        return end, dist, prev

    seen, out = set(), []
    for start in adj:
        if start in seen:
            continue
        a, dist, _ = far(start)
        seen.update(dist)
        b, dist, prev = far(a)
        path, n = [], b
        while n is not None:
            path.append(n)
            n = prev[n]
        out.append((dist[b], path))
    return out


def _polygon_centerline(poly):
    width = 2 * poly.area / max(poly.length, 1e-9)          # rough average width
    spacing = max(width / 4, poly.length / 8000, 0.5)
    pts = np.unique(shapely.get_coordinates(shapely.segmentize(poly.boundary, spacing)), axis=0)
    if len(pts) < 4:
        return None
    edges = shapely.get_parts(shapely.voronoi_polygons(shapely.multipoints(pts), only_edges=True))
    shapely.prepare(poly)
    edges = edges[shapely.contains(poly, edges)]
    if not len(edges):
        return None
    xy = shapely.get_coordinates(edges).reshape(-1, 2, 2)
    e = [((round(p[0][0], 2), round(p[0][1], 2)), (round(p[1][0], 2), round(p[1][1], 2)),
          float(math.hypot(p[1][0] - p[0][0], p[1][1] - p[0][1]))) for p in xy]
    paths = _longest_paths(e)
    return max(paths, key=lambda t: t[0]) if paths else None


def centerline(gdf):
    """Returns dict with longest (m), total (m), pieces, and lines (GeoSeries in EPSG:4326), or None."""
    if gdf is None or gdf.empty:
        return None
    try:
        utm = gdf.estimate_utm_crs()
    except Exception:
        utm = "EPSG:3400"
    g = gdf.to_crs(utm)
    geoms = g.geometry[g.geometry.notna()]
    polys = geoms[geoms.geom_type.isin(["Polygon", "MultiPolygon"])]
    lines = geoms[geoms.geom_type.isin(["LineString", "MultiLineString"])]
    results = []

    if len(polys):
        merged = shapely.union_all(polys.values)
        parts = sorted(shapely.get_parts(merged), key=lambda p: -p.area)[:200]
        for p in parts:
            r = _polygon_centerline(p)
            if r:
                results.append(r)

    if len(lines):
        noded = shapely.get_parts(shapely.union_all(lines.values))
        e = []
        for ln in noded:
            c = shapely.get_coordinates(ln)
            for (x1, y1), (x2, y2) in zip(c[:-1], c[1:]):
                e.append(((round(x1, 2), round(y1, 2)), (round(x2, 2), round(y2, 2)),
                          float(math.hypot(x2 - x1, y2 - y1))))
        results.extend(_longest_paths(e))

    results = [r for r in results if r[0] > 0 and len(r[1]) > 1]
    if not results:
        return None
    from shapely.geometry import LineString
    ls = gpd.GeoSeries([LineString(path) for _, path in results], crs=utm).to_crs(4326)
    lengths = [r[0] for r in results]
    return {"longest": max(lengths), "total": sum(lengths), "pieces": len(lengths), "lines": ls}


def length_text(cl):
    if not cl:
        return ""
    t = f"Centerline length: {cl['longest']:,.0f} m ({cl['longest'] / 1000:.2f} km)"
    if cl["pieces"] > 1:
        t += (f" for the longest piece. {cl['pieces']} separate pieces, "
              f"{cl['total']:,.0f} m ({cl['total'] / 1000:.2f} km) combined.")
    return t


# ---------------------------------------------------------------------------
# Reading dropped files
# ---------------------------------------------------------------------------
def _read_kml(path):
    for kw in ({"driver": "KML"}, {}):
        try:
            return gpd.read_file(path, **kw)
        except Exception:
            pass
    txt = Path(path).read_text(encoding="utf-8", errors="ignore")
    feats = []
    for block in re.findall(r"<coordinates>(.*?)</coordinates>", txt, re.S):
        pts = [[float(t.split(",")[0]), float(t.split(",")[1])] for t in block.split() if len(t.split(",")) >= 2]
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


def _read_vector_bytes(name, data):
    name = name.lower()
    if name.endswith(".gpx"):
        if not GPX_OK:
            raise ValueError("gpxpy not installed")
        g = gpxpy.parse(io.StringIO(data.decode("utf-8", "ignore")))
        feats = [{"type": "Feature", "properties": {"name": w.name or ""},
                  "geometry": {"type": "Point", "coordinates": [w.longitude, w.latitude]}} for w in g.waypoints]
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
        if name.endswith((".zip", ".kmz")):
            zipfile.ZipFile(io.BytesIO(data)).extractall(tmp)
            if name.endswith(".zip"):
                shp = next(tmp.rglob("*.shp"), None)
                if shp is None:
                    raise ValueError("no .shp inside the zip")
                gdf = gpd.read_file(shp)
            else:
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


@st.cache_data(show_spinner=False, max_entries=20)
def _vector_and_length(name, data):
    gdf = _read_vector_bytes(name, data)
    try:
        cl = centerline(gdf)
    except Exception:
        cl = None
    return gdf, cl


def footprint_length(gdf):
    """For other tabs: centerline text for a GeoDataFrame."""
    try:
        return length_text(centerline(gdf))
    except Exception:
        return ""


@st.cache_data(show_spinner=False, max_entries=10)
def _read_raster(data):
    if not RASTER_OK:
        raise ValueError("rasterio not installed, so GeoTIFF imagery can't be shown")
    from PIL import Image
    with rasterio.MemoryFile(data) as mem, mem.open() as src:
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
    folium.LatLngPopup().add_to(m)          # click anywhere = lat/long popup
    for p in (plugins.Fullscreen(),
              plugins.MousePosition(position="bottomleft", separator=", ", num_digits=6, prefix=""),
              plugins.LocateControl(flyTo=True),
              plugins.MeasureControl(primary_length_unit="meters", primary_area_unit="hectares")):
        try:
            p.add_to(m)
        except Exception:
            pass
    return m


def render_map():
    st.subheader("Map")
    ss = st.session_state
    ss.setdefault("map_center", [53.5, -114.5])
    ss.setdefault("map_zoom", 6)
    ss.setdefault("map_target", None)

    if _grid() is None:
        st.caption("ATS: calculated grid. Add ats_grid.npz to the app folder for the official grid.")

    c1, c2 = st.columns([3, 1])
    q = c1.text_input("Search lat/long or ATS", key="map_q",
                      placeholder="55.1234, -118.5678   or   NE-16-75-10-W6M   or   12-16-75-10-W6M",
                      help="Lat/long in decimal degrees (minus sign optional). ATS: quarter or LSD, "
                           "section, township, range, meridian. Section only also works (16-75-10-W6M).")
    c2.write("")
    c2.write("")
    go = c2.button("Go", key="map_go", use_container_width=True)
    if go or (q and q != ss.get("map_last_q")):
        ss["map_last_q"] = q
        res = convert_one(q) if q else None
        if res:
            ss["map_target"] = res
            ss["map_center"] = [res["lat"], res["lon"]]
            ss["map_zoom"] = res["zoom"]
        elif q:
            st.warning("Couldn't read that. Try 55.1234, -118.5678 or NE-16-75-10-W6M.")

    ups = st.file_uploader(
        "Drag in GIS files or imagery to zoom to them",
        type=["zip", "geojson", "json", "kml", "kmz", "gpx", "tif", "tiff"],
        accept_multiple_files=True, key="map_files",
        help="Shapefile as a .zip, GeoJSON, KML, KMZ, GPX, or a georeferenced GeoTIFF. "
             "Footprints show their centerline length.")

    m = _base_map(ss["map_center"], ss["map_zoom"])
    fit = None
    colors = ["#ffcc00", "#00e5ff", "#ff4081", "#76ff03", "#ff9100", "#e040fb"]
    notes = []

    for i, up in enumerate(ups or []):
        name = up.name
        try:
            if name.lower().endswith((".tif", ".tiff")):
                png, b = _read_raster(up.getvalue())
                url = "data:image/png;base64," + base64.b64encode(png).decode()
                folium.raster_layers.ImageOverlay(url, bounds=b, name=name, opacity=0.85).add_to(m)
            else:
                gdf, cl = _vector_and_length(name, up.getvalue())
                if len(gdf) > 5000:
                    gdf = gdf.iloc[:5000]
                    notes.append(f"{name}: showing the first 5000 features.")
                color = colors[i % len(colors)]
                fields = [c for c in gdf.columns if c != "geometry"][:5]
                folium.GeoJson(
                    gdf.__geo_interface__, name=name,
                    style_function=lambda f, c=color: {"color": c, "weight": 3, "fillOpacity": 0.15},
                    marker=folium.CircleMarker(radius=6, color=color, fill=True, fill_opacity=0.9),
                    tooltip=folium.GeoJsonTooltip(fields=fields) if fields else None,
                ).add_to(m)
                if cl:
                    folium.GeoJson(
                        cl["lines"].__geo_interface__, name=f"{name} centerline",
                        style_function=lambda f: {"color": "#ffffff", "weight": 2, "dashArray": "6 6"},
                        tooltip=f"{cl['longest']:,.0f} m ({cl['longest'] / 1000:.2f} km)",
                    ).add_to(m)
                    notes.append(f"**{name}**: {length_text(cl)}")
                minx, miny, maxx, maxy = gdf.total_bounds
                b = [[miny, minx], [maxy, maxx]]
            fit = b if fit is None else [[min(fit[0][0], b[0][0]), min(fit[0][1], b[0][1])],
                                         [max(fit[1][0], b[1][0]), max(fit[1][1], b[1][1])]]
        except Exception as e:
            notes.append(f"{name}: couldn't load ({e})")

    t = ss["map_target"]
    if t:
        tip = f"{t['lat']:.6f}, {t['lon']:.6f}<br>{t['ats']}"
        folium.Marker([t["lat"], t["lon"]], tooltip=tip, popup=tip,
                      icon=folium.Icon(color="red", icon="map-marker")).add_to(m)
        if t.get("poly"):
            folium.Polygon(t["poly"], color="#ff1744", weight=2, fill=True, fill_opacity=0.08,
                           tooltip=t["ats"]).add_to(m)

    if fit:
        m.fit_bounds(fit)
    folium.LayerControl(collapsed=True).add_to(m)

    for n in notes:
        st.write(n)

    out = st_folium(m, height=600, use_container_width=True, returned_objects=["last_clicked"], key="map_view")

    clicked = (out or {}).get("last_clicked")
    if clicked:
        lat, lon = clicked["lat"], clicked["lng"]
        a, src = latlon_to_ats(lat, lon)
        ats_txt = (f"{ats_label(dict(a, lsd=None))}   |   LSD {ats_label(dict(a, quarter=None))}   ({src})"
                   if a else "outside the Alberta ATS grid")
        st.success(f"Clicked: **{lat:.6f}, {lon:.6f}**   |   {ats_txt}")

    if t:
        st.caption(f"Search result: {t['lat']:.6f}, {t['lon']:.6f}  |  {t['ats']}"
                   + (f"  |  LSD {t['lsd']}" if t.get("lsd") and t["kind"] == "Lat, Long" else "")
                   + (f"  ({t['source']})" if t.get("source") else ""))

    # --- Converter ----------------------------------------------------------
    st.markdown("---")
    st.subheader("ATS / Lat, Long converter")
    st.caption("One per line, mix and match. Lat, Long out is the centre of the ATS unit.")
    lines = st.text_area("Locations", height=130, key="conv_in",
                         placeholder="NE-16-75-10-W6M\n55.1234, -118.5678\n12-11-44-5-W5M")
    if lines.strip():
        recs = []
        for ln in lines.splitlines():
            if not ln.strip():
                continue
            r = convert_one(ln)
            if not r:
                recs.append({"Input": ln.strip(), "Lat": None, "Long": None,
                             "Quarter": "Couldn't read", "LSD": "", "Source": ""})
                continue
            recs.append({"Input": r["input"], "Lat": round(r["lat"], 6), "Long": round(r["lon"], 6),
                         "Quarter": r["quarter"] or r["ats"], "LSD": r["lsd"], "Source": r["source"]})
        df = pd.DataFrame(recs)
        st.dataframe(df, hide_index=True, use_container_width=True)
        st.download_button("Download CSV", df.to_csv(index=False).encode(), "ats_latlong.csv",
                           "text/csv", key="conv_dl")
