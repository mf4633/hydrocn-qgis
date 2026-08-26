"""HydroCN integration tests — headless QGIS, live federal services.

Requires a QGIS install and network access; run with QGIS's python, e.g.
  "C:\Program Files\QGIS 3.xin\python-qgis.bat" tests/test_scenarios_qgis.py
(adjust the hardcoded QGIS paths below for your install).

Usage: python-qgis.bat test_scenarios.py <scenario> [<scenario> ...]
Scenarios: load r1 r2 r3 r4 validate
Prints CHECK lines; final line SCENARIOS: <n passed>/<n total checks>.
"""
import csv
import os
import sys
import traceback

SCRATCH = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(SCRATCH, "hydrocn_scenarios")
os.makedirs(OUT, exist_ok=True)

from qgis.core import QgsApplication  # noqa: E402

qgs = QgsApplication([], False)
qgs.initQgis()
sys.path.append(r"C:\Program Files\QGIS 3.24.0\apps\qgis\python\plugins")
from processing.core.Processing import Processing  # noqa: E402
import processing  # noqa: E402

Processing.initialize()
sys.path.insert(0, r"C:\Projects\hydrocn-qgis")

CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append(bool(cond))
    print(f"CHECK {'PASS' if cond else 'FAIL'}: {name}"
          + (f" [{detail}]" if detail else ""))


def read_summary(folder):
    files = sorted(f for f in os.listdir(folder)
                   if f.startswith("run_summary_"))
    with open(os.path.join(folder, files[-1])) as f:
        return f.read()


def run_alg(params):
    from qgis.core import QgsProcessingFeedback

    reg = QgsApplication.processingRegistry()
    p = reg.providerById("hydrocn")
    print("  [pre-run] provider:", "yes" if p else "no",
          "| algs:", [a.id() for a in p.algorithms()] if p else "-",
          "| lookup:", "ok" if reg.algorithmById(
              "hydrocn:calculatecurvenumber") else "MISSING",
          "| create:", "ok" if reg.createAlgorithmById(
              "hydrocn:calculatecurvenumber") else "NONE")

    class FB(QgsProcessingFeedback):
        def pushInfo(self, m):
            print("  [i]", m)

        def pushWarning(self, m):
            print("  [w]", m)

    return processing.run("hydrocn:calculatecurvenumber", params,
                          feedback=FB())


def scenario_load():
    """Plugin load/unload through the real classFactory path."""
    import hydrocn
    plugin = hydrocn.classFactory(None)  # iface unused by this plugin
    plugin.initGui()
    reg = QgsApplication.processingRegistry()
    check("provider registered after initGui",
          reg.providerById("hydrocn") is not None)
    algs = [a.id() for a in reg.providerById("hydrocn").algorithms()]
    check("both algorithms present",
          set(algs) == {"hydrocn:calculatecurvenumber",
                        "hydrocn:validateservices"}, str(algs))
    plugin.unload()
    check("provider removed after unload",
          reg.providerById("hydrocn") is None)
    # re-register for the run scenarios; keep a module-level ref so the
    # test harness itself isn't the thing dropping the plugin
    global _PLUGIN2
    _PLUGIN2 = hydrocn.classFactory(None)
    _PLUGIN2.initGui()
    alg = reg.algorithmById("hydrocn:calculatecurvenumber")
    check("algorithm resolvable after reload cycle",
          alg is not None and alg.id() == "hydrocn:calculatecurvenumber")


def ensure_provider():
    # Hold a module-level reference: on QGIS 3.24 a provider whose Python
    # wrapper is GC'd leaves zombie algorithms in the registry. (The real
    # plugin holds self.provider, and qgis.utils holds the plugin.)
    global _PROVIDER
    reg = QgsApplication.processingRegistry()
    if reg.providerById("hydrocn") is None:
        from hydrocn.hydrocn_provider import HydroCNProvider
        _PROVIDER = HydroCNProvider()
        reg.addProvider(_PROVIDER)


