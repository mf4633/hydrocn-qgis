# HydroCN for QGIS

Free QGIS plugin that computes the **area-weighted SCS Curve Number** for a
watershed or site with zero manual data prep. Draw or load an AOI polygon,
run one Processing tool, and get composite CN, a CN raster, per-class
breakdowns, mean imperviousness, and NOAA Atlas 14 design-storm runoff.

Ported from the author's **HydroCN Builder** toolbox for ArcGIS Pro — same
data sources, same TR-55 tables, same math, so the two tools produce
matching numbers on the same AOI.

## What it fetches (CONUS only, all free federal/state services)

| Data | Source |
|---|---|
| SSURGO soil polygons | USDA Soil Data Access WFS (`mapunitpoly`) |
| Hydrologic soil groups | USDA SDA REST (`component.hydgrp`, dominant component) |
| NLCD 2021 land cover | MRLC WMS |
| NLCD 2021 percent impervious | MRLC WMS |
| DEM (for slope) | NC OneMap DEM03 / ISGS Illinois LiDAR where applicable, else USGS 3DEP |
| 24-hr design storm depths | NOAA Atlas 14 PFDS point estimates |
| Backup land cover | ESA WorldCover 2021 10 m (public AWS S3 COGs) |

**Resilience**: transient service errors (5xx, timeouts) are retried
automatically with backoff. If the MRLC NLCD service is down entirely,
the tool falls back to ESA WorldCover 2021 (an infrastructure-independent
host), remapping its classes to their nearest NLCD equivalents so the
full pipeline still runs — with a clear warning, since ESA has a single
"built-up" class (mapped to NLCD developed-medium) and no
developed-intensity detail. Results under the backup are an
approximation; rerun against real NLCD for design-grade numbers.

## Method

- SSURGO mapunit polygons are clipped to the AOI and rasterized onto the
  NLCD grid; classification runs per unique (NLCD class x soil polygon)
  combination entirely in numpy.
- **Dual hydrologic groups** (A/D, B/D, C/D) resolve to the *drained*
  letter where the combination's mean slope meets the slope threshold
  (default 2%), otherwise the conservative *undrained* letter. Slope comes
  from a Horn (1981) 3x3 finite-difference on the fetched DEM, with cell
  sizes corrected from Web Mercator planar meters to ground meters.
- CN values come from built-in TR-55 / NEH-630 tables with a selectable
  hydrologic condition (Good / Fair / Poor) for forest, brush, pasture,
  and row-crop classes — or supply your own CSV keyed by NLCD code with
  columns `NLCD_Code,A,B,C,D`.
- Composite CN is AMC-II by default; AMC-I (dry) and AMC-III (wet)
  adjustments use the NEH-630 formulas. The summary reports both
  adjust-the-composite and weight-the-adjusted conventions (they differ
  slightly because the formulas are non-linear).
- Design-storm runoff uses the standard SCS relation
  `Q = (P - 0.2S)^2 / (P + 0.8S)` for each NOAA Atlas 14 return period
  (or your custom depths), with volumes in ac-ft and m^3.
- Cell areas are cos^2(lat)-corrected so accumulated areas match geodesic
  ground area despite the Web Mercator working projection.

## Outputs

- **CN polygons** — one feature per SSURGO mapunit with `MUKEY`, `MUSYM`,
  raw and resolved hydrologic group, dominant NLCD class, mean slope,
  weighted `CN` / `CN_Adj`, and area.
- **CN raster** — per-cell CN on the NLCD grid (preserves the full
  NLCD x soil granularity the polygon layer dissolves away).
- **Results folder** — per-class breakdown CSV, runoff summary CSV, run
  summary text file, plus the downloaded DEM / slope / NLCD / impervious /
  SSURGO artifacts for inspection.

## Install

From a release zip: QGIS → *Plugins → Manage and Install Plugins →
Install from ZIP* → pick `hydrocn.zip`.

From source: copy the `hydrocn/` folder into your QGIS profile's
`python/plugins/` directory and enable the plugin.

The tools appear in the **Processing Toolbox** under
**HydroCN → Hydrology**:

- *Calculate Curve Number (SSURGO + NLCD)* — the main tool. Provide an
  AOI polygon layer **or** an extent (the extent widget offers "Use
  Current Map Canvas Extent").
- *Validate Web Services* — 30-second reachability probe of every
  external service; run it first if a CN run produced warnings.

No dependencies beyond a stock QGIS 3.22+ install (GDAL and numpy ship
with QGIS). An internet connection is required unless you supply your own
NLCD raster.

## Limits and honesty notes

- CONUS only: NLCD L48 and NOAA Atlas 14 don't cover AK/HI/territories
  uniformly. The tool warns rather than refuses.
- Downloads are capped at 2048 px per raster, so AOIs beyond ~50 km²
  are downsampled below NLCD's native 30 m (warning emitted); beyond
  ~500 km² results are impressionistic (stronger warning).
- SSURGO hydrologic groups use the dominant component; minor components
  are ignored. Where SDA has no group, an NLCD-based estimate fills in
  and the polygon's `HydroGroupRaw` is blank.
- This tool automates lookups an engineer would otherwise do by hand.
  It does not replace engineering judgment; verify results before use
  in design or permitting.

## Tests

The math/parser core (`hydrocn/hydrocn_core.py`) has no QGIS
dependencies. Run its tests from any Python 3 with numpy:

```
python tests/test_core.py
# or: python -m pytest tests/
```

## Part of the HydroComplete ecosystem

HydroCN is one front door to a family of formula-transparent water-resources
tools by the same author, on whatever platform you already work in:

- **Browser** — [HydroComplete](https://hydrocomplete.com): full stormwater design suite
- **Civil 3D** — [hydrocomplete-civil3d](https://github.com/mf4633/hydrocomplete-civil3d): hydraulics add-in with a Rust/WASM calc engine
- **Engines** — [stormsewer](https://github.com/mf4633/stormsewer) (native Rust) · [hydro-tools](https://github.com/mf4633/hydro-tools) (open hydrology primitives)
- **Research** — [swmm-breach](https://github.com/mf4633/swmm-breach): dam-breach hydrographs for EPA SWMM
- **Free calculators** — [pe-calc.com](https://pe-calc.com)

## Author

Built by [Michael Flynn](https://github.com/mf4633), the maker of
[HydroComplete](https://hydrocomplete.com) — browser-based stormwater
design tools (hydrology, hydraulics, detention routing, sediment) for
civil engineers.

- Source & issues: https://github.com/mf4633/hydrocn-qgis
- HydroComplete: https://hydrocomplete.com

HydroCN is free. If it saved you an afternoon of GIS drudgery, you can
[buy me a coffee](https://buy.stripe.com/14A3cudxo91z1qo0OHdAk00?client_reference_id=hydrocn-qgis).

## License

GPL-2.0-or-later (QGIS plugin requirement). See `hydrocn/LICENSE`.
