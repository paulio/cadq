"""DuckDB schema + connection helpers for the cadq cache file.

A `.cadqcache` is a single DuckDB database that sits next to the source
drawing.  It captures the normalized model and the semantic enrichment so
queries are fast and reproducible.

Geometry is stored as WKB (BLOB) and lifted to shapely on read, keeping the
on-disk format small and DuckDB-spatial-extension friendly without making
the extension a hard requirement at ingest time.
"""

from __future__ import annotations

from pathlib import Path

import duckdb


SCHEMA_VERSION = 1


DDL = [
    # --- meta -------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   VARCHAR PRIMARY KEY,
        value VARCHAR
    );
    """,
    # --- source drawing ---------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS drawing (
        id            INTEGER PRIMARY KEY,
        source_path   VARCHAR NOT NULL,
        dxf_version   VARCHAR,
        units_code    INTEGER,    -- INSUNITS
        units_name    VARCHAR,    -- 'meters', 'feet', ...
        units_to_m    DOUBLE,     -- multiplier from drawing units to metres
        ext_min_x     DOUBLE, ext_min_y     DOUBLE, ext_min_z     DOUBLE,
        ext_max_x     DOUBLE, ext_max_y     DOUBLE, ext_max_z     DOUBLE,
        has_georef    BOOLEAN,
        ingested_at   TIMESTAMP
    );
    """,
    # --- layers -----------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS layers (
        name           VARCHAR PRIMARY KEY,
        color          INTEGER,
        linetype       VARCHAR,
        is_frozen      BOOLEAN,
        is_off         BOOLEAN,
        is_locked      BOOLEAN,
        ontology_type  VARCHAR,   -- e.g. 'landscape.softscape.lawn'
        ontology_conf  DOUBLE,    -- 0..1
        ontology_rule  VARCHAR    -- which rule matched
    );
    """,
    # --- raw entities -----------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS entities (
        id          BIGINT PRIMARY KEY,
        handle      VARCHAR,        -- DXF handle for provenance
        kind        VARCHAR,        -- LINE | LWPOLYLINE | POLYLINE | CIRCLE | ARC | HATCH | TEXT | MTEXT | INSERT | 3DFACE
        layer       VARCHAR,
        is_closed   BOOLEAN,
        z_min       DOUBLE,
        z_max       DOUBLE,
        bbox_min_x  DOUBLE, bbox_min_y DOUBLE,
        bbox_max_x  DOUBLE, bbox_max_y DOUBLE,
        wkb         BLOB,           -- 2D geometry (z stored separately)
        z_values    DOUBLE[],       -- per-vertex Z when polyline carries elevation
        attrs       JSON            -- kind-specific extras
    );
    """,
    # --- text / annotations ----------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS texts (
        id          BIGINT PRIMARY KEY,
        entity_id   BIGINT REFERENCES entities(id),
        layer       VARCHAR,
        text        VARCHAR,
        x DOUBLE, y DOUBLE, z DOUBLE,
        height      DOUBLE,
        rotation    DOUBLE,
        kind        VARCHAR        -- 'text' | 'mtext' | 'attrib'
    );
    """,
    # --- block inserts ----------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS inserts (
        id          BIGINT PRIMARY KEY,
        entity_id   BIGINT REFERENCES entities(id),
        block_name  VARCHAR,
        layer       VARCHAR,
        x DOUBLE, y DOUBLE, z DOUBLE,
        rotation    DOUBLE,
        sx DOUBLE, sy DOUBLE, sz DOUBLE
    );
    """,
    # --- spot elevations (from text or block attribs) --------------------
    """
    CREATE TABLE IF NOT EXISTS spot_elevations (
        id         BIGINT PRIMARY KEY,
        entity_id  BIGINT REFERENCES entities(id),
        layer      VARCHAR,
        x DOUBLE, y DOUBLE,
        z DOUBLE,
        label      VARCHAR,        -- e.g. 'RL 124.32' or 'FFL'
        confidence DOUBLE,
        source     VARCHAR         -- 'text' | 'block_attrib' | 'polyline_z'
    );
    """,
    # --- semantic features (regions / lines / points) --------------------
    """
    CREATE TABLE IF NOT EXISTS features (
        id           VARCHAR PRIMARY KEY,   -- stable slug, e.g. 'lawn-1'
        ontology     VARCHAR NOT NULL,
        name         VARCHAR,
        layer        VARCHAR,
        geom_kind    VARCHAR,               -- 'polygon' | 'line' | 'point'
        wkb          BLOB,
        area_du      DOUBLE,                -- in drawing units squared
        area_m2      DOUBLE,
        length_du    DOUBLE,
        length_m     DOUBLE,
        z_min        DOUBLE,
        z_max        DOUBLE,
        confidence   DOUBLE,
        evidence     JSON                   -- list of {handle, layer, rule, ...}
    );
    """,
    # --- ontology mapping log (for `cadq explain`) -----------------------
    """
    CREATE TABLE IF NOT EXISTS ontology_log (
        target_kind   VARCHAR,    -- 'layer' | 'feature' | 'spot'
        target_id     VARCHAR,
        rule          VARCHAR,
        ontology      VARCHAR,
        confidence    DOUBLE,
        note          VARCHAR
    );
    """,
]


INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_entities_layer ON entities(layer);",
    "CREATE INDEX IF NOT EXISTS idx_entities_kind  ON entities(kind);",
    "CREATE INDEX IF NOT EXISTS idx_features_onto  ON features(ontology);",
    "CREATE INDEX IF NOT EXISTS idx_features_layer ON features(layer);",
    "CREATE INDEX IF NOT EXISTS idx_spots_layer    ON spot_elevations(layer);",
]


def cache_path_for(source: Path) -> Path:
    """Return the conventional cache path next to a source drawing."""
    return source.with_suffix(source.suffix + ".cadqcache")


def connect(path: Path, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open (or create) a cadq cache and ensure the schema is present."""
    path = Path(path)
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path), read_only=read_only)
    if not read_only:
        for stmt in DDL:
            con.execute(stmt)
        for stmt in INDEXES:
            con.execute(stmt)
        con.execute(
            "INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)",
            [str(SCHEMA_VERSION)],
        )
    return con


def find_default_cache(cwd: Path) -> Path | None:
    """Locate a `*.cadqcache` near the user — used when no path is supplied."""
    cwd = Path(cwd)
    candidates = sorted(cwd.glob("*.cadqcache"))
    if candidates:
        return candidates[0]
    candidates = sorted(cwd.glob("**/*.cadqcache"))
    return candidates[0] if candidates else None
