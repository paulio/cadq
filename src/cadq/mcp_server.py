"""MCP server skeleton exposing cadq's query surface to AI harnesses.

The server is intentionally thin: every tool is a one-liner over the
`queries` module so the same code path serves the CLI and MCP clients.

Run with: ``cadq-mcp`` or ``python -m cadq.mcp_server``.

Requires the optional ``mcp`` extra:  ``pip install -e ".[mcp]"``.

The MCP SDK API is still evolving; we use the high-level ``FastMCP`` helper
which is stable across recent releases.  If your installed SDK is older, the
import error message points to the right install command.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP  # type: ignore
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        "The MCP SDK is not installed. Install it with:\n"
        '    pip install -e ".[mcp]"\n'
        f"(import error: {exc})"
    ) from exc

from cadq import __version__
from cadq.ingest import ingest as ingest_file
from cadq.queries import (
    adjacent_features,
    elevation_at,
    elevation_extreme,
    elevation_profile,
    explain as explain_target,
    feature_area,
    feature_boundary,
    feature_to_dict,
    garden_area,
    get_feature,
    info as info_drawing,
    label_search,
    list_features,
    list_layers,
    nearest_features,
    plan as plan_query,
)
from cadq.store import find_default_cache


server = FastMCP(
    name="cadq",
    instructions=(
        "Semantic query tools for DWG/DXF drawings.\n"
        "Always call `info` first to confirm the drawing is loaded.\n"
        "Use `features_list` to discover ontology types before asking for "
        "areas or boundaries.  Every numeric answer comes from the drawing — "
        "do not invent values.\n"
        "Set the active drawing once via the CADQ_CACHE environment variable "
        "or by calling `ingest` with the source path.  Subsequent tool calls "
        "use that cache automatically."
    ),
)


# --- active cache resolution ----------------------------------------------


_active_cache: Path | None = None


def _set_active(cache: Path) -> None:
    global _active_cache
    _active_cache = Path(cache)


def _cache() -> Path:
    if _active_cache is not None:
        return _active_cache
    env = os.environ.get("CADQ_CACHE")
    if env and Path(env).exists():
        return Path(env)
    found = find_default_cache(Path.cwd())
    if not found:
        raise RuntimeError(
            "No active cadq cache. Call `ingest` with a DWG/DXF path, "
            "or set CADQ_CACHE to a .cadqcache file."
        )
    return found


# --- tools -----------------------------------------------------------------


@server.tool()
def version() -> str:
    """Return the cadq version."""
    return __version__


@server.tool()
def ingest(source: str, rules: str | None = None) -> dict[str, Any]:
    """Ingest a DWG or DXF file and make it the active drawing.

    Args:
        source: Absolute path to the .dwg or .dxf file.
        rules: Optional path to an ontology rules YAML.
    """
    out = ingest_file(source, rules=rules)
    _set_active(out)
    return {"cache": str(out), "source": str(source)}


@server.tool()
def info() -> dict[str, Any]:
    """Drawing metadata: units, extents, georef flag, entity counts."""
    return asdict(info_drawing(_cache()))


@server.tool()
def layers_list(name_filter: str | None = None) -> list[dict[str, Any]]:
    """List layers and their ontology classifications."""
    return list_layers(_cache(), name_filter=name_filter)


@server.tool()
def features_list(
    ontology_prefix: str | None = None,
    layer: str | None = None,
    geom_kind: str | None = None,
) -> list[dict[str, Any]]:
    """List semantic features.

    Pass `ontology_prefix='landscape.softscape.lawn'` to find lawns,
    `'landscape.hardscape.driveway'` for driveways, etc.  Optionally
    filter by `geom_kind` ('polygon' | 'line' | 'point').
    """
    return [feature_to_dict(f) for f in list_features(
        _cache(),
        ontology_prefix=ontology_prefix,
        layer=layer,
        geom_kind=geom_kind,
    )]


@server.tool()
def feature_get_tool(feature_id: str) -> dict[str, Any] | None:
    """Get one feature by id (e.g. `lawn-1`), with evidence."""
    f = get_feature(_cache(), feature_id)
    return feature_to_dict(f) if f else None


@server.tool()
def area(feature_id: str) -> dict[str, Any] | None:
    """Return the area of a feature in drawing units and m²."""
    return feature_area(_cache(), feature_id)


@server.tool()
def boundary(feature_id: str, fmt: str = "geojson") -> str:
    """Boundary of a feature as GeoJSON (default) or WKT."""
    out = feature_boundary(_cache(), feature_id, fmt=fmt)
    if out is None:
        return ""
    if isinstance(out, str):
        return out
    return json.dumps(out)


@server.tool()
def elevation_max_tool() -> dict[str, Any] | None:
    """Highest elevation sample on the drawing (x, y, z, source)."""
    return elevation_extreme(_cache(), mode="max")


@server.tool()
def elevation_min_tool() -> dict[str, Any] | None:
    """Lowest elevation sample on the drawing (x, y, z, source)."""
    return elevation_extreme(_cache(), mode="min")


@server.tool()
def elevation_at_tool(x: float, y: float) -> dict[str, Any] | None:
    """IDW-interpolated elevation at (x, y) using nearest samples."""
    return elevation_at(_cache(), x, y)


@server.tool()
def elevation_profile_tool(
    x1: float, y1: float, x2: float, y2: float, samples: int = 25,
) -> dict[str, Any] | None:
    """Sample elevation along a line and return min/max/grade plus the samples."""
    return elevation_profile(_cache(), x1, y1, x2, y2, samples=samples)


@server.tool()
def nearest_tool(
    feature_id: str,
    ontology_prefix: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Features nearest to `feature_id`, optionally filtered by ontology prefix."""
    return nearest_features(
        _cache(), to=feature_id, type_prefix=ontology_prefix, limit=limit,
    )


