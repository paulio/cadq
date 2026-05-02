---
name: cadq
description: Query DWG/DXF CAD drawings semantically from Copilot Chat using the cadq CLI and MCP server. USE WHEN the user asks spatial or semantic questions about a CAD drawing — "highest point", "where is the driveway", "how big is the lawn", "boundary of the building", "what's near the manhole", "elevation at x,y", "section profile", "what's labelled X" — or asks to ingest, classify, or reason about a `.dwg` / `.dxf` / `.cadqcache` file. DO NOT USE FOR generic CAD authoring (drawing/editing entities), 3D modeling, or non-drawing GIS queries.
---

# cadq — semantic query for DWG/DXF drawings

`cadq` ingests a DWG or DXF drawing into a DuckDB-backed cache and exposes a
deterministic CLI + MCP server that answers spatial and semantic questions
about the drawing (lawn area, driveway boundary, highest point, nearest
features, etc.).

This skill teaches the agent how to drive `cadq` from Copilot Chat. **Every
numeric answer in your reply must come from a `cadq` tool call — never invent
coordinates, areas, or elevations.**

## When to invoke this skill

Trigger on any of:

- The user mentions a `.dwg`, `.dxf`, or `.cadqcache` file.
- The user asks a spatial/semantic question about a site plan, survey, or
  landscape drawing (areas, boundaries, elevations, adjacency, labels).
- The user explicitly says *"use cadq"*, *"query the drawing"*, *"ingest this
  CAD file"*.

Do **not** trigger for: drawing/editing CAD entities, 3D model authoring,
generic GIS data not in CAD format.

## Two integration modes

Pick the first that applies:

1. **MCP server (preferred for chat).** If `cadq-mcp` is registered as an MCP
   server in the harness, call its tools directly:
   `ingest`, `info`, `layers_list`, `features_list`, `feature_get_tool`,
   `area`, `boundary`, `elevation_max_tool`, `elevation_min_tool`,
   `elevation_at_tool`, `elevation_profile_tool`, `nearest_tool`,
   `topology_adjacent_tool`, `label_search_tool`, `plan_tool`, `explain_tool`.
   The active drawing is whatever was last `ingest`-ed in the session, or
   whatever `CADQ_CACHE` points at.

2. **CLI fallback.** Otherwise shell out to `cadq` (Typer-based). Every
   command supports `--format json` (default) and writes a stable schema.
   Output is structured — parse the JSON, do not regex the text rendering.

If neither is installed, see *Installation* below.

## Workflow contract

For every CAD question, follow this loop:

1. **Resolve the drawing.** If no cache is loaded yet, run `ingest` on the
   path the user gave (or the first `*.dwg`/`*.dxf` in the workspace).
2. **Orient.** Run `info` once and then `features_list` (no filter) so you
   know what's actually in the drawing before answering.
3. **Plan, then call.** For complex questions, call `plan_tool` /
   `cadq plan "<question>"` first — it returns a suggested sequence of cadq
   tools without executing anything. Use that as a checklist.
4. **Cite evidence.** Each feature carries `evidence` (handle, layer, rule,
   confidence). Quote the relevant fields when stating an answer; if the
   user pushes back, run `explain_tool` / `cadq explain <id>` to show the
   classification trail.
5. **Respect units.** `info` returns `units_name` and `units_to_m`. Areas
   are reported in both drawing units (`area_du`) and m² (`area_m2`); use m²
   when the drawing has real-world units, otherwise drawing units.
6. **Refuse to invent.** If a tool returns no data (e.g. no contours, no
   matching features), say so explicitly. Do not estimate.

## Recipes for the canonical questions

### "Where is the highest point on the drawing?"

```
elevation_max_tool          # MCP
cadq elevation max          # CLI
```

Returns `{x, y, z, source}`. `source` is `spot` (RL/FFL text) or `polyline`
(contour vertex). Quote z and source. If `null`, the drawing has no
elevation data — say so.

### "How big is the lawned area?"

```
features_list ontology_prefix="landscape.softscape.lawn"
# then for each feature id:
area feature_id
```

