# -*- coding: utf-8 -*-
"""
HydroCN core — GIS-platform-independent logic.

Everything in this module is importable from plain Python 3 (numpy is the
only third-party dependency, and QGIS ships it). No qgis.*, no osgeo.*,
no arcpy. The QGIS Processing algorithm in hydrocn_algorithm.py handles
all raster/vector I/O and calls into these functions.

Ported from HydroCN Builder for ArcGIS Pro (same author). The data
sources, CN tables, and math are identical so the two tools produce
matching numbers on the same AOI.

Data sources (all free federal/state web services):
  - SSURGO soil polygons         : USDA SDA WFS (mapunitpoly)
  - SSURGO hydrologic soil groups: USDA SDA REST (component.hydgrp)
  - NLCD land cover              : MRLC WMS (NLCD 2021 L48)
  - NLCD percent impervious      : MRLC WMS (NLCD 2021 Impervious L48)
  - DEM (for slope)              : state high-res where available (NC
                                   OneMap DEM03, ISGS Illinois LiDAR),
                                   else USGS 3DEP ImageServer
  - Design storm depths (24-hr)  : NOAA Atlas 14 PFDS (point estimate)

This module is licensed GPL-2.0-or-later (QGIS plugin requirement).
"""

import csv
import json
import math
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


NLCD_LAYER = "mrlc_download:NLCD_2021_Land_Cover_L48"
NLCD_IMPERVIOUS_LAYER = "mrlc_download:NLCD_2021_Impervious_L48"
MRLC_WMS_URL = "https://www.mrlc.gov/geoserver/mrlc_download/wms"
SDA_REST_URL = "https://sdmdataaccess.sc.egov.usda.gov/tabular/post.rest"
SDA_WFS_URL = (
    "https://SDMDataAccess.sc.egov.usda.gov/Spatial/SDMWGS84GEOGRAPHIC.wfs"
)
NED_IMAGESERVER_URL = (
    "https://elevation.nationalmap.gov/arcgis/rest/services/"
    "3DEPElevation/ImageServer"
)
NOAA_PFDS_URL = "https://hdsc.nws.noaa.gov/cgi-bin/hdsc/new/fe_text_mean.csv"

DEFAULT_SLOPE_THRESHOLD_PCT = 2.0
SQM_PER_ACRE = 4046.8564224
SQM_PER_SQMI = 2_589_988.110336

# Generous CONUS bounds (L48). Used to gate NLCD / NOAA Atlas 14 fetches.
CONUS_BOUNDS = {"lon_min": -125.0, "lon_max": -66.5,
                "lat_min": 24.5, "lat_max": 49.5}

# AOI area above this triggers a downsampling warning. The 2048-pixel
# raster cap means a 50 km^2 AOI downloads at ~50 m cells (NLCD native
# is 30 m), so larger AOIs start losing resolution.
AOI_DOWNSAMPLE_WARN_KM2 = 50.0
AOI_HARD_WARN_KM2 = 500.0

# Known-good ~1 km^2 probe boxes — used for service validation.
PROBE_BBOX_WGS84 = [-78.650, 35.780, -78.640, 35.790]      # Raleigh, NC
PROBE_BBOX_IL_WGS84 = [-89.700, 39.700, -89.690, 39.710]   # Springfield, IL

# State-specific high-resolution DEM ImageServers. Tried before USGS 3DEP
# when the AOI centroid falls inside the state's WGS84 envelope.
STATE_DEM_REGIONS = {
    "NC": {
        "bounds": {"lon_min": -84.5, "lon_max": -75.0,
                   "lat_min": 33.5, "lat_max": 36.7},
        "sources": [
            ("NC OneMap DEM03 (3 m)",
             "https://services.nconemap.gov/secure/rest/services/"
             "Elevation/DEM03/ImageServer"),
        ],
    },
    "IL": {
        "bounds": {"lon_min": -91.6, "lon_max": -87.0,
                   "lat_min": 36.8, "lat_max": 42.6},
        "sources": [
            ("ISGS Illinois LiDAR DEM (1 ft)",
             "https://data.isgs.illinois.edu/arcgis/rest/services/"
             "Elevation/IL_Statewide_Lidar_DEM_WGS/ImageServer"),
        ],
    },
}

# Backup land cover: ESA WorldCover 2021 v200 — public cloud-optimized
# GeoTIFFs on AWS S3, one 3-degree tile per file, no auth. Used when the
# MRLC WMS is down. Infrastructure-independent from mrlc.gov.
ESA_WORLDCOVER_S3 = (
    "https://esa-worldcover.s3.eu-central-1.amazonaws.com/"
    "v200/2021/map/ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"
)

# ESA WorldCover class -> nearest-equivalent NLCD code, so the TR-55
# lookup and the rest of the pipeline run unchanged. Approximations:
# ESA has ONE built-up class (mapped to NLCD 23, developed medium) and
# no pasture-vs-grassland or developed-intensity distinctions.
ESA_TO_NLCD = {
    10: 43,   # Tree cover        -> Mixed Forest
    20: 52,   # Shrubland         -> Shrub/Scrub
    30: 71,   # Grassland         -> Grassland/Herbaceous
    40: 82,   # Cropland          -> Cultivated Crops
    50: 23,   # Built-up          -> Developed, Medium Intensity
    60: 31,   # Bare/sparse       -> Barren Land
    70: 12,   # Snow and ice      -> Perennial Ice/Snow
    80: 11,   # Permanent water   -> Open Water
    90: 95,   # Herbaceous wetland-> Emergent Herbaceous Wetlands
    95: 90,   # Mangroves         -> Woody Wetlands
    100: 72,  # Moss and lichen   -> Sedge/Herbaceous
}


