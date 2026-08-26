# -*- coding: utf-8 -*-
"""
HydroCN — QGIS Processing algorithms.

CalculateCurveNumberAlgorithm: the main SCS Curve Number pipeline.
ValidateServicesAlgorithm: 30-second probe of every external service.

All raster work happens through GDAL numpy arrays on the NLCD grid; all
vector work through OGR. Nothing here needs anything beyond a stock
QGIS install (GDAL, numpy, and PyQGIS all ship with QGIS).

Licensed GPL-2.0-or-later.
"""

import csv
import math
import os
from datetime import datetime

import numpy as np
from osgeo import gdal, ogr, osr
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureSink,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum,
    QgsProcessingParameterExtent,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterFile,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterNumber,
    QgsProcessingParameterRasterDestination,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterString,
    QgsProject,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QCoreApplication, QVariant

from . import hydrocn_core as core

gdal.UseExceptions()
ogr.UseExceptions()
osr.UseExceptions()

WEB_MERCATOR = "EPSG:3857"
WGS84 = "EPSG:4326"


class FeedbackLog:
    """Adapts QgsProcessingFeedback to the core module's log interface."""

    def __init__(self, feedback):
        self._fb = feedback

    def info(self, msg):
        self._fb.pushInfo(msg)

    def warning(self, msg):
        self._fb.pushWarning(msg)


def _osr_srs(epsg_string):
    srs = osr.SpatialReference()
    srs.SetFromUserInput(epsg_string)
    # x,y == lon,lat ordering regardless of the EPSG axis definition
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def _write_polygons_gpkg(path, layer_name, epsg_string, fields, rows):
    """Write polygons to a GeoPackage.

    fields: [(name, ogr_type), ...]
    rows:   [(ogr.Geometry or wkt string, {field: value}), ...]
    """
    drv = ogr.GetDriverByName("GPKG")
    if os.path.exists(path):
        drv.DeleteDataSource(path)
    ds = drv.CreateDataSource(path)
    layer = ds.CreateLayer(
        layer_name, srs=_osr_srs(epsg_string), geom_type=ogr.wkbMultiPolygon
    )
    for name, ftype in fields:
        layer.CreateField(ogr.FieldDefn(name, ftype))
    defn = layer.GetLayerDefn()
    for geom, attrs in rows:
        if isinstance(geom, str):
            geom = ogr.CreateGeometryFromWkt(geom)
        if geom is None or geom.IsEmpty():
            continue
        feat = ogr.Feature(defn)
        feat.SetGeometry(ogr.ForceToMultiPolygon(geom))
        for name, value in attrs.items():
            feat.SetField(name, value)
        layer.CreateFeature(feat)
        feat = None
    ds = None
    return path


def _read_band(path, band=1, nodata_to=None, dtype=np.float64):
    """Read a raster band to numpy, mapping its NoData value to nodata_to."""
    ds = gdal.Open(path)
    b = ds.GetRasterBand(band)
    arr = b.ReadAsArray().astype(dtype)
    nd = b.GetNoDataValue()
    if nd is not None and nodata_to is not None:
        if np.isnan(nd) if isinstance(nd, float) else False:
            arr[np.isnan(arr)] = nodata_to
        else:
            arr[arr == nd] = nodata_to
    gt = ds.GetGeoTransform()
    proj = ds.GetProjection()
    ds = None
    return arr, gt, proj


def _write_gtiff(path, arr, geotransform, projection_wkt, nodata,
                 gdal_type=gdal.GDT_Float32):
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(
        path, arr.shape[1], arr.shape[0], 1, gdal_type,
        options=["COMPRESS=DEFLATE", "TILED=YES"],
    )
    ds.SetGeoTransform(geotransform)
    ds.SetProjection(projection_wkt)
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(nodata)
    band.WriteArray(arr)
    band.FlushCache()
    ds = None
    return path