Sum `area_m2` across all returned lawns. The CLI equivalent:

```
cadq features list --type landscape.softscape.lawn
cadq area --feature lawn-1
```

Report the sum and list each contributing feature id.

### "What is the boundary of the driveway?"

```
features_list ontology_prefix="landscape.hardscape.driveway"
boundary feature_id="driveway-1" fmt="geojson"
```

Or `--format wkt` for short answers. If the driveway was reconstructed by
the polygonize pass (drawn as open edge lines), the `evidence[0].rule`
field will be `polygonize` and confidence is lower — flag that to the user.

### "What's near the manhole?" / "Is the shed within 3 m of the boundary?"

```
nearest_tool feature_id="manhole-1" ontology_prefix="landscape.softscape.tree" limit=5
topology_adjacent_tool feature_id="lawn-1" tolerance=0.001
```

### "What's labelled X?"

```
label_search_tool pattern="front*"
```

Returns `(text, layer, x, y)` rows. Combine with `features_list` to map a
label back to the feature it sits inside.

### "How steep is the driveway?"

1. `features_list ontology_prefix="landscape.hardscape.driveway"` to get the
   feature.
2. `boundary` to read its geometry, take two endpoints of the long axis (or
   ask the user for start/end).
3. `elevation_profile_tool x1 y1 x2 y2 samples=25`. Report `average_grade`
   (rise/run) and `z_drop`.

## Ontology cheat sheet

`features_list` accepts an `ontology_prefix`. The default vocabulary
(extensible per-project via `--rules <yaml>`):

- `landscape.softscape.lawn`, `landscape.softscape.planting`,
  `landscape.softscape.tree`, `landscape.softscape.shrub`
- `landscape.hardscape.driveway`, `landscape.hardscape.path`,
  `landscape.hardscape.patio`, `landscape.hardscape.road`
- `landscape.water`
- `building.footprint`, `building.roof`
- `site.boundary`
- `survey.contour.major`, `survey.contour.minor`,
  `survey.spot_elevation`, `survey.surface`
- `services.drainage`, `services.drainage.manhole`,
  `services.drainage.gully`
- `annotation.text`, `annotation.grid`, `annotation.north_arrow`,
  `annotation.scale_bar`

See `references/ontology.md` for layer-name regex patterns and hatch hints.
See `references/workflows.md` for full chat→tool transcripts.

## Output etiquette

- Show numbers to a sensible precision (e.g. m² to 0.01).
- When you list features, include `id`, `ontology`, `name` (if any),
  `area_m2` or `length_m`, and `confidence`.
- For boundaries, prefer GeoJSON when the user might paste it into a map,
  WKT for short answers in chat.
- For elevation profiles, summarise `z_min`, `z_max`, `average_grade` first;
  offer the full sample list on request.

## Installation (if not already set up)

```powershell
# from the repo containing cadq:
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev,mcp]"
# DWG input also requires the free ODA File Converter; DXF works as-is.
```

Register the MCP server with your harness (e.g. for Copilot/Claude Desktop
clients):

```json
{
  "mcpServers": {
    "cadq": {
      "command": "cadq-mcp",
      "env": { "CADQ_CACHE": "" }
    }
  }
}
```

`scripts/quickstart.ps1` does an end-to-end smoke test — run it once after
install to confirm everything works.

## Guard rails

- **No fabrication.** If a tool returns `null` or `[]`, say so. Do not
  estimate from context.
- **Confidence matters.** Features below `confidence: 0.7` (typically
  polygonized regions or hatch-only matches) should be flagged when quoted.
- **Cite handles.** The `evidence[].handle` is the original DXF handle —
  useful when the user wants to find the entity in their CAD package.
- **Per-project rules.** If the user's layer scheme differs from the default
  ontology, ask whether they want to supply a custom rules YAML
  (`cadq ingest --rules path/to/rules.yaml`) instead of guessing.
- **Avoid paper space.** cadq currently ingests model space only. If a
  query returns nothing surprising, ask whether the relevant geometry is on
  a paper-space layout.
