"""Smoke test: build an in-memory DXF, ingest it, and run a few queries.

Exercises the full pipeline (ingest → DuckDB → ontology → features →
elevation) without needing the ODA File Converter.  This is the test you
run first after `pip install -e ".[dev]"` to confirm the install is sane.
"""

from __future__ import annotations

from pathlib import Path

import ezdxf
import pytest

from cadq.ingest import ingest
from cadq.queries import (
    adjacent_features,
    elevation_at,
    elevation_extreme,
    elevation_profile,
    feature_area,
    feature_boundary,
    garden_area,
    info,
    label_search,
    list_features,
    list_layers,
    nearest_features,
    plan,
)


def _make_sample_dxf(path: Path) -> None:
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 6  # metres
    msp = doc.modelspace()

    # Layer naming follows the default ontology rules.
    doc.layers.add("L-LAWN-01", color=3)
    doc.layers.add("L-DRIVEWAY", color=8)
    doc.layers.add("L-DRIVEWAY-EDGE", color=8)
    doc.layers.add("L-CONTOUR-MAJOR", color=1)
    doc.layers.add("L-SPOT-LEVELS", color=2)
    doc.layers.add("L-PLNT-TREE", color=3)
    doc.layers.add("L-PLNT-CANOPY", color=3)
    doc.layers.add("L-DRAIN-MH", color=4)
    doc.layers.add("L-LABELS", color=7)
    doc.layers.add("SiteBdy", color=5)

    # Define blocks for INSERTs.
    tree_blk = doc.blocks.new(name="TREE-OAK")
    tree_blk.add_circle(center=(0, 0), radius=0.5)
    mh_blk = doc.blocks.new(name="MH-CIRC")
    mh_blk.add_circle(center=(0, 0), radius=0.3)

    # 10x10 lawn (closed polyline) with a label inside.
    msp.add_lwpolyline(
        [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)],
        dxfattribs={"layer": "L-LAWN-01", "closed": True},
    )
    msp.add_text("Front Lawn", dxfattribs={"layer": "L-LABELS"}).set_placement((5, 5))

    # 4x20 driveway as four OPEN lines on an edge layer — ingest must
    # polygonize them back into a region.
    edges = [
        ((20, 0), (24, 0)),
        ((24, 0), (24, 20)),
        ((24, 20), (20, 20)),
        ((20, 20), (20, 0)),
    ]
    for a, b in edges:
        msp.add_line(a, b, dxfattribs={"layer": "L-DRIVEWAY-EDGE"})

    # Two contours at z=10 and z=15
    pl1 = msp.add_polyline3d(
        [(0, 0, 10), (10, 0, 10), (10, 10, 10), (0, 10, 10)],
        dxfattribs={"layer": "L-CONTOUR-MAJOR"},
    )
    pl1.close(True)
    pl2 = msp.add_polyline3d(
        [(2, 2, 15), (8, 2, 15), (8, 8, 15), (2, 8, 15)],
        dxfattribs={"layer": "L-CONTOUR-MAJOR"},
    )
    pl2.close(True)

    # Spot heights as text
    msp.add_text("RL 17.50", dxfattribs={"layer": "L-SPOT-LEVELS"}).set_placement((5, 5))
    msp.add_text("RL 12.00", dxfattribs={"layer": "L-SPOT-LEVELS"}).set_placement((22, 10))

    # A tree (block insert) and a manhole.
    msp.add_blockref("TREE-OAK", insert=(15, 5), dxfattribs={"layer": "L-PLNT-TREE"})
    msp.add_blockref("MH-CIRC", insert=(22, 18), dxfattribs={"layer": "L-DRAIN-MH"})

    # A tree canopy polygon enclosing the trunk above (de-dup test target).
    msp.add_lwpolyline(
        [(13, 3), (17, 3), (17, 7), (13, 7), (13, 3)],
        dxfattribs={"layer": "L-PLNT-CANOPY", "closed": True},
    )

    # Site boundary enclosing the whole drawing (closed polyline).
    msp.add_lwpolyline(
        [(-2, -2), (30, -2), (30, 25), (-2, 25), (-2, -2)],
        dxfattribs={"layer": "SiteBdy", "closed": True},
    )

    doc.saveas(path)


@pytest.fixture()
def cache(tmp_path: Path) -> Path:
    dxf = tmp_path / "site.dxf"
    _make_sample_dxf(dxf)
    return ingest(dxf)


def test_info(cache: Path) -> None:
    meta = info(cache)
    assert meta.units_name == "meters"
    assert meta.units_to_m == 1.0
    assert meta.counts["features"] >= 2  # lawn + driveway at least