class CalculateCurveNumberAlgorithm(QgsProcessingAlgorithm):
    """Area-weighted SCS Curve Number from SSURGO + NLCD, fetched live."""

    AOI = "AOI"
    EXTENT = "EXTENT"
    NLCD_RASTER = "NLCD_RASTER"
    CN_LOOKUP = "CN_LOOKUP"
    SLOPE_THRESHOLD = "SLOPE_THRESHOLD"
    AMC = "AMC"
    HYDRO_CONDITION = "HYDRO_CONDITION"
    CUSTOM_STORMS = "CUSTOM_STORMS"
    FETCH_IMPERVIOUS = "FETCH_IMPERVIOUS"
    FETCH_DEM = "FETCH_DEM"
    OUTPUT = "OUTPUT"
    OUTPUT_CN_RASTER = "OUTPUT_CN_RASTER"
    OUTPUT_FOLDER = "OUTPUT_FOLDER"

    AMC_OPTIONS = ["I (dry)", "II (average)", "III (wet)"]
    AMC_VALUES = ["I", "II", "III"]
    CONDITION_OPTIONS = ["Good", "Fair", "Poor"]

    def tr(self, string):
        return QCoreApplication.translate("HydroCN", string)

    def createInstance(self):
        return CalculateCurveNumberAlgorithm()

    def name(self):
        return "calculatecurvenumber"

    def displayName(self):
        return self.tr("Calculate Curve Number (SSURGO + NLCD)")

    def group(self):
        return self.tr("Hydrology")

    def groupId(self):
        return "hydrology"

    def shortHelpString(self):
        return self.tr(
            "Computes the area-weighted SCS Curve Number for an area of "
            "interest by combining SSURGO soil hydrologic groups with NLCD "
            "2021 land cover, all fetched automatically from free federal "
            "web services (CONUS only).\n\n"
            "Dual hydrologic groups (A/D, B/D, C/D) are resolved to the "
            "drained letter where mean slope (from USGS 3DEP or state "
            "LiDAR DEMs) meets the slope threshold, otherwise the "
            "conservative undrained letter. CN values come from the "
            "built-in TR-55 tables (selectable hydrologic condition) or "
            "your own CSV keyed by NLCD code with columns "
            "NLCD_Code,A,B,C,D.\n\n"
            "Outputs: a polygon layer of SSURGO mapunits with weighted CN "
            "attributes, a per-cell CN raster on the NLCD grid, and a "
            "results folder with per-class breakdown CSV, NOAA Atlas 14 "
            "design-storm runoff CSV, and a run summary.\n\n"
            "Provide an AOI polygon layer or an extent; with both left "
            "empty, the current map canvas extent is used automatically. "
            "An internet connection is required unless you supply your "
            "own NLCD raster."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.AOI, self.tr("Area of Interest (polygon layer)"),
            [QgsProcessing.TypeVectorPolygon], optional=True))
        self.addParameter(QgsProcessingParameterExtent(
            self.EXTENT,
            self.tr("Or: extent (empty = current map canvas extent)"),
            optional=True))
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.NLCD_RASTER,
            self.tr("NLCD raster (leave empty to fetch from MRLC)"),
            optional=True))
        self.addParameter(QgsProcessingParameterFile(
            self.CN_LOOKUP,
            self.tr("CN lookup CSV (NLCD_Code,A,B,C,D; empty = TR-55 built-in)"),
            extension="csv", optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.SLOPE_THRESHOLD,
            self.tr("Slope threshold for dual-HSG drainage (%)"),
            QgsProcessingParameterNumber.Double,
            defaultValue=core.DEFAULT_SLOPE_THRESHOLD_PCT, minValue=0.0))
        self.addParameter(QgsProcessingParameterEnum(
            self.AMC, self.tr("Antecedent moisture condition"),
            options=self.AMC_OPTIONS, defaultValue=1))
        self.addParameter(QgsProcessingParameterEnum(
            self.HYDRO_CONDITION,
            self.tr("Hydrologic condition (forest/pasture/brush/crops)"),
            options=self.CONDITION_OPTIONS, defaultValue=1))
        self.addParameter(QgsProcessingParameterString(
            self.CUSTOM_STORMS,
            self.tr("Custom 24-hr storm depths, inches "
                    "(comma-separated; empty = NOAA Atlas 14)"),
            optional=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.FETCH_IMPERVIOUS,
            self.tr("Fetch MRLC impervious raster and report mean %"),
            defaultValue=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.FETCH_DEM,
            self.tr("Fetch DEM and compute slope (resolves dual HSGs)"),
            defaultValue=True))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, self.tr("CN polygons (SSURGO mapunits)"),
            QgsProcessing.TypeVectorPolygon))
        self.addParameter(QgsProcessingParameterRasterDestination(
            self.OUTPUT_CN_RASTER, self.tr("CN raster")))
        self.addParameter(QgsProcessingParameterFolderDestination(
            self.OUTPUT_FOLDER, self.tr("Results folder (CSVs, summary, "
                                        "downloaded rasters)")))

    # --- AOI resolution -----------------------------------------------------

    def _resolve_aoi(self, parameters, context, feedback):
        """Return (geom_4326, geom_3857) as QgsGeometry singleparts/multiparts."""
        crs_4326 = QgsCoordinateReferenceSystem(WGS84)
        crs_3857 = QgsCoordinateReferenceSystem(WEB_MERCATOR)
        tc = QgsProject.instance().transformContext()

        source = self.parameterAsSource(parameters, self.AOI, context)
        if source is not None and source.featureCount() != 0:
            geoms = []
            for f in source.getFeatures():
                g = f.geometry()
                if g is not None and not g.isEmpty():
                    geoms.append(QgsGeometry(g))
            if not geoms:
                raise QgsProcessingException(
                    "AOI layer has no valid polygon geometries.")
            aoi = QgsGeometry.unaryUnion(geoms)
            src_crs = source.sourceCrs()
        else:
            rect = self.parameterAsExtent(parameters, self.EXTENT, context)
            if rect is not None and not rect.isEmpty():
                src_crs = self.parameterAsExtentCrs(
                    parameters, self.EXTENT, context)
                feedback.pushInfo(
                    f"Using extent AOI in "
                    f"{src_crs.authid() or 'project CRS'}")
            else:
                # No AOI layer and no extent: fall back to the current map
                # canvas, matching the ArcGIS HydroCN Builder behavior.
                # iface is None outside the GUI (qgis_process, scripts).
                try:
                    from qgis.utils import iface
                except ImportError:
                    iface = None
                canvas = iface.mapCanvas() if iface is not None else None
                if canvas is None or canvas.extent().isEmpty():
                    raise QgsProcessingException(
                        "Provide an AOI polygon layer or an extent. (In "
                        "the QGIS window, leaving both empty uses the "
                        "current map canvas extent — but no canvas is "
                        "available here.)")
                rect = canvas.extent()
                src_crs = canvas.mapSettings().destinationCrs()
                feedback.pushInfo(
                    f"No AOI or extent given; using current map canvas "
                    f"extent in {src_crs.authid() or 'project CRS'}")
            aoi = QgsGeometry.fromRect(rect)

        if not src_crs.isValid():
            src_crs = crs_4326

        g4326 = QgsGeometry(aoi)
        if src_crs != crs_4326:
            g4326.transform(QgsCoordinateTransform(src_crs, crs_4326, tc))
        g3857 = QgsGeometry(aoi)
        if src_crs != crs_3857:
            g3857.transform(QgsCoordinateTransform(src_crs, crs_3857, tc))
        return g4326, g3857

    # --- main ---------------------------------------------------------------

    def processAlgorithm(self, parameters, context, feedback):
        log = FeedbackLog(feedback)
        run_state = {"cache": {}, "sda_error_logged": False}
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        out_folder = self.parameterAsString(
            parameters, self.OUTPUT_FOLDER, context)
        os.makedirs(out_folder, exist_ok=True)
        cn_raster_path = self.parameterAsOutputLayer(
            parameters, self.OUTPUT_CN_RASTER, context)

        slope_threshold = self.parameterAsDouble(
            parameters, self.SLOPE_THRESHOLD, context)
        amc = self.AMC_VALUES[
            self.parameterAsEnum(parameters, self.AMC, context)]
        condition = self.CONDITION_OPTIONS[
            self.parameterAsEnum(parameters, self.HYDRO_CONDITION, context)]
        custom_storms_text = self.parameterAsString(
            parameters, self.CUSTOM_STORMS, context) or ""
        fetch_impervious = self.parameterAsBoolean(
            parameters, self.FETCH_IMPERVIOUS, context)
        fetch_dem = self.parameterAsBoolean(
            parameters, self.FETCH_DEM, context)
        cn_lookup_path = self.parameterAsString(
            parameters, self.CN_LOOKUP, context)
        nlcd_layer = self.parameterAsRasterLayer(
            parameters, self.NLCD_RASTER, context)

        # --- AOI + guardrails ----------------------------------------------
        aoi_4326, aoi_3857 = self._resolve_aoi(parameters, context, feedback)
        bb = aoi_4326.boundingBox()
        wgs84_extent = [bb.xMinimum(), bb.yMinimum(),
                        bb.xMaximum(), bb.yMaximum()]
        buffer_deg = 0.005  # ~500 m mid-latitudes
        bbox_buffered = [wgs84_extent[0] - buffer_deg,
                         wgs84_extent[1] - buffer_deg,
                         wgs84_extent[2] + buffer_deg,
                         wgs84_extent[3] + buffer_deg]
        feedback.pushInfo(f"WGS84 bbox (buffered): {bbox_buffered}")

        aoi_km2 = core.bbox_area_km2(wgs84_extent)
        feedback.pushInfo(f"AOI bbox area: ~{aoi_km2:,.2f} km^2")
        if aoi_km2 >= core.AOI_HARD_WARN_KM2:
            feedback.pushWarning(
                f"AOI is ~{aoi_km2:,.0f} km^2 — well above this tool's "
                "comfortable range. NLCD / DEM downloads are capped at "
                "2048 px, yielding cell sizes of several hundred meters. "
                "Results will be impressionistic, not engineering-grade.")
        elif aoi_km2 >= core.AOI_DOWNSAMPLE_WARN_KM2:
            feedback.pushWarning(
                f"AOI is ~{aoi_km2:,.0f} km^2 — NLCD/DEM will be "
                "downsampled coarser than the native 30 m cell. "
                "Consider splitting into sub-basins for detailed work.")

        centroid_lat, centroid_lon = core.centroid_wgs84(wgs84_extent)
        if not core.is_in_conus(centroid_lat, centroid_lon):
            feedback.pushWarning(
                f"AOI centroid ({centroid_lat:.3f}, {centroid_lon:.3f}) is "
                "outside CONUS. NLCD_L48 / MRLC Impervious do not cover "
                "AK / HI / territories, and NOAA Atlas 14 uses regional "
                "volumes. Supply your own NLCD raster and expect runoff "
                "data to be unavailable.")

        # AOI as a GPKG in 3857 — used as warp cutline and rasterize mask.
        aoi_gpkg = os.path.join(out_folder, "aoi_3857.gpkg")
        _write_polygons_gpkg(
            aoi_gpkg, "aoi", WEB_MERCATOR, [("id", ogr.OFTInteger)],
            [(aoi_3857.asWkt(), {"id": 1})])
        aoi_ogr_3857 = ogr.CreateGeometryFromWkt(aoi_3857.asWkt())

        # --- CN lookup ------------------------------------------------------
        if cn_lookup_path:
            cn_lookup = core.load_cn_lookup_csv(cn_lookup_path)
            feedback.pushInfo(
                f"Loaded CN lookup from CSV ({len(cn_lookup)} codes)")
        else:
            cn_lookup = core.default_cn_lookup(condition)
            feedback.pushInfo(
                "Using built-in TR-55 CN lookup table "
                f"(hydrologic condition: {condition})")

        feedback.setProgress(5)
        if feedback.isCanceled():
            return {}

        # --- NLCD -----------------------------------------------------------
        if nlcd_layer is not None:
            nlcd_src = nlcd_layer.source()
            feedback.pushInfo(f"Using provided NLCD raster: {nlcd_src}")
        else:
            nlcd_src = os.path.join(out_folder, f"nlcd_{ts}.tif")
            core.fetch_mrlc_wms(
                bbox_buffered, core.NLCD_LAYER, nlcd_src, log, label="NLCD")

        clipped_nlcd = os.path.join(out_folder, f"nlcd_clipped_{ts}.tif")
        try:
            gdal.Warp(
                clipped_nlcd, nlcd_src, dstSRS=WEB_MERCATOR,
                cutlineDSName=aoi_gpkg, cutlineLayer="aoi",
                cropToCutline=True, dstNodata=0, resampleAlg="near",
                format="GTiff")
        except Exception as e:
            raise QgsProcessingException(
                f"Could not project/clip the NLCD raster: {e}")

        feedback.setProgress(20)
        if feedback.isCanceled():
            return {}

        # --- DEM + slope ----------------------------------------------------
        # Needed to resolve dual-class hydrologic groups (e.g. B/D) into
        # drained vs. undrained per the slope threshold. Graceful
        # degradation: without slope, duals fall back to the conservative
        # undrained letter.
        dem_source_label = None
        slope_tif = None
        if fetch_dem:
            dem_tif = os.path.join(out_folder, f"dem_{ts}.tif")
            try:
                dem_source_label = core.fetch_dem(bbox_buffered, dem_tif, log)
                feedback.pushInfo(f"DEM source: {dem_source_label}")
            except Exception as e:
                feedback.pushWarning(
                    f"DEM fetch failed: {e}. Slope will not be computed; "
                    "dual-HSG soils will default to the undrained letter.")
                dem_tif = None

            if dem_tif:
                try:
                    dem_arr, dem_gt, dem_proj = _read_band(
                        dem_tif, nodata_to=np.nan)
                    # DEM arrives in Web Mercator, whose planar meters are
                    # inflated by 1/cos(lat); shrink the cell size so run
                    # (and therefore slope) is computed in true ground
                    # meters. (Improvement over the ArcGIS v1, which used
                    # planar cells and underestimated slope by cos(lat).)
                    cos_lat = math.cos(math.radians(centroid_lat))
                    slope = core.slope_percent_from_array(
                        dem_arr, abs(dem_gt[1]) * cos_lat,
                        abs(dem_gt[5]) * cos_lat)
                    slope_tif = os.path.join(out_folder, f"slope_{ts}.tif")
                    slope_out = np.where(
                        np.isnan(slope), -9999.0, slope).astype(np.float32)
                    _write_gtiff(slope_tif, slope_out, dem_gt, dem_proj,
                                 -9999.0)
                    feedback.pushInfo(
                        f"Slope raster (Horn 3x3, % rise): {slope_tif}")
                except Exception as e:
                    feedback.pushWarning(
                        f"Slope computation failed: {e}. Dual-HSG soils "
                        "will default to the undrained letter.")
                    slope_tif = None
        else:
            feedback.pushInfo(
                "DEM fetch disabled; dual-HSG soils will use the "
                "conservative undrained letter.")

        feedback.setProgress(35)
        if feedback.isCanceled():
            return {}

        # NLCD grid geometry — everything downstream aligns to this.
        nlcd_ds = gdal.Open(clipped_nlcd)
        nlcd_gt = nlcd_ds.GetGeoTransform()
        nlcd_proj = nlcd_ds.GetProjection()
        n_cols, n_rows = nlcd_ds.RasterXSize, nlcd_ds.RasterYSize
        nlcd_ds = None
        cell_x = abs(nlcd_gt[1])
        cell_y = abs(nlcd_gt[5])
        xmin = nlcd_gt[0]
        ymax = nlcd_gt[3]
        xmax = xmin + cell_x * n_cols
        ymin = ymax - cell_y * n_rows
        grid_bounds = (xmin, ymin, xmax, ymax)

        slope_aligned = None
        if slope_tif:
            try:
                slope_aligned = os.path.join(
                    out_folder, f"slope_aligned_{ts}.tif")
                gdal.Warp(
                    slope_aligned, slope_tif, dstSRS=WEB_MERCATOR,
                    outputBounds=grid_bounds, xRes=cell_x, yRes=cell_y,
                    resampleAlg="bilinear", dstNodata=-9999.0,
                    format="GTiff")
            except Exception as e:
                feedback.pushWarning(
                    f"Slope alignment failed: {e}. Dual-HSG soils will "
                    "default to the undrained letter.")
                slope_aligned = None

        # --- Impervious -----------------------------------------------------
        mean_impervious_pct = None
        if fetch_impervious:
            try:
                imp_tif = os.path.join(out_folder, f"impervious_{ts}.tif")
                core.fetch_mrlc_wms(
                    bbox_buffered, core.NLCD_IMPERVIOUS_LAYER, imp_tif, log,
                    label="Impervious")
                imp_clipped = os.path.join(
                    out_folder, f"impervious_clipped_{ts}.tif")
                gdal.Warp(
                    imp_clipped, imp_tif, dstSRS=WEB_MERCATOR,
                    outputBounds=grid_bounds, xRes=cell_x, yRes=cell_y,
                    cutlineDSName=aoi_gpkg, cutlineLayer="aoi",
                    resampleAlg="bilinear", dstNodata=255, format="GTiff")
                imp_arr, _, _ = _read_band(imp_clipped, nodata_to=np.nan)
                valid = imp_arr[(~np.isnan(imp_arr)) & (imp_arr <= 100)]
                if valid.size:
                    mean_impervious_pct = float(valid.mean())
                    feedback.pushInfo(
                        "Mean imperviousness (NLCD): "
                        f"{mean_impervious_pct:.2f}%")
            except Exception as e:
                feedback.pushWarning(f"Impervious fetch failed: {e}")

        feedback.setProgress(45)
        if feedback.isCanceled():
            return {}

        # --- SSURGO ---------------------------------------------------------
        # Fetch mapunit polygons over the bbox, clip each to the AOI in
        # 3857 with OGR, and rasterize a sequential OID onto the NLCD grid
        # so the classifier can work entirely in numpy.
        clipped_soils = []  # [(ogr geom 3857, mukey, musym)]
        try:
            gml_text = core.fetch_ssurgo_gml(bbox_buffered, log)
            gml_path = os.path.join(out_folder, f"ssurgo_{ts}.gml")
            with open(gml_path, "w", encoding="utf-8") as f:
                f.write(gml_text)
            parsed = core.parse_ssurgo_gml(gml_text)
            feedback.pushInfo(f"SSURGO features parsed: {len(parsed)}")

            tx = osr.CoordinateTransformation(
                _osr_srs(WGS84), _osr_srs(WEB_MERCATOR))

            def clip_to(target_geom):
                out = []
                for wkt, mukey, musym in parsed:
                    g = ogr.CreateGeometryFromWkt(wkt)
                    if g is None:
                        continue
                    g.Transform(tx)
                    if not g.IsValid():
                        g = g.Buffer(0)
                    try:
                        inter = g.Intersection(target_geom)
                    except Exception:
                        continue
                    if inter is not None and not inter.IsEmpty() \
                            and inter.GetArea() > 0:
                        out.append((inter, mukey, musym))
                return out

            clipped_soils = clip_to(aoi_ogr_3857)
            feedback.pushInfo(
                f"Clipped SSURGO: {len(clipped_soils)} polygons")
            if not clipped_soils:
                feedback.pushInfo(
                    "No SSURGO in AOI; retrying with 1 km buffer.")
                clipped_soils = clip_to(aoi_ogr_3857.Buffer(1000.0))
                if clipped_soils:
                    feedback.pushInfo(
                        f"Buffered clip yielded {len(clipped_soils)} polygons")
        except Exception as e:
            feedback.pushWarning(
                f"SSURGO unavailable: {e}. Falling back to NLCD-derived "
                "hydrologic groups for the whole AOI.")
            clipped_soils = []

        has_soils = bool(clipped_soils)
        ssurgo_oid_raster = None
        oid_to_attrs = {}
        if has_soils:
            rows = []
            for oid, (geom, mukey, musym) in enumerate(clipped_soils, 1):
                oid_to_attrs[oid] = {"MUKEY": mukey, "MUSYM": musym}
                rows.append((geom, {"OID": oid, "MUKEY": mukey,
                                    "MUSYM": musym}))
            soils_gpkg = os.path.join(out_folder, f"ssurgo_clipped_{ts}.gpkg")
            _write_polygons_gpkg(
                soils_gpkg, "soils", WEB_MERCATOR,
                [("OID", ogr.OFTInteger), ("MUKEY", ogr.OFTString),
                 ("MUSYM", ogr.OFTString)],
                rows)
            try:
                ssurgo_oid_raster = os.path.join(
                    out_folder, f"ssurgo_oid_{ts}.tif")
                gdal.Rasterize(
                    ssurgo_oid_raster, soils_gpkg, layers=["soils"],
                    attribute="OID", outputBounds=grid_bounds,
                    xRes=cell_x, yRes=cell_y, outputSRS=WEB_MERCATOR,
                    noData=-1, initValues=-1,
                    outputType=gdal.GDT_Int32, format="GTiff")
                feedback.pushInfo(
                    f"Rasterized SSURGO ({len(oid_to_attrs)} polygons) "
                    "to NLCD grid")
            except Exception as e:
                feedback.pushWarning(
                    f"SSURGO rasterization failed: {e}. Falling back to "
                    "NLCD-only classification with NLCD-derived HSG.")
                ssurgo_oid_raster = None
                has_soils = False

        feedback.setProgress(55)
        if feedback.isCanceled():
            return {}

        # --- MUKEY -> hydrologic group (SDA REST, cached per run) -----------
        oid_to_raw_hsg = {}
        if has_soils:
            for oid, attrs in oid_to_attrs.items():
                if feedback.isCanceled():
                    return {}
                raw = core.get_hydro_group_from_mukey(
                    attrs["MUKEY"], run_state, log)
                if raw:
                    oid_to_raw_hsg[oid] = raw

        feedback.setProgress(70)

        # --- classify -------------------------------------------------------
        nlcd_arr, _, _ = _read_band(clipped_nlcd, nodata_to=0,
                                    dtype=np.int32)
        if ssurgo_oid_raster:
            soil_arr, _, _ = _read_band(ssurgo_oid_raster, nodata_to=-1,
                                        dtype=np.int64)
        else:
            soil_arr = np.full(nlcd_arr.shape, -1, dtype=np.int64)
        if slope_aligned:
            slope_arr, _, _ = _read_band(slope_aligned, nodata_to=np.nan)
            slope_arr[slope_arr < 0] = np.nan
        else:
            slope_arr = np.full(nlcd_arr.shape, np.nan, dtype=np.float64)

        n_r = min(nlcd_arr.shape[0], soil_arr.shape[0], slope_arr.shape[0])
        n_c = min(nlcd_arr.shape[1], soil_arr.shape[1], slope_arr.shape[1])
        nlcd_arr = nlcd_arr[:n_r, :n_c]
        soil_arr = soil_arr[:n_r, :n_c]
        slope_arr = slope_arr[:n_r, :n_c]

        # Web Mercator distorts area by sec^2(lat); apply cos^2(center_lat)
        # so accumulated cell areas match geodesic ground area.
        cos2_lat = math.cos(math.radians(centroid_lat)) ** 2
        cell_area_m2 = cell_x * cell_y * cos2_lat
        feedback.pushInfo(
            f"Cell area: {cell_x:.1f}x{cell_y:.1f} m planar * "
            f"cos^2({centroid_lat:.3f}deg) = {cell_area_m2:.2f} m^2 ground")

        cn_arr, cn_adj_arr, per_class, per_polygon = (
            core.accumulate_per_class_per_polygon(
                nlcd_arr, soil_arr, slope_arr, oid_to_raw_hsg, cn_lookup,
                slope_threshold, amc, cell_area_m2))

        # --- CN raster ------------------------------------------------------
        CN_NODATA = -1.0
        cn_out = np.where(np.isnan(cn_arr), CN_NODATA, cn_arr).astype(
            np.float32)
        _write_gtiff(cn_raster_path, cn_out, nlcd_gt, nlcd_proj, CN_NODATA)
        feedback.pushInfo(f"CN raster: {cn_raster_path}")

        total_area = sum(v["area"] for v in per_class.values())
        weighted_cn_sum = sum(v["cn_area"] for v in per_class.values())
        weighted_cn_adj_sum = sum(
            v["cn_adj_area"] for v in per_class.values())

        feedback.setProgress(80)

        # --- output polygons ------------------------------------------------
        fields = QgsFields()
        for name, ftype in (
            ("MUKEY", QVariant.String), ("MUSYM", QVariant.String),
            ("HydroGroupRaw", QVariant.String),
            ("HydroGroup", QVariant.String),
            ("DominantNLCD", QVariant.Int),
            ("DominantNLCD_Pct", QVariant.Double),
            ("SlopePct", QVariant.Double),
            ("CN", QVariant.Double), ("CN_Adj", QVariant.Double),
            ("Area_SqM", QVariant.Double),
        ):
            fields.append(QgsField(name, ftype))

        sink, dest_id = self.parameterAsSink(
            parameters, self.OUTPUT, context, fields,
            QgsWkbTypes.MultiPolygon,
            QgsCoordinateReferenceSystem(WEB_MERCATOR))
        if sink is None:
            raise QgsProcessingException(
                self.invalidSinkError(parameters, self.OUTPUT))

        polygons_written = 0
        if has_soils:
            for oid, (geom, mukey, musym) in enumerate(clipped_soils, 1):
                pp = per_polygon.get(oid)
                if pp is None or pp["area"] <= 0:
                    continue
                cn_w = pp["cn_area"] / pp["area"]
                cn_adj_w = pp["cn_adj_area"] / pp["area"]
                slope_mean = (pp["slope_sum"] / pp["slope_n"]
                              if pp["slope_n"] > 0 else None)
                dom_nlcd, dom_count = max(
                    pp["nlcd_counts"].items(), key=lambda kv: kv[1])
                nlcd_total_cells = sum(pp["nlcd_counts"].values())
                dom_pct = (100.0 * dom_count / nlcd_total_cells
                           if nlcd_total_cells > 0 else 0.0)
                hsg_str = ("/".join(sorted(pp["resolved_groups"]))
                           if pp["resolved_groups"] else "")
                f = QgsFeature(fields)
                f.setGeometry(QgsGeometry.fromWkt(geom.ExportToWkt()))
                f.setAttributes([
                    mukey, musym, pp["raw_group"] or "", hsg_str,
                    int(dom_nlcd), dom_pct, slope_mean,
                    cn_w, cn_adj_w, pp["area"],
                ])
                sink.addFeature(f, QgsFeatureSink.FastInsert)
                polygons_written += 1
        elif total_area > 0:
            # No SSURGO: emit one AOI polygon with composite stats.
            cn_w = weighted_cn_sum / total_area
            cn_adj_w = weighted_cn_adj_sum / total_area
            nlcd_total = {}
            for (n, _h), v in per_class.items():
                nlcd_total[n] = nlcd_total.get(n, 0.0) + v["area"]
            if nlcd_total:
                dom_nlcd, dom_area = max(
                    nlcd_total.items(), key=lambda kv: kv[1])
                dom_pct = 100.0 * dom_area / total_area
            else:
                dom_nlcd, dom_pct = 0, 0.0
            f = QgsFeature(fields)
            f.setGeometry(aoi_3857)
            f.setAttributes(["", "", "", "", int(dom_nlcd), dom_pct, None,
                             cn_w, cn_adj_w, total_area])
            sink.addFeature(f, QgsFeatureSink.FastInsert)
            polygons_written = 1

        feedback.pushInfo(
            f"Classified {total_area / 10000:.1f} ha into "
            f"{len(per_class)} (NLCD x HSG) classes"
            + (f" across {polygons_written} SSURGO polygons"
               if has_soils else " (no SSURGO; single AOI polygon)"))

        # --- composite CN + runoff ------------------------------------------
        final_cn = (weighted_cn_sum / total_area) if total_area > 0 else 0.0
        # Two conventions for the AMC-adjusted composite:
        #   (a) adjust the weighted composite once (simpler, standard)
        #   (b) weight the per-class CN_Adj — differs slightly because the
        #       AMC formulas are non-linear. Both are reported.
        final_cn_adj = core.apply_amc_adjustment(final_cn, amc)
        final_cn_adj_weighted = (
            (weighted_cn_adj_sum / total_area) if total_area > 0 else 0.0)
        total_area_ac = total_area / core.SQM_PER_ACRE
        total_area_sqmi = total_area / core.SQM_PER_SQMI
        feedback.pushInfo(
            f"Total area: {total_area:,.2f} m^2 "
            f"({total_area_ac:,.2f} ac, {total_area_sqmi:.3f} sq mi)")
        feedback.pushInfo(
            f"Area-weighted CN (AMC-II): {final_cn:.2f}"
            + (f"   CN (AMC-{amc}): {final_cn_adj:.2f}"
               if amc != "II" else ""))
        if total_area == 0:
            feedback.pushWarning(
                "No valid CN cells were produced. Check that the AOI "
                "overlaps SSURGO and NLCD coverage.")

        # Per-class breakdown CSV
        breakdown_csv = os.path.join(out_folder, f"cn_breakdown_{ts}.csv")
        try:
            with open(breakdown_csv, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["NLCD_Code", "NLCD_Class", "HydroGroup",
                            "Area_SqM", "Area_Ac", "Pct_of_AOI",
                            "Weighted_CN"])
                for (nlcd_code, hsg), v in sorted(per_class.items()):
                    a = v["area"]
                    wcn = (v["cn_area"] / a) if a > 0 else 0.0
                    pct = (100.0 * a / total_area) if total_area > 0 else 0.0
                    w.writerow([
                        nlcd_code, core.NLCD_CLASS_NAMES.get(nlcd_code, ""),
                        hsg, f"{a:.2f}", f"{a / core.SQM_PER_ACRE:.4f}",
                        f"{pct:.2f}", f"{wcn:.2f}"])
            feedback.pushInfo(f"Per-class breakdown: {breakdown_csv}")
        except Exception as e:
            feedback.pushWarning(f"Breakdown CSV write failed: {e}")
            breakdown_csv = None

        # Design storms: user depths or NOAA Atlas 14
        storm_depths = []
        custom = [d.strip() for d in custom_storms_text.split(",")
                  if d.strip()] if custom_storms_text else []
        if custom:
            for d in custom:
                try:
                    storm_depths.append((f"{float(d):g}-in", float(d)))
                except ValueError:
                    feedback.pushWarning(
                        f"Ignoring non-numeric storm depth: {d}")
        else:
            pfds = core.fetch_noaa_atlas14_pfds(
                centroid_lat, centroid_lon, log)
            if pfds and "24-hr" in pfds:
                for ret_period in sorted(pfds["24-hr"].keys()):
                    storm_depths.append((
                        f"{ret_period}-yr 24-hr", pfds["24-hr"][ret_period]))
                feedback.pushInfo(
                    f"NOAA Atlas 14 at ({centroid_lat:.4f}, "
                    f"{centroid_lon:.4f}): {len(storm_depths)} "
                    "return periods")

        runoff_rows = []
        runoff_csv = None
        if storm_depths and total_area > 0:
            for label, depth in storm_depths:
                q_in = core.compute_scs_runoff(depth, final_cn_adj)
                vol_acft = q_in * total_area_ac / 12.0
                vol_m3 = q_in * 0.0254 * total_area
                runoff_rows.append((label, depth, q_in, vol_acft, vol_m3))
            runoff_csv = os.path.join(out_folder, f"runoff_summary_{ts}.csv")
            try:
                with open(runoff_csv, "w", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["Storm", "P_in", "Q_in", "Volume_AcFt",
                                "Volume_m3"])
                    for label, depth, q, vaf, vm3 in runoff_rows:
                        w.writerow([label, f"{depth:.3f}", f"{q:.3f}",
                                    f"{vaf:.2f}", f"{vm3:.1f}"])
                feedback.pushInfo(f"Runoff summary: {runoff_csv}")
            except Exception as e:
                feedback.pushWarning(f"Runoff CSV write failed: {e}")
                runoff_csv = None

        # Run summary
        summary_file = os.path.join(out_folder, f"run_summary_{ts}.txt")
        dual_resolved = sum(
            1 for v in run_state["cache"].values()
            if isinstance(v, str) and "/" in v)
        with open(summary_file, "w") as f:
            f.write("HydroCN for QGIS - Run Summary\n")
            f.write("==============================\n")
            f.write(f"Run date:            "
                    f"{datetime.now().isoformat(timespec='seconds')}\n")
            f.write("\n-- Data sources --\n")
            f.write(f"NLCD:                "
                    f"{'user raster' if nlcd_layer else core.NLCD_LAYER}\n")
            f.write(f"Impervious layer:    "
                    f"{core.NLCD_IMPERVIOUS_LAYER if fetch_impervious else '(not fetched)'}\n")
            f.write("SSURGO source:       SDA WFS\n")
            f.write(f"DEM source:          "
                    f"{dem_source_label or '(unavailable)'}\n")
            f.write(f"Slope threshold:     {slope_threshold} %\n")
            f.write("Slope engine:        numpy Horn 3x3 "
                    "(ground-corrected cells)\n")
            f.write("\n-- Watershed totals --\n")
            f.write(f"Area:                {total_area:,.2f} m^2  "
                    f"({total_area_ac:,.2f} ac, "
                    f"{total_area_sqmi:.3f} sq mi)\n")
            f.write(f"Composite CN (II):   {final_cn:.2f}\n")
            f.write(f"Hydro condition:     {condition}\n")
            f.write(f"AMC requested:       {amc}\n")
            f.write(f"Composite CN (adj):  {final_cn_adj:.2f}  "
                    "(adjust composite)\n")
            f.write(f"Composite CN (adj):  {final_cn_adj_weighted:.2f}  "
                    "(weighted per-polygon CN_Adj)\n")
            if mean_impervious_pct is not None:
                f.write(f"Mean impervious:     {mean_impervious_pct:.2f} %\n")
            else:
                f.write("Mean impervious:     (unavailable)\n")
            f.write("\n-- Design-storm runoff (using CN-adjusted) --\n")
            if runoff_rows:
                f.write(f"{'Storm':<22}{'P (in)':>10}{'Q (in)':>10}"
                        f"{'V (ac-ft)':>12}{'V (m^3)':>14}\n")
                for label, depth, q, vaf, vm3 in runoff_rows:
                    f.write(f"{label:<22}{depth:>10.3f}{q:>10.3f}"
                            f"{vaf:>12.2f}{vm3:>14.1f}\n")
            else:
                f.write("(no storm depths available - NOAA Atlas 14 fetch "
                        "failed or no custom depths provided)\n")
            f.write("\n-- Diagnostics --\n")
            f.write(f"SDA MUKEY hits:      "
                    f"{sum(1 for v in run_state['cache'].values() if v)}\n")
            f.write(f"SDA MUKEY misses:    "
                    f"{sum(1 for v in run_state['cache'].values() if not v)}\n")
            f.write(f"Dual-HSG MUKEYs:     {dual_resolved}\n")
            f.write(f"NLCD x HSG classes:  {len(per_class)}\n")
            f.write(f"SSURGO polygons:     "
                    f"{len(per_polygon) if has_soils else 0}\n")
        feedback.pushInfo(f"Summary: {summary_file}")
        feedback.setProgress(100)

        return {self.OUTPUT: dest_id,
                self.OUTPUT_CN_RASTER: cn_raster_path,
                self.OUTPUT_FOLDER: out_folder}


class ValidateServicesAlgorithm(QgsProcessingAlgorithm):
    """Probe every external service HydroCN depends on."""

    def tr(self, string):
        return QCoreApplication.translate("HydroCN", string)

    def createInstance(self):
        return ValidateServicesAlgorithm()

    def name(self):
        return "validateservices"

    def displayName(self):
        return self.tr("Validate Web Services")

    def group(self):
        return self.tr("Hydrology")

    def groupId(self):
        return "hydrology"

    def shortHelpString(self):
        return self.tr(
            "Runs a ~30-second probe of every external service HydroCN "
            "depends on (MRLC WMS, USDA SDA REST + WFS, state DEM "
            "ImageServers, USGS 3DEP, NOAA Atlas 14) and reports which "
            "are reachable. Useful after network or firewall changes, "
            "or when a Curve Number run produced warnings.")

    def flags(self):
        return super().flags() | QgsProcessingAlgorithm.FlagNoThreading

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        results = core.run_service_probe(FeedbackLog(feedback))
        failed = [name for name, status in results
                  if status.startswith("FAIL")]
        return {"OK": len(results) - len(failed), "FAILED": len(failed)}
