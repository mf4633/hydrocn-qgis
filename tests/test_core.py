# -*- coding: utf-8 -*-
"""Unit tests for hydrocn_core — pure math + parsers, no QGIS required.

Run from the repo root:  python -m pytest tests/  (or python tests/test_core.py)
"""

import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hydrocn"))
import hydrocn_core as core  # noqa: E402


# --- resolve_hydro_group -----------------------------------------------------

def test_resolve_single_letters():
    for letter in "ABCD":
        assert core.resolve_hydro_group(letter, 5.0, 2.0) == letter


def test_resolve_dual_drained_when_steep():
    assert core.resolve_hydro_group("B/D", 3.0, 2.0) == "B"
    assert core.resolve_hydro_group("C/D", 2.0, 2.0) == "C"  # >= threshold


def test_resolve_dual_undrained_when_flat():
    assert core.resolve_hydro_group("B/D", 1.0, 2.0) == "D"


def test_resolve_dual_undrained_when_no_slope():
    assert core.resolve_hydro_group("A/D", None, 2.0) == "D"


def test_resolve_empty():
    assert core.resolve_hydro_group(None, 1.0, 2.0) is None
    assert core.resolve_hydro_group("", 1.0, 2.0) is None


# --- AMC adjustment ----------------------------------------------------------

def test_amc_ii_unchanged():
    assert core.apply_amc_adjustment(75.0, "II") == 75.0


def test_amc_i_lowers_cn():
    cn1 = core.apply_amc_adjustment(75.0, "I")
    assert 55 < cn1 < 60  # NEH-630: CN 75 -> ~56.9 dry


def test_amc_iii_raises_cn():
    cn3 = core.apply_amc_adjustment(75.0, "III")
    assert 86 < cn3 < 89  # NEH-630: CN 75 -> ~87.5 wet


def test_amc_extremes_passthrough():
    assert core.apply_amc_adjustment(100.0, "I") == 100.0
    assert core.apply_amc_adjustment(0.0, "III") == 0.0


# --- SCS runoff --------------------------------------------------------------

def test_runoff_zero_below_ia():
    # CN 70 -> S = 4.286, Ia = 0.857; P below that yields no runoff
    assert core.compute_scs_runoff(0.5, 70.0) == 0.0


def test_runoff_known_value():
    # TR-55 example: P = 6 in, CN = 80 -> S = 2.5, Ia = 0.5,
    # Q = 5.5^2 / (5.5 + 2.5) = 3.78125
    q = core.compute_scs_runoff(6.0, 80.0)
    assert abs(q - 3.78125) < 1e-9


def test_runoff_monotone_in_cn():
    qs = [core.compute_scs_runoff(3.0, cn) for cn in (60, 70, 80, 90)]
    assert qs == sorted(qs)


# --- CN lookup table ---------------------------------------------------------

def test_cn_lookup_condition_ordering():
    good = core.default_cn_lookup("Good")
    fair = core.default_cn_lookup("Fair")
    poor = core.default_cn_lookup("Poor")
    for code in (41, 51, 71, 81):
        for hsg in "ABCD":
            assert good[code][hsg] <= fair[code][hsg] <= poor[code][hsg]


def test_cn_lookup_invariants():
    fair = core.default_cn_lookup("Fair")
    assert fair[11]["A"] == 100          # open water
    assert fair[24]["D"] == 95           # developed high, D
    assert fair[21] == {"A": 49, "B": 69, "C": 79, "D": 84}


# --- Horn slope --------------------------------------------------------------

def test_slope_flat_is_zero():
    arr = np.full((5, 5), 100.0)
    out = core.slope_percent_from_array(arr, 10.0, 10.0)
    assert np.allclose(out[1:-1, 1:-1], 0.0)
    assert np.isnan(out[0, 0])  # border is NaN


def test_slope_uniform_gradient():
    # z = x -> dz/dx = 1 per cell of 10 m -> 10% slope
    arr = np.tile(np.arange(5, dtype=float), (5, 1))
    out = core.slope_percent_from_array(arr, 10.0, 10.0)
    assert np.allclose(out[1:-1, 1:-1], 10.0)