def test_layers_classified(cache: Path) -> None:
    rows = list_layers(cache)
    by_name = {r["name"]: r for r in rows}
    assert by_name["L-LAWN-01"]["ontology"] == "landscape.softscape.lawn"
    assert by_name["L-DRIVEWAY"]["ontology"] == "landscape.hardscape.driveway"
    assert by_name["L-CONTOUR-MAJOR"]["ontology"] == "survey.contour.major"


def test_lawn_area(cache: Path) -> None:
    lawns = list_features(cache, ontology_prefix="landscape.softscape.lawn")
    assert len(lawns) == 1
    assert lawns[0].area_m2 == pytest.approx(100.0, rel=1e-6)


def test_driveway_boundary_geojson(cache: Path) -> None:
    drives = list_features(cache, ontology_prefix="landscape.hardscape.driveway")
    assert len(drives) == 1
    gj = feature_boundary(cache, drives[0].id, fmt="geojson")
    assert gj["type"] == "Feature"
    assert gj["geometry"]["type"] == "Polygon"


def test_elevation_max_min(cache: Path) -> None:
    hi = elevation_extreme(cache, mode="max")
    lo = elevation_extreme(cache, mode="min")
    assert hi is not None and lo is not None
    # Highest value comes from the 'RL 17.50' spot.
    assert hi["z"] == pytest.approx(17.5)
    # Lowest comes from the lower contour at 10.
    assert lo["z"] == pytest.approx(10.0)


def test_elevation_at_idw(cache: Path) -> None:
    out = elevation_at(cache, 5.0, 5.0)
    assert out is not None
    # The 'RL 17.50' spot is exactly here, so the IDW result should snap.
    assert out["z"] == pytest.approx(17.5, rel=1e-6)


def test_feature_area_helper(cache: Path) -> None:
    drives = list_features(cache, ontology_prefix="landscape.hardscape.driveway")
    out = feature_area(cache, drives[0].id)
    assert out is not None
    assert out["area_m2"] == pytest.approx(80.0, rel=1e-6)


def test_polygonized_driveway_recovered(cache: Path) -> None:
    # Driveway is drawn as four open lines on L-DRIVEWAY-EDGE; polygonize
    # must recover one rectangular feature of 4 x 20 = 80 m².
    drives = list_features(cache, ontology_prefix="landscape.hardscape.driveway")
    assert len(drives) == 1
    assert drives[0].evidence[0]["rule"] == "polygonize"


def test_block_features_for_tree_and_manhole(cache: Path) -> None:
    trees = list_features(cache, ontology_prefix="landscape.softscape.tree")
    # After dedup the trunk is merged into the canopy polygon — so we
    # expect exactly one tree feature and it carries area info.
    assert len(trees) == 1
    assert trees[0].geom_kind == "polygon"
    mhs = list_features(cache, ontology_prefix="services.drainage.manhole")
    assert len(mhs) == 1
    assert mhs[0].geom_kind == "point"


def test_lawn_label_join(cache: Path) -> None:
    lawns = list_features(cache, ontology_prefix="landscape.softscape.lawn")
    assert lawns[0].name == "Front Lawn"


def test_info_extents_real(cache: Path) -> None:
    meta = info(cache)
    # Extents must come from real geometry. With the SiteBdy at -2..30 / -2..25
    # and contours up to z=15, the recomputed extents should reflect that.
    assert meta.extents["min_x"] == pytest.approx(-2.0)
    assert meta.extents["max_x"] >= 30.0
    assert meta.extents["max_z"] == pytest.approx(15.0)


def test_label_search(cache: Path) -> None:
    rows = label_search(cache, "front*")
    assert any("Front Lawn" in r["text"] for r in rows)
    rows2 = label_search(cache, "RL 12*")
    assert any("12" in r["text"] for r in rows2)


def test_nearest_tree_to_manhole(cache: Path) -> None:
    mhs = list_features(cache, ontology_prefix="services.drainage.manhole")
    near = nearest_features(
        cache, to=mhs[0].id,
        type_prefix="landscape.softscape.tree",
        limit=3,
    )
    assert len(near) == 1
    assert near[0]["distance_m"] > 0


def test_topology_adjacent_lawn_to_contour(cache: Path) -> None:
    lawns = list_features(cache, ontology_prefix="landscape.softscape.lawn")
    adj = adjacent_features(cache, to=lawns[0].id, tolerance=1e-3)
    # The 10x10 contour ring sits exactly on the lawn boundary.
    assert any("contour" in (r["ontology"] or "") for r in adj)


def test_elevation_profile(cache: Path) -> None:
    # Profile from inside the lawn (where RL 17.50 sits) to inside the
    # driveway region (where RL 12.00 sits).
    out = elevation_profile(cache, 5.0, 5.0, 22.0, 10.0, samples=11)
    assert out is not None
    assert out["samples"][0]["z"] == pytest.approx(17.5, rel=1e-3)
    assert out["z_drop"] > 0  # falling from lawn to driveway


