"""
Map tab for the AiM Field Assistant.

- Satellite map (Esri World Imagery) with topo, streets, hillshade, roads and labels
- Search a lat/long or an ATS location and fly there (town names: magnifier on the map)
- Drag in almost any GIS file: shapefile (zip or loose parts), KML, KMZ, GPX, GeoJSON,
  GeoPackage, file geodatabase (zip), CSV/Excel with lat/long or ATS, GeoTIFF imagery
- Bright, distinct colour per file (with dark halo so it shows on imagery), colour by field
- ATS quarter grid around your files
- Auto contour lines from free terrain tiles, labelled, blue (low) to red (high)
- Footprint area (ha), centerline length, elevation range, nearest town / city
- Click for lat/long, ATS and elevation, or set Point A / B for an elevation profile
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
    from folium.template import Template          # folium 0.17+
except Exception:
    from jinja2 import Template

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


def footprint_area_ha(gdf):
    """Area of all polygons in hectares, overlaps counted once. None if there are no polygons."""
    if gdf is None or gdf.empty:
        return None
    polys = gdf[gdf.geometry.notna() & gdf.geom_type.isin(["Polygon", "MultiPolygon"])]
    if polys.empty:
        return None
    try:
        crs = polys.estimate_utm_crs()
    except Exception:
        crs = "EPSG:3400"
    merged = shapely.union_all(polys.to_crs(crs).geometry.values)
    return merged.area / 10000.0


def length_text(cl):
    if not cl:
        return ""
    t = f"Centerline length: {cl['longest']:,.0f} m ({cl['longest'] / 1000:.2f} km)"
    if cl["pieces"] > 1:
        t += (f" for the longest piece. {cl['pieces']} separate pieces, "
              f"{cl['total']:,.0f} m ({cl['total'] / 1000:.2f} km) combined.")
    return t

# ---------------------------------------------------------------------------
# Reading dropped files (any GIS type)
# ---------------------------------------------------------------------------
RASTER_EXT = (".tif", ".tiff", ".jp2", ".img", ".ecw", ".sid", ".vrt")
SIDECAR_RE = re.compile(r"\.(shp\.xml|aux\.xml|shx|dbf|prj|cpg|sbn|sbx|qix|qmd|fix|atx|ain|aih|ixs|mxs)$", re.I)
VECTOR_IN_ZIP = (".shp", ".gpkg", ".geojson", ".json", ".kml", ".gpx", ".fgb", ".gml", ".csv", ".xlsx", ".dxf", ".tab")
PROVINCES = {"AB": "Alberta", "BC": "British Columbia", "SK": "Saskatchewan"}


def _clean_gdf(gdf):
    gdf = gdf[gdf.geometry.notna()]
    gdf = gdf[~gdf.geometry.is_empty]
    if gdf.empty:
        raise ValueError("no features with a location")
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    gdf = gdf.to_crs("EPSG:4326")
    geom = gpd.GeoSeries(shapely.force_2d(gdf.geometry.values), index=gdf.index, crs="EPSG:4326")
    out = gpd.GeoDataFrame(gdf.drop(columns=[gdf.geometry.name]), geometry=geom, crs="EPSG:4326")
    for c in out.columns:
        if c == "geometry":
            continue
        if not (pd.api.types.is_numeric_dtype(out[c]) or pd.api.types.is_bool_dtype(out[c])):
            out[c] = out[c].astype(str).replace({"None": "", "nan": "", "NaT": ""})
    return out.reset_index(drop=True)


def _layers(path):
    for fn in (lambda p: list(gpd.list_layers(p)["name"]),
               lambda p: [n for n, *_ in __import__("pyogrio").list_layers(p)],
               lambda p: __import__("fiona").listlayers(p)):
        try:
            return fn(str(path))
        except Exception:
            pass
    return [None]


def _read_multi(path, label):
    """Every layer of a file (KML folders, GeoPackage and geodatabase layers)."""
    names = _layers(path)
    out = []
    for n in names:
        try:
            g = gpd.read_file(path, layer=n) if n is not None else gpd.read_file(path)
            if len(g):
                out.append((label if len(names) == 1 else f"{label} : {n}", g))
        except Exception:
            pass
    return out


def _read_kml(path):
    try:
        parts = _read_multi(path, "kml")
        if parts:
            return pd.concat([g.to_crs(4326) if g.crs else g.set_crs(4326) for _, g in parts], ignore_index=True)
    except Exception:
        pass
    txt = Path(path).read_text(encoding="utf-8", errors="ignore")
    feats = []
    for pm in re.findall(r"<Placemark.*?</Placemark>", txt, re.S) or [txt]:
        nm = re.search(r"<name>(.*?)</name>", pm, re.S)
        for block in re.findall(r"<coordinates>(.*?)</coordinates>", pm, re.S):
            pts = [[float(t.split(",")[0]), float(t.split(",")[1])] for t in block.split() if len(t.split(",")) >= 2]
            if len(pts) == 1:
                geom = {"type": "Point", "coordinates": pts[0]}
            elif len(pts) > 1:
                closed = pts[0] == pts[-1] and len(pts) > 3
                geom = {"type": "Polygon", "coordinates": [pts]} if closed else {"type": "LineString", "coordinates": pts}
            else:
                continue
            feats.append({"type": "Feature", "properties": {"name": nm.group(1).strip() if nm else ""}, "geometry": geom})
    if not feats:
        raise ValueError("could not read KML")
    return gpd.GeoDataFrame.from_features(feats, crs="EPSG:4326")


def _read_gpx(data):
    if not GPX_OK:
        raise ValueError("gpxpy not installed")
    g = gpxpy.parse(io.StringIO(data.decode("utf-8", "ignore")))
    feats = [{"type": "Feature", "properties": {"name": w.name or "", "elev_m": w.elevation},
              "geometry": {"type": "Point", "coordinates": [w.longitude, w.latitude]}} for w in g.waypoints]
    for t in g.tracks:
        for s in t.segments:
            pts = [[p.longitude, p.latitude] for p in s.points]
            if len(pts) > 1:
                feats.append({"type": "Feature", "properties": {"name": t.name or "track"},
                              "geometry": {"type": "LineString", "coordinates": pts}})
    for r in g.routes:
        pts = [[p.longitude, p.latitude] for p in r.points]
        if len(pts) > 1:
            feats.append({"type": "Feature", "properties": {"name": r.name or "route"},
                          "geometry": {"type": "LineString", "coordinates": pts}})
    if not feats:
        raise ValueError("no waypoints, tracks or routes in GPX")
    return gpd.GeoDataFrame.from_features(feats, crs="EPSG:4326")


LAT_RE = re.compile(r"^(lat|latitude|lat_dd|latdd|y|lat\W.*|latitude\W.*)$", re.I)
LON_RE = re.compile(r"^(lon|long|lng|longitude|long_dd|lon_dd|x|lon\W.*|long\W.*|longitude\W.*)$", re.I)


def _table_points(df):
    """Points from a table: lat/long columns, or a column of ATS locations."""
    df = df.dropna(how="all")
    lat = next((c for c in df.columns if LAT_RE.match(str(c).strip())), None)
    lon = next((c for c in df.columns if LON_RE.match(str(c).strip())), None)
    if lat is not None and lon is not None:
        la = pd.to_numeric(df[lat], errors="coerce")
        lo = pd.to_numeric(df[lon], errors="coerce")
        swap = la.abs() > lo.abs()
        la, lo = la.where(~swap, lo), lo.where(~swap, la)
        lo = -lo.abs()
        ok = la.notna() & lo.notna()
        if ok.any():
            return gpd.GeoDataFrame(df[ok].copy(), geometry=gpd.points_from_xy(lo[ok], la[ok]), crs="EPSG:4326")
    for c in df.columns:
        vals = df[c].astype(str)
        hits = vals.map(lambda v: convert_one(v) if parse_ats_any(v) else None)
        if hits.notna().sum() >= max(1, 0.5 * len(df)):
            ok = hits.notna()
            pts = gpd.points_from_xy([h["lon"] for h in hits[ok]], [h["lat"] for h in hits[ok]])
            return gpd.GeoDataFrame(df[ok].copy(), geometry=pts, crs="EPSG:4326")
    raise ValueError("no lat/long or ATS columns found")


def _read_path(p, label):
    """One file on disk -> list of (label, gdf)."""
    suf = p.suffix.lower()
    if suf == ".kml":
        return [(label, _read_kml(p))]
    if suf == ".gpx":
        return [(label, _read_gpx(p.read_bytes()))]
    if suf in (".csv", ".txt"):
        return [(label, _table_points(pd.read_csv(p, sep=None, engine="python")))]
    if suf in (".xlsx", ".xlsm", ".xls"):
        out = []
        for sh, df in pd.read_excel(p, sheet_name=None).items():
            try:
                out.append((f"{label} : {sh}", _table_points(df)))
            except Exception:
                pass
        if not out:
            raise ValueError("no lat/long or ATS columns found")
        return out if len(out) > 1 else [(label, out[0][1])]
    parts = _read_multi(p, label)
    if not parts:
        raise ValueError("no features found")
    return parts


@st.cache_data(show_spinner="Reading file...", max_entries=30)
def _read_any(name, data, sidecars=()):
    """Bytes of an upload -> list of (label, gdf, centerline, area_ha). sidecars = ((name, bytes), ...)."""
    if not GEO_OK:
        raise ValueError("geopandas not installed")
    low = name.lower()
    found = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        if low.endswith(".gpx"):
            found = [(name, _read_gpx(data))]
        elif low.endswith((".zip", ".kmz")):
            zipfile.ZipFile(io.BytesIO(data)).extractall(tmp)
            gdbs = [d for d in tmp.rglob("*.gdb") if d.is_dir()]
            for d in gdbs:
                found += _read_multi(d, f"{name} : {d.name}")
            files = [f for f in tmp.rglob("*") if f.is_file() and f.suffix.lower() in VECTOR_IN_ZIP
                     and not any(str(f).startswith(str(d)) for d in gdbs) and "__MACOSX" not in str(f)]
            for f in files[:25]:
                try:
                    lab = name if len(files) == 1 and not gdbs else f"{name} : {f.name}"
                    found += _read_path(f, lab)
                except Exception:
                    pass
            if not found:
                raise ValueError("nothing readable inside (looked for shapefile, geodatabase, KML, GeoPackage, "
                                 "GeoJSON, GPX, CSV)")
        else:
            p = tmp / Path(name).name
            p.write_bytes(data)
            for sn, sb in sidecars:
                (tmp / Path(sn).name).write_bytes(sb)
            found = _read_path(p, name)
    out = []
    for lab, g in found:
        try:
            g = _clean_gdf(g)
        except Exception:
            continue
        try:
            cl = centerline(g)
        except Exception:
            cl = None
        try:
            area = footprint_area_ha(g)
        except Exception:
            area = None
        out.append((lab, g, cl, area))
    if not out:
        raise ValueError("no features with a location")
    return out


def _vector_and_length(name, data):
    """Kept for older callers: first layer only."""
    lab, g, cl, area = _read_any(name, data)[0]
    return g, cl, area


def footprint_length(gdf):
    """For other tabs: centerline text for a GeoDataFrame."""
    try:
        return length_text(centerline(gdf))
    except Exception:
        return ""


@st.cache_data(show_spinner="Reading imagery...", max_entries=10)
def _read_raster(data):
    if not RASTER_OK:
        raise ValueError("rasterio not installed, so imagery can't be shown")
    from PIL import Image
    with rasterio.MemoryFile(data) as mem, mem.open() as src:
        if src.crs is None:
            raise ValueError("this image has no coordinate system (not georeferenced)")
        w, s, e, n = transform_bounds(src.crs, "EPSG:4326", *src.bounds)
        scale = max(src.width, src.height) / 2000
        out_w = max(1, int(src.width / max(scale, 1)))
        out_h = max(1, int(src.height / max(scale, 1)))
        bands = [1, 2, 3] if src.count >= 3 else [1]
        arr = src.read(bands, out_shape=(len(bands), out_h, out_w), masked=True).astype("float32")
        mask = np.ma.getmaskarray(arr).any(axis=0)
        arr = arr.filled(np.nan)
    lo, hi = np.nanpercentile(arr, 2), np.nanpercentile(arr, 98)
    arr = np.nan_to_num(np.clip((arr - lo) / max(hi - lo, 1e-6) * 255, 0, 255)).astype("uint8")
    img = arr[0] if len(bands) == 1 else np.moveaxis(arr, 0, -1)
    im = Image.fromarray(img).convert("RGBA")
    if mask.any():
        a = np.where(mask, 0, 255).astype("uint8")
        im.putalpha(Image.fromarray(a))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue(), [[s, w], [n, e]]


def _group_uploads(ups):
    """Pair loose shapefile parts (.shp + .dbf + .prj ...) and return [(name, bytes, sidecars)]."""
    files = {u.name: u.getvalue() for u in ups or []}
    side = {k: v for k, v in files.items() if SIDECAR_RE.search(k)}
    out, used = [], set()
    for k, v in files.items():
        if k in side:
            continue
        stem = re.sub(r"\.[^.]+$", "", k).lower()
        sc = tuple((sk, sv) for sk, sv in side.items() if SIDECAR_RE.sub("", sk).lower() == stem)
        used.update(sk for sk, _ in sc)
        out.append((k, v, sc))
    lonely = [k for k in side if k not in used and not k.lower().endswith(".xml")]
    return out, lonely


# ---------------------------------------------------------------------------
# Elevation (free terrain tiles, no key needed)
# ---------------------------------------------------------------------------
DEM_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"


def _tile_xy(lon, lat, z):
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    return x, y


def _tile_lat(y, z):
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / 2 ** z))))


def _fetch_tile(z, x, y):
    import requests
    from PIL import Image
    r = requests.get(DEM_URL.format(z=z, x=x, y=y), timeout=20)
    r.raise_for_status()
    a = np.asarray(Image.open(io.BytesIO(r.content)).convert("RGB"), dtype=np.float32)
    return a[..., 0] * 256.0 + a[..., 1] + a[..., 2] / 256.0 - 32768.0


@st.cache_data(show_spinner=False, max_entries=400)
def _dem_tile(z, x, y):
    return _fetch_tile(z, x, y)


def _pick_zoom(w, s, e, n, max_tiles, zmax=14):
    for z in range(zmax, 4, -1):
        x0, y0 = _tile_xy(w, n, z)
        x1, y1 = _tile_xy(e, s, z)
        if (int(x1) - int(x0) + 1) * (int(y1) - int(y0) + 1) <= max_tiles:
            return z
    return 5


@st.cache_data(show_spinner="Loading elevation...", max_entries=20)
def _dem_window(w, s, e, n, max_tiles=30):
    """Elevation grid for a box. Returns lon (cols), lat (rows, south to north), elev (rows, cols), z, pixel_m."""
    from concurrent.futures import ThreadPoolExecutor
    z = _pick_zoom(w, s, e, n, max_tiles)
    x0, y0 = _tile_xy(w, n, z)
    x1, y1 = _tile_xy(e, s, z)
    tx0, ty0, tx1, ty1 = int(x0), int(y0), int(x1), int(y1)
    jobs = [(z, tx, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)]
    with ThreadPoolExecutor(8) as ex:
        tiles = list(ex.map(lambda j: _fetch_tile(*j), jobs))
    H, W = (ty1 - ty0 + 1) * 256, (tx1 - tx0 + 1) * 256
    mos = np.empty((H, W), np.float32)
    for (zz, tx, ty), t in zip(jobs, tiles):
        mos[(ty - ty0) * 256:(ty - ty0 + 1) * 256, (tx - tx0) * 256:(tx - tx0 + 1) * 256] = t
    lon = (tx0 * 256 + np.arange(W) + 0.5) / (256 * 2 ** z) * 360 - 180
    lat = np.array([_tile_lat(ty0 + (i + 0.5) / 256, z) for i in range(H)])
    ci = np.flatnonzero((lon >= w) & (lon <= e))
    ri = np.flatnonzero((lat >= s) & (lat <= n))
    if len(ci) < 3 or len(ri) < 3:
        ci, ri = np.arange(W), np.arange(H)
    mos = mos[ri[0]:ri[-1] + 1, ci[0]:ci[-1] + 1][::-1]
    lat = lat[ri[0]:ri[-1] + 1][::-1]
    lon = lon[ci[0]:ci[-1] + 1]
    mos[(mos < -500) | (mos > 9000)] = np.nan
    pixel_m = 40075016.7 * math.cos(math.radians((s + n) / 2)) / (256 * 2 ** z)
    return {"lon": lon, "lat": lat, "elev": mos, "z": z, "pixel_m": pixel_m}


def _smooth(a, passes=2):
    k = np.array([1, 2, 3, 2, 1], np.float32) / 9.0
    a = np.where(np.isfinite(a), a, np.nanmean(a))
    for _ in range(passes):
        a = np.apply_along_axis(lambda r: np.convolve(np.pad(r, 2, mode="edge"), k, "valid"), 1, a)
        a = np.apply_along_axis(lambda r: np.convolve(np.pad(r, 2, mode="edge"), k, "valid"), 0, a)
    return a


def _nice_interval(relief, target=14):
    raw = max(relief, 1) / target
    for v in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500):
        if v >= raw:
            return v
    return 1000


# Contour colours, low to high. Drawn with a dark edge so they show on imagery.
RAMP = ["#40C4FF", "#B2FF59", "#FFFF00", "#FFAB40", "#FF80AB"]


def _ramp(t):
    t = min(max(t, 0.0), 1.0) * (len(RAMP) - 1)
    i = min(int(t), len(RAMP) - 2)
    f = t - i
    a = [int(RAMP[i][k:k + 2], 16) for k in (1, 3, 5)]
    b = [int(RAMP[i + 1][k:k + 2], 16) for k in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * f):02x}" for x, y in zip(a, b))


@st.cache_data(show_spinner="Drawing contours...", max_entries=20)
def _contours(bbox, interval=None):
    import contourpy
    from shapely.geometry import LineString, mapping
    d = _dem_window(*bbox)
    zz = _smooth(d["elev"], passes=2 if d["pixel_m"] < 15 else 1)
    lo, hi = float(np.nanpercentile(zz, 1)), float(np.nanpercentile(zz, 99))
    iv = interval or _nice_interval(hi - lo)
    levels = np.arange(math.ceil(np.nanmin(zz) / iv) * iv, np.nanmax(zz) + 1e-6, iv)
    while len(levels) > 120:
        iv *= 2
        levels = np.arange(math.ceil(np.nanmin(zz) / iv) * iv, np.nanmax(zz) + 1e-6, iv)
    gen = contourpy.contour_generator(d["lon"], d["lat"], zz, line_type="Separate")
    tol = abs(d["lon"][1] - d["lon"][0]) * 0.6
    emin, emax = (float(levels[0]), float(levels[-1])) if len(levels) else (lo, hi)
    feats = []
    for lv in levels:
        idx = int(round(lv / iv)) % 5 == 0
        for seg in gen.lines(lv):
            if len(seg) < 4:
                continue
            ln = LineString(seg).simplify(tol)
            if ln.length < tol * 4:
                continue
            feats.append({"type": "Feature",
                          "properties": {"elev": f"{lv:,.0f} m", "v": float(lv), "idx": idx,
                                         "c": _ramp((lv - emin) / max(emax - emin, 1e-6))},
                          "geometry": mapping(ln)})
    return {"type": "FeatureCollection", "features": feats}, iv, emin, emax


def elevation_at(points, max_tiles=40):
    """[(lat, lon), ...] -> elevations (m), bilinear from terrain tiles. NaN where it fails."""
    pts = np.asarray(points, float)
    if not len(pts):
        return np.array([])
    s, n = pts[:, 0].min(), pts[:, 0].max()
    w, e = pts[:, 1].min(), pts[:, 1].max()
    z = _pick_zoom(w, s, e, n, max_tiles)
    out = np.full(len(pts), np.nan)
    tiles = {}
    for i, (la, lo) in enumerate(pts):
        fx, fy = _tile_xy(lo, la, z)
        px, py = fx * 256 - 0.5, fy * 256 - 0.5
        x0, y0 = int(math.floor(px)), int(math.floor(py))
        vals = []
        for dy in (0, 1):
            for dx in (0, 1):
                gx, gy = x0 + dx, y0 + dy
                key = (gx // 256, gy // 256)
                if key not in tiles:
                    try:
                        tiles[key] = _dem_tile(z, key[0], key[1])
                    except Exception:
                        tiles[key] = None
                t = tiles[key]
                vals.append(np.nan if t is None else float(t[gy % 256, gx % 256]))
        ax, ay = px - x0, py - y0
        out[i] = (vals[0] * (1 - ax) * (1 - ay) + vals[1] * ax * (1 - ay)
                  + vals[2] * (1 - ax) * ay + vals[3] * ax * ay)
    return out


def _footprint_elev(gdf, dem):
    """(min, max) elevation inside polygons, or along lines/points (25 m buffer)."""
    try:
        utm = gdf.estimate_utm_crs()
        g = gdf.to_crs(utm)
        geom = shapely.union_all([x if x.geom_type in ("Polygon", "MultiPolygon") else x.buffer(25)
                                  for x in g.geometry.values])
        geom = gpd.GeoSeries([geom], crs=utm).to_crs(4326).iloc[0]
        X, Y = np.meshgrid(dem["lon"], dem["lat"])
        inside = shapely.contains_xy(geom, X, Y) & np.isfinite(dem["elev"])
        if not inside.any():
            c = geom.representative_point()
            v = elevation_at([(c.y, c.x)])[0]
            return (v, v) if np.isfinite(v) else None
        v = dem["elev"][inside]
        return float(v.min()), float(v.max())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Nearest town / city
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def _places():
    rows = [ln.split("|") for ln in PLACES_TXT.strip().splitlines()]
    df = pd.DataFrame(rows, columns=["name", "prov", "kind", "lat", "lon"])
    df["lat"] = df["lat"].astype(float)
    df["lon"] = df["lon"].astype(float)
    df["kind"] = df["kind"].map({"C": "City", "T": "Town", "V": "Community"})
    return df


DIRS = ["north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"]


def _bearing_word(lat1, lon1, lat2, lon2):
    """Direction of point 2 as seen from point 1."""
    y = math.sin(math.radians(lon2 - lon1)) * math.cos(math.radians(lat2))
    x = (math.cos(math.radians(lat1)) * math.sin(math.radians(lat2))
         - math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.cos(math.radians(lon2 - lon1)))
    b = (math.degrees(math.atan2(y, x)) + 360) % 360
    return DIRS[int((b + 22.5) // 45) % 8]


def nearest_places(geom):
    """geom in EPSG:4326 -> dict with nearest 'town' (town or city), 'city', 'community', each a dict, plus line ends."""
    df = _places()
    c = geom.centroid
    cand = df[(df.lat - c.y).abs().lt(4) & (df.lon - c.x).abs().lt(7)].copy()
    if cand.empty:
        return {}
    gs = gpd.GeoSeries([geom], crs=4326)
    utm = gs.estimate_utm_crs()
    g = gs.to_crs(utm).iloc[0]
    pts = gpd.GeoSeries(gpd.points_from_xy(cand.lon, cand.lat), crs=4326).to_crs(utm)
    cand["dist"] = pts.distance(g).values
    cand["pt"] = list(pts.values)
    out = {}
    from shapely.ops import nearest_points
    for key, mask in (("town", cand.kind.isin(["Town", "City"])), ("city", cand.kind.eq("City")),
                      ("community", cand.kind.eq("Community"))):
        sub = cand[mask]
        if sub.empty:
            continue
        r = sub.loc[sub["dist"].idxmin()]
        a, _ = nearest_points(g, r["pt"])
        a = gpd.GeoSeries([a], crs=utm).to_crs(4326).iloc[0]
        out[key] = {"name": r["name"], "kind": r["kind"], "prov": PROVINCES.get(r["prov"], r["prov"]),
                    "km": r["dist"] / 1000.0, "lat": r["lat"], "lon": r["lon"],
                    "dir": _bearing_word(r["lat"], r["lon"], c.y, c.x), "near": (a.y, a.x)}
    return out


def _km(v):
    return f"{v:.1f} km" if v < 10 else f"{v:.0f} km"


def place_sentence(near, subject="The Project"):
    t = near.get("town")
    if not t:
        return ""
    if t["km"] < 0.05:
        return f"{subject} is located within {t['name']}, {t['prov']}."
    return f"{subject} is located approximately {_km(t['km'])} {t['dir']} of {t['name']}, {t['prov']}."


# ---------------------------------------------------------------------------
# Map
# ---------------------------------------------------------------------------
PALETTE = ["#FF1744", "#00E5FF", "#FFEA00", "#76FF03", "#FF9100", "#D500F9",
           "#2979FF", "#FF4081", "#1DE9B6", "#C6FF00", "#FFFFFF", "#FFAB00"]


IMAGERY = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer"

# Leaflet.TileLayer.Fallback 1.0.4 (c) Boris Seang, Apache-2.0: when a tile is missing,
# show the next zoom out, scaled up, instead of a blank.
FALLBACK_JS = r"""/*!
 Leaflet.TileLayer.Fallback 1.0.4+e36cde9
 (c) 2015-2018 Boris Seang
 License Apache-2.0
 */
