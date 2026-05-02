"""Read-side queries: features, areas, boundaries, elevation, info."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from shapely import wkb as shp_wkb
from shapely.geometry import Point, mapping
from shapely.geometry.base import BaseGeometry
from shapely.ops import polygonize, unary_union

from cadq.store import connect


# --- data classes ----------------------------------------------------------


@dataclass
class DrawingInfo:
    source_path: str
    dxf_version: str | None
    units_name: str | None
    units_to_m: float | None
    extents: dict[str, float]
    has_georef: bool
    counts: dict[str, int]


@dataclass
class FeatureRow:
    id: str
    ontology: str
    name: str | None
    layer: str | None
    geom_kind: str
    area_du: float | None
    area_m2: float | None
    length_du: float | None
    length_m: float | None
    z_min: float | None
    z_max: float | None
    confidence: float | None
    evidence: list[dict[str, Any]]


# --- helpers ---------------------------------------------------------------


def _open(cache: Path):
    return connect(cache, read_only=True)


def _load_geom(blob: bytes | memoryview | None) -> BaseGeometry | None:
    if blob is None:
        return None
    return shp_wkb.loads(bytes(blob))


# --- public API ------------------------------------------------------------


def info(cache: Path) -> DrawingInfo:
    con = _open(cache)
    try:
        row = con.execute(
            """
            SELECT source_path, dxf_version, units_name, units_to_m,
                   ext_min_x, ext_min_y, ext_min_z,
                   ext_max_x, ext_max_y, ext_max_z,
                   has_georef
            FROM drawing WHERE id = 1
            """
        ).fetchone()
        counts = {
            "layers": con.execute("SELECT count(*) FROM layers").fetchone()[0],
            "entities": con.execute("SELECT count(*) FROM entities").fetchone()[0],
            "texts": con.execute("SELECT count(*) FROM texts").fetchone()[0],
            "inserts": con.execute("SELECT count(*) FROM inserts").fetchone()[0],
            "spot_elevations": con.execute("SELECT count(*) FROM spot_elevations").fetchone()[0],
            "features": con.execute("SELECT count(*) FROM features").fetchone()[0],
        }
    finally:
        con.close()
    if not row:
        raise RuntimeError(f"Cache has no drawing record: {cache}")
    return DrawingInfo(
        source_path=row[0],
        dxf_version=row[1],
        units_name=row[2],
        units_to_m=row[3],
        extents={
            "min_x": row[4], "min_y": row[5], "min_z": row[6],
            "max_x": row[7], "max_y": row[8], "max_z": row[9],
        },
        has_georef=bool(row[10]),
        counts=counts,
    )


def list_layers(cache: Path, *, name_filter: str | None = None) -> list[dict[str, Any]]:
    con = _open(cache)
    try:
        sql = "SELECT name, color, ontology_type, ontology_conf, ontology_rule FROM layers"
        params: list[Any] = []
        if name_filter:
            sql += " WHERE name ILIKE ?"
            params.append(f"%{name_filter}%")
        sql += " ORDER BY name"
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()
    return [
        {
            "name": r[0], "color": r[1],
            "ontology": r[2], "confidence": r[3], "rule": r[4],
        }
        for r in rows
    ]


def list_features(
    cache: Path,
    *,
    ontology_prefix: str | None = None,
    layer: str | None = None,
    geom_kind: str | None = None,
) -> list[FeatureRow]:
    con = _open(cache)
    try:
        sql = """
            SELECT id, ontology, name, layer, geom_kind, area_du, area_m2,
                   length_du, length_m, z_min, z_max, confidence, evidence
            FROM features
        """
        clauses, params = [], []
        if ontology_prefix:
            clauses.append("ontology LIKE ?")
            params.append(ontology_prefix.rstrip(".") + "%")
        if layer:
            clauses.append("layer = ?")
            params.append(layer)
        if geom_kind:
            clauses.append("geom_kind = ?")
            params.append(geom_kind)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ontology, id"
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()
    return [
        FeatureRow(
            id=r[0], ontology=r[1], name=r[2], layer=r[3], geom_kind=r[4],
            area_du=r[5], area_m2=r[6],
            length_du=r[7], length_m=r[8],
            z_min=r[9], z_max=r[10],
            confidence=r[11],
            evidence=json.loads(r[12] or "[]"),
        )
        for r in rows
    ]


def get_feature(cache: Path, feature_id: str) -> FeatureRow | None:
    rows = list_features(cache)
    for f in rows:
        if f.id == feature_id:
            return f
    return None


def feature_area(cache: Path, feature_id: str) -> dict[str, Any] | None:
    f = get_feature(cache, feature_id)
    if not f:
        return None
    return {
        "id": f.id,
        "ontology": f.ontology,
        "area_drawing_units": f.area_du,
        "area_m2": f.area_m2,
        "confidence": f.confidence,
    }


def feature_boundary(
    cache: Path, feature_id: str, *, fmt: str = "geojson"
) -> dict[str, Any] | str | None:
    con = _open(cache)
    try:
        row = con.execute(
            "SELECT id, ontology, wkb FROM features WHERE id = ?",
            [feature_id],
        ).fetchone()
    finally:
        con.close()
    if not row:
        return None
    geom = _load_geom(row[2])
    if geom is None:
        return None
    if fmt == "geojson":
        return {
            "type": "Feature",
            "properties": {"id": row[0], "ontology": row[1]},
            "geometry": mapping(geom),
        }
    if fmt == "wkt":
        return geom.wkt
    raise ValueError(f"Unsupported boundary format: {fmt}")


# --- elevation -------------------------------------------------------------


_ELEVATION_ONTOLOGY_PREFIXES = (
    "survey.contour",
    "survey.surface",
    "survey.spot_elevation",
    "building.roof",
)


def _all_elevation_points(con) -> list[tuple[float, float, float, str]]:
    """Aggregate every (x, y, z, source) sample we can find.

    Only polylines on elevation-bearing layers (contours, surfaces, roofs,
    etc.) contribute vertex samples — otherwise generic closed polygons
    (lawns, driveways) drawn with implicit z=0 would dominate.  Spot
    elevations from text are always included.
    """
    out: list[tuple[float, float, float, str]] = []

    # Spot heights from text
    for x, y, z in con.execute(
        "SELECT x, y, z FROM spot_elevations WHERE z IS NOT NULL"
    ).fetchall():
        out.append((float(x), float(y), float(z), "spot"))

    # Polyline vertices with Z, restricted to elevation-bearing layers.
    onto_filter = " OR ".join(["l.ontology_type LIKE ?"] * len(_ELEVATION_ONTOLOGY_PREFIXES))
    onto_params = [p + "%" for p in _ELEVATION_ONTOLOGY_PREFIXES]
    rows = con.execute(
        f"""
        SELECT e.wkb, e.z_values
        FROM entities e
        JOIN layers l ON l.name = e.layer
        WHERE e.kind IN ('LWPOLYLINE', 'POLYLINE')
          AND e.z_values IS NOT NULL
          AND ({onto_filter})
        """,
        onto_params,
    ).fetchall()
    for blob, zs in rows:
        if not blob or not zs:
            continue
        try:
            geom = shp_wkb.loads(bytes(blob))
        except Exception:
            continue
        coords = list(geom.exterior.coords) if geom.geom_type == "Polygon" else list(geom.coords)
        if len(zs) == len(coords):
            for (x, y, *_), z in zip(coords, zs):
                if z is None:
                    continue
                out.append((float(x), float(y), float(z), "polyline"))
        else:
            # Constant elevation: assume zs[0] applies to all vertices.
            z = float(zs[0])
            for x, y, *_ in coords:
                out.append((float(x), float(y), z, "polyline"))

    return out


def elevation_extreme(cache: Path, mode: str = "max") -> dict[str, Any] | None:
    if mode not in {"max", "min"}:
        raise ValueError("mode must be 'max' or 'min'")
    con = _open(cache)
    try:
        pts = _all_elevation_points(con)
    finally:
        con.close()
    if not pts:
        return None
    chosen = max(pts, key=lambda p: p[2]) if mode == "max" else min(pts, key=lambda p: p[2])
    x, y, z, source = chosen
    return {"x": x, "y": y, "z": z, "source": source}


def elevation_at(cache: Path, x: float, y: float, *, k: int = 3) -> dict[str, Any] | None:
    """Estimate elevation at (x, y) using inverse-distance weighting on the
    k nearest sample points.  Good enough for an MVP; replace with a TIN
    lookup when contours are densified."""
    con = _open(cache)
    try:
        pts = _all_elevation_points(con)
    finally:
        con.close()
    if not pts:
        return None
    target = Point(x, y)
    # nearest-k by squared distance (no sklearn dependency)
    scored = sorted(((px - x) ** 2 + (py - y) ** 2, px, py, pz) for px, py, pz, _ in pts)
    nearest = scored[: max(1, k)]
    if nearest[0][0] == 0:
        _, px, py, pz = nearest[0]
        return {"x": x, "y": y, "z": pz, "method": "exact", "samples": 1}
    weights = [1.0 / d for d, *_ in nearest]
    zs = [pz for _, _, _, pz in nearest]
    z = sum(w * z for w, z in zip(weights, zs)) / sum(weights)
    return {"x": x, "y": y, "z": z, "method": "idw", "samples": len(nearest)}


# --- explain ---------------------------------------------------------------


def explain(cache: Path, target_id: str) -> list[dict[str, Any]]:
    con = _open(cache)
    try:
        rows = con.execute(
            """
            SELECT target_kind, target_id, rule, ontology, confidence, note
            FROM ontology_log
            WHERE target_id = ?
            """,
            [target_id],
        ).fetchall()
    finally:
        con.close()
    return [
        {
            "target_kind": r[0], "target_id": r[1],
            "rule": r[2], "ontology": r[3],
            "confidence": r[4], "note": r[5],
        }
        for r in rows
    ]


def feature_to_dict(f: FeatureRow) -> dict[str, Any]:
    return asdict(f)


# --- topology / spatial queries -------------------------------------------


def _load_feature_geom(con, feature_id: str) -> tuple[BaseGeometry | None, str | None, str | None]:
    row = con.execute(
        "SELECT wkb, ontology, geom_kind FROM features WHERE id = ?",
        [feature_id],
    ).fetchone()
    if not row:
        return None, None, None
    return _load_geom(row[0]), row[1], row[2]


def nearest_features(
    cache: Path,
    *,
    to: str,
    type_prefix: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Return features nearest to feature ``to``, optionally filtered by
    ontology prefix (e.g. ``landscape.softscape.tree``)."""
    con = _open(cache)
    try:
        target_geom, target_ontology, _ = _load_feature_geom(con, to)
        if target_geom is None:
            return []
        sql = "SELECT id, ontology, layer, geom_kind, wkb FROM features WHERE id != ?"
        params: list[Any] = [to]
        if type_prefix:
            sql += " AND ontology LIKE ?"
            params.append(type_prefix.rstrip(".") + "%")
        rows = con.execute(sql, params).fetchall()
        units_to_m = _open_units(con)
    finally:
        con.close()
    out: list[dict[str, Any]] = []
    for fid, onto, layer, gkind, blob in rows:
        g = _load_geom(blob)
        if g is None:
            continue
        d_du = float(target_geom.distance(g))
        out.append({
            "id": fid,
            "ontology": onto,
            "layer": layer,
            "geom_kind": gkind,
            "distance_drawing_units": d_du,
            "distance_m": d_du * units_to_m,
        })
    out.sort(key=lambda r: r["distance_drawing_units"])
    return out[: max(1, limit)]


