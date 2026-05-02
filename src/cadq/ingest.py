"""DWG/DXF → cadqcache ingest pipeline.

The ingest step is intentionally narrow:

1. Convert DWG → DXF if needed (via ODA File Converter).
2. Read the DXF with ezdxf.
3. Extract layers, model-space entities, text, inserts, spot heights.
4. Map layer names to ontology types.
5. Promote closed polygons / hatches into rows in the ``features`` table.

The richer enrichment passes (topology, TIN, text-to-region label join) are
left as extension points and marked with ``# TODO``; the MVP wires up enough
to answer the three example questions in the brainstorm.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import ezdxf
from ezdxf.document import Drawing
from ezdxf.entities import DXFEntity
from shapely import wkb as shp_wkb
from shapely.geometry import LineString, Point, Polygon, mapping  # noqa: F401
from shapely.geometry.base import BaseGeometry
from shapely.ops import polygonize, unary_union

from cadq import config as user_config
from cadq.ontology import Match, Ontology
from cadq.store import cache_path_for, connect


# --- units ------------------------------------------------------------------

# AutoCAD INSUNITS code → (name, factor to metres).
# Ref: ezdxf.units / DXF reference.
_INSUNITS: dict[int, tuple[str, float]] = {
    0: ("unitless", 1.0),
    1: ("inches", 0.0254),
    2: ("feet", 0.3048),
    3: ("miles", 1609.344),
    4: ("millimeters", 0.001),
    5: ("centimeters", 0.01),
    6: ("meters", 1.0),
    7: ("kilometers", 1000.0),
    8: ("microinches", 2.54e-8),
    9: ("mils", 2.54e-5),
    10: ("yards", 0.9144),
    14: ("decimeters", 0.1),
}


# --- DWG → DXF --------------------------------------------------------------


def _find_oda_converter() -> str | None:
    """Locate the ODA File Converter binary if installed."""
    found, _ = find_oda_converter_with_source()
    return found


def find_oda_converter_with_source() -> tuple[str | None, str | None]:
    """Locate the ODA converter and report which mechanism found it.

    Resolution order:
    1. ``ODA_FILE_CONVERTER`` environment variable
    2. ``oda_file_converter`` user-config entry (set via ``cadq oda set-path``)
    3. ``ODAFileConverter`` on PATH
    4. Common Windows install locations
    """
    env = os.environ.get("ODA_FILE_CONVERTER")
    if env and Path(env).exists():
        return env, "env"

    cfg = user_config.get("oda_file_converter")
    if cfg and Path(cfg).exists():
        return str(cfg), "config"

    for name in ("ODAFileConverter", "ODAFileConverter.exe"):
        on_path = shutil.which(name)
        if on_path:
            return on_path, "path"

    for candidate in _oda_default_locations():
        if Path(candidate).exists():
            return candidate, "auto"
    return None, None


def _oda_default_locations() -> list[str]:
    """Return common install locations for ODA File Converter on Windows.

    ODA's installer creates a versioned folder name like
    ``ODAFileConverter 26.6.0`` so we glob those too.
    """
    out: list[str] = []
    roots = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ]
    for root in roots:
        if not root:
            continue
        root_path = Path(root)
        if not root_path.exists():
            continue
        # Fixed legacy names
        for sub in ("ODA/ODAFileConverter", "ODA\\ODAFileConverter"):
            candidate = root_path / sub / "ODAFileConverter.exe"
            out.append(str(candidate))
        # Versioned folders e.g. "ODAFileConverter 26.6.0"
        try:
            for child in root_path.glob("ODAFileConverter*"):
                exe = child / "ODAFileConverter.exe"
                out.append(str(exe))
            oda_root = root_path / "ODA"
            if oda_root.exists():
                for child in oda_root.glob("ODAFileConverter*"):
                    exe = child / "ODAFileConverter.exe"
                    out.append(str(exe))
        except OSError:
            continue
    return out


def _dwg_to_dxf(dwg_path: Path) -> Path:
    """Convert DWG to a temporary DXF file, returning the new path."""
    converter = _find_oda_converter()
    if not converter:
        raise RuntimeError(
            "DWG input requires the free ODA File Converter and cadq "
            "could not find it. Either:\n"
            "  - run `cadq oda install` for download instructions, then\n"
            "  - run `cadq oda set-path <full path to ODAFileConverter.exe>` "
            "if it is installed in a non-standard location, or\n"
            "  - set the ODA_FILE_CONVERTER environment variable, or\n"
            "  - save the drawing as DXF in your CAD package and ingest "
            "that instead."
        )
    src_dir = Path(tempfile.mkdtemp(prefix="cadq-in-"))
    dst_dir = Path(tempfile.mkdtemp(prefix="cadq-out-"))
    staged = src_dir / dwg_path.name
    shutil.copy2(dwg_path, staged)
    # ODAFileConverter <inDir> <outDir> <outVer> <outFmt> <recurse> <audit> [filter]
    subprocess.run(
        [converter, str(src_dir), str(dst_dir), "ACAD2018", "DXF", "0", "1", "*.DWG"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    out = dst_dir / (dwg_path.stem + ".dxf")
    if not out.exists():
        raise RuntimeError(f"ODA conversion produced no DXF for {dwg_path.name}")
    return out


# --- helpers ---------------------------------------------------------------


@dataclass
class _Counters:
    entity: int = 0
    text: int = 0
    insert: int = 0
    spot: int = 0
    feature: int = 0
    per_onto: dict[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.per_onto is None:
            self.per_onto = {}

    def next_entity(self) -> int:
        self.entity += 1
        return self.entity


def _bbox(geom: BaseGeometry) -> tuple[float, float, float, float]:
    minx, miny, maxx, maxy = geom.bounds
    return minx, miny, maxx, maxy


def _polyline_points(e: DXFEntity) -> list[tuple[float, float, float]]:
    """Return [(x, y, z), ...] for LWPOLYLINE / POLYLINE."""
    pts: list[tuple[float, float, float]] = []
    if e.dxftype() == "LWPOLYLINE":
        elev = float(getattr(e.dxf, "elevation", 0.0) or 0.0)
        for x, y, *_ in e.get_points("xy"):
            pts.append((float(x), float(y), elev))
    elif e.dxftype() == "POLYLINE":
        for v in e.vertices:  # type: ignore[attr-defined]
            loc = v.dxf.location
            pts.append((float(loc.x), float(loc.y), float(loc.z)))
    return pts


def _is_closed(e: DXFEntity, pts: list[tuple[float, float, float]]) -> bool:
    if getattr(e.dxf, "flags", 0) and e.dxftype() == "POLYLINE":
        # bit 1 = closed
        if int(e.dxf.flags) & 1:
            return True
    if getattr(e, "closed", False):
        return True
    if len(pts) >= 3:
        x0, y0, _ = pts[0]
        xn, yn, _ = pts[-1]
        if abs(x0 - xn) < 1e-9 and abs(y0 - yn) < 1e-9:
            return True
    return False


def _safe_polygon(pts: list[tuple[float, float, float]]) -> Polygon | None:
    if len(pts) < 3:
        return None
    ring = [(p[0], p[1]) for p in pts]
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    try:
        poly = Polygon(ring)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.is_empty or poly.area <= 0:
            return None
        return poly
    except Exception:
        return None


def _slug(s: str) -> str:
    keep = []
    for ch in s.lower():
        if ch.isalnum():
            keep.append(ch)
        elif ch in "-_/.":
            keep.append("-")
        else:
            keep.append("-")
    out = "".join(keep).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out or uuid.uuid4().hex[:8]


# --- main entry point ------------------------------------------------------


def ingest(
    source: str | Path,
    *,
    rules: str | Path | None = None,
    cache: str | Path | None = None,
) -> Path:
    """Ingest a DWG or DXF file into a `.cadqcache` and return its path."""
    source = Path(source).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(source)

    suffix = source.suffix.lower()
    if suffix == ".dwg":
        dxf_path = _dwg_to_dxf(source)
    elif suffix == ".dxf":
        dxf_path = source
    else:
        raise ValueError(f"Unsupported input: {source.suffix}. Use .dwg or .dxf.")

    cache_path = Path(cache).expanduser().resolve() if cache else cache_path_for(source)
    if cache_path.exists():
        cache_path.unlink()

    onto = Ontology.load(rules)
    doc = ezdxf.readfile(str(dxf_path))

    con = connect(cache_path)
    try:
        _write_drawing_meta(con, source, doc)
        _write_layers(con, doc, onto)
        counters = _Counters()
        _write_entities(con, doc, counters)
        _build_features_from_polylines(con, doc, onto, counters)
        _build_features_from_hatches(con, doc, onto, counters)
        _build_features_from_inserts(con, doc, onto, counters)
        _build_features_from_polygonize(con, doc, onto, counters)
        _build_line_features(con, doc, onto, counters)
        _extract_spot_elevations(con, doc, onto, counters)
        _label_features_from_text(con)
        _dedupe_tree_features(con)
        _update_extents_from_entities(con)
        con.commit()
    finally:
        con.close()

    return cache_path


def _write_drawing_meta(con, source: Path, doc: Drawing) -> None:
    insunits = int(doc.header.get("$INSUNITS", 0) or 0)
    units_name, units_to_m = _INSUNITS.get(insunits, ("unknown", 1.0))
    ext_min = doc.header.get("$EXTMIN", (0.0, 0.0, 0.0))
    ext_max = doc.header.get("$EXTMAX", (0.0, 0.0, 0.0))
    has_geo = "GEODATA" in (o.dxftype() for o in doc.objects)
    con.execute(
        """
        INSERT OR REPLACE INTO drawing
        VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            str(source),
            doc.dxfversion,
            insunits,
            units_name,
            units_to_m,
            float(ext_min[0]), float(ext_min[1]), float(ext_min[2]),
            float(ext_max[0]), float(ext_max[1]), float(ext_max[2]),
            has_geo,
            datetime.now(timezone.utc).replace(tzinfo=None),
        ],
    )