!function(i,e){"function"==typeof define&&define.amd?define(["leaflet"],e):e("object"==typeof module&&module.exports?require("leaflet"):i.L)}(this,function(i){i.TileLayer.Fallback=i.TileLayer.extend({options:{minNativeZoom:0},initialize:function(e,r){i.TileLayer.prototype.initialize.call(this,e,r)},createTile:function(e,r){var t=i.TileLayer.prototype.createTile.call(this,e,r);return t._originalCoords=e,t._originalSrc=t.src,t},_createCurrentCoords:function(i){var e=this._wrapCoords(i);return e.fallback=!0,e},_originalTileOnError:i.TileLayer.prototype._tileOnError,_tileOnError:function(i,e,r){var t,l,o,a=this,n=e._originalCoords,c=e._currentCoords=e._currentCoords||a._createCurrentCoords(n),s=e._fallbackZoom=void 0===e._fallbackZoom?n.z-1:e._fallbackZoom-1,f=e._fallbackScale=2*(e._fallbackScale||1),p=a.getTileSize(),u=e.style;if(s<a.options.minNativeZoom)return this._originalTileOnError(i,e,r);c.z=s,c.x=Math.floor(c.x/2),c.y=Math.floor(c.y/2),t=a.getTileUrl(c),u.width=p.x*f+"px",u.height=p.y*f+"px",l=(n.y-c.y*f)*p.y,u.marginTop=-l+"px",o=(n.x-c.x*f)*p.x,u.marginLeft=-o+"px",u.clip="rect("+l+"px "+(o+p.x)+"px "+(l+p.y)+"px "+o+"px)",a.fire("tilefallback",{tile:e,url:e._originalSrc,urlMissing:e.src,urlFallback:t}),e.src=t},getTileUrl:function(e){var r=e.z=e.fallback?e.z:this._getZoomForUrl(),t={r:i.Browser.retina?"@2x":"",s:this._getSubdomain(e),x:e.x,y:e.y,z:r};if(this._map&&!this._map.options.crs.infinite){var l=this._globalTileRange.max.y-e.y;this.options.tms&&(t.y=l),t["-y"]=l}return i.Util.template(this._url,i.extend(t,this.options))}}),i.tileLayer.fallback=function(e,r){return new i.TileLayer.Fallback(e,r)}});"""


class FallbackTileLayer(folium.TileLayer):
    """Tile layer that fills missing close-up tiles with the zoomed-out tile instead of a blank."""
    _template = Template("""
        {% macro script(this, kwargs) %}
            if (!L.TileLayer.Fallback) { """ + FALLBACK_JS + """ }
            var {{ this.get_name() }} = L.tileLayer.fallback(
                {{ this.tiles|tojson }},
                {{ this.options|tojavascript }}
            );
        {% endmacro %}
        """)


@st.cache_data(show_spinner=False, max_entries=200, ttl=7 * 24 * 3600)
def _imagery_max_zoom(lat, lon):
    """Deepest zoom Esri has real imagery for at this spot (checks Esri's tile map). None if unknown."""
    import requests
    for z in (19, 18, 17, 16, 15):
        x, y = _tile_xy(lon, lat, z)
        try:
            r = requests.get(f"{IMAGERY}/tilemap/{z}/{int(y)}/{int(x)}/1/1", timeout=5)
            if r.ok and (r.json().get("data") or [0])[0] == 1:
                return z
        except Exception:
            return None
    return None


def _base_map(center, zoom, native=None):
    m = folium.Map(location=center, zoom_start=zoom, tiles=None, control_scale=True, max_zoom=21)
    FallbackTileLayer(
        tiles=IMAGERY + "/tile/{z}/{y}/{x}?blankTile=false",
        attr="Esri, Maxar, Earthstar Geographics", name="Satellite", max_zoom=21,
        max_native_zoom=native or 19).add_to(m)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
        attr="Esri", name="Topo", max_zoom=21, max_native_zoom=19, show=False).add_to(m)
    folium.TileLayer("OpenStreetMap", name="Streets", max_zoom=21, max_native_zoom=19, show=False).add_to(m)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/Elevation/World_Hillshade/MapServer/tile/{z}/{y}/{x}",
        attr="Esri", name="Hillshade", overlay=True, show=False, opacity=0.45,
        max_zoom=21, max_native_zoom=16).add_to(m)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}",
        attr="Esri", name="Roads", overlay=True, show=False, max_zoom=21, max_native_zoom=19).add_to(m)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
        attr="Esri", name="Labels", overlay=True, control=True, max_zoom=21, max_native_zoom=19).add_to(m)
    for p in (plugins.Fullscreen(),
              plugins.MousePosition(position="bottomleft", separator=", ", num_digits=6, prefix=""),
              plugins.LocateControl(flyTo=True),
              plugins.MeasureControl(primary_length_unit="meters", secondary_length_unit="kilometers",
                                     primary_area_unit="hectares")):
        try:
            p.add_to(m)
        except Exception:
            pass
    try:
        plugins.Geocoder(collapsed=True, position="topleft", add_marker=False).add_to(m)
    except Exception:
        pass
    return m


def _style_for(color, field=None, cmap=None):
    def f(feat):
        c = color
        if field and cmap is not None:
            c = cmap.get(str(feat["properties"].get(field)), "#9E9E9E")
        gt = (feat.get("geometry") or {}).get("type", "")
        if "Point" in gt:
            return {"color": "#000000", "weight": 1.5, "fillColor": c, "fillOpacity": 1, "radius": 7}
        if "Line" in gt:
            return {"color": c, "weight": 4, "opacity": 1}
        return {"color": c, "weight": 3, "opacity": 1, "fillColor": c, "fillOpacity": 0.28}
    return f


def _halo(feat):
    gt = (feat.get("geometry") or {}).get("type", "")
    if "Point" in gt:
        return {"color": "#000000", "weight": 0, "fillOpacity": 0, "opacity": 0}
    return {"color": "#000000", "weight": 7 if "Line" in gt else 6, "opacity": 0.55, "fillOpacity": 0}


def _add_vector(m, lay):
    gdf = lay["gdf"]
    if len(gdf) > 5000:
        gdf = gdf.iloc[:5000]
    gj = gdf.__geo_interface__
    fg = folium.FeatureGroup(name=f'<span style="color:{lay["color"]}">&#9632;</span> {lay["label"]}', show=True)
    fields = [c for c in gdf.columns if c != "geometry"][:8]
    if not gdf.geom_type.str.contains("Point").all():
        folium.GeoJson(gj, style_function=_halo, marker=folium.CircleMarker(radius=1),
                       control=False).add_to(fg)
    folium.GeoJson(
        gj, style_function=_style_for(lay["color"], lay.get("field"), lay.get("cmap")),
        marker=folium.CircleMarker(radius=7, fill=True),
        highlight_function=lambda f: {"weight": 6, "fillOpacity": 0.45},
        tooltip=folium.GeoJsonTooltip(fields=fields[:5]) if fields else None,
        popup=folium.GeoJsonPopup(fields=fields) if fields else None,
    ).add_to(fg)
    cl = lay.get("cl")
    if cl:
        folium.GeoJson(
            cl["lines"].__geo_interface__,
            style_function=lambda f: {"color": "#FFFFFF", "weight": 2, "dashArray": "6 6"},
            tooltip=f"Centerline {cl['longest']:,.0f} m ({cl['longest'] / 1000:.2f} km)",
        ).add_to(fg)
    fg.add_to(m)


def _ats_grid_layer(bbox, limit=3500):
    g = _grid()
    if g is None:
        return None, 0
    w, s, e, n = bbox
    sel = np.flatnonzero((g["lon"] > w) & (g["lon"] < e) & (g["lat"] > s) & (g["lat"] < n))
    if len(sel) > limit or not len(sel):
        return None, len(sel)
    feats = []
    for i in sel:
        ring = [[float(p[0]), float(p[1])] for p in g["corners"][i][[0, 1, 3, 2, 0]]]
        feats.append({"type": "Feature", "properties": {"ATS": ats_label(dict(_decode(g["keys"][i]), lsd=None))},
                      "geometry": {"type": "Polygon", "coordinates": [ring]}})
    return {"type": "FeatureCollection", "features": feats}, len(sel)


def _pad_bbox(b, frac=0.25, min_m=800):
    w, s, e, n = b
    lat = (s + n) / 2
    dy = max((n - s) * frac, min_m / 111320)
    dx = max((e - w) * frac, min_m / max(_m_per_deg_lon(lat), 1))
    return (w - dx, s - dy, e + dx, n + dy)


def _union_bounds(bs):
    bs = [b for b in bs if b]
    if not bs:
        return None
    return (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs))


def _profile(a, b, n=200):
    la = np.linspace(a[0], b[0], n)
    lo = np.linspace(a[1], b[1], n)
    k = _m_per_deg_lon((a[0] + b[0]) / 2)
    seg = np.hypot(np.diff(la) * 111320, np.diff(lo) * k)
    dist = np.concatenate([[0], np.cumsum(seg)])
    return dist, elevation_at(list(zip(la, lo)), max_tiles=60)