def scenario_r1():
    """AOI polygon layer in EPSG:2264 (NC StatePlane ft) + custom CN CSV +
    AMC III + Poor condition + custom storm depths."""
    from qgis.core import (
        QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature,
        QgsGeometry, QgsProject, QgsRectangle, QgsVectorFileWriter,
        QgsVectorLayer, QgsFields)

    # Build the Raleigh probe bbox as a polygon reprojected into 2264
    tc = QgsProject.instance().transformContext()
    tx = QgsCoordinateTransform(
        QgsCoordinateReferenceSystem("EPSG:4326"),
        QgsCoordinateReferenceSystem("EPSG:2264"), tc)
    g = QgsGeometry.fromRect(QgsRectangle(-78.650, 35.780, -78.640, 35.790))
    g.transform(tx)
    layer = QgsVectorLayer("Polygon?crs=EPSG:2264", "aoi", "memory")
    f = QgsFeature()
    f.setGeometry(g)
    layer.dataProvider().addFeatures([f])
    aoi_path = os.path.join(OUT, "aoi_2264.gpkg")
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    QgsVectorFileWriter.writeAsVectorFormatV3(layer, aoi_path, tc, opts)

    # Custom CN CSV: every class gets A=40 B=50 C=60 D=70 so any output
    # CN must lie in {40,50,60,70} and the composite in [40,70].
    csv_path = os.path.join(OUT, "custom_cn.csv")
    with open(csv_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["NLCD_Code", "A", "B", "C", "D"])
        for code in (11, 21, 22, 23, 24, 31, 41, 42, 43, 52, 71, 81, 82,
                     90, 95):
            w.writerow([code, 40, 50, 60, 70])

    folder = os.path.join(OUT, "r1")
    res = run_alg({
        "AOI": aoi_path, "EXTENT": None, "NLCD_RASTER": None,
        "CN_LOOKUP": csv_path, "SLOPE_THRESHOLD": 2.0,
        "AMC": 2,              # III
        "HYDRO_CONDITION": 2,  # Poor (moot with CSV, but exercises enum)
        "CUSTOM_STORMS": "1.0, 3.6, 7.0",
        "FETCH_IMPERVIOUS": False, "FETCH_DEM": True,
        "OUTPUT": os.path.join(folder, "cn.gpkg"),
        "OUTPUT_CN_RASTER": os.path.join(folder, "cn.tif"),
        "OUTPUT_FOLDER": folder,
    })
    from qgis.core import QgsVectorLayer as VL, QgsRasterLayer as RL
    vl = VL(res["OUTPUT"], "cn", "ogr")
    check("r1: polygon output valid with features",
          vl.isValid() and vl.featureCount() > 0,
          f"{vl.featureCount()} features")
    feats = list(vl.getFeatures())
    check("r1: composite CN honors custom CSV range",
          all(40.0 <= f["CN"] <= 70.0 for f in feats),
          str([round(f["CN"], 1) for f in feats]))
    check("r1: AMC III raises CN_Adj above CN",
          all(f["CN_Adj"] > f["CN"] for f in feats))
    rl = RL(res["OUTPUT_CN_RASTER"], "r")
    stats = rl.dataProvider().bandStatistics(1)
    check("r1: raster CN values within CSV table",
          rl.isValid() and 40.0 <= stats.minimumValue
          and stats.maximumValue <= 70.0,
          f"min={stats.minimumValue} max={stats.maximumValue}")
    runoff_files = [f for f in os.listdir(folder)
                    if f.startswith("runoff_summary_")]
    with open(os.path.join(folder, runoff_files[-1])) as fh:
        rows = list(csv.DictReader(fh))
    check("r1: three custom storm rows", len(rows) == 3,
          str([r["Storm"] for r in rows]))
    check("r1: runoff increases with depth",
          float(rows[0]["Q_in"]) < float(rows[1]["Q_in"])
          < float(rows[2]["Q_in"]))
    summary = read_summary(folder)
    check("r1: summary AMC III + impervious not fetched",
          "AMC requested:       III" in summary
          and "(not fetched)" in summary)


def scenario_r2():
    """Denver extent — exercises the USGS 3DEP path (no state DEM)."""
    folder = os.path.join(OUT, "r2")
    run_alg({
        "AOI": None, "EXTENT": "-105.010,-105.000,39.740,39.750 [EPSG:4326]",
        "NLCD_RASTER": None, "CN_LOOKUP": None, "SLOPE_THRESHOLD": 2.0,
        "AMC": 1, "HYDRO_CONDITION": 1, "CUSTOM_STORMS": "",
        "FETCH_IMPERVIOUS": True, "FETCH_DEM": True,
        "OUTPUT": os.path.join(folder, "cn.gpkg"),
        "OUTPUT_CN_RASTER": os.path.join(folder, "cn.tif"),
        "OUTPUT_FOLDER": folder,
    })
    summary = read_summary(folder)
    check("r2: DEM came from USGS 3DEP",
          "DEM source:          USGS 3DEP" in summary)
    check("r2: composite CN produced",
          "Composite CN (II):" in summary
          and "Composite CN (II):   0.00" not in summary)


def scenario_r3():
    """Mid-Lake-Michigan extent — no SSURGO coverage; single-AOI fallback,
    open water CN 100."""
    folder = os.path.join(OUT, "r3")
    res = run_alg({
        "AOI": None, "EXTENT": "-87.000,-86.990,44.000,44.010 [EPSG:4326]",
        "NLCD_RASTER": None, "CN_LOOKUP": None, "SLOPE_THRESHOLD": 2.0,
        "AMC": 1, "HYDRO_CONDITION": 1, "CUSTOM_STORMS": "2.5",
        "FETCH_IMPERVIOUS": False, "FETCH_DEM": False,
        "OUTPUT": os.path.join(folder, "cn.gpkg"),
        "OUTPUT_CN_RASTER": os.path.join(folder, "cn.tif"),
        "OUTPUT_FOLDER": folder,
    })
    from qgis.core import QgsVectorLayer as VL
    vl = VL(res["OUTPUT"], "cn", "ogr")
    feats = list(vl.getFeatures())
    check("r3: single AOI fallback polygon", len(feats) == 1)
    if feats:
        f = feats[0]
        check("r3: fallback row has empty MUKEY and CN=100 (open water)",
              (f["MUKEY"] or "") == "" and abs(f["CN"] - 100.0) < 0.5,
              f"CN={f['CN']}")
        check("r3: dominant NLCD is open water (11)",
              f["DominantNLCD"] == 11, str(f["DominantNLCD"]))