@server.tool()
def topology_adjacent_tool(
    feature_id: str, tolerance: float = 1e-6,
) -> list[dict[str, Any]]:
    """Features that touch / overlap / abut the given feature."""
    return adjacent_features(_cache(), to=feature_id, tolerance=tolerance)


@server.tool()
def label_search_tool(pattern: str) -> list[dict[str, Any]]:
    """Search drawing text labels (glob pattern, e.g. `drive*`)."""
    return label_search(_cache(), pattern)


@server.tool()
def plan_tool(question: str) -> dict[str, Any]:
    """Suggest a sequence of cadq tools for a natural-language question."""
    return plan_query(question)


@server.tool()
def explain_tool(target_id: str) -> list[dict[str, Any]]:
    """Why was `target_id` (layer name or feature id) classified as it was?"""
    return explain_target(_cache(), target_id)


@server.tool()
def garden_tool(
    subtract_hedges: bool = False,
    subtract_canopies: bool = False,
    include_geometry: bool = False,
) -> dict[str, Any] | None:
    """Derive the garden / open ground area.

    `garden = site.boundary - (building + hardscape + water + manholes)`.
    Pass `subtract_hedges=True` if hedges should not count as garden.  When
    `include_geometry=False` (default) the residual GeoJSON is omitted to
    keep responses small.
    """
    out = garden_area(
        _cache(),
        subtract_hedges=subtract_hedges,
        subtract_canopies=subtract_canopies,
    )
    if out is None:
        return None
    if not include_geometry:
        out.pop("geometry_geojson", None)
    return out


@server.tool()
def trees_tool() -> dict[str, Any]:
    """Count unique tree features (after trunk-in-canopy de-duplication)."""
    rows = list_features(_cache(), ontology_prefix="landscape.softscape.tree")
    by_kind: dict[str, int] = {}
    for r in rows:
        by_kind[r.geom_kind] = by_kind.get(r.geom_kind, 0) + 1
    return {
        "count": len(rows),
        "by_geom_kind": by_kind,
        "feature_ids": [r.id for r in rows],
    }


# --- entry point ----------------------------------------------------------


def main() -> None:
    """Run the MCP server over stdio (the standard MCP transport)."""
    server.run()


if __name__ == "__main__":  # pragma: no cover
    main()