def _write_layers(con, doc: Drawing, onto: Ontology) -> None:
    for layer in doc.layers:
        name = layer.dxf.name
        m = onto.classify_layer(name)
        con.execute(
            """
            INSERT OR REPLACE INTO layers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                name,
                int(getattr(layer.dxf, "color", 7) or 7),
                str(getattr(layer.dxf, "linetype", "") or ""),
                bool(layer.is_frozen()),
                bool(layer.is_off()),
                bool(layer.is_locked()),
                m.ontology if m else None,
                m.confidence if m else None,
                m.rule if m else None,
            ],
        )
        # Log every match for explain.
        if m:
            for cand in onto.all_layer_matches(name):
                con.execute(
                    "INSERT INTO ontology_log VALUES (?, ?, ?, ?, ?, ?)",
                    ["layer", name, cand.rule, cand.ontology, cand.confidence, None],
                )


def _write_entities(con, doc: Drawing, counters: _Counters) -> None:
    msp = doc.modelspace()
    for e in msp:
        kind = e.dxftype()
        layer = e.dxf.layer
        handle = e.dxf.handle
        try:
            if kind in ("LWPOLYLINE", "POLYLINE"):
                pts = _polyline_points(e)
                if len(pts) < 2:
                    continue
                closed = _is_closed(e, pts)
                if closed and len(pts) >= 3:
                    geom: BaseGeometry | None = _safe_polygon(pts)
                else:
                    geom = LineString([(p[0], p[1]) for p in pts])
                if geom is None or geom.is_empty:
                    continue
                zs = [p[2] for p in pts]
                eid = counters.next_entity()
                bx0, by0, bx1, by1 = _bbox(geom)
                con.execute(
                    """
                    INSERT INTO entities VALUES
                    (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        eid, handle, kind, layer, closed,
                        float(min(zs)), float(max(zs)),
                        bx0, by0, bx1, by1,
                        bytes(geom.wkb), zs,
                        json.dumps({"vertex_count": len(pts)}),
                    ],
                )

            elif kind in ("LINE",):
                start = (float(e.dxf.start.x), float(e.dxf.start.y))
                end = (float(e.dxf.end.x), float(e.dxf.end.y))
                geom = LineString([start, end])
                zs = [float(e.dxf.start.z), float(e.dxf.end.z)]
                eid = counters.next_entity()
                bx0, by0, bx1, by1 = _bbox(geom)
                con.execute(
                    "INSERT INTO entities VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        eid, handle, kind, layer, False,
                        float(min(zs)), float(max(zs)),
                        bx0, by0, bx1, by1,
                        bytes(geom.wkb), zs, json.dumps({}),
                    ],
                )

            elif kind in ("CIRCLE",):
                c = e.dxf.center
                r = float(e.dxf.radius)
                geom = Point(float(c.x), float(c.y)).buffer(r)
                eid = counters.next_entity()
                bx0, by0, bx1, by1 = _bbox(geom)
                con.execute(
                    "INSERT INTO entities VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        eid, handle, kind, layer, True,
                        float(c.z), float(c.z),
                        bx0, by0, bx1, by1,
                        bytes(geom.wkb), [float(c.z)],
                        json.dumps({"radius": r}),
                    ],
                )

            elif kind in ("HATCH",):
                # Convert each polygon path into a polygon row.
                for path in e.paths:  # type: ignore[attr-defined]
                    try:
                        verts = [(float(v[0]), float(v[1])) for v in path.vertices]  # type: ignore[attr-defined]
                    except Exception:
                        continue
                    poly = _safe_polygon([(x, y, 0.0) for x, y in verts])
                    if poly is None:
                        continue
                    eid = counters.next_entity()
                    bx0, by0, bx1, by1 = _bbox(poly)
                    con.execute(
                        "INSERT INTO entities VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            eid, handle, kind, layer, True,
                            0.0, 0.0,
                            bx0, by0, bx1, by1,
                            bytes(poly.wkb), [0.0],
                            json.dumps({"pattern": str(getattr(e.dxf, "pattern_name", "") or "")}),
                        ],
                    )

            elif kind in ("TEXT", "MTEXT"):
                text = (e.plain_text() if kind == "MTEXT" else e.dxf.text) or ""
                ip = getattr(e.dxf, "insert", None) or e.dxf.align_point
                eid = counters.next_entity()
                con.execute(
                    "INSERT INTO entities VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        eid, handle, kind, layer, False,
                        float(ip.z), float(ip.z),
                        float(ip.x), float(ip.y), float(ip.x), float(ip.y),
                        bytes(Point(float(ip.x), float(ip.y)).wkb), [float(ip.z)],
                        json.dumps({}),
                    ],
                )
                counters.text += 1
                con.execute(
                    "INSERT INTO texts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        counters.text, eid, layer, text,
                        float(ip.x), float(ip.y), float(ip.z),
                        float(getattr(e.dxf, "height", 0.0) or 0.0),
                        float(getattr(e.dxf, "rotation", 0.0) or 0.0),
                        kind.lower(),
                    ],
                )

            elif kind == "INSERT":
                ip = e.dxf.insert
                eid = counters.next_entity()
                con.execute(
                    "INSERT INTO entities VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        eid, handle, kind, layer, False,
                        float(ip.z), float(ip.z),
                        float(ip.x), float(ip.y), float(ip.x), float(ip.y),
                        bytes(Point(float(ip.x), float(ip.y)).wkb), [float(ip.z)],
                        json.dumps({"block": e.dxf.name}),
                    ],
                )
                counters.insert += 1
                con.execute(
                    "INSERT INTO inserts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        counters.insert, eid, e.dxf.name, layer,
                        float(ip.x), float(ip.y), float(ip.z),
                        float(getattr(e.dxf, "rotation", 0.0) or 0.0),
                        float(getattr(e.dxf, "xscale", 1.0) or 1.0),
                        float(getattr(e.dxf, "yscale", 1.0) or 1.0),
                        float(getattr(e.dxf, "zscale", 1.0) or 1.0),
                    ],
                )
                # Also extract any ATTRIBs as text rows.
                for attrib in e.attribs:  # type: ignore[attr-defined]
                    counters.text += 1
                    aip = attrib.dxf.insert
                    con.execute(
                        "INSERT INTO texts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            counters.text, eid, layer, str(attrib.dxf.text or ""),
                            float(aip.x), float(aip.y), float(aip.z),
                            float(getattr(attrib.dxf, "height", 0.0) or 0.0),
                            float(getattr(attrib.dxf, "rotation", 0.0) or 0.0),
                            "attrib",
                        ],
                    )
        except Exception:
            # Per-entity failures must not abort the whole ingest.
            # TODO: surface these in `cadq ingest --verbose`.
            continue


