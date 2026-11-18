# Changelog

## 1.0.4

- **Download DEM checkbox.** New option "Download DEM and slope rasters
  (save and add to map)", on by default. When checked, the run saves
  `DEM.tif` and `Slope.tif` (percent rise) in the results folder and
  returns them as the **DEM** and **Slope DEM** outputs so QGIS loads
  them with the CN layers. Checking it fetches the DEM even if "Fetch DEM
  and compute slope" is off; unchecking it still lets slope be computed
  without keeping the rasters.
- **SSURGO outages no longer pass silently.** When USDA Soil Data Access
  is down, the run now stops with a clear message by default. A new
  checkbox, "If USDA SSURGO is down, continue with soil groups estimated
  from land cover (screening only)", lets it finish; the run summary,
  log and composite-CN line are then marked SCREENING ONLY and the
  summary's SSURGO source reads UNAVAILABLE. (Before, the summary still
  said "SSURGO source: SDA WFS".) An AOI that SSURGO simply doesn't
  cover, such as open water, still falls back as before.
- **Estimated soil groups for forest changed from A to B** (and barren
  from A to B). Only runs that fall back to land-cover soil groups are
  affected: forest went from CN 30-36 to CN 55-66, which removes a large
  low bias in forested watersheds.
- The polygon output is now titled "CN polygons" rather than "CN
  polygons (SSURGO mapunits)", since it is a single AOI polygon when no
  SSURGO is used.

## 1.0.3

Homepage in `metadata.txt` now points at this README
(`https://github.com/mf4633/hydrocn-qgis`) instead of hydrocomplete.com.
QGIS plugin review requires the homepage to describe plugin usage; the
README is that page (install, tools, inputs, outputs, limits). No
calculation changes.

## 1.0.2

Same code as 1.0.1. New version number only: plugins.qgis.org already
recorded 1.0.1 as an unapproved upload, so a unique version is required
to submit the public package.

## 1.0.1

Hardening only. Curve numbers, runoff depths and every other result are
unchanged from 1.0.0 — there is no need to re-run past analyses.

- Web requests refuse any URL scheme other than `http` and `https`. Nothing
  previously stopped a `file://` or custom-scheme URL reaching `urlopen` if one
  arrived from configuration or a service response.
- `http_get` accepts a `max_bytes` ceiling, and the SSURGO GML fetch caps at
  64 MB. Without a limit a malfunctioning endpoint could exhaust memory before
  anything parsed. It reads one byte past the cap and stops rather than
  buffering the remainder.
- XML parsing prefers `defusedxml` where the QGIS environment supplies it, and
  otherwise uses the standard-library parser, which has not expanded external
  entities since Python 3.8.
- A soil polygon that cannot be intersected with the area of interest is logged
  at debug rather than skipped silently. One unusable polygon should not abort a
  run, but nor should it hide a data problem.

Released to clear the plugins.qgis.org security scan, which blocks a plugin on
any Bandit finding. The scan now reports zero.

## 1.0.0

First release.

- Composite SCS curve number for an area of interest, from SSURGO soil polygons
  and hydrologic groups (USDA SDA), NLCD 2021 land cover and imperviousness
  (MRLC), and a DEM for slope-resolved dual hydrologic groups (A/D, B/D, C/D).
- TR-55 curve numbers by condition (Good/Fair/Poor) or your own CSV table.
- NOAA Atlas 14 design-storm runoff depths and volumes.
- Outputs a CN polygon layer, a per-cell CN raster on the NLCD grid, and
  CSV/summary artifacts.
- Falls back to ESA WorldCover when the federal land-cover server is down.
- CONUS only. Requires an internet connection.

Ported from HydroCN Builder for ArcGIS Pro by the same author; the data
sources, CN tables and math are identical, so the two produce matching numbers
on the same area of interest.