def adjacent_features(
    cache: Path,
    *,
    to: str,
    tolerance: float = 1e-6,
) -> list[dict[str, Any]]:
    """Features whose geometry touches or overlaps feature ``to``."""
    con = _open(cache)
    try:
        target_geom, _, _ = _load_feature_geom(con, to)
        if target_geom is None:
            return []
        rows = con.execute(
            "SELECT id, ontology, layer, geom_kind, wkb FROM features WHERE id != ?",
            [to],
        ).fetchall()
    finally:
        con.close()
    out: list[dict[str, Any]] = []
    expanded = target_geom.buffer(tolerance) if tolerance > 0 else target_geom
    for fid, onto, layer, gkind, blob in rows:
        g = _load_geom(blob)
        if g is None:
            continue
        if expanded.intersects(g):
            relation = "overlaps" if target_geom.overlaps(g) else (
                "touches" if target_geom.touches(g) else "intersects"
            )
            out.append({
                "id": fid, "ontology": onto, "layer": layer,
                "geom_kind": gkind, "relation": relation,
            })
    return out


def label_search(cache: Path, pattern: str) -> list[dict[str, Any]]:
    """Glob-ish search over text labels in the drawing.

    The pattern is converted to a SQL ILIKE: ``*`` → ``%``, ``?`` → ``_``.
    """
    sql_pat = pattern.replace("*", "%").replace("?", "_")
    if "%" not in sql_pat and "_" not in sql_pat:
        sql_pat = f"%{sql_pat}%"
    con = _open(cache)
    try:
        rows = con.execute(
            """
            SELECT t.id, t.text, t.layer, t.x, t.y, t.z, t.kind
            FROM texts t
            WHERE t.text ILIKE ?
            ORDER BY t.id
            """,
            [sql_pat],
        ).fetchall()
    finally:
        con.close()
    return [
        {
            "id": r[0], "text": r[1], "layer": r[2],
            "x": r[3], "y": r[4], "z": r[5], "kind": r[6],
        }
        for r in rows
    ]