def test_slope_nan_propagates_to_neighbors():
    arr = np.full((5, 5), 100.0)
    arr[2, 2] = np.nan
    out = core.slope_percent_from_array(arr, 10.0, 10.0)
    # Horn uses the 8 neighbors, not the center: every interior cell
    # adjacent to the NaN goes NaN, but the NaN cell's own slope is
    # computed from its (valid) neighbors.
    assert np.isnan(out[1, 1]) and np.isnan(out[3, 3])
    assert np.isnan(out[1, 2]) and np.isnan(out[2, 1])
    assert out[2, 2] == 0.0


def test_slope_rejects_small_arrays():
    try:
        core.slope_percent_from_array(np.zeros((2, 5)), 1.0, 1.0)
        assert False, "expected ValueError"
    except ValueError:
        pass


# --- accumulator -------------------------------------------------------------

def _simple_inputs():
    nlcd = np.array([[41, 41], [82, 82]], dtype=np.int32)   # forest / crops
    soil = np.array([[1, 1], [2, -1]], dtype=np.int64)
    slope = np.array([[3.0, 3.0], [1.0, 1.0]])
    return nlcd, soil, slope


def test_accumulator_basic():
    nlcd, soil, slope = _simple_inputs()
    lookup = core.default_cn_lookup("Fair")
    cn, cn_adj, per_class, per_poly = core.accumulate_per_class_per_polygon(
        nlcd, soil, slope,
        {1: "B/D", 2: "C"}, lookup, 2.0, "II", 900.0)
    # OID 1: forest on B/D at 3% slope >= 2% -> drained B -> CN 60
    assert cn[0, 0] == 60
    # OID 2: crops on C -> CN 85
    assert cn[1, 0] == 85
    # No-soil cell: crops -> NLCD-estimated HSG B -> CN 78
    assert cn[1, 1] == 78
    assert per_poly[1]["area"] == 2 * 900.0
    assert ("41", "B") not in per_class  # keys are ints
    assert (41, "B") in per_class


def test_accumulator_amc_iii_adjusts():
    nlcd, soil, slope = _simple_inputs()
    lookup = core.default_cn_lookup("Fair")
    _, cn_adj, _, _ = core.accumulate_per_class_per_polygon(
        nlcd, soil, slope, {1: "B", 2: "C"}, lookup, 2.0, "III", 900.0)
    expected = core.apply_amc_adjustment(60.0, "III")
    assert abs(cn_adj[0, 0] - expected) < 1e-9


def test_accumulator_empty_nlcd():
    nlcd = np.zeros((3, 3), dtype=np.int32)
    soil = np.full((3, 3), -1, dtype=np.int64)
    slope = np.full((3, 3), np.nan)
    cn, cn_adj, per_class, per_poly = core.accumulate_per_class_per_polygon(
        nlcd, soil, slope, {}, core.default_cn_lookup(), 2.0, "II", 900.0)
    assert np.all(np.isnan(cn))
    assert per_class == {} and per_poly == {}


# --- NOAA Atlas 14 parser ----------------------------------------------------

NOAA_NEW_FORMAT = """some preamble
by duration for ARI (years):,1,2,5,10,25,50,100,200,500,1000
5-min:,0.406,0.474,0.569,0.646,0.752,0.836,0.923,1.01,1.14,1.24
24-hr:,2.89,3.48,4.42,5.22,6.42,7.42,8.51,9.71,11.5,12.9
"""

NOAA_OLD_FORMAT = '''"by duration for return period in years"
"Duration,1,2,5,10,25,50,100,200,500,1000"
"5-min:,0.36,0.44,0.52,0.60,0.70,0.78,0.86,0.95,1.07,1.17"
"24-hr:,2.75,3.30,4.20,5.00,6.10,7.00,8.00,9.10,10.7,12.0"
"by duration for upper bound of 90% confidence interval"
"Duration,1,2,5,10,25,50,100,200,500,1000"
"24-hr:,9.99,9.99,9.99,9.99,9.99,9.99,9.99,9.99,9.99,9.99"
'''