def _layer_ontology(con, layer: str) -> tuple[str | None, float | None]:
    row = con.execute(
        "SELECT ontology_type, ontology_conf FROM layers WHERE name = ?",
        [layer],
    ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def _units_to_m(con) -> float:
    row = con.execute("SELECT units_to_m FROM drawing WHERE id=1").fetchone()
    return float(row[0]) if row else 1.0


def _new_feature_id(con, ontology: str, counters: _Counters) -> str:
    base = ontology.split(".")[-1] if ontology else "feature"
    base = _slug(base)
    counters.per_onto[base] = counters.per_onto.get(base, 0) + 1
    candidate = f"{base}-{counters.per_onto[base]}"
    counters.feature += 1
    # Defensive: ensure uniqueness if a previous run sneaked in.
    row = con.execute("SELECT 1 FROM features WHERE id=?", [candidate]).fetchone()
    if row:
        candidate = f"{base}-{uuid.uuid4().hex[:6]}"
    return candidate


def _build_features_from_polylines(con, doc, onto: Ontology, counters: _Counters) -> None:
    units_to_m = _units_to_m(con)
    rows = con.execute(
        """
        SELECT id, handle, layer, kind, is_closed, wkb, z_min, z_max
        FROM entities
        WHERE kind IN ('LWPOLYLINE', 'POLYLINE') AND is_closed = TRUE
        """
    ).fetchall()
    for eid, handle, layer, kind, is_closed, wkb_bytes, zmin, zmax in rows:
        ontology_type, conf = _layer_ontology(con, layer)
        if not ontology_type:
            continue
        # Contours and similar line-natured ontologies should not become
        # polygon features even when the source polyline happens to close.
        if ontology_type.startswith(("survey.contour", "survey.surface", "building.roof")):
            continue
        try:
            geom = shp_wkb.loads(bytes(wkb_bytes))
        except Exception:
            continue
        if not isinstance(geom, Polygon):
            continue
        area_du = float(geom.area)
        area_m2 = area_du * units_to_m * units_to_m
        fid = _new_feature_id(con, ontology_type, counters)
        evidence = [{
            "handle": handle, "layer": layer, "rule": "layer-ontology",
            "kind": kind, "confidence": conf,
        }]
        con.execute(
            """
            INSERT INTO features VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                fid, ontology_type, None, layer, "polygon",
                bytes(geom.wkb), area_du, area_m2,
                None, None,
                float(zmin), float(zmax),
                conf, json.dumps(evidence),
            ],
        )
        con.execute(
            "INSERT INTO ontology_log VALUES (?, ?, ?, ?, ?, ?)",
            ["feature", fid, "polyline-closed-on-layer", ontology_type, conf, layer],
        )


def _build_features_from_hatches(con, doc, onto: Ontology, counters: _Counters) -> None:
    units_to_m = _units_to_m(con)
    rows = con.execute(
        """
        SELECT id, handle, layer, wkb, attrs
        FROM entities
        WHERE kind = 'HATCH'
        """
    ).fetchall()
    for eid, handle, layer, wkb_bytes, attrs_json in rows:
        attrs = json.loads(attrs_json or "{}")
        pattern = attrs.get("pattern")
        layer_onto, layer_conf = _layer_ontology(con, layer)
        hatch_onto = onto.hatch_hint(pattern)
        ontology_type = layer_onto or hatch_onto
        if not ontology_type:
            continue
        # Boost confidence when both signals agree.
        conf = layer_conf or 0.5
        rule = "layer-ontology"
        if layer_onto and hatch_onto and layer_onto == hatch_onto:
            conf = min(1.0, (layer_conf or 0.5) + 0.1)
            rule = "layer+hatch"
        elif hatch_onto and not layer_onto:
            conf = 0.6
            rule = "hatch-pattern"
        try:
            geom = shp_wkb.loads(bytes(wkb_bytes))
        except Exception:
            continue
        if not isinstance(geom, Polygon):
            continue
        area_du = float(geom.area)
        area_m2 = area_du * units_to_m * units_to_m
        fid = _new_feature_id(con, ontology_type, counters)
        evidence = [{
            "handle": handle, "layer": layer, "rule": rule,
            "kind": "HATCH", "pattern": pattern, "confidence": conf,
        }]
        con.execute(
            """
            INSERT INTO features VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                fid, ontology_type, None, layer, "polygon",
                bytes(geom.wkb), area_du, area_m2,
                None, None,
                None, None,
                conf, json.dumps(evidence),
            ],
        )
        con.execute(
            "INSERT INTO ontology_log VALUES (?, ?, ?, ?, ?, ?)",
            ["feature", fid, rule, ontology_type, conf, layer],
        )