def _pt(text):
    r = convert_one(text) if text and str(text).strip() else None
    return (r["lat"], r["lon"]) if r else None


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
                           "section, township, range, meridian. Section only also works (16-75-10-W6M). "
                           "To search a town name, use the magnifier on the map.")
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
        "Drag in GIS files or imagery", accept_multiple_files=True, key="map_files",
        help="Any common GIS file: shapefile (.zip, or drop the .shp, .dbf, .shx and .prj together), KML, KMZ, "
             "GPX, GeoJSON, GeoPackage, file geodatabase (.zip), CSV or Excel with lat/long or ATS columns, "
             "GeoTIFF imagery. Each file gets its own colour.")

    # ---- read everything -------------------------------------------------
    layers, rasters, notes = [], [], []
    groups, lonely = _group_uploads(ups)
    if lonely:
        notes.append("Shapefile parts without a .shp: " + ", ".join(lonely)
                     + ". Drop the .shp with them, or zip them together.")
    for name, data, sidecars in groups:
        low = name.lower()
        try:
            if low.endswith(RASTER_EXT):
                png, b = _read_raster(data)
                rasters.append({"label": name, "png": png, "b": b})
            elif low.endswith((".jpg", ".jpeg", ".png", ".heic", ".pdf", ".docx")):
                notes.append(f"{name}: not a GIS file (no location). For imagery use a GeoTIFF.")
            elif low.endswith(".shp") and not any(s[0].lower().endswith(".dbf") for s in sidecars):
                notes.append(f"{name}: also drop the matching .dbf, .shx and .prj (or zip them together).")
            else:
                for lab, g, cl, area in _read_any(name, data, sidecars):
                    layers.append({"label": lab, "gdf": g, "cl": cl, "area": area})
        except Exception as e:
            notes.append(f"{name}: couldn't load ({e})")

    # ---- options ---------------------------------------------------------
    if layers:
        with st.expander("Layer colours", expanded=False):
            for i, lay in enumerate(layers):
                cc1, cc2 = st.columns([1, 3])
                lay["color"] = cc1.color_picker(lay["label"][:40], PALETTE[i % len(PALETTE)], key=f"map_col_{i}")
                cats = [c for c in lay["gdf"].columns if c != "geometry"
                        and 2 <= lay["gdf"][c].nunique() <= 12]
                fld = cc2.selectbox("Colour by field", ["(one colour)"] + cats, key=f"map_fld_{i}")
                if fld != "(one colour)":
                    vals = sorted(lay["gdf"][fld].astype(str).unique())
                    lay["field"] = fld
                    lay["cmap"] = {v: PALETTE[k % len(PALETTE)] for k, v in enumerate(vals)}
                    cc2.markdown(" ".join(f'<span style="background:{c};color:#000;padding:1px 6px;'
                                          f'border-radius:4px;margin-right:4px">{v}</span>'
                                          for v, c in lay["cmap"].items()), unsafe_allow_html=True)
    for i, lay in enumerate(layers):
        lay.setdefault("color", ss.get(f"map_col_{i}", PALETTE[i % len(PALETTE)]))

    o1, o2, o3, o4 = st.columns([1, 1, 1, 2])
    show_cont = o1.checkbox("Contour lines", value=True, key="map_cont",
                            help="Elevation contours around your files (or the search point). "
                                 "Colours go from blue (low) to pink (high). Lines are labelled with the elevation.")
    iv_pick = o2.selectbox("Interval", ["Auto", 1, 2, 5, 10, 25, 50, 100], key="map_iv",
                           help="Contour spacing in metres.")
    show_ats = o3.checkbox("ATS grid", value=True, key="map_ats", help="Quarter-section lines around your files.")
    mode = o4.radio("Map click", ["Lat/long", "Point A", "Point B"], horizontal=True, key="map_mode",
                    help="Lat/long: shows coordinates, ATS and elevation. Point A / B: sets the ends of the "
                         "elevation profile below.")

    # ---- area of interest ------------------------------------------------
    t = ss["map_target"]
    vb = _union_bounds([tuple(l["gdf"].total_bounds) for l in layers]
                       + [(r["b"][0][1], r["b"][0][0], r["b"][1][1], r["b"][1][0]) for r in rasters])
    aoi = _pad_bbox(vb) if vb else (_pad_bbox((t["lon"], t["lat"], t["lon"], t["lat"]), min_m=1500) if t else None)

    if vb:
        ctr = ((vb[1] + vb[3]) / 2, (vb[0] + vb[2]) / 2)
    elif t:
        ctr = (t["lat"], t["lon"])
    else:
        ctr = tuple(ss["map_center"])
    native = _imagery_max_zoom(round(ctr[0], 3), round(ctr[1], 3))
    m = _base_map(ss["map_center"], ss["map_zoom"], native)

    for r in rasters:
        url = "data:image/png;base64," + base64.b64encode(r["png"]).decode()
        folium.raster_layers.ImageOverlay(url, bounds=r["b"], name=r["label"], opacity=0.9).add_to(m)

    if aoi and show_ats:
        gj, cnt = _ats_grid_layer(aoi)
        if gj:
            folium.GeoJson(gj, name="ATS grid",
                           style_function=lambda f: {"color": "#FFFFFF", "weight": 1, "opacity": 0.6,
                                                     "fillOpacity": 0, "dashArray": "4 4"},
                           highlight_function=lambda f: {"weight": 2.5, "opacity": 1},
                           tooltip=folium.GeoJsonTooltip(fields=["ATS"], labels=False)).add_to(m)
        elif cnt:
            notes.append("ATS grid hidden: area is too big to draw every quarter.")

    dem = None
    cont_info = None
    if aoi and show_cont:
        try:
            gj, iv, emin, emax = _contours(tuple(round(v, 5) for v in aoi),
                                           None if iv_pick == "Auto" else float(iv_pick))
            dem = _dem_window(*tuple(round(v, 5) for v in aoi))
            fg = folium.FeatureGroup(name=f"Contours ({iv:g} m)", show=True)
            # dark edge under every line, then the coloured line on top
            folium.GeoJson(gj, style_function=lambda f: {"color": "#000000",
                                                         "weight": 5 if f["properties"]["idx"] else 3.5,
                                                         "opacity": 0.6},
                           control=False).add_to(fg)
            folium.GeoJson(gj, style_function=lambda f: {"color": f["properties"]["c"],
                                                         "weight": 3 if f["properties"]["idx"] else 1.8,
                                                         "opacity": 1},
                           highlight_function=lambda f: {"weight": 5},
                           tooltip=folium.GeoJsonTooltip(fields=["elev"], labels=False)).add_to(fg)
            # labels: every line if there aren't many, else the thick ones; repeated along long lines
            feats = gj["features"]
            lab = feats if len(feats) <= 60 else [f for f in feats if f["properties"]["idx"]]
            step = max(aoi[2] - aoi[0], (aoi[3] - aoi[1]) * 1.6) / 4      # roughly a label every quarter of the view
            n_lab = 0
            for f in sorted(lab, key=lambda f: -len(f["geometry"]["coordinates"])):
                xy = f["geometry"]["coordinates"]
                piece, plen = [xy[0]], 0.0
                for (x0, y0), (x1, y1) in zip(xy[:-1], xy[1:]):
                    piece.append((x1, y1))
                    plen += math.hypot((x1 - x0) * 0.6, y1 - y0)
                    if plen >= step or (x1, y1) == tuple(xy[-1]):
                        if len(piece) >= 2 and plen > step * 0.15 and n_lab < 150:
                            if piece[-1][0] < piece[0][0]:
                                piece = piece[::-1]                          # keep text upright
                            pl = folium.PolyLine([(y, x) for x, y in piece], weight=0, opacity=0)
                            pl.add_to(fg)
                            plugins.PolyLineTextPath(
                                pl, f"{f['properties']['elev']}", center=True, offset=5,
                                attributes={"fill": "#FFFFFF", "font-size": "15", "font-weight": "bold",
                                            "stroke": "#000000", "stroke-width": "3.5",
                                            "paint-order": "stroke", "stroke-linejoin": "round"}).add_to(fg)
                            n_lab += 1
                        piece, plen = [(x1, y1)], 0.0
            fg.add_to(m)
            cont_info = (iv, emin, emax)
        except Exception as e:
            notes.append(f"Contours: elevation data couldn't be loaded ({type(e).__name__}).")

    for lay in layers:
        _add_vector(m, lay)

    # nearest town (all files together) and elevation per file
    near = {}
    if layers:
        try:
            near = nearest_places(shapely.union_all([shapely.union_all(l["gdf"].geometry.values) for l in layers]))
            tw = near.get("town")
            if tw and tw["km"] > 0.05:
                folium.PolyLine([tw["near"], (tw["lat"], tw["lon"])], color="#FFFFFF", weight=2,
                                dash_array="2 8", tooltip=f"{_km(tw['km'])} to {tw['name']}").add_to(m)
                folium.CircleMarker((tw["lat"], tw["lon"]), radius=7, color="#000000", weight=2, fill=True,
                                    fill_color="#FFFFFF", fill_opacity=1,
                                    tooltip=f"{tw['name']} ({tw['kind']})").add_to(m)
        except Exception:
            near = {}
    if dem is not None:
        for lay in layers:
            lay["elev"] = _footprint_elev(lay["gdf"], dem)

    # profile A-B
    pa, pb = _pt(ss.get("prof_a_txt")), _pt(ss.get("prof_b_txt"))
    for p, lbl, col in ((pa, "A", "green"), (pb, "B", "red")):
        if p:
            folium.Marker(p, tooltip=f"Point {lbl}",
                          icon=folium.DivIcon(html=f'<div style="background:{col};color:#fff;border:2px solid #fff;'
                                                   f'border-radius:50%;width:24px;height:24px;text-align:center;'
                                                   f'font:bold 14px/20px sans-serif;margin:-12px 0 0 -12px">'
                                                   f'{lbl}</div>')).add_to(m)
    if pa and pb:
        folium.PolyLine([pa, pb], color="#FFEA00", weight=3, dash_array="8 6", tooltip="Elevation profile").add_to(m)

    if t:
        tip = f"{t['lat']:.6f}, {t['lon']:.6f}<br>{t['ats']}"
        folium.Marker([t["lat"], t["lon"]], tooltip=tip, popup=tip,
                      icon=folium.Icon(color="red", icon="map-marker")).add_to(m)
        if t.get("poly"):
            folium.Polygon(t["poly"], color="#FF1744", weight=3, fill=True, fill_opacity=0.08,
                           tooltip=t["ats"]).add_to(m)

    if vb:
        m.fit_bounds([[vb[1], vb[0]], [vb[3], vb[2]]], padding=(20, 20))
    folium.LayerControl(collapsed=True).add_to(m)

    # legend chips
    chips = [f'<span style="display:inline-block;width:14px;height:14px;background:{l["color"]};'
             f'border:2px solid #000;margin:0 5px -2px 0"></span>{l["label"]}' for l in layers]
    if cont_info:
        iv, emin, emax = cont_info
        chips.append(f'<span style="display:inline-block;width:70px;height:10px;margin:0 5px 0 0;'
                     f'background:linear-gradient(90deg,{",".join(RAMP)})"></span>'
                     f'Contours {emin:,.0f} to {emax:,.0f} m, every {iv:g} m')
    if chips:
        st.markdown('<div style="line-height:2">' + " &nbsp;&nbsp; ".join(chips) + "</div>",
                    unsafe_allow_html=True)
    for n in notes:
        st.caption(n)

    out = st_folium(m, height=650, use_container_width=True, returned_objects=["last_clicked"], key="map_view")

    # ---- click -----------------------------------------------------------
    clicked = (out or {}).get("last_clicked")
    if clicked:
        lat, lon = clicked["lat"], clicked["lng"]
        sig = (round(lat, 7), round(lon, 7))
        if mode in ("Point A", "Point B") and ss.get("map_click_used") != sig:
            ss["map_click_used"] = sig
            ss["prof_a_txt" if mode == "Point A" else "prof_b_txt"] = f"{lat:.6f}, {lon:.6f}"
            st.rerun()
        a, src = latlon_to_ats(lat, lon)
        ats_txt = (f"{ats_label(dict(a, lsd=None))}   |   LSD {ats_label(dict(a, quarter=None))}"
                   if a else "outside the Alberta ATS grid")
        try:
            el = elevation_at([(lat, lon)])[0]
            el_txt = f"   |   Elevation ~{el:,.0f} m" if np.isfinite(el) else ""
        except Exception:
            el_txt = ""
        st.success(f"Clicked: **{lat:.6f}, {lon:.6f}**   |   {ats_txt}{el_txt}")

    if t:
        st.caption(f"Search result: {t['lat']:.6f}, {t['lon']:.6f}  |  {t['ats']}"
                   + (f"  |  LSD {t['lsd']}" if t.get("lsd") and t["kind"] == "Lat, Long" else "")
                   + (f"  ({t['source']})" if t.get("source") else ""))

    # ---- footprint summary -----------------------------------------------
    if layers:
        st.markdown("---")
        st.subheader("Footprint summary")
        for lay in layers:
            rows = []
            if lay["area"] is not None:
                rows.append(f"Footprint area: **{lay['area']:,.3f} ha**")
            if lay["cl"]:
                rows.append(length_text(lay["cl"]))
            if lay.get("elev"):
                lo, hi = lay["elev"]
                rows.append(f"Elevation: {lo:,.0f} to {hi:,.0f} m ({hi - lo:,.0f} m change)" if hi - lo >= 1
                            else f"Elevation: ~{lo:,.0f} m")
            st.markdown(f'<span style="display:inline-block;width:12px;height:12px;background:{lay["color"]};'
                        f'border:2px solid #000;margin-right:6px"></span>**{lay["label"]}**',
                        unsafe_allow_html=True)
            st.markdown("\n".join(f"- {r}" for r in rows) if rows else "- Points only, nothing to measure.")
        if near:
            st.markdown("**Location**")
            rows = []
            for key, lbl in (("town", "Nearest town or city"), ("city", "Nearest city"),
                             ("community", "Nearest community")):
                p = near.get(key)
                if key == "community" and p and near.get("town") and p["km"] >= near["town"]["km"]:
                    continue
                if p and not (key == "city" and near.get("town", {}).get("name") == p["name"]):
                    rows.append(f"{lbl}: {_km(p['km'])} {p['dir']} of {p['name']} ({p['kind']})"
                                if p["km"] >= 0.05 else f"{lbl}: within {p['name']} ({p['kind']})")
            st.markdown("\n".join(f"- {r}" for r in rows))
            sent = place_sentence(near)
            if sent:
                st.code(sent, language=None)
        st.caption("Distances are straight-line from the nearest edge of the footprint. "
                   "Elevation is from free terrain data (about 5 to 30 m detail), good for planning, not survey.")

    # ---- elevation profile -----------------------------------------------
    st.markdown("---")
    st.subheader("Elevation change between 2 points")
    st.caption("Type lat/long or ATS, or set the map click to Point A / Point B and click the map.")
    p1, p2, p3 = st.columns([2, 2, 1])
    p1.text_input("Point A", key="prof_a_txt", placeholder="55.1234, -118.5678 or NE-16-75-10-W6M")
    p2.text_input("Point B", key="prof_b_txt", placeholder="55.1400, -118.5300")
    p3.write("")
    p3.write("")
    if p3.button("Clear", key="prof_clear", use_container_width=True):
        ss["prof_a_txt"] = ""
        ss["prof_b_txt"] = ""
        st.rerun()
    if pa and pb:
        try:
            dist, el = _profile(pa, pb)
            ok = np.isfinite(el)
            if ok.sum() < 2:
                raise ValueError("no elevation data here")
            d = np.diff(el[ok])
            ea, eb = el[ok][0], el[ok][-1]
            run = dist[-1]
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("Point A", f"{ea:,.0f} m")
            k2.metric("Point B", f"{eb:,.0f} m")
            k3.metric("Change A to B", f"{eb - ea:+,.0f} m",
                      f"{(eb - ea) / run * 100:+.1f}% avg slope" if run > 0 else None, delta_color="off")
            k4.metric("Distance", f"{run:,.0f} m" if run < 10000 else f"{run / 1000:,.1f} km")
            st.caption(f"Highest {np.nanmax(el):,.0f} m, lowest {np.nanmin(el):,.0f} m, "
                       f"total climb {d[d > 0].sum():,.0f} m, total drop {-d[d < 0].sum():,.0f} m "
                       f"(going A to B). {_bearing_word(pa[0], pa[1], pb[0], pb[1]).capitalize()} from A to B.")
            df = pd.DataFrame({"Distance (m)": dist.round(0), "Elevation (m)": el.round(1)})
            st.line_chart(df, x="Distance (m)", y="Elevation (m)", height=240)
            st.download_button("Download profile (CSV)", df.to_csv(index=False).encode(), "elevation_profile.csv",
                               "text/csv", key="prof_dl")
        except Exception as e:
            st.warning(f"Couldn't get elevation for this line ({e}).")
    elif ss.get("prof_a_txt") or ss.get("prof_b_txt"):
        st.caption("Need both points (lat/long or ATS).")

    # ---- nearest town for a search point (no files) ------------------------
    if t and not layers:
        try:
            from shapely.geometry import Point
            sent = place_sentence(nearest_places(Point(t["lon"], t["lat"])), "The site")
            if sent:
                st.caption("Nearest town or city to the search point:")
                st.code(sent, language=None)
        except Exception:
            pass

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