# --- elevation profile ----------------------------------------------------


def elevation_profile(
    cache: Path,
    x1: float, y1: float, x2: float, y2: float,
    *,
    samples: int = 25,
) -> dict[str, Any] | None:
    """Sample elevation along a line via IDW on the nearest spot/contour
    samples.  Returns chainage, x, y, z arrays plus min/max/grade."""
    if samples < 2:
        samples = 2
    con = _open(cache)
    try:
        pts = _all_elevation_points(con)
    finally:
        con.close()
    if not pts:
        return None
    line_len = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    if line_len <= 0:
        return None
    profile: list[dict[str, float]] = []
    for i in range(samples):
        t = i / (samples - 1)
        sx = x1 + t * (x2 - x1)
        sy = y1 + t * (y2 - y1)
        scored = sorted(((px - sx) ** 2 + (py - sy) ** 2, pz) for px, py, pz, _ in pts)
        nearest = scored[:3]
        if nearest[0][0] == 0:
            z = nearest[0][1]
        else:
            weights = [1.0 / d for d, _ in nearest]
            zs = [pz for _, pz in nearest]
            z = sum(w * zz for w, zz in zip(weights, zs)) / sum(weights)
        profile.append({"chainage": t * line_len, "x": sx, "y": sy, "z": float(z)})
    zs = [p["z"] for p in profile]
    z_min, z_max = min(zs), max(zs)
    grade = (zs[-1] - zs[0]) / line_len if line_len else 0.0
    return {
        "from": {"x": x1, "y": y1},
        "to": {"x": x2, "y": y2},
        "length_drawing_units": line_len,
        "samples": profile,
        "z_min": z_min,
        "z_max": z_max,
        "z_drop": zs[0] - zs[-1],
        "average_grade": grade,  # rise/run, drawing units
    }