# Match things like 'RL 124.32', 'FFL 12.5', 'IL: 4.20', '+12.345'
import re as _re  # local alias to keep top-level imports tidy

_LEVEL_RE = _re.compile(
    r"""
    (?P<prefix>RL|FFL|TBM|FGL|FCL|IL|CL|GL|SL)?      # optional prefix
    \s*[:=]?\s*
    (?P<sign>[+\-])?
    (?P<value>\d{1,4}(?:\.\d{1,4})?)
    \b
    """,
    _re.IGNORECASE | _re.VERBOSE,
)


def _extract_spot_elevations(con, doc, onto: Ontology, counters: _Counters) -> None:
    """Promote text on survey/spot layers into spot_elevation rows."""
    rows = con.execute(
        """
        SELECT t.id, t.entity_id, t.layer, t.text, t.x, t.y, t.z
        FROM texts t
        """
    ).fetchall()
    for tid, eid, layer, text, x, y, z in rows:
        layer_onto, layer_conf = _layer_ontology(con, layer)
        is_spot_layer = (layer_onto or "").startswith("survey.spot_elevation")
        m = _LEVEL_RE.search(text or "")
        if not m:
            # Polyline/INSERT z still wins below.
            continue
        # Require either a spot layer or an explicit level prefix to avoid
        # treating arbitrary numbers as levels.
        prefix = m.group("prefix")
        if not is_spot_layer and not prefix:
            continue
        try:
            value = float(m.group("value"))
            if m.group("sign") == "-":
                value = -value
        except ValueError:
            continue
        confidence = 0.9 if (is_spot_layer and prefix) else (0.8 if prefix else 0.6)
        counters.spot += 1
        con.execute(
            "INSERT INTO spot_elevations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                counters.spot, eid, layer,
                float(x), float(y), float(value),
                text, confidence, "text",
            ],
        )

    # Also pick up any polyline whose elevation looks meaningful on a contour
    # layer — vertices already carry Z.  TODO: associate text labels with
    # contours to upgrade confidence.