# ---------------------------------------------------------------------------
# Places: name | province | C=city, T=town, V=other community | lat | lon
# From the Canadian Geographical Names Database (Natural Resources Canada).
# ---------------------------------------------------------------------------
PLACES_TXT = """
Manyberries|AB|V|49.4006|-110.6947
Etzikom|AB|V|49.4769|-111.1072
New Dayton|AB|V|49.4256|-112.3814
Wrentham|AB|V|49.5144|-112.1728
Raymond|AB|T|49.4650|-112.6611
Stirling|AB|V|49.5032|-112.5268
Aetna|AB|V|49.1339|-113.2400
Mountain View|AB|V|49.1344|-113.5964
Leavitt|AB|V|49.1709|-113.4531
Cardston|AB|T|49.1962|-113.3071
Elkwater|AB|V|49.6608|-110.2817
Coutts|AB|V|49.0028|-111.9589
Milk River|AB|T|49.1554|-112.0833
Warner|AB|V|49.2828|-112.2078
Magrath|AB|T|49.4170|-112.8662
Spring Coulee|AB|V|49.3331|-113.0527
Walsh|AB|V|49.9472|-110.0425
Irvine|AB|V|49.9553|-110.2767
Dunmore|AB|V|49.9703|-110.5842
Medicine Hat|AB|C|50.0408|-110.6767
Redcliff|AB|T|50.0775|-110.7856
Seven Persons|AB|V|49.8761|-110.9084
Bow Island|AB|T|49.8672|-111.3789
Grassy Lake|AB|V|49.8253|-111.6981
Johnson's Addition|AB|V|49.7827|-112.1769
Chin|AB|V|49.7635|-112.4449
Barnwell|AB|V|49.7606|-112.2633
Taber|AB|T|49.7847|-112.1510
Purple Springs|AB|V|49.8156|-111.8925
Burdett|AB|V|49.8350|-111.5234
Winnifred|AB|V|49.9067|-111.2103
Whitla|AB|V|49.8822|-111.0571
McNab|AB|V|49.3494|-112.2903
Hill Spring|AB|V|49.2931|-113.6222
Glenwood|AB|V|49.3659|-113.5149
Coaldale|AB|T|49.7278|-112.6186
Lethbridge|AB|C|49.6928|-112.8352
Coalhurst|AB|T|49.7467|-112.9307
Diamond City|AB|V|49.7956|-112.8389
Hartleyville|AB|V|49.3644|-113.6094
Ralston|AB|V|50.2458|-111.1686
Suffield|AB|V|50.2183|-111.1611
Hilda|AB|V|50.4797|-110.0328
Schuler|AB|V|50.3407|-110.0887
Pincher|AB|V|49.5248|-113.9467
Pincher Creek|AB|T|49.4853|-113.9475
Cowley|AB|V|49.5680|-114.0735
Chokio|AB|V|49.5819|-113.6597
Fort Macleod|AB|T|49.7231|-113.4042
Orton|AB|V|49.7320|-113.2641
Moon River Estates|AB|V|49.7286|-113.1486
McDermott|AB|V|49.7309|-112.9481
Sunset Acres|AB|V|49.6666|-112.9392
Monarch|AB|V|49.8044|-113.1078
Shaughnessy|AB|V|49.8528|-112.8422
Picture Butte|AB|T|49.8769|-112.7797
Beaver Mines|AB|V|49.4567|-114.1972
Lundbreck|AB|V|49.5845|-114.1642
Burmis|AB|V|49.5572|-114.2878
Hillcrest Mines|AB|V|49.5689|-114.3773
Bellevue|AB|V|49.5808|-114.3661
Frank|AB|V|49.6027|-114.4058
Blairmore|AB|V|49.6081|-114.4428
Coleman|AB|V|49.6353|-114.5031
Hazell|AB|V|49.6300|-114.6644
Waterton Park|AB|V|49.0517|-113.9142
Iron Springs|AB|V|49.9300|-112.6875
Turin|AB|V|49.9641|-112.5283
Fincastle|AB|V|49.8083|-112.0017
Empress|AB|V|50.9543|-110.0081
Acadia Valley|AB|V|51.1585|-110.2093
Oyen|AB|T|51.3528|-110.4833
Bindloss|AB|V|50.8800|-110.2736
Jenner|AB|V|50.7467|-111.1844
Iddesleigh|AB|V|50.7356|-111.3031
Patricia|AB|V|50.6994|-111.6769
Duchess|AB|V|50.7311|-111.9064
Rosemary|AB|V|50.7575|-112.0897
Brooks|AB|C|50.5672|-111.8950
Cassils|AB|V|50.5769|-112.0461
Rainier|AB|V|50.3683|-112.0925
Scandia|AB|V|50.2781|-112.0467
Rolling Hills|AB|V|50.2256|-111.7744
Vauxhall|AB|T|50.0683|-112.1067
Grantham|AB|V|50.0687|-112.0087
Hays|AB|V|50.0981|-111.7936
Tilley|AB|V|50.4522|-111.6533
Travers|AB|V|50.2481|-112.5549
Enchant|AB|V|50.1700|-112.4225
Lomond|AB|V|50.3494|-112.6406
Nobleford|AB|T|49.8803|-113.0542
Barons|AB|V|49.9975|-113.0806
Carmangay|AB|V|50.1294|-113.1114
Champion|AB|V|50.2397|-113.1481
Vulcan|AB|T|50.4042|-113.2606
Milo|AB|V|50.5700|-112.8819
Millicent|AB|V|50.7103|-111.7789
Granum|AB|T|49.8711|-113.5083
Claresholm|AB|T|50.0197|-113.5828
Stavely|AB|T|50.1611|-113.6417
Gleichen|AB|V|50.8661|-113.0547
Cluny|AB|V|50.8411|-112.8694
Siksika|AB|V|50.7924|-112.8547
Arrowwood|AB|V|50.7392|-113.1498
Shouldice|AB|V|50.7167|-112.9739
Bassano|AB|T|50.7875|-112.4683
Sibbald|AB|V|51.3883|-110.1503
Cereal|AB|V|51.4183|-110.8003
Chinook|AB|V|51.4494|-110.9253
Youngstown|AB|V|51.5278|-111.2000
Hussar|AB|V|51.0414|-112.6817
Standard|AB|V|51.1097|-112.9836
Namaka|AB|V|50.9583|-113.2871
New Brigden|AB|V|51.7006|-110.4900
Hanna|AB|T|51.6458|-111.9253
Craigmyle|AB|V|51.6697|-112.2431
Delia|AB|V|51.6306|-112.3739
Michichi|AB|V|51.5853|-112.5336
Munson|AB|V|51.5633|-112.7383
Morrin|AB|V|51.6603|-112.7672
Parkland|AB|V|50.2561|-113.6586
Nanton|AB|T|50.3460|-113.7771
Rosedale|AB|V|51.4167|-112.6333
Drumheller|AB|T|51.4644|-112.7189
Nacmine|AB|V|51.4700|-112.7844
Cambria|AB|V|51.3939|-112.5896
Wayne|AB|V|51.3815|-112.6581
East Coulee|AB|V|51.3436|-112.4979
Kirkcaldy|AB|V|50.3350|-113.2378
Ensign|AB|V|50.4750|-113.4317
Richdale|AB|V|51.6103|-111.6042
Chancellor|AB|V|51.0885|-112.8353
Makepeace|AB|V|50.9394|-112.6161
Mossleigh|AB|V|50.7203|-113.3244
Carseland|AB|V|50.8544|-113.4706
Blackie|AB|V|50.6044|-113.6228
Herronton|AB|V|50.6247|-113.4394
Brant|AB|V|50.5169|-113.5117
Cayley|AB|V|50.4481|-113.8472
Connemara|AB|V|50.4001|-113.8293
Durward|AB|V|50.2956|-113.7131
Kew|AB|V|50.7329|-114.4173
Millarville|AB|V|50.7564|-114.3255
Turner Valley|AB|T|50.6753|-114.2803
Black Diamond|AB|T|50.6881|-114.2344
Okotoks|AB|T|50.7261|-113.9775
High River|AB|T|50.5797|-113.8742
Longview|AB|V|50.5308|-114.2308
Bearspaw, Chiniki, Wesley|AB|V|50.4365|-114.4332
Queenstown|AB|V|50.6394|-112.9394
Foremost|AB|V|49.4764|-111.4469
Legend|AB|V|49.4844|-111.6097
Skiff|AB|V|49.5025|-111.7911
Judson|AB|V|49.5222|-112.3456
Welling|AB|V|49.4811|-112.7856
Del Bonita|AB|V|49.0294|-112.7906
Dorothy|AB|V|51.2792|-112.3250
Sunnynook|AB|V|51.2939|-111.6681
Pollockville|AB|V|51.1061|-111.5903
Cessford|AB|V|51.0064|-111.5567
Wardlow|AB|V|50.9053|-111.5461
Gem|AB|V|50.9506|-112.1889
Altario|AB|V|51.9186|-110.1622
Compeer|AB|V|51.8594|-110.0131
Consort|AB|V|52.0123|-110.7708
Monitor|AB|V|51.9783|-110.5694
Pemukan|AB|V|51.9719|-110.4442
Bodo|AB|V|52.1533|-110.0792
Hayter|AB|V|52.3564|-110.1078
Provost|AB|T|52.3533|-110.2647
Cadogan|AB|V|52.3192|-110.4486
Loyalist|AB|V|51.9858|-110.9544
Veteran|AB|V|52.0011|-111.1188
Coronation|AB|T|52.0947|-111.4478
Throne|AB|V|52.0534|-111.2877
Federal|AB|V|52.1117|-111.5719
Fleet|AB|V|52.1533|-111.7300
Castor|AB|T|52.2192|-111.9036
Bulwark|AB|V|52.2417|-111.5867
Brownfield|AB|V|52.3172|-111.4331
Gadsby|AB|V|52.2968|-112.3560
Halkirk|AB|V|52.2825|-112.1536
Botha|AB|V|52.3081|-112.5239
Stettler|AB|T|52.3239|-112.7044
Big Valley|AB|V|52.0328|-112.7519
Scollard|AB|V|51.9375|-112.8339
Rumsey|AB|V|51.8419|-112.8425
Byemoor|AB|V|51.9797|-112.2871
Endiang|AB|V|51.9539|-112.1583
Sedalia|AB|V|51.6750|-110.6647
Czar|AB|V|52.4547|-110.8283
Hughenden|AB|V|52.5106|-110.9672
Metiskow|AB|V|52.4086|-110.6333
Amisk|AB|V|52.5656|-111.0572
Leahurst|AB|V|52.3763|-112.6437
Warden|AB|V|52.2592|-112.7421
Erskine|AB|V|52.3211|-112.8797
Nevis|AB|V|52.3303|-113.0314
Ardley|AB|V|52.2611|-113.2317
Delburne|AB|V|52.1981|-113.2347
Haynes|AB|V|52.3175|-113.3917
Joffre|AB|V|52.3371|-113.5366
Alix|AB|V|52.3981|-113.1885
Mirror|AB|V|52.4656|-113.1101
Tees|AB|V|52.4675|-113.3208
Clive|AB|V|52.4774|-113.4452
Bashaw|AB|T|52.5831|-112.9703
Rochon Sands|AB|V|52.4540|-112.8817
White Sands|AB|V|52.4667|-112.8167
Red Willow|AB|V|52.4504|-112.5731
Donalda|AB|V|52.5836|-112.5742
Alliance|AB|V|52.4344|-111.7853
Galahad|AB|V|52.5142|-111.9306
Forestburg|AB|V|52.5839|-112.0644
Rowley|AB|V|51.7622|-112.7883
Twining|AB|V|51.6328|-113.2975
Three Hills|AB|T|51.7031|-113.2686
Trochu|AB|T|51.8286|-113.2281
Swalwell|AB|V|51.5572|-113.3183
Carbon|AB|V|51.4858|-113.1506
Hesketh|AB|V|51.4703|-112.9753
Bircham|AB|V|51.4589|-113.4308
Acme|AB|V|51.4972|-113.5086
Beiseker|AB|V|51.3850|-113.5361
Irricana|AB|T|51.3225|-113.6077
Linden|AB|V|51.5906|-113.4883
Sunnyslope|AB|V|51.6490|-113.5577
Rockyford|AB|V|51.2342|-113.1425
Rosebud|AB|V|51.2997|-112.9511
Redland|AB|V|51.2897|-113.0081
Lawsonburg|AB|V|51.4836|-112.1592
Rose Lynn|AB|V|51.4178|-111.6772
Strathmore|AB|T|51.0456|-113.3931
Ardenode|AB|V|51.1444|-113.4236
Lyalta|AB|V|51.1145|-113.6016
Dalroy|AB|V|51.1286|-113.6692
Keoma|AB|V|51.2169|-113.6486
Kathyrn|AB|V|51.2183|-113.7083
Nightingale|AB|V|51.1603|-113.3331
Chestermere|AB|C|51.0373|-113.8280
High Point Estates|AB|V|51.0309|-113.7807
Langdon|AB|V|50.9737|-113.6701
Cheadle|AB|V|51.0144|-113.5406
Indus|AB|V|50.9158|-113.7800
Shepard|AB|V|50.9530|-113.9081
Dalemead|AB|V|50.8747|-113.6375
Aldersyde|AB|V|50.6761|-113.8789
Airdrie|AB|C|51.2814|-114.0043
Calgary|AB|C|51.0458|-114.0575
Stonelaw|AB|V|51.8686|-112.4806
Hardisty|AB|T|52.6750|-111.3044
Armada|AB|V|50.4139|-112.7536
Bragg Creek|AB|V|50.9503|-114.5697
Priddis|AB|V|50.8861|-114.3294
Cochrane|AB|T|51.1897|-114.4844
Cochrane Lake Subd.|AB|V|51.2434|-114.4824
Conrich|AB|V|51.0933|-113.8633
De Winton|AB|V|50.8231|-114.0186
Artists View Park West|AB|V|51.0788|-114.2718
Heritage Woods|AB|V|51.0555|-114.2408
Benchlands|AB|V|51.2834|-114.8009
Waiparous|AB|V|51.2828|-114.8351
Canmore|AB|T|51.0928|-115.3608
Banff|AB|T|51.1790|-115.5688
Exshaw|AB|V|51.0635|-115.1592
Lac des Arcs|AB|V|51.0501|-115.1565
Pigeon Mountain|AB|V|51.0379|-115.2649
Morley|AB|V|51.1672|-114.8520
Ghost Lake|AB|V|51.2066|-114.7641
Harvie Heights|AB|V|51.1300|-115.3762
Lake Louise|AB|V|51.4253|-116.1806
Saskatchewan River Crossing|AB|V|51.9758|-116.7433
Crossfield|AB|T|51.4306|-114.0308
Carstairs|AB|T|51.5656|-114.0994
Cremona|AB|V|51.5453|-114.4853
Dogpound|AB|V|51.4567|-114.3972
Didsbury|AB|T|51.6594|-114.1375
Olds|AB|T|51.7919|-114.1075
Water Valley|AB|V|51.5061|-114.6114
Bergen|AB|V|51.7081|-114.6325
Sundre|AB|T|51.7983|-114.6397
Bowden|AB|T|51.9332|-114.0318
Innisfail|AB|T|52.0242|-113.9535
Caroline|AB|V|52.0948|-114.7382
Spruce View|AB|V|52.0856|-114.3103
Markerville|AB|V|52.1236|-114.1706
Dickson|AB|V|52.0581|-114.3077
Penhold|AB|T|52.1369|-113.8731
Springbrook|AB|V|52.1767|-113.8715
Torrington|AB|V|51.7914|-113.6058
Wimborne|AB|V|51.8664|-113.5935
Huxley|AB|V|51.9243|-113.2300
Elnora|AB|V|51.9936|-113.2012
Lousana|AB|V|52.1128|-113.1900
Pine Lake|AB|V|52.1103|-113.4822
Tuttle|AB|V|52.2012|-113.8432
Balmoral NW|AB|V|52.2832|-113.7143
Balmoral SE|AB|V|52.2754|-113.7004
Canyon Heights|AB|V|52.2906|-113.7040
Central Park|AB|V|52.3309|-113.8231
Herder|AB|V|52.2699|-113.7122
Linn Valley|AB|V|52.3214|-113.8730
Poplar Ridge|AB|V|52.2994|-113.9370
Spruce Lane Acres|AB|V|52.3503|-113.8135
Woodland Hills|AB|V|52.2270|-113.8315
Blackfalds|AB|T|52.3783|-113.7961
Sylvan Lake|AB|T|52.3057|-114.0962
Norglenwold|AB|V|52.3245|-114.1319
Red Deer|AB|C|52.2686|-113.8103
Jarvis Bay|AB|V|52.3370|-114.0749
Birchcliff|AB|V|52.3619|-114.1181
Benalto|AB|V|52.3078|-114.2783
Half Moon Bay|AB|V|52.3450|-114.1715
Sunbreaker Cove|AB|V|52.3875|-114.1872
Eckville|AB|T|52.3600|-114.3669
Condor|AB|V|52.3264|-114.5553
Alhambra|AB|V|52.3431|-114.6725
Leslieville|AB|V|52.3833|-114.6047
Rocky Mountain House|AB|T|52.3793|-114.9187
Bentley|AB|T|52.4664|-114.0511
Gull Lake|AB|V|52.4622|-113.9411
Parkland Beach|AB|V|52.6072|-114.0653
Lacombe|AB|C|52.4644|-113.7342
College Heights|AB|V|52.4892|-113.7333
Morningside|AB|V|52.5778|-113.6339
Ponoka|AB|T|52.6750|-113.5800
Samson|AB|V|52.7567|-113.4333
Hobbema|AB|V|52.8194|-113.4607
Wetaskiwin|AB|C|52.9692|-113.3769
Gwynne|AB|V|52.9867|-113.1989
Bittern Lake|AB|V|53.0081|-113.0567
Duhamel|AB|V|52.9156|-112.9633
New Norway|AB|V|52.8689|-112.9511
Camrose|AB|C|53.0181|-112.8387
Ohaton|AB|V|52.9700|-112.6606
Kelsey|AB|V|52.8458|-112.5489
Bawlf|AB|V|52.9194|-112.4661
Edberg|AB|V|52.7847|-112.7892
Ferintosh|AB|V|52.7658|-112.9717
Rosalind|AB|V|52.7897|-112.4439
Daysland|AB|V|52.8673|-112.2643
Meeting Creek|AB|V|52.6819|-112.7317
Heisler|AB|V|52.6711|-112.2239
Strome|AB|V|52.8108|-112.0672
Killam|AB|T|52.7897|-111.8553
Sedgewick|AB|T|52.7747|-111.6928
Lougheed|AB|V|52.7444|-111.5428
Chauvin|AB|V|52.6936|-110.1411
Edgerton|AB|V|52.7586|-110.4581
Wainwright|AB|T|52.8382|-110.8530
Fabyan|AB|V|52.8819|-110.9936
Irma|AB|V|52.9119|-111.2283
Jarrow|AB|V|52.9408|-111.3953
Kinsella|AB|V|53.0016|-111.5243
Viking|AB|T|53.0947|-111.7756
Bruce|AB|V|53.1739|-112.0386
Holden|AB|V|53.2333|-112.2339
Round Hill|AB|V|53.1653|-112.6308
Ryley|AB|V|53.2908|-112.4297
Armena|AB|V|53.1225|-112.9489
Kingman|AB|V|53.2133|-112.7494
Tofield|AB|T|53.3708|-112.6658
New Sarepta|AB|V|53.2742|-113.1456
Hay Lakes|AB|V|53.1967|-113.0572
Lloydminster|AB|C|53.2790|-110.0193
Blackfoot|AB|V|53.2892|-110.1733
Kitscoty|AB|V|53.3419|-110.3333
Islay|AB|V|53.3939|-110.5389
Vermilion|AB|T|53.3547|-110.8528
Paradise Valley|AB|V|53.0328|-110.2958
McLaughlin|AB|V|52.9897|-110.1650
Leedale|AB|V|52.5819|-114.4697
Rimbey|AB|T|52.6392|-114.2367
O'Chiese|AB|V|52.6839|-115.2561
Alder Flats|AB|V|52.9331|-114.9569
Buck Lake|AB|V|52.9486|-114.7767
Lodgepole|AB|V|53.1006|-115.3136
Drayton Valley|AB|T|53.2215|-114.9743
Winfield|AB|V|52.9653|-114.4231
Bluffton|AB|V|52.7494|-114.2917
Mulhurst|AB|V|53.0493|-113.9811
Poplar Bay|AB|V|53.0078|-114.1215
Grandview|AB|V|52.9879|-114.0658
Crystal Springs|AB|V|52.9733|-114.0409
Norris Beach|AB|V|52.9666|-114.0234
Ma-Me-O Beach|AB|V|52.9735|-113.9602
Silver Beach|AB|V|53.0368|-113.9904
Argentia Beach|AB|V|53.0536|-114.0164
Golden Days|AB|V|53.0514|-114.0533
Itaska Beach|AB|V|53.0722|-114.0800
Sundance Beach|AB|V|53.0825|-114.1239
Ermineskin Tribe, Louis Bull,Montana,Samson|AB|V|52.9899|-113.9448
Millet|AB|T|53.0923|-113.4727
Breton|AB|V|53.1101|-114.4706
Warburg|AB|V|53.1817|-114.3202
Denwood|AB|V|52.8176|-110.8914
Mannville|AB|V|53.3403|-111.1778
Minburn|AB|V|53.3156|-111.3642
Innisfree|AB|V|53.3825|-111.5308
Ranfurly|AB|V|53.4072|-111.6797
Lavoy|AB|V|53.4587|-111.8628
Vegreville|AB|T|53.4956|-112.0517
Mundare|AB|T|53.5939|-112.3417
Two Hills|AB|T|53.7111|-111.7447
Hilliard|AB|V|53.6400|-112.4819
Chipman|AB|V|53.6983|-112.6378
Lamont|AB|T|53.7625|-112.7784
Hairy Hill|AB|V|53.7633|-111.9786
Warwick|AB|V|53.6272|-112.0522
Bruderheim|AB|T|53.7997|-112.9283
Josephburg|AB|V|53.7133|-113.0700
Sherwood Park|AB|T|53.5339|-113.2903
Ardrossan|AB|V|53.5522|-113.1439
Cooking Lake|AB|V|53.4114|-113.1217
Collingwood Cove|AB|V|53.4469|-113.0053
Half Moon Estates|AB|V|53.4615|-113.0843
Beaumont|AB|C|53.3525|-113.4153
Leduc|AB|C|53.2667|-113.5458
Rolly View|AB|V|53.2528|-113.3169
Kavanagh|AB|V|53.1822|-113.5169
Thorsby|AB|T|53.2269|-114.0481
Calmar|AB|T|53.2664|-113.8093
Devon|AB|T|53.3613|-113.7248
Stony Plain|AB|T|53.5264|-114.0069
Spruce Grove|AB|C|53.5539|-113.9022
Spring Lake|AB|V|53.5186|-114.1331
Edmonton|AB|C|53.5358|-113.4937
St. Albert|AB|C|53.6369|-113.6203
Fort Saskatchewan|AB|C|53.7002|-113.2254
Buford|AB|V|53.2471|-113.9266
Marwayne|AB|V|53.5175|-110.3300
Dewberry|AB|V|53.5881|-110.5250
Derwent|AB|V|53.6545|-110.9665
Myrnam|AB|V|53.6617|-111.2303
Clandonald|AB|V|53.5719|-110.7139
Elk Point|AB|T|53.9011|-110.9006
Willingdon|AB|V|53.8286|-112.1181
Andrew|AB|V|53.8783|-112.3353
St. Paul|AB|T|53.9928|-111.2961
Horseshoe Bay|AB|V|54.1262|-111.3640
Glendon|AB|V|54.2525|-111.1603
Bonnyville|AB|T|54.2672|-110.7386
Pelican Narrows|AB|V|54.2629|-110.8900
Bonnyville Beach|AB|V|54.2253|-110.8674
Vilna|AB|V|54.1167|-111.9208
Ashmont|AB|V|54.1292|-111.5681
Mallaig|AB|V|54.2103|-111.3608
Morinville|AB|T|53.8028|-113.6439
Legal|AB|T|53.9495|-113.5951
Bon Accord|AB|T|53.8369|-113.4122
Gibbons|AB|T|53.8297|-113.3283
Redwater|AB|T|53.9508|-113.1075
Thorhild|AB|V|54.1589|-113.1253
Waskatenau|AB|V|54.0939|-112.7842
Westlock|AB|T|54.1553|-113.8569
Barrhead|AB|T|54.1217|-114.4036
Onoway|AB|T|53.7025|-114.1919
Clyde|AB|V|54.1502|-113.6361
Busby|AB|V|53.9481|-113.8906
Pickardville|AB|V|54.0519|-113.8811
Vimy|AB|V|54.0621|-113.6469
Nakamun Park|AB|V|53.8776|-114.2137
Birch Cove|AB|V|53.9538|-114.3724
Sangudo|AB|V|53.8879|-114.8946
Rochfort Bridge|AB|V|53.9136|-115.0364
Mayerthorpe|AB|T|53.9547|-115.1381
Green Court|AB|V|54.0078|-115.2386
Blue Ridge|AB|V|54.1250|-115.3778
Cherhill|AB|V|53.8192|-114.6772
Glenevis|AB|V|53.7897|-114.5169
Rich Valley|AB|V|53.8483|-114.3500
Gainford|AB|V|53.5847|-114.7853
Betula Beach|AB|V|53.5362|-114.6769
Seba Beach|AB|V|53.5593|-114.7344
Silver Sands|AB|V|53.6369|-114.6569
South View|AB|V|53.6515|-114.6645
Val Quentin|AB|V|53.6640|-114.3845
Alberta Beach|AB|V|53.6756|-114.3528
Sunset Point|AB|V|53.6924|-114.3472
West Cove|AB|V|53.6983|-114.5036
Ross Haven|AB|V|53.7342|-114.4075
Yellowstone|AB|V|53.7335|-114.3807
Gunn|AB|V|53.7305|-114.3480
Sunrise Beach|AB|V|53.7838|-114.0546
Sandy Beach|AB|V|53.8084|-114.0391
Calahoo|AB|V|53.7145|-113.9584
Clearwater Estates|AB|V|53.5694|-114.0807
Crystal Meadows|AB|V|53.5634|-114.1680
Dawn Valley|AB|V|53.6214|-114.0203
Devonshire Meadows|AB|V|53.3888|-113.8301
Fleming Park|AB|V|53.4432|-113.7201
Garden Grove Estates|AB|V|53.4836|-113.9471
Glory Hills|AB|V|53.6828|-113.9710
Green Acre Estates|AB|V|53.4979|-113.9405
Hewitt Estates|AB|V|53.8073|-113.3974
Hu Haven|AB|V|53.7709|-113.2052
Hubbles Lake|AB|V|53.5593|-114.0880
Meso West|AB|V|53.5915|-114.3084
Namao|AB|V|53.7671|-113.4735
Osborne Acres|AB|V|53.5580|-113.7996
Panorama Heights|AB|V|53.6318|-113.9422
Peterburn Estates|AB|V|53.4952|-113.9655
Rolling Heights|AB|V|53.6383|-113.9925
Rolling Meadows|AB|V|53.5826|-113.8541
Sunset View Acres|AB|V|53.4502|-113.7445
Upper and Lower Viscount Estates|AB|V|53.6748|-113.5608
Upper Manor Estates|AB|V|53.6874|-113.5753
Westbrooke Crescents|AB|V|53.5926|-113.9710
Westlake Estates|AB|V|53.5283|-114.0785
Woodbend Crescent|AB|V|53.4287|-113.7315
Woodland Park|AB|V|53.4323|-113.7951
Bristol Oakes|AB|V|53.6824|-113.5637
Villeneuve|AB|V|53.6600|-113.8107
Entwistle|AB|V|53.5944|-114.9958
Evansburg|AB|V|53.6004|-115.0169
Wildwood|AB|V|53.6105|-115.2469
Niton Junction|AB|V|53.6219|-115.7697
Peers|AB|V|53.6658|-115.9925
Edson|AB|T|53.5866|-116.4361
Rocky Rapids|AB|V|53.2797|-114.9517
Wabamun|AB|V|53.5609|-114.4720
Point Alison|AB|V|53.5503|-114.4858
Lakeview|AB|V|53.5597|-114.4575
Kapasiwin|AB|V|53.5442|-114.4447
Paul|AB|V|53.5489|-114.4336
Duffield|AB|V|53.5281|-114.3442
Duvernay|AB|V|53.7861|-111.6925
Beauvallon|AB|V|53.6592|-111.3616
Riverview|AB|V|53.8619|-110.6419
Lindbergh|AB|V|53.8875|-110.6728
Heinsburg|AB|V|53.7675|-110.5217
Frog Lake|AB|V|53.8721|-110.3907
Sputinow|AB|V|53.9219|-110.2678
Elizabeth Metis Settlement|AB|V|54.2170|-110.0920
Ardmore|AB|V|54.3314|-110.4774
Fort Kent|AB|V|54.3108|-110.6044
Cherry Grove|AB|V|54.3561|-110.0753
Cold Lake|AB|C|54.5167|-110.2572
Goodfish Lake|AB|V|54.2975|-111.8058
Therien|AB|V|54.2389|-111.2552
Kikino|AB|V|54.4442|-112.0939
Buffalo Lake Metis Settlement|AB|V|54.5791|-112.4482
Bellis|AB|V|54.1428|-112.1503
Smoky Lake|AB|T|54.1167|-112.4667
Newbrook|AB|V|54.3260|-112.9492
Spedden|AB|V|54.1386|-111.7256
Medley|AB|V|54.4191|-110.2678
Tulliby Lake|AB|V|53.7139|-110.1769
Lac La Biche|AB|T|54.7686|-111.9777
Beaver Lake|AB|V|54.7530|-111.9274
Bone Town|AB|V|54.7111|-111.9583
Plamondon|AB|V|54.8497|-112.3419
Atmore|AB|V|54.8192|-112.5497
Grassland|AB|V|54.8200|-112.6872
Imperial Mills|AB|V|55.0001|-111.7307
Heart Lake|AB|V|54.9832|-111.5604
Rossian|AB|V|54.9545|-112.3713
St. Michael|AB|V|53.8303|-112.6358
Egremont|AB|V|54.0383|-113.1261
Abee|AB|V|54.2380|-113.0248
Long Lake|AB|V|54.4517|-112.7751
Perryvale|AB|V|54.4678|-113.3867
Meanook|AB|V|54.5766|-113.3194
Ellscott|AB|V|54.5031|-112.9011
Rochester|AB|V|54.3725|-113.4606
Tawatinaw|AB|V|54.2979|-113.4774
Boyle|AB|V|54.5897|-112.8067
Jarvie|AB|V|54.4547|-113.9869
Fawcett|AB|V|54.5394|-114.0962
Mewatha Beach|AB|V|54.6065|-112.7412
Bondiss|AB|V|54.5981|-112.6950
Athabasca|AB|T|54.7151|-113.2843
Island Lake|AB|V|54.8415|-113.5581
Island Lake South|AB|V|54.8230|-113.5420
Whispering Hills|AB|V|54.7584|-113.5525
West Baptiste|AB|V|54.7622|-113.5744
South Baptiste|AB|V|54.7219|-113.5726
Sunset Beach|AB|V|54.7415|-113.5408
Larkspur|AB|V|54.4387|-113.7680
Dapp|AB|V|54.3450|-113.9158
Pibroch|AB|V|54.2544|-113.8750
Caslan|AB|V|54.6311|-112.5117
Hylo|AB|V|54.6775|-112.2114
Venice|AB|V|54.6978|-112.1481
Colinton|AB|V|54.6206|-113.2528
Chisholm|AB|V|54.9094|-114.1689
Flatbush|AB|V|54.6892|-114.1564
Neerlandia|AB|V|54.3192|-114.3772
Fort Assiniboine|AB|V|54.3342|-114.7747
Campsie|AB|V|54.1278|-114.6467
Whitecourt|AB|T|54.1411|-115.6842
Smith|AB|V|55.1620|-114.0407
Calling Lake|AB|V|55.2463|-113.1983
Centre Calling Lake|AB|V|55.2103|-113.1904
Bigstone Cree|AB|V|55.2671|-113.2257
Wandering River|AB|V|55.1956|-112.4753
Breynat|AB|V|55.1414|-112.4906
Amesbury|AB|V|55.0811|-112.4972
Slave Lake|AB|T|55.2822|-114.7703
Cardiff|AB|V|53.7728|-113.6000
Carbondale|AB|V|53.7464|-113.5269
Lancaster Park|AB|V|53.6728|-113.4908
Sunnybrook|AB|V|53.1930|-114.1999
Tomahawk|AB|V|53.3979|-114.7627
Violet Grove|AB|V|53.1597|-115.0369
Buck Creek|AB|V|53.1039|-114.8956
Pipestone|AB|V|53.0536|-113.7947
Burnstick Lake|AB|V|51.9894|-114.8919
Marlboro|AB|V|53.5556|-116.7961
Pine Shadows|AB|V|53.6031|-116.3051
Obed|AB|V|53.5583|-117.2149
Hondo|AB|V|55.0716|-114.0386
Sawridge|AB|V|55.2901|-114.7498
Canyon Creek|AB|V|55.3666|-115.0968
Widewater|AB|V|55.3604|-115.0369
Kinuso|AB|V|55.3319|-115.4353
Faust|AB|V|55.3158|-115.6275
Joussard|AB|V|55.3952|-115.9511
Enilda|AB|V|55.4169|-116.3114
Grouard Mission|AB|V|55.5422|-116.1536
Grouard|AB|V|55.5203|-116.1611
High Prairie|AB|T|55.4347|-116.4875
Valleyview|AB|T|55.0659|-117.2819
Sturgeon Lake Cree|AB|V|55.1286|-117.4761
Crooked Creek|AB|V|55.1708|-117.8897
DeBolt|AB|V|55.2217|-118.0203
Bezanson|AB|V|55.2286|-118.3608
Fox Creek|AB|T|54.4043|-116.8102
Swan Hills|AB|T|54.7167|-115.4017
Grande Cache|AB|T|53.8881|-119.1186
Hinton|AB|T|53.4000|-117.5723
Cadomin|AB|V|53.0282|-117.3312
Robb|AB|V|53.2333|-116.9786
Brûlé Mines|AB|V|53.3103|-117.8708
Entrance|AB|V|53.3748|-117.6845
Jasper|AB|V|52.8711|-118.0931
East Prairie Metis Settlement|AB|V|55.1891|-116.2697
Peavine Metis Settlement|AB|V|55.7845|-116.2975
Gift Lake|AB|V|55.8825|-115.8125
Whitefish Lake|AB|V|55.9331|-115.3686
McLennan|AB|T|55.7062|-116.9057
Donnelly|AB|V|55.7289|-117.1047
Falher|AB|T|55.7372|-117.2014
Girouxville|AB|V|55.7536|-117.3389
Guy|AB|V|55.5489|-117.1200
Wabasca-Desmarais|AB|V|55.9792|-113.8427
Sandy Lake|AB|V|55.8144|-113.4161
Trout Lake|AB|V|56.4958|-114.5575
Chipewyan Lake|AB|V|56.9403|-113.4733
Red Earth Creek|AB|V|56.5363|-115.2979
Fort McMurray|AB|T|56.7275|-111.3872
Anzac|AB|V|56.4478|-111.0460
Fort McMurray #468|AB|V|56.4436|-111.1786
Gregoire Lake Estates|AB|V|56.4669|-111.1943
Saprae Creek|AB|V|56.6592|-111.1432
Conklin|AB|V|55.6308|-111.0899
Fort MacKay|AB|V|57.1869|-111.6357
Fort Chipewyan|AB|V|58.7144|-111.1583
Mikisew Cree|AB|V|58.7678|-111.0465
Fitzgerald|AB|V|59.8617|-111.6056
Garden Creek|AB|V|58.7047|-113.8860
John D'Or Prairie|AB|V|58.4938|-115.1439
Tallcree|AB|V|58.4358|-115.7425
Fort Vermilion|AB|V|58.3893|-116.0074
La Crête|AB|V|58.1878|-116.4167
Rocky Lane|AB|V|58.4588|-116.2591
Beaver|AB|V|58.4708|-116.5844
High Level|AB|T|58.5160|-117.1378
Dene Tha'|AB|V|58.5149|-117.0645
Paddle Prairie|AB|V|57.9511|-117.4722
Keg River|AB|V|57.7460|-117.6104
Buffalo Head Prairie|AB|V|58.0508|-116.3494
Rainbow Lake|AB|T|58.5044|-119.4003
Zama City|AB|V|59.1537|-118.6867
Meander River|AB|V|59.0434|-117.7144
Manning|AB|T|56.9200|-117.6288
Notikewin|AB|V|56.9747|-117.6250
North Star|AB|V|56.8564|-117.6153
Dixonville|AB|V|56.5374|-117.6720
Deadwood|AB|V|56.7411|-117.4578
Cadotte Lake|AB|V|56.4705|-116.3822
Woodland Cree|AB|V|56.4863|-116.0581
St. Isidore|AB|V|56.2040|-117.1074
Peace River|AB|T|56.2389|-117.2918
Nampa|AB|V|56.0381|-117.1302
Jean Côté|AB|V|55.8981|-117.3106
Reno|AB|V|55.9943|-116.9998
Grimshaw|AB|T|56.1886|-117.6058
Berwyn|AB|V|56.1488|-117.7245
Brownvale|AB|V|56.1282|-117.8958
Whitelaw|AB|V|56.1089|-118.0761
Fairview|AB|T|56.0672|-118.3842
Bluesky|AB|V|56.0735|-118.2388
Hines Creek|AB|V|56.2486|-118.6027
Bay Tree|AB|V|55.8250|-119.9061
Bonanza|AB|V|55.9128|-119.8275
Gordondale|AB|V|55.8400|-119.5403
Spirit River|AB|T|55.7786|-118.8367
Rycroft|AB|V|55.7558|-118.7086
Woking|AB|V|55.5925|-118.7754
Wanham|AB|V|55.7375|-118.3854
Eaglesham|AB|V|55.7825|-117.8833
Watino|AB|V|55.7126|-117.6239
Worsley|AB|V|56.5067|-119.1339
Cleardale|AB|V|56.3558|-119.4778
Whitburn|AB|V|55.8689|-119.2017
Blueberry Mountain|AB|V|55.9267|-119.1497
Hythe|AB|V|55.3300|-119.5505
Valhalla Centre|AB|V|55.4036|-119.3819
La Glace|AB|V|55.4028|-119.1542
Sexsmith|AB|T|55.3503|-118.7817
Webster|AB|V|55.4419|-118.6881
Clairmont|AB|V|55.2614|-118.7936
Beaverlodge|AB|T|55.2118|-119.4221
Wembley|AB|T|55.1556|-119.1458
Grande Prairie|AB|C|55.1708|-118.7961
Hayfield|AB|V|55.1708|-119.6147
Elmworth|AB|V|55.0539|-119.6158
Flyingshot Lake|AB|V|55.1421|-118.8627
Riverview Pines Subdivision|AB|V|55.0875|-118.9460
Glen Leslie|AB|V|55.1994|-118.4872
Grovedale|AB|V|55.0278|-118.8594
Hawk Hills|AB|V|57.2555|-117.4133
Steen River|AB|V|59.6275|-117.1672
Indian Cabins|AB|V|59.8675|-117.0347
Moose Portage|AB|V|55.2292|-113.8831
Rich Lake|AB|V|54.5078|-111.6186
St. Lina|AB|V|54.2961|-111.4536
Radway|AB|V|54.0600|-112.9450
Warspite|AB|V|54.0911|-112.6144
Coronado|AB|V|53.8878|-113.2953
Musidora|AB|V|53.6897|-111.5842
Fallis|AB|V|53.5853|-114.6408
McLeod Valley|AB|V|53.7206|-115.9706
Lone Pine|AB|V|54.3058|-115.1147
Sunset House|AB|V|55.1120|-116.8986
Forshee|AB|V|52.5580|-114.1452
Roosville|BC|V|49.0236|-115.0469
Grasmere|BC|V|49.1000|-115.0833
Baynes Lake|BC|V|49.2333|-115.2167
Elko|BC|V|49.3012|-115.1110
Jaffray|BC|V|49.3744|-115.3000
Wardner|BC|V|49.4170|-115.4290
Bull River|BC|V|49.4701|-115.4482
Mayook|BC|V|49.4869|-115.5769
Cranbrook|BC|C|49.5155|-115.7589
Cokato|BC|V|49.4667|-115.0667
Fernie|BC|C|49.5068|-115.0688
Hosmer|BC|V|49.5897|-114.9645
Sparwood|BC|T|49.7326|-114.8919
Elkford|BC|T|50.0104|-114.9296
Moyie|BC|V|49.2888|-115.8316
Yahk|BC|V|49.0850|-116.0819
Kitchener|BC|V|49.1567|-116.3395
Creston|BC|T|49.1006|-116.5075
Wynndel|BC|V|49.1800|-116.5525
Lister|BC|V|49.0474|-116.4631
Canyon|BC|V|49.0813|-116.4428
Huscroft|BC|V|49.0167|-116.4667
Crawford Bay|BC|V|49.6766|-116.8224
Balfour|BC|V|49.6283|-116.9718
Procter|BC|V|49.6156|-116.9558
Harrop|BC|V|49.6016|-117.0532
Riondel|BC|V|49.7618|-116.8562
Ymir|BC|V|49.2833|-117.2167
Six Mile|BC|V|49.5785|-117.2251
Ainsworth Hot Springs|BC|V|49.7343|-116.9126
Nelson|BC|C|49.4975|-117.2824
Winlaw|BC|V|49.6144|-117.5497
Slocan|BC|V|49.7667|-117.4667
Silverton|BC|V|49.9528|-117.3583
New Denver|BC|V|49.9917|-117.3736
Salmo|BC|V|49.1916|-117.2768
Fruitvale|BC|V|49.1139|-117.5478
Montrose|BC|V|49.0788|-117.5907
Trail|BC|C|49.1000|-117.7000
Warfield|BC|V|49.0945|-117.7486
Rossland|BC|V|49.0775|-117.8008
Genelle|BC|V|49.2139|-117.6908
Ootischenia|BC|V|49.2833|-117.6333
Castlegar|BC|C|49.3218|-117.6603
Blueberry Creek|BC|V|49.2460|-117.6587
Kinnaird|BC|V|49.2834|-117.6508
Raspberry|BC|V|49.3358|-117.6574
Robson|BC|V|49.3375|-117.6914
Glade|BC|V|49.3964|-117.5354
Shoreacres|BC|V|49.4307|-117.5268
Bonnington Falls|BC|V|49.4667|-117.4833
Krestova|BC|V|49.4447|-117.5822
Taghum|BC|V|49.4934|-117.3989
Brilliant|BC|V|49.3217|-117.6431
Glenmerry|BC|V|49.1000|-117.6667
Sunningdale|BC|V|49.1167|-117.7167
Park Siding|BC|V|49.1725|-117.4935
Christina Lake|BC|V|49.0392|-118.2159
Grand Forks|BC|V|49.0290|-118.4533
Sion|BC|V|49.0207|-118.4838
Niagara|BC|V|49.1071|-118.4658
Greenwood|BC|V|49.0949|-118.6763
Midway|BC|V|49.0071|-118.7730
Rock Creek|BC|V|49.0596|-118.9990
Bridesville|BC|V|49.0358|-119.1424
Mount Baldy|BC|V|49.1522|-119.2375
Osoyoos|BC|V|49.0311|-119.4648
Cawston|BC|V|49.1802|-119.7638
Oliver|BC|V|49.1826|-119.5527
Willowbrook|BC|V|49.2607|-119.5935
Keremeos|BC|V|49.2064|-119.8264
Hedley|BC|V|49.3604|-120.0718
Apex Mountain|BC|V|49.3942|-119.9072
Penticton|BC|V|49.4822|-119.5849
Kaleden|BC|V|49.3928|-119.5959
Kimberley|BC|C|49.6871|-115.9829
Wycliffe|BC|V|49.6013|-115.8607
Fort Steele|BC|V|49.6169|-115.6255
St. Mary's|BC|V|49.6203|-115.6961
Marysville|BC|V|49.6373|-115.9559
Wasa|BC|V|49.7833|-115.7333
Canal Flats|BC|V|50.1576|-115.8169
Kaslo|BC|V|49.9086|-116.9112
Fairmont Hot Springs|BC|V|50.3349|-115.8565
?Akisq'nuk|BC|V|50.4512|-115.9597
Windermere|BC|V|50.4667|-115.9833
Invermere|BC|T|50.5007|-116.0382
Wilmer|BC|V|50.5426|-116.0634
Panorama|BC|V|50.4583|-116.2375
Radium Hot Springs|BC|T|50.6230|-116.0805
Edgewater|BC|V|50.7000|-116.1333
Meadow Creek|BC|V|50.2392|-116.9725
Cooper Creek|BC|V|50.2023|-116.9629
Lardeau|BC|V|50.1430|-116.9559
Hills|BC|V|50.1089|-117.4933
Burton|BC|V|49.9856|-117.8801
Nakusp|BC|V|50.2418|-117.7991
Fauquier|BC|V|49.8667|-118.0667
Edgewood|BC|V|49.7833|-118.1500
Kuskonook|BC|V|49.3000|-116.6500
Sanca|BC|V|49.3917|-116.7350
Boswell|BC|V|49.4503|-116.7638
Nelway|BC|V|49.0147|-117.2730
Paterson|BC|V|49.0067|-117.8311
Cascade|BC|V|49.0187|-118.2071
Gilpin|BC|V|49.0063|-118.3286
Kettle Valley|BC|V|49.0536|-118.9449
Okanagan Falls|BC|V|49.3428|-119.5712
Olalla|BC|V|49.2634|-119.8290
Princeton|BC|V|49.4524|-120.5142
Manning Park|BC|V|49.0668|-120.7816
Eastgate|BC|V|49.1375|-120.6125
Sunshine Valley|BC|V|49.2727|-121.2388
Hemlock Valley|BC|V|49.3769|-121.9353
Hope|BC|V|49.3802|-121.4299
Laidlaw|BC|V|49.3223|-121.6181
Ruby Creek|BC|V|49.3468|-121.5966
Harrison Hot Springs|BC|V|49.3000|-121.7833
Silver Creek|BC|V|49.3634|-121.4556
Haig|BC|V|49.3967|-121.4530
Agassiz|BC|V|49.2333|-121.7667
Harrison Mills|BC|V|49.2559|-121.9488
West Popkum|BC|V|49.1822|-121.7727
Popkum|BC|V|49.2002|-121.7361
Rosedale|BC|V|49.1820|-121.8044
Chilliwack|BC|V|49.1750|-121.9417
Sardis|BC|V|49.1367|-121.9591
Promontory|BC|V|49.1006|-121.9373
Cultus Lake|BC|V|49.0714|-121.9661
Vedder Crossing|BC|V|49.1062|-121.9708
Lindell Beach|BC|V|49.0333|-122.0167
Slesse Park|BC|V|49.0805|-121.8206
Yarrow|BC|V|49.0833|-122.0500
Abbotsford|BC|V|49.0478|-122.2703
Matsqui|BC|V|49.1083|-122.2917
Huntingdon|BC|V|49.0051|-122.2656
Aberdeen|BC|V|49.0503|-122.4112
Clearbrook|BC|V|49.0500|-122.3333
Aldergrove|BC|V|49.0576|-122.4706
Mission|BC|V|49.1376|-122.3037
Murrayville|BC|V|49.0904|-122.6162
Langley|BC|V|49.1038|-122.6588
White Rock|BC|V|49.0252|-122.7962
Brookswood|BC|V|49.0718|-122.6660
Fern Ridge|BC|V|49.0483|-122.6556
Walnut Grove|BC|V|49.1806|-122.6482
Fort Langley|BC|V|49.1667|-122.5833
Douglas|BC|V|49.0028|-122.7356
South Surrey|BC|V|49.0543|-122.8329
Cloverdale|BC|V|49.1117|-122.7249
Tsawwassen|BC|V|49.0183|-123.0805
North Delta|BC|V|49.1547|-122.9114
Ladner|BC|V|49.0917|-123.0778
Richmond|BC|V|49.1539|-123.1582
Leq' a: mel|BC|V|49.1671|-122.0864
Lake Errock|BC|V|49.2291|-122.0116
Durieu|BC|V|49.2175|-122.2350
Hatzic Island|BC|V|49.1624|-122.2364
Baker Trails|BC|V|49.0854|-121.9268
Bell Acres|BC|V|49.0780|-121.8662
University Hill|BC|V|49.2669|-123.2418
Vancouver|BC|V|49.2448|-123.1154
Burnaby|BC|V|49.2521|-122.9617
New Westminster|BC|V|49.2167|-122.9167
Port Moody|BC|V|49.2786|-122.8676
Belcarra|BC|V|49.3182|-122.9282
North Vancouver|BC|V|49.3142|-123.0672
Anmore|BC|V|49.3115|-122.8488
Pleasantside|BC|V|49.2934|-122.8474
Port Coquitlam|BC|V|49.2567|-122.7617
Coquitlam|BC|V|49.2539|-122.8488
Surrey|BC|V|49.1745|-122.8278
Lynn Valley|BC|V|49.3416|-123.0328
Deep Cove|BC|V|49.3232|-122.9479
Delbrook|BC|V|49.3430|-123.0852
Seymour Heights|BC|V|49.3192|-123.0016
Dundarave|BC|V|49.3380|-123.1805
Hollyburn|BC|V|49.3324|-123.1633
Caulfeild|BC|V|49.3416|-123.2605
Westmount|BC|V|49.3439|-123.2092
Lions Bay|BC|V|49.4591|-123.2361
Pitt Meadows|BC|V|49.2172|-122.6863
Maple Ridge|BC|V|49.2174|-122.5979
Coalmont|BC|V|49.5089|-120.6954
Tulameen|BC|V|49.5458|-120.7583
Summerland|BC|V|49.6054|-119.6726
Naramata|BC|V|49.5942|-119.5871
Peachland|BC|V|49.7786|-119.7361
Kelowna|BC|V|49.8833|-119.4833
Westside|BC|V|49.8822|-119.5352
Glenrosa|BC|V|49.8472|-119.6673
Westbank|BC|V|49.8286|-119.6314
Okanagan Mission|BC|V|49.8167|-119.4833
Braeloch|BC|V|49.7833|-119.5167
Ellison|BC|V|49.9363|-119.3576
Rutland|BC|V|49.8883|-119.3910
Beaverdell|BC|V|49.4368|-119.0890
Carmi|BC|V|49.5009|-119.1220
Okanagan|BC|V|50.0076|-119.3969
Winfield|BC|V|50.0333|-119.4018
Woodsdale|BC|V|50.0495|-119.3898
Okanagan Centre|BC|V|50.0500|-119.4500
Oyama|BC|V|50.1111|-119.3676
Lumby|BC|V|50.2500|-118.9667
Lavington|BC|V|50.2313|-119.1066
Blue Springs|BC|V|50.2500|-118.8500
Shuswap Falls|BC|V|50.2940|-118.8051
Silver Star|BC|V|50.3572|-119.0594
Cherryville|BC|V|50.2407|-118.6205
Big White|BC|V|49.7214|-118.9404
Squamish|BC|V|49.7024|-123.1467
Brackendale|BC|V|49.7667|-123.1500
Garibaldi Highlands|BC|V|49.7417|-123.1347
Whistler|BC|V|50.1215|-122.9620
Alpine Meadows|BC|V|50.1500|-122.9667
Whistler Creek|BC|V|50.0935|-122.9935
Pemberton|BC|V|50.3181|-122.7982
Mount Currie|BC|V|50.3148|-122.7180
Birken|BC|V|50.4833|-122.6167
D'Arcy|BC|V|50.5500|-122.4833
Pemberton Meadows|BC|V|50.4417|-122.9167
Ogden|BC|V|50.7819|-122.8222
Gold Bridge|BC|V|50.8500|-122.8333
Horseshoe Bay|BC|V|49.3674|-123.2731
Snug Cove|BC|V|49.3829|-123.3357
Gambier Harbour|BC|V|49.3728|-123.4138
New Brighton|BC|V|49.4500|-123.4333
Williamsons Landing|BC|V|49.4497|-123.4686
Langdale|BC|V|49.4341|-123.4765
Garibaldi|BC|V|49.9667|-123.1500
Samahquam|BC|V|50.0513|-122.5395
Tipella|BC|V|49.7446|-122.1538
Bear Creek|BC|V|49.5290|-121.7613
Silver River|BC|V|49.5756|-121.8181
Dogwood Valley|BC|V|49.4845|-121.4252
Squeah|BC|V|49.5042|-121.4205
Yale|BC|V|49.4712|-121.4253
Spuzzum|BC|V|49.6886|-121.4168
North Bend|BC|V|49.8833|-121.4500
Boston Bar|BC|V|49.8624|-121.4433
Lytton|BC|V|50.2009|-121.5721
Lower Nicola|BC|V|50.1597|-120.8783
Merritt|BC|V|50.1114|-120.7897
Lillooet|BC|V|50.6941|-121.9387
South Shalalth|BC|V|50.7288|-122.2444
Seton Portage|BC|V|50.7069|-122.2890
Seton Lake|BC|V|50.7047|-122.2813
Ts'kw'aylaxw|BC|V|50.8167|-121.6667
Upper Hat Creek 1 (Bonaparte)|BC|V|50.8167|-121.5833
Lower Hat Creek 2 (Bonaparte)|BC|V|50.8840|-121.4930
Moha|BC|V|50.8641|-122.1697
T'it'q'et|BC|V|50.5833|-121.8500
Clinton|BC|V|51.0942|-121.5868
Chasm|BC|V|51.2167|-121.4833
70 Mile House|BC|V|51.3084|-121.3960
Cache Creek|BC|V|50.8119|-121.3233
Ashcroft|BC|V|50.7211|-121.2807
Boston Flats|BC|V|50.7699|-121.3041
Walhachin|BC|V|50.7514|-120.9907
Oregon Jack Creek|BC|V|50.6667|-121.2667
Spences Bridge|BC|V|50.4234|-121.3441
Logan Lake|BC|V|50.4913|-120.8130
Coldwater|BC|V|50.0024|-120.9853
Brookmere|BC|V|49.8181|-120.8750
Allison Lake|BC|V|49.6889|-120.6014
Bankeir|BC|V|49.7107|-120.2249
Upper Nicola|BC|V|50.2197|-120.4621
Vernon|BC|V|50.2642|-119.2689
Coldstream|BC|V|50.2292|-119.2266
Killiney Beach|BC|V|50.1899|-119.5062
Fintry|BC|V|50.1316|-119.5064
Wilson Landing|BC|V|49.9821|-119.4973
Nahun|BC|V|50.0833|-119.5000
Caesars|BC|V|50.0667|-119.5000
Traders Cove|BC|V|49.9361|-119.5041
Kedleston|BC|V|50.3167|-119.1833
Armstrong|BC|V|50.4461|-119.1992
Enderby|BC|V|50.5549|-119.1410
Glenemma|BC|V|50.4694|-119.3525
Hullcar|BC|V|50.5030|-119.2698
Ashton Creek|BC|V|50.5560|-119.0126
Yankee Flats|BC|V|50.5164|-119.3755
Falkland|BC|V|50.5009|-119.5570
Grindrod|BC|V|50.6276|-119.1225
Ranchero|BC|V|50.6570|-119.1983
Deep Creek|BC|V|50.6094|-119.2134
Six Mile Point|BC|V|50.7696|-119.0112
Mara|BC|V|50.6853|-119.0675
Annis|BC|V|50.7907|-119.0818
Sicamous|BC|V|50.8350|-118.9802
Salmon Arm|BC|V|50.7000|-119.2667
Canoe|BC|V|50.7515|-119.2274
Sunnybrae|BC|V|50.7732|-119.2699
Tappen|BC|V|50.7840|-119.3306
Adams Lake|BC|V|50.7366|-119.3206
Monte Lake|BC|V|50.5254|-119.8325
Westwold|BC|V|50.4679|-119.7467
Holmwood|BC|V|50.6189|-119.9540
Duck Range|BC|V|50.6192|-119.8302
Martin Prairie|BC|V|50.6624|-119.8118
Pritchard|BC|V|50.6851|-119.8122
Shuswap|BC|V|50.7896|-119.7088
Chase|BC|V|50.8221|-119.6883
Malakwa|BC|V|50.9400|-118.7888
Solsqua|BC|V|50.8707|-118.9395
Blind Bay|BC|V|50.8833|-119.3833
Revelstoke|BC|C|51.0012|-118.1962
Kingfisher|BC|V|50.6167|-118.7333
Seymour Arm|BC|V|51.2484|-118.9396
Canyon Hot Springs|BC|V|51.1369|-117.8522
Rogers Pass|BC|V|51.3009|-117.5213
Blaeberry|BC|V|51.4333|-117.0667
Donald|BC|V|51.4958|-117.1736
Golden|BC|T|51.2949|-116.9639
Nicholson|BC|V|51.2481|-116.9014
Parson|BC|V|51.0708|-116.6325
Harrogate|BC|V|50.9833|-116.4500
Spillimacheen|BC|V|50.9072|-116.3635
Brisco|BC|V|50.8333|-116.2667
Dry Gulch|BC|V|50.5842|-116.0414
Field|BC|V|51.3949|-116.4900
Shelter Bay|BC|V|50.6352|-117.9252
Galena|BC|V|50.6167|-117.8667
Galena Bay|BC|V|50.6667|-117.8500
Beaton|BC|V|50.7333|-117.7333
Trout Lake|BC|V|50.6472|-117.5389
Ferguson|BC|V|50.6833|-117.4833
Brouse|BC|V|50.2167|-117.7500
Carrolls Landing|BC|V|50.0333|-117.9000
Rosebery|BC|V|50.0333|-117.4111
Howser|BC|V|50.3000|-116.9500
Crowsnest|BC|V|49.6368|-114.6953
Skookumchuck|BC|V|49.9167|-115.7333
Balmoral|BC|V|50.8531|-119.3546
Notch Hill|BC|V|50.8528|-119.4351
Sorrento|BC|V|50.8782|-119.4671
Scotch Creek|BC|V|50.9014|-119.4509
Celista|BC|V|50.9444|-119.3490
Anglemont|BC|V|50.9667|-119.1667
Magna Bay|BC|V|50.9618|-119.2836
Eagle Bay|BC|V|50.9333|-119.2167
White Lake|BC|V|50.8798|-119.3015
Lee Creek|BC|V|50.9078|-119.5341
Pinantan Lake|BC|V|50.7264|-120.0362
Monte Creek|BC|V|50.6487|-119.9581
Campbell Creek|BC|V|50.6540|-120.0850
Dallas|BC|V|50.6667|-120.1667
Barnhartvale|BC|V|50.6505|-120.1705
Valleyview|BC|V|50.6738|-120.2497
Kamloops|BC|V|50.6783|-120.3477
Rayleigh|BC|V|50.7974|-120.3197
Westsyde|BC|V|50.7637|-120.3485
Heffley Creek|BC|V|50.8583|-120.2706
Sun Peaks|BC|V|50.8780|-119.9081
Tranquille|BC|V|50.7200|-120.5208
Savona|BC|V|50.7511|-120.8405
Cherry Creek|BC|V|50.7125|-120.6250
Hustalen 1 (Adams Lake)|BC|V|50.9532|-119.6667
Lac Le Jeune|BC|V|50.4817|-120.4930
Stump Lake|BC|V|50.3883|-120.3278
Douglas Lake|BC|V|50.1667|-120.2000
Black Pines|BC|V|50.9339|-120.2584
Cahilty|BC|V|50.9500|-120.0167
McLure|BC|V|51.0539|-120.2245
Louis Creek|BC|V|51.1401|-120.1216
Barriere|BC|V|51.1819|-120.1334
Chinook Cove|BC|V|51.2333|-120.1667
Simpcw|BC|V|51.2654|-120.1512
Darfield|BC|V|51.3000|-120.1833
Little Fort|BC|V|51.4249|-120.2069
Clearwater|BC|V|51.6500|-120.0333
Blackpool|BC|V|51.5969|-120.1198
Birch Island|BC|V|51.5971|-119.9104
Vavenby|BC|V|51.5858|-119.7261
Avola|BC|V|51.7812|-119.3232
Blue River|BC|V|52.1061|-119.3059
Mica Creek|BC|V|52.0059|-118.5615
Summit Lake|BC|V|50.1523|-117.6607
Shoreholme|BC|V|50.3000|-117.8500
Albreda|BC|V|52.6375|-119.1625
Valemount|BC|T|52.8305|-119.2650
Cedarside|BC|V|52.7823|-119.2501
Tête Jaune Cache|BC|V|52.9667|-119.4292
Mount Robson|BC|V|53.0309|-119.2277
Dunster|BC|V|53.1270|-119.8388
McBride|BC|T|53.3000|-120.1667
Crescent Spur|BC|V|53.5717|-120.6955
Loos|BC|V|53.6021|-120.7189
Dome Creek|BC|V|53.7425|-121.0261
Penny|BC|V|53.8474|-121.2835
Sinclair Mills|BC|V|54.0167|-121.6833
Upper Fraser|BC|V|54.1082|-121.9306
Aleza Lake|BC|V|54.1143|-122.0337
Willow River|BC|V|54.0787|-122.4744
Giscome|BC|V|54.0667|-122.3667
Prince George|BC|C|53.9116|-122.7757
Shelley|BC|V|54.0000|-122.6167
Foreman|BC|V|53.9500|-122.6833
Bonnet Hill|BC|V|53.9000|-122.6167
Miworth|BC|V|53.9399|-122.9491
Bear Lake|BC|V|54.4959|-122.6792
Buckhorn|BC|V|53.7942|-122.6547
Pineview|BC|V|53.8367|-122.6577
Red Rock|BC|V|53.6886|-122.6709
Stoner|BC|V|53.6291|-122.6625
Hixon|BC|V|53.4204|-122.5862
Salmon Valley|BC|V|54.0833|-122.7000
Cinema|BC|V|53.2333|-122.4500
Ten Mile Lake|BC|V|53.0883|-122.4403
Barlow Creek|BC|V|53.0230|-122.4453
Quesnel|BC|V|52.9806|-122.5043
Red Bluff|BC|V|52.9654|-122.4608
Rich Bar|BC|V|52.9149|-122.4565
Bouchie Lake|BC|V|53.0262|-122.5973
Cottonwood|BC|V|53.0522|-122.1564
Wells|BC|V|53.1041|-121.5705
Horsefly|BC|V|52.3333|-121.4167
Likely|BC|V|52.6187|-121.5647
Hydraulic|BC|V|52.6148|-121.7070
Big Lake Ranch|BC|V|52.4049|-121.8762
Quesnel Forks|BC|V|52.6564|-121.6626
Kersley|BC|V|52.8358|-122.4185
Gravelle Ferry|BC|V|52.8500|-122.2333
McLeese Lake|BC|V|52.4137|-122.2962
Soda Creek|BC|V|52.3275|-122.2748
Williams Lake|BC|V|52.1415|-122.1429
150 Mile House|BC|V|52.1147|-121.9355
Esler|BC|V|52.1042|-122.1843
Chilcotin Road Area|BC|V|52.1166|-122.1790
Dog Creek|BC|V|52.0944|-122.1143
South Lakeside|BC|V|52.1083|-122.0972
Pine Valley|BC|V|52.1753|-122.0897
Fox Mountain|BC|V|52.1402|-122.1163
Commodore Heights|BC|V|52.1667|-122.1333
Wildwood|BC|V|52.2134|-122.0888
Miocene|BC|V|52.2503|-121.7714
Black Creek|BC|V|52.2981|-121.0931
Dugan Lake|BC|V|52.1667|-121.9250
Riske Creek|BC|V|51.9596|-122.5182
Springhouse|BC|V|51.9667|-122.1333
St. Joseph Mission|BC|V|52.0667|-121.9500
141 Mile House|BC|V|51.9940|-121.8456
Lac la Hache|BC|V|51.8145|-121.4652
122 Mile House|BC|V|51.8483|-121.5988
Tatton|BC|V|51.7057|-121.3854
108 Mile Ranch|BC|V|51.7281|-121.3505
105 Mile House|BC|V|51.7069|-121.3311
103 Mile|BC|V|51.6890|-121.3135
100 Mile House|BC|V|51.6391|-121.2945
Buffalo Creek|BC|V|51.7333|-121.1500
Gateway|BC|V|51.6808|-121.2115
Forest Grove|BC|V|51.7667|-121.1000
Eagle Creek|BC|V|51.8597|-120.8657
Deka Lake|BC|V|51.6083|-120.8500
Mahood Falls|BC|V|51.8426|-120.6393
Canim Lake|BC|V|51.7669|-120.8943
Imperial Ranchettes|BC|V|51.6130|-121.1830
Lone Butte|BC|V|51.5579|-121.2160
Anderson Subdivision|BC|V|51.5795|-121.1382
93 Mile|BC|V|51.5748|-121.3408
Roe Lake|BC|V|51.5208|-120.8313
Bridge Lake|BC|V|51.4786|-120.7255
Loon Lake|BC|V|51.0833|-121.3000
Vidette|BC|V|51.1667|-120.9000
Criss Creek|BC|V|51.0500|-120.7333
Canoe Creek|BC|V|51.5956|-122.0922
Gang Ranch|BC|V|51.5538|-122.3510
Big Bar Creek|BC|V|51.1829|-122.1170
Hanceville|BC|V|51.9417|-123.0986
Alexis Creek|BC|V|52.0833|-123.2833
Tl'etinqox-t'in Government Office|BC|V|52.1126|-123.2357
Big Creek|BC|V|51.7188|-123.0376
Blackwater|BC|V|53.2875|-123.1513
Punchaw|BC|V|53.4306|-123.1824
Baldy Hughes|BC|V|53.6246|-122.9332
Colwood|BC|V|48.4321|-123.4965
Langford|BC|V|48.4566|-123.4974
Oak Bay|BC|V|48.4301|-123.3089
Saanich|BC|V|48.4878|-123.3880
Victoria|BC|V|48.4334|-123.3617
View Royal|BC|V|48.4583|-123.4500
Esquimalt|BC|V|48.4289|-123.4080
Brentwood Bay|BC|V|48.5683|-123.4562
Central Saanich|BC|V|48.5669|-123.3971
Sidney|BC|V|48.6500|-123.4000
Saanichton|BC|V|48.6000|-123.4167
Ardmore|BC|V|48.6389|-123.4667
North Saanich|BC|V|48.6217|-123.4215
Swartz Bay|BC|V|48.6827|-123.4131
Lighthouse Point|BC|V|48.8687|-123.2841
Sturdies Bay|BC|V|48.8792|-123.3194
Mayne|BC|V|48.8501|-123.2970
Port Washington|BC|V|48.8166|-123.3212
Saturna|BC|V|48.7945|-123.1946
Fulford Harbour|BC|V|48.7706|-123.4503
Nukko Lake|BC|V|54.0833|-122.9833
Mackenzie|BC|T|55.3354|-123.0955
Isle Pierre|BC|V|53.9500|-123.2500
Telachick|BC|V|53.8621|-123.1823
Mud River|BC|V|53.7612|-123.0119
Beaverley|BC|V|53.8172|-122.8813
Reid Lake|BC|V|53.9667|-123.1000
Shady Valley|BC|V|53.9793|-122.7269
Anzac|BC|V|54.7667|-122.5000
Chetwynd|BC|T|55.6949|-121.6192
Powder King|BC|V|55.3505|-122.6145
Hasler Flat|BC|V|55.6111|-121.9669
Lemoray|BC|V|55.5389|-122.4833
Dokie Siding|BC|V|55.6625|-121.7343
Wabi Hill|BC|V|55.6707|-121.5324
Twidwell Bend|BC|V|55.6155|-121.5715
East Pine|BC|V|55.7167|-121.2167
Arras|BC|V|55.7535|-120.5258
Groundbirch|BC|V|55.7800|-120.9233
Sunset Prairie|BC|V|55.8397|-120.7640
Progress|BC|V|55.7817|-120.7162
Willow Valley|BC|V|55.8563|-120.8715
Moberly Lake|BC|V|55.8333|-121.7585
Hudson's Hope|BC|T|56.0319|-121.9069
Attachie|BC|V|56.2205|-121.4233
Bear Flat|BC|V|56.2740|-121.2307
Farrell Creek|BC|V|56.1228|-121.7379
Mile 62 1/2|BC|V|56.4084|-121.1573
Charlie Lake|BC|V|56.2974|-120.9854
Clairmont|BC|V|56.2559|-120.8999
Fort St. John|BC|C|56.2461|-120.8446
Grand Haven|BC|V|56.2402|-120.8987
Old Fort|BC|V|56.2022|-120.8233
Charlie Lake part B|BC|V|56.3368|-121.0093
South Taylor|BC|V|56.1068|-120.6337
Taylor|BC|T|56.1544|-120.6799
Farmington|BC|V|55.9000|-120.5000
Dawson Creek|BC|C|55.7595|-120.2345
Rolla|BC|V|55.8986|-120.1426
Tower Lake|BC|V|56.0155|-120.5613
Kilkerran|BC|V|55.8395|-120.2716
Bessborough|BC|V|55.8109|-120.5064
Seven Mile Corner|BC|V|55.8976|-120.3243
Clayhurst|BC|V|56.1868|-120.0305
Goodlow|BC|V|56.3336|-120.1365
Cecil Lake|BC|V|56.3060|-120.5768
Montney|BC|V|56.4503|-120.9265
Rose Prairie|BC|V|56.5087|-120.7830
Osborn|BC|V|56.6042|-120.3778
Peejay|BC|V|56.8833|-120.6167
Altona|BC|V|56.8775|-120.9537
Prespatou|BC|V|56.9223|-121.0626
Buick|BC|V|56.7625|-121.2700
Wonowon|BC|V|56.7288|-121.8142
Kobes|BC|V|56.6389|-121.6500
Upper Halfway|BC|V|56.5176|-122.2257
Pink Mountain|BC|V|57.0357|-122.5073
Boring Ranch|BC|V|56.9500|-122.7000
Brady Ranch|BC|V|56.8333|-122.6333
Beatton Ranch|BC|V|56.7333|-122.5833
McKearney Ranch|BC|V|56.6333|-122.4667
Simpson Ranch|BC|V|56.6000|-122.4333
Federal Ranch|BC|V|56.4000|-122.3833
Sikanni Chief|BC|V|57.2346|-122.6947
Buckinghorse River|BC|V|57.3901|-122.8417
Fort Nelson|BC|T|58.8056|-122.6972
Snake River|BC|V|59.0364|-122.4392
Tumbler Ridge|BC|T|55.1333|-121.0000
Kelly Lake|BC|V|55.2602|-120.0430
Renata|BC|V|49.4268|-118.1070
Deer Park|BC|V|49.4162|-118.0394
Zamora|BC|V|49.1500|-118.9833
Rhone|BC|V|49.2190|-119.0183
Westbridge|BC|V|49.1715|-118.9753
Christian Valley|BC|V|49.5513|-118.8016
McCulloch|BC|V|49.7948|-119.1902
Fosthall|BC|V|50.3488|-117.9351
Retallack|BC|V|50.0414|-117.1395
Argenta|BC|V|50.1667|-116.9167
Shutty Bench|BC|V|49.9629|-116.9073
Sandon|BC|V|49.9833|-117.2333
Johnsons Landing|BC|V|50.0833|-116.8833
Birchdale|BC|V|50.0344|-116.8735
Mirror Lake|BC|V|49.8829|-116.9017
Kootenay Bay|BC|V|49.6849|-116.8664
Gray Creek|BC|V|49.6333|-116.7833
Hall|BC|V|49.3740|-117.2426
Erie|BC|V|49.1912|-117.3374
Meadows|BC|V|49.1859|-117.3955
Jersey|BC|V|49.1000|-117.2333
Kingsgate|BC|V|49.0024|-116.1780
Lumberton|BC|V|49.4238|-115.8768
Old Town|BC|V|49.5500|-115.9667
Meachen|BC|V|49.6207|-116.2673
Ta Ta Creek|BC|V|49.7833|-115.7833
Premier Lake|BC|V|49.9517|-115.6587
Kootenay Crossing|BC|V|50.8833|-116.0500
Vermilion Crossing|BC|V|51.0333|-115.9833
Oliver's Landing|BC|V|49.5833|-123.2208
Britannia Beach|BC|V|49.6280|-123.2037
Baker Creek|BC|V|52.9274|-123.0174
Saskatoon|SK|V|52.1307|-106.6587
McCord|SK|V|49.4280|-106.8297
Orkney|SK|V|49.1438|-107.9113
Aneroid|SK|V|49.7167|-107.3000
Meyronne|SK|V|49.6695|-106.8377
Admiral|SK|V|49.7134|-108.0056
Hazenmore|SK|V|49.6874|-107.1368
Kincaid|SK|V|49.6716|-107.0051
Cadillac|SK|V|49.7259|-107.7370
Val Marie|SK|V|49.2457|-107.7284
Bracken|SK|V|49.1782|-108.0958
Mankota|SK|V|49.4199|-107.0707
Ponteix|SK|V|49.7458|-107.4887
Lafleche|SK|V|49.7052|-106.5735
Glentworth|SK|V|49.4241|-106.6828
Ferland|SK|V|49.4489|-106.9507
Climax|SK|V|49.2077|-108.3884
Frontier|SK|V|49.2060|-108.5625
Robsart|SK|V|49.3833|-109.2833
Consul|SK|V|49.2946|-109.5196
Eastend|SK|V|49.5167|-108.8167
Dollard|SK|V|49.6167|-108.5833
Shaunavon|SK|T|49.6500|-108.4167
Nekaneet|SK|V|49.7657|-109.2769
Maple Creek|SK|T|49.9064|-109.4796
Piapot|SK|V|49.9877|-109.1211
Carmichael|SK|V|50.0455|-108.6485
Tompkins|SK|V|50.0626|-108.7970
Gull Lake|SK|T|50.0954|-108.4848
Simmie|SK|V|49.9440|-108.1052
Darlings Beach|SK|V|49.9791|-107.9252
Pambrun|SK|V|49.9333|-107.4500
Neville|SK|V|49.9667|-107.6333
Vanguard|SK|V|49.9059|-107.2994
Gravelbourg|SK|V|49.8771|-106.5522
Illerbrun|SK|V|49.9333|-108.3500
Blumenhof|SK|V|50.0229|-107.6996
Swift Current|SK|C|50.2844|-107.7913
Wymark|SK|V|50.1101|-107.7361
Chortitz|SK|V|50.1056|-107.6447
Schoenfeld|SK|V|50.0889|-107.6991
McMahon|SK|V|50.0757|-107.5632
Rheinfeld|SK|V|50.1167|-107.5667
Rosenhof|SK|V|50.2054|-107.5715
Springfeld|SK|V|50.1419|-107.7335
Rhineland|SK|V|50.1667|-107.6500
Webb|SK|V|50.1833|-108.2000
Beverley|SK|V|50.2685|-107.9797
Golden Prairie|SK|V|50.2213|-109.6308
Richmound|SK|V|50.4547|-109.7561
Fox Valley|SK|V|50.4656|-109.4805
Hazlet|SK|V|50.4007|-108.5953
Fort Walsh|SK|V|49.5667|-109.8833
Hodgeville|SK|V|50.1134|-106.9624
Shamrock|SK|V|50.1607|-106.6243
Glenbain|SK|V|49.8443|-107.0210
Waldeck|SK|V|50.3591|-107.5951
Rush Lake|SK|V|50.4052|-107.3992
Herbert|SK|V|50.4277|-107.2210
Morse|SK|V|50.4159|-107.0396
Ernfold|SK|V|50.4462|-106.8920
Chaplin|SK|V|50.4616|-106.6575
Central Butte|SK|V|50.7976|-106.5067
Gouldtown|SK|V|50.6167|-107.2167
Main Centre|SK|V|50.5822|-107.3444
Stewart Valley|SK|V|50.5978|-107.8049
Cantuar|SK|V|50.3830|-108.0086
Success|SK|V|50.4597|-108.0790
Pennant|SK|V|50.5333|-108.2333
Burstall|SK|V|50.6559|-109.9084
Mendham|SK|V|50.7727|-109.6645
Leader|SK|T|50.8888|-109.5402
Prelate|SK|V|50.8537|-109.4043
Sceptre|SK|V|50.8622|-109.2667
Lancer|SK|V|50.8000|-108.8833
Abbey|SK|V|50.7369|-108.7604
Shackleton|SK|V|50.6849|-108.6054
Cabri|SK|V|50.6194|-108.4609
Beaver Flat|SK|V|50.6604|-107.6563
Mistusinne|SK|V|51.0635|-106.5206
Riverhurst|SK|V|50.9025|-106.8670
Elbow|SK|V|51.1212|-106.5963
Hitchcock Bay|SK|V|51.0472|-106.8667
Birsay|SK|V|51.0976|-106.9810
Lucky Lake|SK|V|50.9833|-107.1333
Beechy|SK|V|50.8833|-107.3833
Demaine|SK|V|50.8905|-107.2587
Kyle|SK|V|50.8319|-108.0384
White Bear|SK|V|50.8796|-108.2195
Eston|SK|T|51.1500|-108.7500
Glidden|SK|V|51.2398|-109.1677
Eatonia|SK|V|51.2234|-109.3897
Laporte|SK|V|51.2145|-109.5116
Mantario|SK|V|51.2602|-109.6955
Wartime|SK|V|51.2136|-108.1926
Elrose|SK|V|51.2000|-108.0333
Coteau Beach|SK|V|51.1663|-106.8246
Dunblane|SK|V|51.2023|-106.8800
Macrorie|SK|V|51.3261|-107.0851
Loreburn|SK|V|51.2309|-106.5987
Strongfield|SK|V|51.3338|-106.5955
Hawarden|SK|V|51.4149|-106.5995
Glenside|SK|V|51.4536|-106.8071
Broderick|SK|V|51.4883|-106.9192
Outlook|SK|V|51.4891|-107.0619
Wiseton|SK|V|51.3137|-107.6490
Dinsmore|SK|V|51.3332|-107.4444
Milden|SK|V|51.4873|-107.5200
Sovereign|SK|V|51.5167|-107.7167
Conquest|SK|V|51.5306|-107.2396
Marengo|SK|V|51.4794|-109.7829
Flaxcombe|SK|V|51.4785|-109.6014
Kindersley|SK|T|51.4700|-109.1574
Beadle|SK|V|51.4671|-109.0036
Netherhill|SK|V|51.4701|-108.8564
Brock|SK|V|51.4421|-108.7197
D'Arcy|SK|V|51.4667|-108.5333
Fiske|SK|V|51.4833|-108.4000
Rosetown|SK|T|51.5571|-107.9840
Zealandia|SK|V|51.6155|-107.7431
McGee|SK|V|51.5029|-108.2510
Loverna|SK|V|51.6550|-109.9902
Hoosier|SK|V|51.6341|-109.7369
Smiley|SK|V|51.6319|-109.4702
Major|SK|V|51.8724|-109.6136
Coleville|SK|V|51.7105|-109.2461
Dodsland|SK|V|51.7997|-108.8360
Plenty|SK|V|51.7829|-108.6461
Kerrobert|SK|T|51.9167|-109.1333
Herschel|SK|V|51.6383|-108.3558
Harris|SK|V|51.7348|-107.5797
Tessier|SK|V|51.8078|-107.4285
Laura|SK|V|51.8480|-107.2809
Swanson|SK|V|51.7000|-107.1500
Donavon|SK|V|51.8000|-107.1333
Camp Dundurn|SK|V|51.8522|-106.5548
Dundurn|SK|V|51.8093|-106.5068
Furdale|SK|V|52.0781|-106.6879
Riverside Estates|SK|V|52.0473|-106.7011
Casa Rio|SK|V|52.0275|-106.6461
Beaver Creek|SK|V|51.9729|-106.6686
Grasswood|SK|V|52.0573|-106.6470
Cedar Villa Estates|SK|V|52.0965|-106.7719
Riversedge|SK|V|52.2222|-106.5528
Pike Lake|SK|V|51.9116|-106.8177
Delisle|SK|V|51.9267|-107.1337
Vanscoy|SK|V|52.0019|-106.9738
Dunfermline|SK|V|52.1339|-107.0395
Grandora|SK|V|52.1144|-106.9796
Asquith|SK|V|52.1336|-107.2261
Kinley|SK|V|52.0678|-107.4269
Perdue|SK|V|52.0544|-107.5444
Biggar|SK|T|52.0580|-107.9846
Springwater|SK|V|51.9727|-108.3783
Ruthilda|SK|V|51.8921|-108.4661
Duperow|SK|V|51.9700|-108.2428
Martensville|SK|V|52.2921|-106.6578
Warman|SK|V|52.3186|-106.5781
Osler|SK|V|52.3679|-106.5372
Dalmeny|SK|V|52.3423|-106.7732
Neuhorst|SK|V|52.4009|-106.5775
Rheinland|SK|V|52.4174|-106.5051
Langham|SK|V|52.3609|-106.9649
Hepburn|SK|V|52.5229|-106.7290
Gruenthal|SK|V|52.4634|-106.5185
Greenfeld|SK|V|52.4365|-106.6388
Waldheim|SK|V|52.6188|-106.6529
Sonningdale|SK|V|52.3833|-107.6833
Cando|SK|V|52.3731|-108.2149
Wilkie|SK|T|52.4082|-108.7063
Scott|SK|V|52.3667|-108.8333
Landis|SK|V|52.2000|-108.4500
Tramping Lake|SK|V|52.1333|-108.9500
Handel|SK|V|52.0635|-108.6936
Luseland|SK|T|52.0813|-109.3896
Salvador|SK|V|52.1681|-109.5089
Denzil|SK|V|52.2278|-109.6492
Primate|SK|V|52.2632|-109.8230
Macklin|SK|T|52.3333|-109.9373
Evesham|SK|V|52.3911|-109.8444
Ibstone|SK|V|52.5667|-108.3000
Unity|SK|T|52.4398|-109.1576
Senlac|SK|V|52.4918|-109.7071
Marsden|SK|V|52.8393|-109.8221
Neilburg|SK|V|52.8387|-109.6245
Cut Knife|SK|V|52.7450|-109.0175
Rockhaven|SK|V|52.6715|-108.8705
Baldwinton|SK|V|52.7888|-109.2563
Battleford|SK|T|52.7382|-108.3078
North Battleford|SK|C|52.7782|-108.2921
Prongua|SK|V|52.7437|-108.5461
Delmas|SK|V|52.9289|-108.6028
Lone Rock|SK|V|53.0490|-109.8792
Furness|SK|V|53.1167|-109.9667
Marshall|SK|V|53.1935|-109.7784
Lashburn|SK|V|53.1269|-109.6133
Waseca|SK|V|53.1002|-109.4709
Maidstone|SK|V|53.0894|-109.2882
Paynton|SK|V|53.0139|-108.9357
Aberfeldy|SK|V|53.2500|-109.8833
Denholm|SK|V|52.6572|-108.0104
Ruddell|SK|V|52.6051|-107.8579
Richard|SK|V|52.6929|-107.7030
Maymont|SK|V|52.5628|-107.7068
Radisson|SK|V|52.4615|-107.3915
Borden|SK|V|52.4056|-107.2385
Speers|SK|V|52.7083|-107.5583
Hafford|SK|V|52.7258|-107.3529
Keatley|SK|V|52.8000|-107.4667
Krydor|SK|V|52.7884|-107.0752
Blaine Lake|SK|V|52.8302|-106.8809
Marcelin|SK|V|52.9260|-106.7943
Laird|SK|V|52.7147|-106.5886
Fielding|SK|V|52.5184|-107.5502
Stranraer|SK|V|51.7082|-108.4855
Frenchman Butte|SK|V|53.5907|-109.6334
Onion Lake|SK|V|53.7156|-109.9989
Paradise Hill|SK|V|53.5378|-109.4581
St. Walburg|SK|V|53.6326|-109.1975
Spruce Lake|SK|V|53.5425|-109.0882
Hillmond|SK|V|53.4278|-109.6974
Greenstreet|SK|V|53.4614|-109.8152
Tangleflags|SK|V|53.4833|-109.6333
Northminster|SK|V|53.3667|-109.9333
Rex|SK|V|53.4500|-109.9333
Little Fishing Lake|SK|V|53.8591|-109.5645
Turtleford|SK|V|53.3887|-108.9583
Mervin|SK|V|53.3341|-108.8737
Thunderchild|SK|V|53.4576|-108.8696
Livelong|SK|V|53.4485|-108.7040
Edam|SK|V|53.1876|-108.7694
Vawn|SK|V|53.1060|-108.6644
Meota|SK|V|53.0358|-108.4537
Metinota|SK|V|53.0317|-108.3988
Martinson's Beach|SK|V|53.1000|-108.4333
Prince|SK|V|52.9624|-108.3667
Crystal Bay-Sunset|SK|V|53.5426|-108.8668
Evergreen Brightsand|SK|V|53.6144|-108.8347
Powm Beach|SK|V|53.5734|-108.7044
Parkland Beach|SK|V|53.5514|-108.6944
Kopp's Kove|SK|V|53.5289|-108.6923
Turtle Lake South Bay|SK|V|53.5122|-108.7048
Evergreen Acres|SK|V|53.5162|-108.6614
Sunset View Beach|SK|V|53.5491|-108.6228
Indian Point - Golden Sands|SK|V|53.5897|-108.5986
Horseshoe Bay|SK|V|53.6179|-108.6387
Kivimaa-Moonlight Bay|SK|V|53.6036|-108.6647
Fairholme|SK|V|53.4277|-108.5396
Glaslyn|SK|V|53.3577|-108.3528
Cochin|SK|V|53.0823|-108.3373
Sleepy Hollow|SK|V|53.0631|-108.3206
Day's Beach|SK|V|53.0502|-108.3352
Lanz Point|SK|V|53.0294|-108.2798
Pelican Point|SK|V|53.0506|-108.3176
Aquadeo|SK|V|53.1319|-108.4204
Summerfield Beach|SK|V|53.0401|-108.3083
Rabbit Lake|SK|V|53.1449|-107.7668
Spruce Bay|SK|V|53.2069|-107.6917
Mullingar|SK|V|53.0848|-107.6629
Mayfair|SK|V|52.9833|-107.6000
Alticane|SK|V|52.9015|-107.4807
Leask|SK|V|53.0211|-106.7407
Pelican Cove|SK|V|53.1668|-107.0539
Pebble Baye|SK|V|53.1915|-107.0088
Big Shell|SK|V|53.2004|-107.1287
Echo Bay|SK|V|53.2167|-107.1333
Mont Nebo|SK|V|53.2833|-106.8500
Shell Lake|SK|V|53.3062|-107.0629
Kilwinning|SK|V|53.0998|-106.6613
Parkside|SK|V|53.1656|-106.5358
Canwood|SK|V|53.3616|-106.6042
Debden|SK|V|53.5265|-106.8814
Big River|SK|V|53.5548|-107.0868
Spiritwood|SK|V|53.3640|-107.5189
Leoville|SK|V|53.6333|-107.5500
Medstead|SK|V|53.2990|-108.0802
Belbutte|SK|V|53.3667|-107.8167
Bapaume|SK|V|53.3833|-107.6744
Chitek Lake|SK|V|53.7538|-107.7312
Pelican Lake|SK|V|53.7251|-107.7388
Mildred|SK|V|53.3500|-107.3333
Bodmin|SK|V|53.7544|-106.9864
Phillips Grove|SK|V|53.8697|-106.9348
Nesslin Lake|SK|V|53.9300|-106.7865
Green Lake|SK|V|54.2908|-107.7917
Ranger|SK|V|53.5854|-107.7011
Loon Lake|SK|V|54.0265|-109.1557
Makwa|SK|V|54.0059|-108.9063
Rapid View|SK|V|54.1500|-108.8167
Flying Dust|SK|V|54.0417|-108.5194
Meadow Lake|SK|C|54.1266|-108.4344
Canoe Lake Cree|SK|V|54.1018|-108.0853
St. Cyr Lake|SK|V|54.2167|-108.0667
Dore Lake|SK|V|54.6209|-107.4089
Sled Lake|SK|V|54.4280|-107.3211
Dorintosh|SK|V|54.3527|-108.6246
Greig Lake|SK|V|54.4433|-108.6925
Goodsoil|SK|V|54.3998|-109.2308
Pierceland|SK|V|54.3393|-109.7815
Beacon Hill|SK|V|54.3546|-109.6542
Peerless|SK|V|54.3474|-109.2296
Cole Bay|SK|V|55.0974|-108.3223
Jans Bay|SK|V|55.1506|-108.0994
Beauval|SK|V|55.1472|-107.6112
English River|SK|V|55.2696|-107.6014
Île-à-la-Crosse|SK|V|55.4493|-107.9038
Pinehouse|SK|V|55.5194|-106.5736
Buffalo Narrows|SK|T|55.8661|-108.4895
Clearwater River Dene|SK|V|56.0486|-108.7235
Michel Village|SK|V|56.0126|-109.1135
St. George's Hill|SK|V|55.8769|-108.9608
La Loche|SK|T|56.4858|-109.4343
Black Point|SK|V|56.3667|-109.4500
Garson Lake|SK|V|56.3127|-109.9557
Descharme Lake|SK|V|57.1043|-109.2124
Patuanak|SK|V|55.8964|-107.7053
Turnor Lake|SK|V|56.4601|-108.7196
Camsell Portage|SK|V|59.6093|-109.2643
Uranium City|SK|V|59.5667|-108.6167
Bear Creek|SK|V|56.2975|-108.9361
Brûlé|AB|V|53.2841|-117.8744
Eastview Acres|AB|V|49.7895|-113.0162
Eldorado|SK|V|59.5571|-108.4997
Palo|SK|V|52.1667|-108.3000
Cluff Lake|SK|V|58.3653|-109.5308
Cree Lake|SK|V|57.4642|-106.7478
Little Pine|SK|V|52.9166|-109.0583
Lucky Man|SK|V|52.9889|-107.2366
Moosomin|SK|V|53.0827|-108.3165
Mosquito, Grizzly Bear's Head, Lean Man First Nations|SK|V|52.4904|-108.2484
Onion Lake Cree Nation|SK|V|53.7126|-109.9640
Poundmaker|SK|V|52.8961|-108.9743
Red Pheasant|SK|V|52.4579|-108.1796
Saulteaux|SK|V|53.1256|-108.3295
Sweetgrass|SK|V|52.7655|-108.7159
Thunderchild First Nation|SK|V|53.4801|-108.8746
Fond du Lac|SK|V|59.3275|-107.1889
Whitecap (Whitecap Dakota First Nation)|SK|V|51.8767|-106.7120
Mistawasis (Mistawasis Nêhiyawak)|SK|V|53.1749|-106.8066
Muskeg Lake (Muskeg Lake Cree Nation #102)|SK|V|53.0029|-106.8741
Canoe Narrows (Canoe Lake Cree First Nation)|SK|V|55.1636|-108.1574
Flying Dust First Nation|SK|V|54.1354|-108.4043
Makwa Sahgaiehcan First Nation|SK|V|54.0274|-109.1653
Island Lake (Ministikwan Lake Cree Nation)|SK|V|54.0480|-109.6695
Dillon (Buffalo River Dene Nation)|SK|V|55.9277|-108.9400
Big Island Lake Cree Nation|SK|V|54.4112|-109.6209
English River First Nation|SK|V|55.9153|-107.7173
Waterhen Lake|SK|V|54.5220|-108.4094
Birch Narrows First Nation|SK|V|56.4724|-108.6899
Starblanket (Ahtahkakoop)|SK|V|53.3729|-106.9309
Witchekan Lake|SK|V|53.5124|-107.6050
Siksika Nation|AB|V|50.8591|-113.0508
Tsuut'ina Nation|AB|V|50.9484|-114.1459
Chiniki|AB|V|51.1699|-114.8464
Sunchild First Nation|AB|V|52.6998|-115.3400
Stand Off (Blood)|AB|V|49.4618|-113.2967
Brocket (Piikani Nation)|AB|V|49.5487|-113.7490
Alexis Nakota Sioux Nation|AB|V|53.7289|-114.4870
Alexander|AB|V|53.8039|-113.9206
Louis Bull|AB|V|52.8747|-113.5721
Enoch Cree Nation #440|AB|V|53.5020|-113.7555
Montana|AB|V|52.7262|-113.4255
Ermineskin Tribe|AB|V|52.8359|-113.4525
Beaver First Nation|AB|V|58.4558|-116.2748
Tallcree (Tallcree Tribal Government)|AB|V|58.0611|-115.6216
Fox Lake (Little Red River Cree Nation)|AB|V|58.4649|-114.5335
Assumption (Dene Tha')|AB|V|58.6982|-118.6913
Horse Lake First Nation|AB|V|55.3517|-119.6937
Driftpile Cree Nation|AB|V|55.3432|-115.7825
Duncan's First Nation|AB|V|56.1021|-117.8650
Kapawe'no First Nation|AB|V|55.5486|-116.1549
Little Buffalo (Lubicon Lake)|AB|V|56.4387|-116.1081
Sawridge First Nation|AB|V|55.2976|-114.8397
Sturgeon Lake Cree Nation|AB|V|55.0676|-117.4181
Arcadia (Sucker Creek)|AB|V|55.4042|-116.1543
Swan River First Nation|AB|V|55.3259|-115.4334
Bigstone Cree Nation|AB|V|55.9464|-113.7830
Atikameg (Whitefish Lake)|AB|V|55.9189|-115.6481
Beaver Lake Cree Nation|AB|V|54.6814|-111.9111
Mikisew Cree First Nation|AB|V|58.7113|-111.1833
Saddle Lake (Saddle Lake Cree Nation)|AB|V|53.9703|-111.6935
Athabasca Chipewyan First Nation|AB|V|58.5482|-111.0302
Cold Lake First Nations|AB|V|54.3371|-110.2362
Kehewin Cree Nation|AB|V|54.1161|-110.8571
Fort McKay First Nation|AB|V|57.1795|-111.6349
Fort McMurray #468 First Nation|AB|V|56.4135|-111.1575
Chipewyan Prairie First Nation|AB|V|55.9318|-110.7218
Stoney|AB|V|51.1698|-114.8469
Bearspaw|AB|V|51.1699|-114.8466
Woodland Cree First Nation|AB|V|56.4724|-116.4018
Wesley|AB|V|51.1700|-114.8465
Loon Lake (Loon River Cree)|AB|V|56.5355|-115.3927
Smith's Landing First Nation|AB|V|59.9990|-111.8781
Peerless Lake (Peerless Trout First Nation)|AB|V|56.6784|-114.5819
Saulteau First Nations|BC|V|55.8522|-121.6530
Fort Nelson First Nation|BC|V|58.7701|-122.6597
Prophet River First Nation|BC|V|58.0925|-122.6975
West Moberly First Nations|BC|V|55.8279|-121.8525
Halfway River First Nation|BC|V|56.5134|-121.9650
Blueberry River First Nations|BC|V|56.7040|-121.1124
Doig River First Nation|BC|V|56.5779|-120.4956
Burrard (Tsleil-Waututh Nation)|BC|V|49.3110|-122.9891
Musqueam|BC|V|49.2249|-123.1992
N'Quatqua|BC|V|50.5275|-122.4842
Mount Currie (Lil'wat Nation)|BC|V|50.3321|-122.6627
Aitchelitz|BC|V|49.1504|-121.9869
Chehalis (Sts'ailes)|BC|V|49.2985|-121.9142
Kwikwetlem First Nation|BC|V|49.2302|-122.8060
Skookumchuck (Skatin Nations)|BC|V|49.9383|-122.4104
Katzie|BC|V|49.2014|-122.6769
Kwantlen First Nation|BC|V|49.1714|-122.5655
Scowlitz (Sq'éwlets)|BC|V|49.2359|-122.0025
Semiahmoo|BC|V|49.0145|-122.7777
Shxwhá:y Village|BC|V|49.1690|-121.9889
Skowkale|BC|V|49.1417|-121.9406
Soowahlie|BC|V|49.0810|-121.9690
Skwah|BC|V|49.1777|-121.9720
Squiala First Nation|BC|V|49.1580|-121.9794
Tzeachten|BC|V|49.1119|-121.9540
Yakweakwioose|BC|V|49.1326|-121.9386
Tsawwassen First Nation|BC|V|49.0379|-123.0977
Sumas First Nation|BC|V|49.0555|-122.1950
Deroche (Leq' a: mel First Nation)|BC|V|49.1875|-122.0746
Kwaw-kwaw-Apilt|BC|V|49.1668|-121.9752
Seabird Island|BC|V|49.2551|-121.7304
Skawahlook First Nation|BC|V|49.3568|-121.6062
Chawathil|BC|V|49.3698|-121.5526
Cheam|BC|V|49.1898|-121.7835
Peters First Nation|BC|V|49.2939|-121.6579
Shxw'ow'hamel First Nation|BC|V|49.3430|-121.6067
Union Bar First Nation|BC|V|49.4352|-121.4381
Yale (Yale First Nation)|BC|V|49.5622|-121.4260
Bridge River|BC|V|50.7695|-121.9641
Cayoose Creek|BC|V|50.6743|-121.9341
Xaxli'p|BC|V|50.7346|-121.8644
Pavilion (Ts'kw'aylaxw First Nation)|BC|V|50.8859|-121.8190
Shalalth (Tsal'alh)|BC|V|50.7292|-122.2144
Lower Similkameen|BC|V|49.0514|-119.7238
Upper Similkameen|BC|V|49.3559|-120.0787
Spallumcheen (Splatsin)|BC|V|50.5425|-119.1387
Westbank First Nation|BC|V|49.8345|-119.6158
?aqam|BC|V|49.5835|-115.7545
Tobacco Plains|BC|V|49.0933|-115.0819
?Akisq'nuk First Nation|BC|V|50.4113|-115.9082
Lower Kootenay|BC|V|49.0667|-116.5163
Lheidli T'enneh|BC|V|54.0088|-122.6143
McLeod Lake|BC|V|54.9846|-123.0458
Pauquachin|BC|V|48.6272|-123.4585
Tsartlip|BC|V|48.5813|-123.4675
Tsawout First Nation|BC|V|48.5920|-123.3907
Tseycum|BC|V|48.6660|-123.4513
Songhees Nation (Songhees First Nation)|BC|V|48.4414|-123.4282
Bonaparte|BC|V|50.8422|-121.3853
Skeetchestn|BC|V|50.8409|-120.9459
Tk'emlúps te Secwépemc|BC|V|50.6820|-120.2965
Little Shuswap Lake|BC|V|50.8702|-119.5997
Neskonlith|BC|V|50.7733|-119.7306
Simpcw First Nation|BC|V|51.3169|-120.1490
Cook's Ferry (Oregon Jack Creek)|BC|V|50.6194|-121.3191
Cook's Ferry|BC|V|50.4241|-121.3179
Shulus (Lower Nicola)|BC|V|50.1383|-120.8577
Nicomen|BC|V|50.2644|-121.3982
Shackan|BC|V|50.2934|-121.1754
Nooaitch|BC|V|50.1534|-121.0351
Boothroyd|BC|V|49.9631|-121.4850
Boston Bar First Nation|BC|V|49.8772|-121.4504
Whispering Pines/Clinton|BC|V|50.9867|-120.2408
High Bar|BC|V|51.1130|-122.0181
Kanaka Bar|BC|V|50.1122|-121.5620
Siska|BC|V|50.1363|-121.5716
Skuppah|BC|V|50.1876|-121.5739
?Esdilagh First Nation|BC|V|52.5628|-122.4968
Alkali Lake (Esk'etemc)|BC|V|51.7887|-122.2364
Tl'etinqox Government|BC|V|52.0170|-123.1764
Red Bluff (Lhtako Dene Nation)|BC|V|52.9549|-122.4511
Stone (Yunesit'in Government)|BC|V|51.9232|-123.1352
Toosey|BC|V|51.9318|-122.5023
Sugarcane (Williams Lake)|BC|V|52.1083|-121.9939
Canoe Creek (Stswecem'c Xgat'tem First Nation)|BC|V|51.5888|-122.2090
Turtle Lake Lodge|SK|V|53.6098|-108.5907
Bayview Heights|SK|V|53.1336|-108.3981
Mowery Beach|SK|V|53.6336|-108.8406
Majestic Subdivision|BC|V|49.0266|-116.1937
Glenlily|BC|V|49.0500|-116.1667
Halcyon Hot Springs|BC|V|50.5247|-117.9014
St. Leon Hot Springs|BC|V|50.4424|-117.8846
Hall's Landing|BC|V|50.6953|-117.9816
West Creston|BC|V|49.0901|-116.6196
Arrow Creek|BC|V|49.1333|-116.4500
Emerald Lake|BC|V|51.4401|-116.5399
Woodbury Creek|BC|V|49.7751|-116.9059
Walkers|BC|V|49.7084|-116.8643
Lemon Creek|BC|V|49.7000|-117.4833
Perrys|BC|V|49.6583|-117.5111
Queens Bay|BC|V|49.6567|-116.9329
Appledale|BC|V|49.6500|-117.5333
Lebahdo|BC|V|49.6000|-117.5833
Vallican|BC|V|49.5667|-117.6500
Passmore|BC|V|49.5333|-117.6500
Slocan Park|BC|V|49.5167|-117.6167
South Slocan|BC|V|49.4576|-117.5237
Playmor Junction|BC|V|49.4500|-117.5333
Crescent Valley|BC|V|49.4464|-117.5578
Kragmont|BC|V|49.1750|-115.2167
Sweetwater|BC|V|49.1735|-115.2435
Koocanusa Marina|BC|V|49.2607|-115.2846
Rosen Lake|BC|V|49.3942|-115.2542
Tie Lake|BC|V|49.4203|-115.3232
Spruceland and Wilks/Kahn|BC|V|49.5489|-115.8179
Booth Creek|BC|V|49.5762|-115.8580
Clearview|BC|V|49.6103|-115.8078
St. Marys Lake|BC|V|49.6240|-116.1087
Meadowbrook|BC|V|49.7081|-115.9058
Lazy Lake|BC|V|49.8182|-115.6273
North Premier Lake|BC|V|49.9750|-115.6742
Grainger Road, Columbia Lake|BC|V|50.1805|-115.8167
Edwards Lake|BC|V|49.0927|-115.1253
Moberly School Rd.|BC|V|51.4166|-117.0126
Upper Donald|BC|V|51.3366|-116.9558
Norbury Lake|BC|V|49.5302|-115.4790
Lewis Bay|BC|V|49.4698|-116.7706
Calder Road-Selkirk View|BC|V|49.8797|-118.1716
Whatshan Campground|BC|V|49.9149|-118.1182
Red Mountain Rd|BC|V|49.9047|-117.3617
Pass Creek|BC|V|49.3885|-114.6793
Erris|BC|V|49.6185|-120.3689
East Princeton|BC|V|49.4618|-120.4494
Ashnola / Riverside|BC|V|49.2231|-119.9702
Twin Lakes|BC|V|49.3420|-119.7186
Nursery|BC|V|49.0167|-118.4000
Usona|AB|V|52.8435|-113.7527
Hoadley|AB|V|52.8436|-114.3626
Viewmar Estates|AB|V|52.7170|-114.3183
Raymond Shores|AB|V|52.6142|-114.0168
Meridean Beach|AB|V|52.5966|-114.0018
Wilsons Beach|AB|V|52.5146|-113.9390
Birch Bay|AB|V|52.5717|-114.0783
Sandy Point|AB|V|52.5018|-114.0044
Medal Beach|AB|V|52.6964|-113.0051
Sunset Heights|AB|V|52.7187|-113.0349
Sherman Park|AB|V|52.7523|-113.0800
Johnsons Beach|AB|V|52.7231|-113.0768
Norman Place Estates|AB|V|52.6937|-113.0245
Lakeview Acres|AB|V|52.7071|-113.0491
Lochinvar|AB|V|52.5244|-113.6753
Homeglen|AB|V|52.7998|-114.0741
Dorchester Ranch|AB|V|52.9120|-114.0260
Poor Mans Flats|AB|V|52.6688|-114.1043
First Chain Lakes|AB|V|52.6021|-113.4539
Jeannette Lake|SK|V|54.5342|-108.5330
Westham Island|BC|V|49.0904|-123.1604
Salt Prairie|AB|V|55.6464|-116.0631
Carcajou|AB|V|57.7591|-117.0654
Eagles Quay|AB|V|52.3791|-114.2449
Aspen Grove|BC|V|49.9384|-120.6292
Quilchena|BC|V|50.1662|-120.4996
"""