def esa_worldcover_tiles(bbox_wgs84):
    """Tile names (e.g. 'N33W081') covering a WGS84 bbox.

    ESA WorldCover tiles are 3x3 degrees, named by their south-west
    corner: latitude N/S + 2 digits, longitude E/W + 3 digits.
    """
    eps = 1e-9
    lon_min, lat_min, lon_max, lat_max = bbox_wgs84
    tiles = []
    lat = math.floor(lat_min / 3.0) * 3
    while lat <= math.floor((lat_max - eps) / 3.0) * 3:
        lon = math.floor(lon_min / 3.0) * 3
        while lon <= math.floor((lon_max - eps) / 3.0) * 3:
            ns = "N" if lat >= 0 else "S"
            ew = "E" if lon >= 0 else "W"
            tiles.append(f"{ns}{abs(lat):02d}{ew}{abs(lon):03d}")
            lon += 3
        lat += 3
    return tiles


def http_head_ok(url, timeout=30):
    """True if a HEAD request to url returns 2xx/3xx."""
    req = urllib.request.Request(
        url, method="HEAD", headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
        return 200 <= r.status < 400


NLCD_CLASS_NAMES = {
    11: "Open Water", 12: "Perennial Ice/Snow",
    21: "Developed, Open Space", 22: "Developed, Low Intensity",
    23: "Developed, Medium Intensity", 24: "Developed, High Intensity",
    31: "Barren Land",
    41: "Deciduous Forest", 42: "Evergreen Forest", 43: "Mixed Forest",
    51: "Dwarf Scrub", 52: "Shrub/Scrub",
    71: "Grassland/Herbaceous", 72: "Sedge/Herbaceous",
    73: "Lichens", 74: "Moss",
    81: "Pasture/Hay", 82: "Cultivated Crops",
    90: "Woody Wetlands", 95: "Emergent Herbaceous Wetlands",
}


# --- tiny HTTP layer (stdlib only; QGIS Python has no guaranteed requests) --

_SSL_CTX = ssl.create_default_context()
_UA = "HydroCN-QGIS/1.0 (+https://hydrocomplete.com)"


def http_get(url, params=None, timeout=60, retries=2, backoff=3.0):
    """GET returning raw bytes. Raises urllib.error.* on failure.

    Transient failures — 5xx responses, timeouts, connection errors —
    are retried up to `retries` extra times with growing backoff.
    Federal geodata services shed load with brief 503s routinely, and a
    momentary hiccup shouldn't abort a multi-minute run. Client errors
    (4xx) raise immediately.
    """
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(
                req, timeout=timeout, context=_SSL_CTX
            ) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code not in (500, 502, 503, 504) or attempt >= retries:
                raise
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt >= retries:
                raise
        time.sleep(backoff * (attempt + 1))


def http_post_json(url, payload, timeout=30):
    """POST a JSON body, return parsed-JSON response."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "User-Agent": _UA},
    )
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
        return json.loads(r.read().decode("utf-8"))


# --- geometry helpers --------------------------------------------------------

def centroid_wgs84(bbox_wgs84):
    """Return (lat, lon) for a [xmin, ymin, xmax, ymax] bbox centroid."""
    lon = 0.5 * (bbox_wgs84[0] + bbox_wgs84[2])
    lat = 0.5 * (bbox_wgs84[1] + bbox_wgs84[3])
    return lat, lon


def point_in_bounds(lat, lon, bounds):
    return (bounds["lat_min"] <= lat <= bounds["lat_max"]
            and bounds["lon_min"] <= lon <= bounds["lon_max"])


def is_in_conus(lat, lon):
    return point_in_bounds(lat, lon, CONUS_BOUNDS)


def bbox_area_km2(bbox_wgs84):
    """Approximate WGS84-bbox area in km^2 (cosine correction at mean lat)."""
    lon_min, lat_min, lon_max, lat_max = bbox_wgs84
    mean_lat = 0.5 * (lat_min + lat_max)
    dx_km = (lon_max - lon_min) * 111.320 * math.cos(math.radians(mean_lat))
    dy_km = (lat_max - lat_min) * 111.320
    return abs(dx_km * dy_km)


# --- SSURGO ------------------------------------------------------------------

def get_hydro_group_from_mukey(mukey, run_state, log):
    """Look up dominant-component hydrologic group for a MUKEY via SDA REST.

    run_state is a dict scoped to one run holding:
      - run_state["cache"]: {mukey -> group or None}
      - run_state["sda_error_logged"]: bool, suppress duplicate error
    Nothing persists between runs — a hardcoded MUKEY table is exactly the
    kind of thing that poisons results.
    """
    if not mukey or not mukey.isdigit():
        return None
    cache = run_state["cache"]
    if mukey in cache:
        return cache[mukey]

    sql = (
        "SELECT mu.mukey, compname, comppct_r, hydgrp "
        "FROM component AS c "
        "INNER JOIN mapunit AS mu ON c.mukey = mu.mukey "
        f"WHERE mu.mukey = '{mukey}' "
        "AND c.majcompflag = 'Yes' "
        "ORDER BY c.comppct_r DESC"
    )
    try:
        data = http_post_json(SDA_REST_URL, {"query": sql, "format": "JSON"})
    except Exception as e:  # network / JSON — degrade to NLCD-based estimate
        if not run_state.get("sda_error_logged"):
            log.info(f"SSURGO SDA lookup unavailable: {e}")
            log.info("Falling back to NLCD-based hydrologic group estimates.")
            run_state["sda_error_logged"] = True
        cache[mukey] = None
        return None

    rows = data.get("Table") or []
    if not rows:
        cache[mukey] = None
        return None

    hydro_group = rows[0][3]
    if not hydro_group:
        cache[mukey] = None
        return None

    # Accept single-letter ("A"-"D") or dual ("A/D", "B/D", "C/D").
    # The dual form is returned verbatim so the caller can resolve
    # drained vs. undrained from slope.
    singles = {"A", "B", "C", "D"}
    if len(hydro_group) == 1 and hydro_group in singles:
        cache[mukey] = hydro_group
        return hydro_group
    if "/" in hydro_group:
        parts = hydro_group.split("/")
        if len(parts) == 2 and parts[0] in singles and parts[1] in singles:
            cache[mukey] = hydro_group
            return hydro_group

    cache[mukey] = None
    return None


def resolve_hydro_group(raw, slope_pct, threshold_pct):
    """Collapse a SSURGO hydrologic group to a single letter A-D.

    Rules:
      - Single letter ("A"/"B"/"C"/"D"): returned as-is.
      - Dual letter ("A/D", "B/D", "C/D"): first letter = drained,
        second letter = undrained. Pick the drained letter if the
        feature's mean slope >= threshold_pct, otherwise undrained.
      - If slope_pct is None (no DEM available), return the UNDRAINED
        letter — the conservative choice for design since the undrained
        group has the higher CN.
    """
    if not raw:
        return None
    if len(raw) == 1:
        return raw
    if "/" in raw:
        drained, undrained = raw.split("/", 1)
        if slope_pct is None:
            return undrained
        return drained if slope_pct >= threshold_pct else undrained
    return raw[0]


def estimate_hydro_group_from_nlcd(nlcd_code):
    """Coarse hydrologic group when SSURGO lookup fails."""
    mapping = {
        11: "D", 12: "D",
        21: "B", 22: "C", 23: "C", 24: "D",
        31: "A",
        41: "A", 42: "A", 43: "A",
        51: "B", 52: "B",
        71: "B", 72: "B", 73: "B", 74: "B",
        81: "B", 82: "B",
        90: "D", 95: "D",
    }
    return mapping.get(nlcd_code, "C")


def fetch_ssurgo_gml(bbox_wgs84, log):
    """Fetch mapunitpoly GML text via the SDA WFS. Raises on total failure."""
    bbox = ",".join(f"{c:.6f}" for c in bbox_wgs84)
    candidate_urls = [
        f"{SDA_WFS_URL}?SERVICE=WFS&VERSION=1.0.0&REQUEST=GetFeature"
        f"&TYPENAME=mapunitpoly&OUTPUTFORMAT=GML2&BBOX={bbox}",
        f"{SDA_WFS_URL}?SERVICE=WFS&VERSION=1.1.0&REQUEST=GetFeature"
        f"&TYPENAME=mapunitpoly&OUTPUTFORMAT=GML2&BBOX={bbox}",
    ]
    for i, url in enumerate(candidate_urls, 1):
        try:
            log.info(f"SSURGO WFS attempt {i}")
            text = http_get(url, timeout=60).decode("utf-8", errors="replace")
            if len(text) > 1000:
                return text
            log.info(f"  response only {len(text)} chars; retrying")
        except Exception as e:
            log.info(f"  failed: {e}")
    raise RuntimeError(
        "SSURGO WFS unavailable. The service may be down, or the AOI may "
        "fall outside SSURGO coverage."
    )


def parse_ssurgo_gml(text):
    """Parse SSURGO WFS GML into [(wkt_polygon, mukey, musym), ...].

    Handles both gml:posList and gml:coordinates encodings, and both
    gml:featureMember and ms:mapunitpoly containers (first matching
    pattern wins — parsing both would duplicate every polygon).
    """
    root = ET.fromstring(text)
    ns = {
        "gml": "http://www.opengis.net/gml",
        "ms": "http://mapserver.gis.umn.edu/mapserver",
    }

    out = []
    for pattern in (".//gml:featureMember", ".//ms:mapunitpoly"):
        features = root.findall(pattern, ns)
        if not features:
            continue
        for feature in features:
            mukey = _find_text(feature, [".//ms:mukey", ".//mukey"], ns) or ""
            musym = _find_text(feature, [".//ms:musym", ".//musym"], ns) or ""
            wkt = _parse_gml_polygon_wkt(feature, ns)
            if wkt:
                out.append((wkt, mukey.strip(), musym.strip()))
        break  # first successful pattern wins
    return out


def _find_text(element, xpaths, ns):
    for xp in xpaths:
        node = element.find(xp, ns)
        if node is not None and node.text:
            return node.text
    return None


def _wkt_from_points(points):
    """points = [(x, y), ...] -> closed WKT POLYGON or None."""
    if len(points) < 3:
        return None
    if points[0] != points[-1]:
        points = points + [points[0]]
    ring = ", ".join(f"{x:.8f} {y:.8f}" for x, y in points)
    return f"POLYGON (({ring}))"


def _parse_gml_polygon_wkt(feature, ns):
    # Try gml:posList (space-separated x y x y ...)
    pos_elements = feature.findall(".//gml:posList", ns)
    if pos_elements and pos_elements[0].text:
        try:
            coords = [float(c) for c in pos_elements[0].text.strip().split()]
            points = [(coords[i], coords[i + 1])
                      for i in range(0, len(coords) - 1, 2)]
            wkt = _wkt_from_points(points)
            if wkt:
                return wkt
        except (ValueError, IndexError):
            pass

    # Fall back to gml:coordinates (comma-separated x,y pairs, space-delimited)
    exterior = feature.find(
        ".//gml:outerBoundaryIs//gml:LinearRing//gml:coordinates", ns
    )
    coord_text = (exterior.text.strip()
                  if (exterior is not None and exterior.text) else None)
    if not coord_text:
        coord_elements = feature.findall(".//gml:coordinates", ns)
        coord_text = (coord_elements[0].text.strip()
                      if coord_elements and coord_elements[0].text else None)
    if not coord_text:
        return None
    points = []
    for pair in coord_text.split():
        if "," not in pair:
            continue
        try:
            x_str, y_str = pair.split(",")[:2]
            points.append((float(x_str), float(y_str)))
        except ValueError:
            continue
    return _wkt_from_points(points)


# --- raster service fetchers -------------------------------------------------

def _bbox_pixel_size(bbox_wgs84, floor_px, cap_px=2048):
    width = max(floor_px, min(cap_px,
                int(abs(bbox_wgs84[2] - bbox_wgs84[0]) * 10000)))
    height = max(floor_px, min(cap_px,
                 int(abs(bbox_wgs84[3] - bbox_wgs84[1]) * 10000)))
    return width, height


def fetch_dem_imageserver(bbox_wgs84, out_tif, log, service_base_url, label):
    """Download a float-elevation DEM GeoTIFF from an ArcGIS ImageServer.

    Uses exportImage with pixelType=F32 so real elevation values (not
    color-ramped visualization) come back. Output is delivered in Web
    Mercator (imageSR=3857) to match the rest of the pipeline.
    """
    export_url = service_base_url.rstrip("/")
    if not export_url.endswith("exportImage"):
        export_url = f"{export_url}/exportImage"
    width, height = _bbox_pixel_size(bbox_wgs84, floor_px=64)
    params = {
        "bbox": f"{bbox_wgs84[0]},{bbox_wgs84[1]},{bbox_wgs84[2]},{bbox_wgs84[3]}",
        "bboxSR": 4326,
        "size": f"{width},{height}",
        "imageSR": 3857,
        "format": "tiff",
        "pixelType": "F32",
        "noDataInterpretation": "esriNoDataMatchAny",
        "interpolation": "RSP_BilinearInterpolation",
        "f": "image",
    }
    log.info(f"Requesting DEM from {label}: {width}x{height} px")
    content = http_get(export_url, params=params, timeout=180)
    # ImageServer returns JSON (not TIFF) on parameter errors.
    if content[:1] in (b"{", b"["):
        raise RuntimeError(
            f"{label} ImageServer returned an error: "
            f"{content[:300].decode('utf-8', errors='replace')}"
        )
    with open(out_tif, "wb") as f:
        f.write(content)
    log.info(f"Saved DEM raster: {out_tif} ({len(content):,} bytes)")
    return out_tif


def state_dem_sources_for_bbox(bbox_wgs84):
    """Return ordered (label, ImageServer base URL) pairs for the AOI."""
    lat, lon = centroid_wgs84(bbox_wgs84)
    sources = []
    for region in STATE_DEM_REGIONS.values():
        if point_in_bounds(lat, lon, region["bounds"]):
            sources.extend(region["sources"])
    return sources


def fetch_dem(bbox_wgs84, out_tif, log):
    """Try state high-res DEMs (NC, IL), then fall back to USGS 3DEP.

    Returns the source label of whichever service succeeded.
    """
    state_errors = []
    for label, service_url in state_dem_sources_for_bbox(bbox_wgs84):
        try:
            fetch_dem_imageserver(bbox_wgs84, out_tif, log, service_url, label)
            return label
        except Exception as e:
            state_errors.append(f"{label}: {e}")
            log.warning(f"State DEM ({label}) failed: {e}")

    try:
        fetch_dem_imageserver(
            bbox_wgs84, out_tif, log, NED_IMAGESERVER_URL, "USGS 3DEP"
        )
        return "USGS 3DEP"
    except Exception as e:
        if state_errors:
            raise RuntimeError(
                "All DEM sources failed. "
                + "; ".join(state_errors) + f"; USGS 3DEP: {e}"
            ) from e
        raise


def fetch_mrlc_wms(bbox_wgs84, layer, out_tif, log, label="MRLC"):
    """Download an MRLC raster (NLCD land-cover, impervious) as GeoTIFF."""
    width, height = _bbox_pixel_size(bbox_wgs84, floor_px=16)
    params = {
        "SERVICE": "WMS",
        "VERSION": "1.1.1",
        "REQUEST": "GetMap",
        "LAYERS": layer,
        "STYLES": "",
        "BBOX": f"{bbox_wgs84[0]},{bbox_wgs84[1]},{bbox_wgs84[2]},{bbox_wgs84[3]}",
        "WIDTH": width,
        "HEIGHT": height,
        "FORMAT": "image/geotiff",
        "SRS": "EPSG:4326",
    }
    log.info(f"Requesting {label} from MRLC WMS: {width}x{height} px")
    content = http_get(MRLC_WMS_URL, params=params, timeout=120)
    # MRLC returns XML ServiceException on error even with HTTP 200.
    if content[:5] == b"<?xml":
        raise RuntimeError(
            "MRLC WMS returned an error document: "
            f"{content[:300].decode('utf-8', errors='replace')}"
        )
    with open(out_tif, "wb") as f:
        f.write(content)
    log.info(f"Saved {label} raster: {out_tif} ({len(content):,} bytes)")
    return out_tif


# --- NOAA Atlas 14 -----------------------------------------------------------

def fetch_noaa_atlas14_pfds(lat, lon, log, retries=2):
    """Fetch NOAA Atlas 14 PFDS point estimate (mean) for a lat/lon.

    Returns {duration_label: {return_period_yr (int): depth_in}}, or None
    if the service is unreachable / the response can't be parsed.
    """
    params = {
        "lat": f"{lat:.6f}",
        "lon": f"{lon:.6f}",
        "data": "depth",
        "units": "english",
        "series": "pds",
    }
    try:
        text = http_get(NOAA_PFDS_URL, params=params, timeout=60,
                        retries=retries).decode("utf-8", errors="replace")
    except Exception as e:
        log.warning(f"NOAA Atlas 14 fetch failed: {e}")
        return None

    result = parse_noaa_atlas14_csv(text)
    if not result:
        log.warning(
            "NOAA Atlas 14 response could not be parsed; storm runoff "
            f"will be omitted. (First 300 chars: {text[:300]!r})"
        )
        return None
    return result


def parse_noaa_atlas14_csv(text):
    """Parse NOAA PFDS CSV, returning the MEAN-estimate block.

    Handles two real-world response formats:
      - Current (2026): single-line header, single block.
          'by duration for ARI (years):,1,2,5,10,25,50,100,200,500,1000'
          '5-min:,0.406,0.474,...'
      - Historical (quoted, multi-block with upper/lower 90% CIs):
          '"by duration for return period in years"'
          '"Duration,1,2,5,10,25,50,100,200,500,1000"'
          '"5-min:,0.36,0.44,..."'
          '"by duration for upper bound..."'    <- stop here

    Returns {duration_label: {return_period_yr: depth_in}} or {} if no
    data could be extracted.
    """
    lines = text.splitlines()
    result = {}
    header = None
    in_block = False
    block_count = 0

    for raw in lines:
        line = raw.strip().strip('"')
        if not line:
            continue

        fields = [f.strip().strip('"').rstrip(":") for f in line.split(",")]
        low = line.lower()

        # "by duration ..." marks the start of each block. In the new
        # format the return periods are comma-separated on the SAME line;
        # in the old format they're on the next line.
        if low.startswith("by duration"):
            block_count += 1
            if block_count > 1:
                break  # second block = upper/lower 90% CI; stop
            candidate = [v for v in fields[1:] if _is_int(v)]
            if candidate:
                try:
                    header = [int(float(v)) for v in candidate]
                except ValueError:
                    header = None
            else:
                header = None  # will come on the next line (old format)
            in_block = True
            continue

        if not in_block:
            continue
        if not fields[0]:
            continue

        # Old format: header line within the block
        if header is None:
            candidate = [v for v in fields[1:] if _is_int(v)]
            if candidate:
                try:
                    header = [int(float(v)) for v in candidate]
                except ValueError:
                    header = None
            continue

        # Duration row: "24-hr,2.89,3.48,..."
        depths = []
        for v in fields[1:]:
            try:
                depths.append(float(v))
            except ValueError:
                pass
        if len(depths) >= len(header):
            result[fields[0]] = dict(zip(header, depths[:len(header)]))

    return result


def _is_int(s):
    try:
        int(float(s))
        return True
    except (ValueError, TypeError):
        return False


# --- SCS / TR-55 math --------------------------------------------------------

def apply_amc_adjustment(cn_ii, amc):
    """Adjust AMC-II CN to AMC-I (dry) or AMC-III (wet). NEH-630 formulas."""
    amc = (amc or "II").upper()
    if cn_ii <= 0 or cn_ii >= 100:
        return cn_ii
    if amc == "I":
        return 4.2 * cn_ii / (10.0 - 0.058 * cn_ii)
    if amc == "III":
        return 23.0 * cn_ii / (10.0 + 0.13 * cn_ii)
    return cn_ii  # "II" or anything else: unchanged


def compute_scs_runoff(p_inches, cn):
    """SCS CN direct-runoff depth Q (inches). Returns 0 if P <= Ia."""
    if cn <= 0:
        return 0.0
    s = 1000.0 / cn - 10.0
    ia = 0.2 * s
    if p_inches <= ia:
        return 0.0
    return (p_inches - ia) ** 2 / (p_inches - ia + s)


def default_cn_lookup(condition="Fair"):
    """NLCD code -> {A,B,C,D} CN values.

    TR-55 / NEH-630 values, parameterized by hydrologic condition where
    the condition axis is meaningful (forest, brush, pasture, row crops).
    Water, ice, developed classes, barren, and wetlands don't vary.

    condition: "Good", "Fair", or "Poor". Default "Fair" is the typical
    engineering default when field condition data is unavailable.
    """
    cond = (condition or "Fair").title()
    base = {i: {"A": 70, "B": 70, "C": 70, "D": 70} for i in range(256)}

    # Condition-invariant classes
    fixed = {
        11: {"A": 100, "B": 100, "C": 100, "D": 100},   # Open Water
        12: {"A": 100, "B": 100, "C": 100, "D": 100},   # Perennial Ice/Snow
        21: {"A": 49, "B": 69, "C": 79, "D": 84},       # Developed Open Space
        22: {"A": 61, "B": 75, "C": 83, "D": 87},       # Developed Low
        23: {"A": 77, "B": 85, "C": 90, "D": 92},       # Developed Medium
        24: {"A": 89, "B": 92, "C": 94, "D": 95},       # Developed High
        31: {"A": 77, "B": 86, "C": 91, "D": 94},       # Barren / fallow
        90: {"A": 87, "B": 94, "C": 97, "D": 98},       # Woody Wetlands
        95: {"A": 87, "B": 94, "C": 97, "D": 98},       # Emergent Wetlands
    }
    base.update(fixed)

    # Condition-varying classes (TR-55 Table 2-2b, 2-2c)
    by_condition = {
        "Good": {
            41: {"A": 30, "B": 55, "C": 70, "D": 77},
            42: {"A": 30, "B": 55, "C": 70, "D": 77},
            43: {"A": 30, "B": 55, "C": 70, "D": 77},
            51: {"A": 30, "B": 48, "C": 65, "D": 73},
            52: {"A": 30, "B": 48, "C": 65, "D": 73},
            71: {"A": 39, "B": 61, "C": 74, "D": 80},
            72: {"A": 39, "B": 61, "C": 74, "D": 80},
            73: {"A": 39, "B": 61, "C": 74, "D": 80},
            74: {"A": 39, "B": 61, "C": 74, "D": 80},
            81: {"A": 39, "B": 61, "C": 74, "D": 80},
            82: {"A": 67, "B": 78, "C": 85, "D": 89},
        },
        "Fair": {
            41: {"A": 36, "B": 60, "C": 73, "D": 79},
            42: {"A": 36, "B": 60, "C": 73, "D": 79},
            43: {"A": 36, "B": 60, "C": 73, "D": 79},
            51: {"A": 35, "B": 56, "C": 70, "D": 77},
            52: {"A": 35, "B": 56, "C": 70, "D": 77},
            71: {"A": 49, "B": 69, "C": 79, "D": 84},
            72: {"A": 49, "B": 69, "C": 79, "D": 84},
            73: {"A": 49, "B": 69, "C": 79, "D": 84},
            74: {"A": 49, "B": 69, "C": 79, "D": 84},
            81: {"A": 49, "B": 69, "C": 79, "D": 84},
            # TR-55 has no "fair" row-crop row; use good
            82: {"A": 67, "B": 78, "C": 85, "D": 89},
        },
        "Poor": {
            41: {"A": 45, "B": 66, "C": 77, "D": 83},
            42: {"A": 45, "B": 66, "C": 77, "D": 83},
            43: {"A": 45, "B": 66, "C": 77, "D": 83},
            51: {"A": 48, "B": 67, "C": 77, "D": 83},
            52: {"A": 48, "B": 67, "C": 77, "D": 83},
            71: {"A": 68, "B": 79, "C": 86, "D": 89},
            72: {"A": 68, "B": 79, "C": 86, "D": 89},
            73: {"A": 68, "B": 79, "C": 86, "D": 89},
            74: {"A": 68, "B": 79, "C": 86, "D": 89},
            81: {"A": 68, "B": 79, "C": 86, "D": 89},
            82: {"A": 72, "B": 81, "C": 88, "D": 91},
        },
    }
    base.update(by_condition.get(cond, by_condition["Fair"]))
    return base


def load_cn_lookup_csv(path):
    """Load a user CN lookup CSV with columns NLCD_Code, A, B, C, D."""
    out = {}
    with open(path, "r", newline="") as f:
        for row in csv.DictReader(f):
            out[int(row["NLCD_Code"])] = {
                "A": int(row["A"]), "B": int(row["B"]),
                "C": int(row["C"]), "D": int(row["D"]),
            }
    return out


# --- slope (Horn 1981, pure numpy) ------------------------------------------

def slope_percent_from_array(arr, cell_x, cell_y):
    """Horn (1981) 3x3 slope in percent rise — pure numpy.

    Returns a same-shape array whose 1-cell outer border is NaN (Horn
    requires a full 3x3 neighborhood). NaN input anywhere in a
    neighborhood propagates to NaN output for that cell.
    """
    import numpy as np
    if arr.ndim != 2:
        raise ValueError(f"Expected 2D array, got {arr.ndim}D")
    if arr.shape[0] < 3 or arr.shape[1] < 3:
        raise ValueError(f"Array too small for 3x3 slope: shape {arr.shape}")
    a = arr[:-2, :-2]
    b = arr[:-2, 1:-1]
    c = arr[:-2, 2:]
    d = arr[1:-1, :-2]
    f = arr[1:-1, 2:]
    g = arr[2:, :-2]
    h = arr[2:, 1:-1]
    i = arr[2:, 2:]
    dzdx = ((c + 2.0 * f + i) - (a + 2.0 * d + g)) / (8.0 * cell_x)
    dzdy = ((g + 2.0 * h + i) - (a + 2.0 * b + c)) / (8.0 * cell_y)
    slope_pct_interior = (dzdx ** 2 + dzdy ** 2) ** 0.5 * 100.0
    out = np.full(arr.shape, np.nan, dtype=np.float64)
    out[1:-1, 1:-1] = slope_pct_interior
    return out


# --- classification core -----------------------------------------------------

def accumulate_per_class_per_polygon(
    nlcd_arr, soil_oid_arr, slope_arr,
    oid_to_raw_hsg, cn_lookup, slope_threshold, amc, cell_area_m2,
):
    """Pure-numpy CN classification core.

    Iterates unique (NLCD code, SSURGO OID) combinations and for each:
      - computes mean slope from slope_arr[cell_mask],
      - resolves HSG (raw -> single letter via slope threshold),
      - looks up CN from cn_lookup,
      - applies AMC-I/III adjustment,
      - writes CN/CN_adj into per-cell output arrays,
      - accumulates per-class (NLCD x HSG) and per-polygon (OID) totals.

    Inputs:
      nlcd_arr        : 2D int array (NLCD codes; values < 11 ignored).
      soil_oid_arr    : 2D int array of SSURGO OIDs (-1 = no SSURGO).
      slope_arr       : 2D float array of slope % (NaN = NoData).
      oid_to_raw_hsg  : {OID: raw_hsg_letter_or_dual}; OIDs missing here
                        fall through to estimate_hydro_group_from_nlcd.
      cn_lookup       : {nlcd_code: {'A': cn, 'B': cn, 'C': cn, 'D': cn}}
      slope_threshold : %; duals resolve to the drained letter when
                        mean slope >= threshold.
      amc             : 'I', 'II', or 'III'.
      cell_area_m2    : true ground cell area (cos^2(lat)-corrected for
                        Web Mercator).

    Returns:
      cn_arr      : 2D float64 array, NaN where unclassified.
      cn_adj_arr  : 2D float64 array, NaN where unclassified.
      per_class   : {(nlcd, hsg): {area, cn_area, cn_adj_area}}
      per_polygon : {oid: {area, cn_area, cn_adj_area, slope_sum,
                           slope_n, nlcd_counts, raw_group,
                           resolved_groups}}
    """
    import numpy as np

    cn_arr = np.full(nlcd_arr.shape, np.nan, dtype=np.float64)
    cn_adj_arr = np.full(nlcd_arr.shape, np.nan, dtype=np.float64)
    per_class = {}
    per_polygon = {}

    valid_nlcd_mask = (nlcd_arr >= 11)
    if not np.any(valid_nlcd_mask):
        return cn_arr, cn_adj_arr, per_class, per_polygon

    unique_nlcd = np.unique(nlcd_arr[valid_nlcd_mask])

    for nlcd_code in unique_nlcd:
        nlcd_int = int(nlcd_code)
        if nlcd_int < 11:
            continue
        nlcd_mask = (nlcd_arr == nlcd_int)
        oids_here = np.unique(soil_oid_arr[nlcd_mask])

        for oid in oids_here:
            oid_int = int(oid)
            cell_mask = nlcd_mask & (soil_oid_arr == oid_int)
            n_cells = int(cell_mask.sum())
            if n_cells == 0:
                continue

            slope_vals = slope_arr[cell_mask]
            valid_slope = slope_vals[~np.isnan(slope_vals)]
            mean_slope = (
                float(np.mean(valid_slope)) if valid_slope.size > 0 else None
            )

            if oid_int >= 0 and oid_int in oid_to_raw_hsg:
                raw_group = oid_to_raw_hsg[oid_int]
            else:
                raw_group = estimate_hydro_group_from_nlcd(nlcd_int)

            hsg = resolve_hydro_group(raw_group, mean_slope, slope_threshold)

            cn_value = cn_lookup.get(
                nlcd_int, {"A": 70, "B": 70, "C": 70, "D": 70}
            ).get(hsg, 70)
            cn_adj_value = apply_amc_adjustment(cn_value, amc)

            cn_arr[cell_mask] = cn_value
            cn_adj_arr[cell_mask] = cn_adj_value

            area = n_cells * cell_area_m2
            key = (nlcd_int, hsg or "")
            pc = per_class.setdefault(
                key, {"area": 0.0, "cn_area": 0.0, "cn_adj_area": 0.0}
            )
            pc["area"] += area
            pc["cn_area"] += cn_value * area
            pc["cn_adj_area"] += cn_adj_value * area

            if oid_int >= 0:
                pp = per_polygon.setdefault(oid_int, {
                    "area": 0.0, "cn_area": 0.0, "cn_adj_area": 0.0,
                    "slope_sum": 0.0, "slope_n": 0,
                    "nlcd_counts": {}, "raw_group": raw_group,
                    "resolved_groups": set(),
                })
                pp["area"] += area
                pp["cn_area"] += cn_value * area
                pp["cn_adj_area"] += cn_adj_value * area
                if mean_slope is not None and mean_slope == mean_slope:
                    pp["slope_sum"] += mean_slope * n_cells
                    pp["slope_n"] += n_cells
                pp["nlcd_counts"][nlcd_int] = (
                    pp["nlcd_counts"].get(nlcd_int, 0) + n_cells
                )
                if hsg:
                    pp["resolved_groups"].add(hsg)

    return cn_arr, cn_adj_arr, per_class, per_polygon


# --- service probe -----------------------------------------------------------

def run_service_probe(log, cancel_check=None):
    """Hit each external service with a known-good small bbox and report.

    Returns a list of (service_name, status_string). Each result is
    logged the moment its check completes so a caller watching the log
    sees steady progress. No retries — a diagnostic should report the
    current truth fast, not paper over it. `cancel_check` (optional
    zero-arg callable) is consulted between services; when it returns
    True the probe stops early and reports what it has.
    """
    lat, lon = centroid_wgs84(PROBE_BBOX_WGS84)
    log.info("=== HydroCN service probe ===")
    log.info(f"Probe bbox: {PROBE_BBOX_WGS84} (~1 km^2 near Raleigh, NC)")

    results = []

    def emit(name, status):
        results.append((name, status))
        line = f"  {name}: {status}"
        if (status.startswith("FAIL") or "not advertised" in status
                or "not parsed" in status):
            log.warning(line)
        else:
            log.info(line)

    def canceled():
        if cancel_check is not None and cancel_check():
            log.warning("Probe canceled; reporting services checked so far.")
            return True
        return False

    # MRLC NLCD + Impervious — one GetCapabilities, check both layers
    try:
        caps = http_get(
            MRLC_WMS_URL,
            params={"SERVICE": "WMS", "REQUEST": "GetCapabilities"},
            timeout=30, retries=0,
        ).decode("utf-8", errors="replace")
        for name, layer in (
            ("MRLC WMS (land cover)", NLCD_LAYER),
            ("MRLC WMS (impervious)", NLCD_IMPERVIOUS_LAYER),
        ):
            ok = layer.split(":")[-1] in caps
            emit(name,
                 "OK" if ok else f"reachable but layer {layer} not advertised")
    except Exception as e:
        emit("MRLC WMS (land cover)", f"FAIL: {e}")
        emit("MRLC WMS (impervious)", f"FAIL: {e}")
    if canceled():
        return results

    # USDA SDA REST
    try:
        j = http_post_json(
            SDA_REST_URL,
            {"query": "SELECT TOP 1 mukey FROM mapunit", "format": "JSON"},
        )
        rows = j.get("Table") or []
        emit("USDA SDA REST",
             "OK" if rows else f"reachable but empty response: {str(j)[:120]}")
    except Exception as e:
        emit("USDA SDA REST", f"FAIL: {e}")
    if canceled():
        return results

    # USDA SDA WFS — note: this server 400s a VERSION=1.0.0 GetCapabilities
    # even though VERSION=1.0.0 GetFeature works; probe with 1.1.0.
    try:
        caps = http_get(
            f"{SDA_WFS_URL}?SERVICE=WFS&VERSION=1.1.0&REQUEST=GetCapabilities",
            timeout=30, retries=0,
        ).decode("utf-8", errors="replace")
        emit("USDA SDA WFS",
             "OK" if "mapunitpoly" in caps
             else "reachable but mapunitpoly not advertised")
    except Exception as e:
        emit("USDA SDA WFS", f"FAIL: {e}")
    if canceled():
        return results

    # State DEM ImageServers (NC / IL)
    state_probe_boxes = [
        ("NC OneMap DEM03", PROBE_BBOX_WGS84,
         STATE_DEM_REGIONS["NC"]["sources"]),
        ("ISGS Illinois LiDAR DEM", PROBE_BBOX_IL_WGS84,
         STATE_DEM_REGIONS["IL"]["sources"]),
    ]
    for probe_label, probe_bbox, probe_sources in state_probe_boxes:
        for src_label, service_url in probe_sources:
            try:
                content = http_get(
                    f"{service_url.rstrip('/')}/exportImage",
                    params={
                        "bbox": ",".join(str(v) for v in probe_bbox),
                        "bboxSR": 4326,
                        "size": "64,64",
                        "imageSR": 3857,
                        "format": "tiff",
                        "pixelType": "F32",
                        "f": "image",
                    },
                    timeout=30, retries=0,
                )
                ok = content[:2] == b"II"  # GeoTIFF little-endian magic
                emit(f"{probe_label} ({src_label})",
                     "OK" if ok else
                     f"reachable but not TIFF: "
                     f"{content[:80].decode('utf-8', errors='replace')}")
            except Exception as e:
                emit(f"{probe_label} ({src_label})", f"FAIL: {e}")
        if canceled():
            return results

    # USGS 3DEP
    try:
        j = json.loads(http_get(
            NED_IMAGESERVER_URL, params={"f": "json"}, timeout=30, retries=0,
        ).decode("utf-8", errors="replace"))
        ok = (j.get("serviceDataType", "").lower().startswith("esriimageservice")
              or "pixelType" in j)
        emit("USGS 3DEP ImageServer",
             "OK" if ok else f"reachable, info: {str(j)[:120]}")
    except Exception as e:
        emit("USGS 3DEP ImageServer", f"FAIL: {e}")
    if canceled():
        return results

    # ESA WorldCover S3 (backup land cover)
    try:
        ok = http_head_ok(
            ESA_WORLDCOVER_S3.format(tile="N33W081"), timeout=30)
        emit("ESA WorldCover S3 (backup)",
             "OK" if ok else "reachable but unexpected status")
    except Exception as e:
        emit("ESA WorldCover S3 (backup)", f"FAIL: {e}")
    if canceled():
        return results

    # NOAA Atlas 14 PFDS
    try:
        pfds = fetch_noaa_atlas14_pfds(lat, lon, log, retries=0)
        if pfds and "24-hr" in pfds:
            rps = sorted(pfds["24-hr"].keys())
            emit("NOAA Atlas 14 PFDS",
                 f"OK ({len(rps)} return periods, 24-hr {rps[0]}-{rps[-1]} yr)")
        else:
            emit("NOAA Atlas 14 PFDS", "reachable but no 24-hr row parsed")
    except Exception as e:
        emit("NOAA Atlas 14 PFDS", f"FAIL: {e}")

    log.info("=== probe complete ===")
    return results