# --- new passes: blocks, polygonize, lines, labels, extents ---------------


def _build_features_from_inserts(con, doc, onto: Ontology, counters: _Counters) -> None:
    """Promote INSERTs whose block name matches a block_rule into point features."""
    units_to_m = _units_to_m(con)  # noqa: F841 — symmetry with other builders
    rows = con.execute(
        """
        SELECT i.id, i.entity_id, i.block_name, i.layer, i.x, i.y, i.z, e.handle
        FROM inserts i
        JOIN entities e ON e.id = i.entity_id
        """
    ).fetchall()
    for _iid, _eid, block_name, layer, x, y, z, handle in rows:
        m = onto.classify_block(block_name or "")
        if m is None:
            continue
        # Use layer ontology as a confidence boost when it agrees.
        layer_onto, layer_conf = _layer_ontology(con, layer)
        conf = m.confidence
        rule = "block-name"
        if layer_onto and layer_onto == m.ontology:
            conf = min(1.0, conf + 0.05)
            rule = "block-name+layer"
        geom = Point(float(x), float(y))
        fid = _new_feature_id(con, m.ontology, counters)
        evidence = [{
            "handle": handle, "block": block_name, "layer": layer,
            "rule": rule, "confidence": conf,
        }]
        con.execute(
            """
            INSERT INTO features VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                fid, m.ontology, block_name, layer, "point",
                bytes(geom.wkb), None, None,
                None, None,
                float(z), float(z),
                conf, json.dumps(evidence),
            ],
        )
        con.execute(
            "INSERT INTO ontology_log VALUES (?, ?, ?, ?, ?, ?)",
            ["feature", fid, rule, m.ontology, conf, block_name],
        )


def _build_features_from_polygonize(con, doc, onto: Ontology, counters: _Counters) -> None:
    """Recover polygons from networks of open lines on the same layer.

    Many CAD authors draw boundaries (e.g. driveway edges, plot outlines) as
    a collection of LINE / open polylines that *would* form a ring if joined.
    Shapely's polygonize stitches them and we promote the result to features.
    Rings already covered by closed polylines or hatches on the same layer are
    suppressed by area-overlap.
    """
    units_to_m = _units_to_m(con)
    # Group open line entities by layer.
    rows = con.execute(
        """
        SELECT layer, wkb
        FROM entities
        WHERE kind IN ('LINE', 'LWPOLYLINE', 'POLYLINE') AND is_closed = FALSE
        """
    ).fetchall()
    by_layer: dict[str, list[BaseGeometry]] = {}
    for layer, blob in rows:
        try:
            g = shp_wkb.loads(bytes(blob))
        except Exception:
            continue
        if g.is_empty:
            continue
        by_layer.setdefault(layer, []).append(g)

    for layer, geoms in by_layer.items():
        ontology_type, layer_conf = _layer_ontology(con, layer)
        if not ontology_type:
            continue
        # Only attempt polygonization for ontologies where a region makes sense.
        if not ontology_type.startswith((
            "landscape.hardscape",
            "landscape.softscape",
            "landscape.water",
            "site.boundary",
            "building.footprint",
        )):
            continue
        try:
            merged = unary_union(geoms)
            polys = list(polygonize([merged]))
        except Exception:
            continue
        if not polys:
            continue

        # Pre-fetch existing polygon features on this layer to avoid duplicates.
        existing = con.execute(
            "SELECT wkb FROM features WHERE layer = ? AND geom_kind = 'polygon'",
            [layer],
        ).fetchall()
        existing_polys: list[BaseGeometry] = []
        for (blob,) in existing:
            try:
                existing_polys.append(shp_wkb.loads(bytes(blob)))
            except Exception:
                continue

        for poly in polys:
            if poly.is_empty or poly.area <= 0:
                continue
            # Suppress if substantially the same as an existing feature on this layer.
            duplicate = False
            for ep in existing_polys:
                inter = poly.intersection(ep).area
                if inter >= 0.95 * min(poly.area, ep.area):
                    duplicate = True
                    break
            if duplicate:
                continue
            area_du = float(poly.area)
            area_m2 = area_du * units_to_m * units_to_m
            fid = _new_feature_id(con, ontology_type, counters)
            conf = max(0.4, (layer_conf or 0.5) - 0.2)  # polygonize is inferential
            evidence = [{
                "layer": layer, "rule": "polygonize",
                "source_lines": len(geoms), "confidence": conf,
            }]
            con.execute(
                """
                INSERT INTO features VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    fid, ontology_type, None, layer, "polygon",
                    bytes(poly.wkb), area_du, area_m2,
                    None, None,
                    None, None,
                    conf, json.dumps(evidence),
                ],
            )
            con.execute(
                "INSERT INTO ontology_log VALUES (?, ?, ?, ?, ?, ?)",
                ["feature", fid, "polygonize", ontology_type, conf, layer],
            )


