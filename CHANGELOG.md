# Changelog

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