def test_noaa_parse_new_format():
    r = core.parse_noaa_atlas14_csv(NOAA_NEW_FORMAT)
    assert r["24-hr"][100] == 8.51
    assert r["5-min"][1] == 0.406
    assert len(r["24-hr"]) == 10


def test_noaa_parse_old_format_stops_at_ci_block():
    r = core.parse_noaa_atlas14_csv(NOAA_OLD_FORMAT)
    assert r["24-hr"][100] == 8.00  # not the 9.99 CI value
    assert r["24-hr"][1] == 2.75


def test_noaa_parse_garbage():
    assert core.parse_noaa_atlas14_csv("<html>error</html>") == {}


# --- SSURGO GML parser -------------------------------------------------------

SSURGO_GML = """<?xml version="1.0" encoding="UTF-8"?>
<wfs:FeatureCollection
   xmlns:ms="http://mapserver.gis.umn.edu/mapserver"
   xmlns:wfs="http://www.opengis.net/wfs"
   xmlns:gml="http://www.opengis.net/gml">
  <gml:featureMember>
    <ms:mapunitpoly>
      <ms:mukey>545800</ms:mukey>
      <ms:musym>ApB</ms:musym>
      <ms:geometry>
        <gml:Polygon>
          <gml:outerBoundaryIs>
            <gml:LinearRing>
              <gml:coordinates>-78.65,35.78 -78.64,35.78 -78.64,35.79 -78.65,35.78</gml:coordinates>
            </gml:LinearRing>
          </gml:outerBoundaryIs>
        </gml:Polygon>
      </ms:geometry>
    </ms:mapunitpoly>
  </gml:featureMember>
  <gml:featureMember>
    <ms:mapunitpoly>
      <ms:mukey>545801</ms:mukey>
      <ms:musym>CfC2</ms:musym>
      <ms:geometry>
        <gml:Polygon>
          <gml:outerBoundaryIs>
            <gml:LinearRing>
              <gml:coordinates>-78.66,35.78 -78.65,35.78 -78.65,35.79 -78.66,35.78</gml:coordinates>
            </gml:LinearRing>
          </gml:outerBoundaryIs>
        </gml:Polygon>
      </ms:geometry>
    </ms:mapunitpoly>
  </gml:featureMember>
</wfs:FeatureCollection>
"""


def test_ssurgo_gml_parse_no_duplicates():
    # Both gml:featureMember and ms:mapunitpoly patterns match this doc;
    # only the first may be used or every polygon doubles.
    feats = core.parse_ssurgo_gml(SSURGO_GML)
    assert len(feats) == 2
    wkt, mukey, musym = feats[0]
    assert mukey == "545800" and musym == "ApB"
    assert wkt.startswith("POLYGON ((")
    assert wkt.count("-78.65") >= 2  # ring closed back to start


def test_wkt_ring_closure():
    wkt = core._wkt_from_points([(0, 0), (1, 0), (1, 1)])
    assert wkt.count("0.00000000 0.00000000") == 2  # first == last


def test_wkt_too_few_points():
    assert core._wkt_from_points([(0, 0), (1, 1)]) is None


# --- misc helpers ------------------------------------------------------------

def test_bbox_area_km2():
    # 0.1 x 0.1 deg near the equator ~ 123.9 km^2
    a = core.bbox_area_km2([0.0, 0.0, 0.1, 0.1])
    assert abs(a - (11.132 ** 2)) < 1.0


def test_conus_check():
    assert core.is_in_conus(35.78, -78.65)        # Raleigh
    assert not core.is_in_conus(61.2, -149.9)     # Anchorage


def test_state_dem_selection():
    assert any("NC OneMap" in lbl for lbl, _ in
               core.state_dem_sources_for_bbox(core.PROBE_BBOX_WGS84))
    assert any("ISGS" in lbl for lbl, _ in
               core.state_dem_sources_for_bbox(core.PROBE_BBOX_IL_WGS84))
    assert core.state_dem_sources_for_bbox([-105.1, 39.7, -105.0, 39.8]) == []


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
            except Exception as e:
                failures += 1
                print(f"ERROR {name}: {e}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