def scenario_r4():
    """Raleigh extent, DEM fetch disabled — SlopePct must be NULL and the
    run must still complete."""
    folder = os.path.join(OUT, "r4")
    res = run_alg({
        "AOI": None, "EXTENT": "-78.650,-78.640,35.780,35.790 [EPSG:4326]",
        "NLCD_RASTER": None, "CN_LOOKUP": None, "SLOPE_THRESHOLD": 2.0,
        "AMC": 0,  # I
        "HYDRO_CONDITION": 0, "CUSTOM_STORMS": "",
        "FETCH_IMPERVIOUS": False, "FETCH_DEM": False,
        "OUTPUT": os.path.join(folder, "cn.gpkg"),
        "OUTPUT_CN_RASTER": os.path.join(folder, "cn.tif"),
        "OUTPUT_FOLDER": folder,
    })
    from qgis.core import QgsVectorLayer as VL, NULL
    vl = VL(res["OUTPUT"], "cn", "ogr")
    feats = list(vl.getFeatures())
    check("r4: completes without DEM", vl.isValid() and len(feats) > 0)
    check("r4: SlopePct is NULL without DEM",
          all(f["SlopePct"] == NULL or f["SlopePct"] is None for f in feats))
    check("r4: AMC I lowers CN_Adj below CN",
          all(f["CN_Adj"] < f["CN"] for f in feats))


def scenario_r5():
    """User-supplied NLCD raster (the tif fetched in r4) must reproduce
    the fetched-NLCD result on the same extent."""
    r4_folder = os.path.join(OUT, "r4")
    nlcd_tifs = [f for f in os.listdir(r4_folder)
                 if f.startswith("nlcd_2") and f.endswith(".tif")]
    nlcd_path = os.path.join(r4_folder, nlcd_tifs[-1])
    folder = os.path.join(OUT, "r5")
    res = run_alg({
        "AOI": None, "EXTENT": "-78.650,-78.640,35.780,35.790 [EPSG:4326]",
        "NLCD_RASTER": nlcd_path, "CN_LOOKUP": None, "SLOPE_THRESHOLD": 2.0,
        "AMC": 0, "HYDRO_CONDITION": 0, "CUSTOM_STORMS": "",
        "FETCH_IMPERVIOUS": False, "FETCH_DEM": False,
        "OUTPUT": os.path.join(folder, "cn.gpkg"),
        "OUTPUT_CN_RASTER": os.path.join(folder, "cn.tif"),
        "OUTPUT_FOLDER": folder,
    })
    from qgis.core import QgsVectorLayer as VL
    r4_cn = {f["MUKEY"]: f["CN"]
             for f in VL(os.path.join(r4_folder, "cn.gpkg"), "a", "ogr")
             .getFeatures()}
    r5_cn = {f["MUKEY"]: f["CN"]
             for f in VL(res["OUTPUT"], "b", "ogr").getFeatures()}
    check("r5: user NLCD reproduces fetched-NLCD per-polygon CN",
          set(r4_cn) == set(r5_cn)
          and all(abs(r4_cn[k] - r5_cn[k]) < 0.01 for k in r4_cn),
          f"r4={ {k: round(v, 2) for k, v in r4_cn.items()} } "
          f"r5={ {k: round(v, 2) for k, v in r5_cn.items()} }")


def scenario_validate():
    res = processing.run("hydrocn:validateservices", {})
    check("validate: alg runs and reports counts",
          res.get("OK", 0) + res.get("FAILED", 0) >= 8, str(res))


SCENARIOS = {"load": scenario_load, "r1": scenario_r1, "r2": scenario_r2,
             "r3": scenario_r3, "r4": scenario_r4, "r5": scenario_r5,
             "validate": scenario_validate}

names = sys.argv[1:] or list(SCENARIOS)
for name in names:
    print(f"\n=== scenario: {name} ===")
    if name != "load":
        ensure_provider()
    try:
        SCENARIOS[name]()
    except Exception:
        traceback.print_exc()
        CHECKS.append(False)
        print(f"CHECK FAIL: scenario {name} raised")

print(f"\nSCENARIOS: {sum(CHECKS)}/{len(CHECKS)} checks passed")
# skip exitQgis(): QGIS 3.24 headless teardown crashes after success
os._exit(0 if all(CHECKS) else 1)