# --- planner --------------------------------------------------------------


_PLAN_HINTS: list[tuple[tuple[str, ...], list[str]]] = [
    (("highest", "high point", "summit", "peak", "max elev"),
     ["elevation_max"]),
    (("lowest", "low point", "min elev"),
     ["elevation_min"]),
    (("elevation at", "level at", "height at"),
     ["elevation_at <x> <y>"]),
    (("profile", "section", "long section", "longitudinal"),
     ["elevation_profile <x1> <y1> <x2> <y2>"]),
    (("how big", "how large", "area of", "size of"),
     ["features_list --type <ontology>", "area --feature <id>"]),
    (("boundary", "outline", "perimeter"),
     ["features_list --type <ontology>", "boundary --feature <id> --format geojson"]),
    (("nearest", "closest", "near"),
     ["features_list --type <type>", "nearest --to <id> --type <other>"]),
    (("touch", "adjacent", "next to", "abut"),
     ["topology_adjacent --to <id>"]),
    (("called", "labelled", "labeled", "named"),
     ["label_search '<text>*'"]),
    (("lawn", "grass", "turf"),
     ["features_list --type landscape.softscape.lawn"]),
    (("driveway", "drive"),
     ["features_list --type landscape.hardscape.driveway"]),
    (("tree", "trees"),
     ["features_list --type landscape.softscape.tree"]),
    (("building", "house", "footprint"),
     ["features_list --type building.footprint"]),
]