def _build_line_features(con, doc, onto: Ontology, counters: _Counters) -> None:
    """Promote lines/polylines on contour or boundary layers into line features.

    Includes both open and closed polylines for these ontologies — closed
    contours are still curves, not regions.
    """
    units_to_m = _units_to_m(con)
    rows = con.execute(
        """
        SELECT e.id, e.handle, e.layer, e.kind, e.wkb, e.z_min, e.z_max
        FROM entities e
        JOIN layers l ON l.name = e.layer
        WHERE e.kind IN ('LINE', 'LWPOLYLINE', 'POLYLINE')
          AND (l.ontology_type LIKE 'survey.contour%'
               OR l.ontology_type LIKE 'survey.surface%'
               OR l.ontology_type LIKE 'site.boundary%'
               OR l.ontology_type LIKE 'building.roof%')
        """
    ).fetchall()
    for _eid, handle, layer, kind, blob, zmin, zmax in rows:
        ontology_type, layer_conf = _layer_ontology(con, layer)
        if not ontology_type:
            continue
        try:
            geom = shp_wkb.loads(bytes(blob))
        except Exception:
            continue
        if geom.is_empty:
            continue
        # Closed polylines come back as Polygons after our ingest; turn them
        # into the boundary linestring for these ontologies.
        if isinstance(geom, Polygon):
            geom = LineString(geom.exterior.coords)
        length_du = float(geom.length)
        length_m = length_du * units_to_m
        fid = _new_feature_id(con, ontology_type, counters)
        evidence = [{
            "handle": handle, "layer": layer, "rule": "layer-ontology",
            "kind": kind, "confidence": layer_conf,
        }]
        con.execute(
            """
            INSERT INTO features VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                fid, ontology_type, None, layer, "line",
                bytes(geom.wkb), None, None,
                length_du, length_m,
                float(zmin) if zmin is not None else None,
                float(zmax) if zmax is not None else None,
                layer_conf, json.dumps(evidence),
            ],
        )
        con.execute(
            "INSERT INTO ontology_log VALUES (?, ?, ?, ?, ?, ?)",
            ["feature", fid, "line-on-layer", ontology_type, layer_conf, layer],
        )


def _label_features_from_text(con) -> None:
    """For each unnamed polygon feature on a nameable ontology, pick the
    nearest text label whose centroid lies inside the polygon and assign it
    as the name.

    Pure-numeric labels (`12.5`, `RL 17.50`) are skipped — those are levels.
    Contour, survey, and annotation features are skipped — naming them with
    ambient text creates false attributions.
    """
    feat_rows = con.execute(
        """
        SELECT id, ontology, wkb FROM features
        WHERE name IS NULL AND geom_kind = 'polygon'
        """
    ).fetchall()
    if not feat_rows:
        return
    text_rows = con.execute("SELECT text, x, y FROM texts").fetchall()
    if not text_rows:
        return

    candidates: list[tuple[str, Point]] = []
    for text, x, y in text_rows:
        t = (text or "").strip()
        if not t:
            continue
        if _LEVEL_RE.fullmatch(t.replace(" ", "")):
            continue
        if all((c.isdigit() or c in ".+-") for c in t):
            continue
        candidates.append((t, Point(float(x), float(y))))
    if not candidates:
        return

    nameable = ("landscape.", "building.", "site.")
    for fid, ontology, blob in feat_rows:
        if not ontology or not ontology.startswith(nameable):
            continue
        try:
            poly = shp_wkb.loads(bytes(blob))
        except Exception:
            continue
        inside: list[tuple[float, str]] = []
        for txt, pt in candidates:
            if poly.contains(pt):
                inside.append((poly.centroid.distance(pt), txt))
        if inside:
            inside.sort()
            con.execute("UPDATE features SET name = ? WHERE id = ?", [inside[0][1], fid])


def _dedupe_tree_features(con) -> None:
    """Collapse tree trunk + canopy duplicates so one tree = one feature.

    A trunk (``geom_kind='point'``) inside a canopy (``geom_kind='polygon'``)
    on the ``landscape.softscape.tree`` ontology represents the same tree.
    We keep the canopy (it carries area + spread information) and merge the
    trunk's evidence into it, then delete the trunk row.

    A trunk is also dropped when it's effectively co-located with another
    trunk (within 5% of the smaller bounding extent) — this handles the
    common ``TreeTrunk`` block + ``Tree`` text label both rendering as
    points on top of each other.
    """
    rows = con.execute(
        """
        SELECT id, geom_kind, wkb, evidence, layer
        FROM features
        WHERE ontology = 'landscape.softscape.tree'
        """
    ).fetchall()
    if not rows:
        return
    polygons: list[tuple[str, BaseGeometry, str | None]] = []
    points: list[tuple[str, BaseGeometry, str | None]] = []
    for fid, kind, blob, _ev, layer in rows:
        try:
            g = shp_wkb.loads(bytes(blob))
        except Exception:
            continue
        if kind == "polygon":
            polygons.append((fid, g, layer))
        elif kind == "point":
            points.append((fid, g, layer))

    to_delete: set[str] = set()

    # 1. Trunks inside canopies -> drop trunk, merge evidence.
    for trunk_id, trunk_g, trunk_layer in points:
        if trunk_id in to_delete:
            continue
        for poly_id, poly_g, _ in polygons:
            if poly_g.contains(trunk_g) or poly_g.distance(trunk_g) < 1e-6:
                _merge_evidence(con, dst=poly_id, src=trunk_id, rule="trunk-in-canopy")
                to_delete.add(trunk_id)
                break

    # 2. Co-located point duplicates (within tolerance).
    remaining = [p for p in points if p[0] not in to_delete]
    for i, (a_id, a_g, _) in enumerate(remaining):
        if a_id in to_delete:
            continue
        for b_id, b_g, _ in remaining[i + 1:]:
            if b_id in to_delete:
                continue
            if a_g.distance(b_g) < 0.25:  # 25cm in metric drawings; tweak per project
                _merge_evidence(con, dst=a_id, src=b_id, rule="colocated-points")
                to_delete.add(b_id)

    if to_delete:
        # DuckDB params for IN list
        placeholders = ",".join(["?"] * len(to_delete))
        con.execute(
            f"DELETE FROM features WHERE id IN ({placeholders})",
            list(to_delete),
        )
        con.execute(
            f"DELETE FROM ontology_log WHERE target_id IN ({placeholders})",
            list(to_delete),
        )


def _merge_evidence(con, *, dst: str, src: str, rule: str) -> None:
    """Append the source feature's evidence to the destination feature's."""
    row_dst = con.execute(
        "SELECT evidence FROM features WHERE id = ?", [dst]
    ).fetchone()
    row_src = con.execute(
        "SELECT evidence FROM features WHERE id = ?", [src]
    ).fetchone()
    if not row_dst or not row_src:
        return
    try:
        ev_dst = json.loads(row_dst[0] or "[]")
        ev_src = json.loads(row_src[0] or "[]")
    except json.JSONDecodeError:
        return
    for e in ev_src:
        e = dict(e)
        e["merged_from"] = src
        e["merge_rule"] = rule
        ev_dst.append(e)
    con.execute(
        "UPDATE features SET evidence = ? WHERE id = ?",
        [json.dumps(ev_dst), dst],
    )


def _update_extents_from_entities(con) -> None:
    """Header EXTMIN/EXTMAX are often stale (1e+20).  Recompute from
    the geometry we actually loaded."""
    row = con.execute(
        """
        SELECT min(bbox_min_x), min(bbox_min_y), min(z_min),
               max(bbox_max_x), max(bbox_max_y), max(z_max)
        FROM entities
        """
    ).fetchone()
    if not row or row[0] is None:
        return
    con.execute(
        """
        UPDATE drawing
        SET ext_min_x = ?, ext_min_y = ?, ext_min_z = ?,
            ext_max_x = ?, ext_max_y = ?, ext_max_z = ?
        WHERE id = 1
        """,
        [
            float(row[0]), float(row[1]),
            float(row[2]) if row[2] is not None else 0.0,
            float(row[3]), float(row[4]),
            float(row[5]) if row[5] is not None else 0.0,
        ],
    )