def test_plan_keywords() -> None:
    p = plan("Where is the highest point on the drawing?")
    assert "elevation_max" in p["suggested_tools"]
    p2 = plan("how big is the lawned area?")
    assert any("landscape.softscape.lawn" in t for t in p2["suggested_tools"])
    p3 = plan("what is the boundary of the driveway?")
    assert any("driveway" in t for t in p3["suggested_tools"])
    assert any("boundary" in t for t in p3["suggested_tools"])


def test_pascalcase_layer_classification() -> None:
    """UK survey style PascalCase layer names must classify correctly."""
    from cadq.ontology import Ontology

    onto = Ontology.load()
    cases = {
        "Tree": "landscape.softscape.tree",
        "TreeSpread": "landscape.softscape.tree",
        "TreeTrunk": "landscape.softscape.tree",
        "Hedge": "landscape.softscape.hedge",
        "AssumedBoundary": "site.boundary",
        "LevelsSpot": "survey.spot_elevation",
        "LevelsBuilding": "survey.spot_elevation",
        "Building": "building.footprint",
        "Wall": "building.wall",
        "Footpath": "landscape.hardscape.path",
        "Step": "landscape.hardscape.step",
        "Fence": "landscape.hardscape.fence",
        "Bank": "survey.bank",
        "Canopy": "landscape.softscape.tree",
    }
    for name, expected in cases.items():
        m = onto.classify_layer(name)
        assert m is not None, f"{name} did not classify"
        assert m.ontology == expected, (
            f"{name}: got {m.ontology}, expected {expected}"
        )


def test_block_classification_pascalcase() -> None:
    from cadq.ontology import Ontology

    onto = Ontology.load()
    assert onto.classify_block("ConiferCanopy").ontology == "landscape.softscape.tree"
    assert onto.classify_block("TreeTrunk").ontology == "landscape.softscape.tree"
    assert onto.classify_block("ManholeRound").ontology == "services.drainage.manhole"


def test_geom_kind_filter(cache: Path) -> None:
    """`geom_kind` filter narrows results to one geometry type."""
    polys = list_features(cache, geom_kind="polygon")
    points = list_features(cache, geom_kind="point")
    lines = list_features(cache, geom_kind="line")
    assert all(f.geom_kind == "polygon" for f in polys)
    assert all(f.geom_kind == "point" for f in points)
    assert all(f.geom_kind == "line" for f in lines)
    # Manhole is a point; lawn is a polygon.
    assert any(f.ontology.startswith("services.drainage") for f in points)
    assert any(f.ontology == "landscape.softscape.lawn" for f in polys)


def test_tree_dedup_collapses_trunk_into_canopy(cache: Path) -> None:
    """Trunk inside canopy => one tree feature, not two."""
    trees = list_features(cache, ontology_prefix="landscape.softscape.tree")
    # We added one canopy polygon containing one trunk INSERT;
    # de-dup should leave a single feature with merged evidence.
    assert len(trees) == 1
    t = trees[0]
    # Canopy wins (it carries area info).
    assert t.geom_kind == "polygon"
    # Trunk evidence is merged in.
    rules = [e.get("merge_rule") for e in t.evidence if "merge_rule" in e]
    assert "trunk-in-canopy" in rules


def test_garden_area_default(cache: Path) -> None:
    """Garden = site - building/hardscape/water/manhole footprints."""
    out = garden_area(cache)
    assert out is not None
    # Site is 32 x 27 = 864 sq.units (also m² since INSUNITS=meters)
    assert out["site_area_m2"] == pytest.approx(864.0, rel=1e-6)
    # Garden < site (we subtracted at least the driveway 80 m²).
    assert out["garden_area_m2"] < out["site_area_m2"]
    # The driveway subtraction prefix should report 80 m² removed.
    drive_row = next(
        s for s in out["subtractions"]
        if s["ontology_prefix"].startswith("landscape.hardscape")
    )
    assert drive_row["removed_du"] == pytest.approx(80.0, rel=1e-3)


def test_garden_subtract_hedges_flag(cache: Path) -> None:
    """The flag is plumbed even when no hedges exist (returns identical area)."""
    base = garden_area(cache)
    with_hedge = garden_area(cache, subtract_hedges=True)
    assert base is not None and with_hedge is not None
    # No hedge features in the synthetic sample, so the area is unchanged.
    assert with_hedge["garden_area_m2"] == pytest.approx(base["garden_area_m2"])
    assert with_hedge["subtracted_hedges"] is True