def plan(question: str) -> dict[str, Any]:
    """Suggest a tool sequence for a natural-language question.

    This is intentionally rule-based — the AI harness still does the actual
    reasoning; this tool just nudges it toward the right cadq calls.
    """
    q = question.lower()
    suggestions: list[str] = []
    for keywords, tools in _PLAN_HINTS:
        if any(k in q for k in keywords):
            for t in tools:
                if t not in suggestions:
                    suggestions.append(t)
    if not suggestions:
        suggestions = [
            "info",
            "layers_list",
            "features_list",
        ]
    return {
        "question": question,
        "suggested_tools": suggestions,
        "note": (
            "Run `info` and `features_list` first if the drawing context "
            "isn't yet loaded.  All numeric answers must come from these "
            "tools — do not invent values."
        ),
    }


def _open_units(con) -> float:
    row = con.execute("SELECT units_to_m FROM drawing WHERE id=1").fetchone()
    return float(row[0]) if row and row[0] else 1.0


# --- garden derivation ---------------------------------------------------


def _site_polygon(con) -> tuple[BaseGeometry | None, str]:
    """Find the best available site polygon.

    Priority:
        1. Largest closed polygon feature on ``site.boundary``.
        2. Polygonized union of ``site.boundary`` line features.
        3. Convex hull of those line features (fallback).

    Returns ``(geometry, method)`` or ``(None, "none")``.
    """
    # 1. Closed polygons.
    rows = con.execute(
        """
        SELECT wkb FROM features
        WHERE ontology = 'site.boundary' AND geom_kind = 'polygon'
        ORDER BY area_du DESC NULLS LAST
        """
    ).fetchall()
    polys: list[BaseGeometry] = []
    for (blob,) in rows:
        try:
            polys.append(shp_wkb.loads(bytes(blob)))
        except Exception:
            continue
    if polys:
        return polys[0], "site.boundary polygon feature"

    # 2/3. Lines.
    rows = con.execute(
        """
        SELECT wkb FROM features
        WHERE ontology = 'site.boundary' AND geom_kind = 'line'
        """
    ).fetchall()
    lines: list[BaseGeometry] = []
    for (blob,) in rows:
        try:
            lines.append(shp_wkb.loads(bytes(blob)))
        except Exception:
            continue
    if not lines:
        return None, "none"
    merged = unary_union(lines)
    polygonized = list(polygonize([merged]))
    if polygonized:
        site = max(polygonized, key=lambda p: p.area)
        return site, f"polygonize of site.boundary lines ({len(polygonized)} ring(s))"
    return merged.convex_hull, "convex hull of site.boundary lines (open boundary)"


_GARDEN_SUBTRACTION_PREFIXES_DEFAULT = (
    "building.",
    "landscape.hardscape.",
    "landscape.water",
    "services.drainage.manhole",
)

_GARDEN_HEDGE_PREFIX = "landscape.softscape.hedge"
_GARDEN_TREE_CANOPY_PREFIX = "landscape.softscape.tree"


def _polygons_for_prefix(con, prefix: str) -> list[BaseGeometry]:
    rows = con.execute(
        """
        SELECT wkb FROM features
        WHERE ontology LIKE ? AND geom_kind = 'polygon'
        """,
        [prefix.rstrip(".") + "%"],
    ).fetchall()
    out: list[BaseGeometry] = []
    for (blob,) in rows:
        try:
            g = shp_wkb.loads(bytes(blob))
        except Exception:
            continue
        if g.is_valid and not g.is_empty and g.area > 0:
            out.append(g)
    return out


def _polygons_for_ontologies(con, prefixes: tuple[str, ...]) -> list[BaseGeometry]:
    out: list[BaseGeometry] = []
    for p in prefixes:
        out.extend(_polygons_for_prefix(con, p))
    return out


def garden_area(
    cache: Path,
    *,
    subtract_hedges: bool = False,
    subtract_canopies: bool = False,
    extra_subtract: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    """Derive the residual open-ground (garden) polygon and its area.

    ``garden = site - (buildings + hardscape + water + drainage manholes)``.

    By default hedges and tree canopies are *not* subtracted (you can have
    grass under a tree, hedges sit at the edge of garden beds).  Pass
    ``subtract_hedges=True`` or ``subtract_canopies=True`` to remove them.
    Additional ontology prefixes can be subtracted via ``extra_subtract``.
    """
    con = _open(cache)
    try:
        site, method = _site_polygon(con)
        if site is None:
            return None
        site_area_du = float(site.area)
        prefixes = list(_GARDEN_SUBTRACTION_PREFIXES_DEFAULT)
        if subtract_hedges:
            prefixes.append(_GARDEN_HEDGE_PREFIX)
        if subtract_canopies:
            prefixes.append(_GARDEN_TREE_CANOPY_PREFIX)
        prefixes.extend(extra_subtract)

        breakdown: list[dict[str, Any]] = []
        residual = site
        for prefix in prefixes:
            polys = _polygons_for_prefix(con, prefix)
            if not polys:
                breakdown.append({
                    "ontology_prefix": prefix,
                    "feature_count": 0,
                    "removed_du": 0.0,
                    "after_du": float(residual.area),
                })
                continue
            sub = unary_union(polys).intersection(site)
            removed = float(sub.area)
            residual = residual.difference(sub)
            breakdown.append({
                "ontology_prefix": prefix,
                "feature_count": len(polys),
                "removed_du": removed,
                "after_du": float(residual.area),
            })

        units_to_m = _open_units(con)
        out = {
            "site_method": method,
            "site_area_drawing_units": site_area_du,
            "site_area_m2": site_area_du * units_to_m * units_to_m,
            "garden_area_drawing_units": float(residual.area),
            "garden_area_m2": float(residual.area) * units_to_m * units_to_m,
            "subtractions": breakdown,
            "subtracted_hedges": subtract_hedges,
            "subtracted_canopies": subtract_canopies,
            "geometry_geojson": mapping(residual),
        }

        # Informational extras.
        hedge_polys = _polygons_for_prefix(con, _GARDEN_HEDGE_PREFIX)
        canopy_polys = _polygons_for_prefix(con, _GARDEN_TREE_CANOPY_PREFIX)
        out["hedge_area_drawing_units"] = float(unary_union(hedge_polys).area) if hedge_polys else 0.0
        out["hedge_area_m2"] = out["hedge_area_drawing_units"] * units_to_m * units_to_m
        out["canopy_area_drawing_units"] = float(unary_union(canopy_polys).area) if canopy_polys else 0.0
        out["canopy_area_m2"] = out["canopy_area_drawing_units"] * units_to_m * units_to_m
        return out
    finally:
        con.close()
