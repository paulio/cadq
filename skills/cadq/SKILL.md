---
name: cadq
description: Query DWG/DXF CAD drawings semantically from Copilot Chat using the cadq CLI and MCP server. USE WHEN the user asks spatial or semantic questions about a CAD drawing — "highest point", "where is the driveway", "how big is the lawn or garden", "boundary of the building", "how many trees", "what's near the manhole", "elevation at x,y", "section profile", "what's labelled X" — or asks to ingest, classify, or reason about a `.dwg` / `.dxf` / `.cadqcache` file. DO NOT USE FOR generic CAD authoring (drawing/editing entities), 3D modeling, or non-drawing GIS queries.
---

# cadq — semantic query for DWG/DXF drawings

`cadq` ingests a DWG or DXF drawing into a DuckDB-backed cache and exposes a
deterministic CLI + MCP server that answers spatial and semantic questions
about the drawing (lawn area, driveway boundary, highest point, garden
area, tree count, nearest features, etc.).

This skill teaches the agent how to drive `cadq` from Copilot Chat. **Every
numeric answer in your reply must come from a `cadq` tool call — never invent
coordinates, areas, or elevations.**

## When to invoke this skill

Trigger on any of:

- The user mentions a `.dwg`, `.dxf`, or `.cadqcache` file.
- The user asks a spatial/semantic question about a site plan, survey, or
  landscape drawing (areas, boundaries, elevations, adjacency, labels,
  garden / open-ground, tree / hedge counts).
- The user explicitly says *"use cadq"*, *"query the drawing"*, *"ingest this
  CAD file"*.

Do **not** trigger for: drawing/editing CAD entities, 3D model authoring,
generic GIS data not in CAD format.

## Two integration modes

Pick the first that applies:

1. **MCP server (preferred for chat).** If `cadq-mcp` is registered as an MCP
   server in the harness, call its tools directly. Available tools:
   - `version`, `ingest`, `info`
   - `layers_list`, `features_list`, `feature_get_tool`
   - `area`, `boundary`
   - `elevation_max_tool`, `elevation_min_tool`, `elevation_at_tool`,
     `elevation_profile_tool`
   - `nearest_tool`, `topology_adjacent_tool`, `label_search_tool`
   - `garden_tool`, `trees_tool`
   - `plan_tool`, `explain_tool`

   The active drawing is whatever was last `ingest`-ed in the session, or
   whatever `CADQ_CACHE` points at.

2. **CLI fallback.** Otherwise shell out to `cadq` (Typer-based). Every
   command supports `--format json` (default) and writes a stable schema.
   Output is structured — parse the JSON, do not regex the text rendering.

If neither is installed, see *Installation* below. For DWG inputs the
agent may also need to drive `cadq oda install` / `cadq oda set-path` —
see *ODA File Converter* below.

## Workflow contract

For every CAD question, follow this loop:

1. **Resolve the drawing.** If no cache is loaded yet, run `ingest` on the
   path the user gave (or the first `*.dwg`/`*.dxf` in the workspace).
   - DWG ingest will fail with a runtime error if the ODA File Converter
     isn't set up. The error message tells the user to run
     `cadq oda install` or `cadq oda set-path`. Echo that hint, don't try
     to be clever.
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
   Many UK surveys ship with `units_name="unitless"` but the geometry is
   actually metres — flag that to the user, don't guess.
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

### "How big is the garden?"

For drawings with no explicit lawn/garden polygon (typical UK surveys),
use the dedicated derivation tool:

```
garden_tool                                       # MCP
cadq garden                                       # CLI, default
cadq garden --subtract-hedges                     # exclude hedge area
cadq garden --subtract-canopies                   # rarely wanted
cadq garden --subtract building. --with-geometry  # custom + GeoJSON
```

The result computes `garden = site.boundary - building.* -
landscape.hardscape.* - landscape.water - services.drainage.manhole` and
reports:

- `site_area_m2`, `garden_area_m2` — the headline numbers
- `site_method` — one of `"site.boundary polygon feature"`,
  `"polygonize of site.boundary lines"`, or
  `"convex hull of site.boundary lines (open boundary)"`. Flag the
  convex-hull case as approximate.
- `subtractions[]` — per-prefix breakdown so the user can sanity-check
- `hedge_area_m2`, `canopy_area_m2` — informational extras

### "How many trees are there?"

```
trees_tool                  # MCP
cadq trees                  # CLI
```

Returns `{count, by_geom_kind, feature_ids}`. The pipeline already
collapses trunk-in-canopy duplicates, so this number is one entry per
unique tree (canopy-with-trunk-merged or standalone trunk/canopy). The
`by_geom_kind` split tells you how many were canopies vs. standalone
trunks. For canopies-only or trunks-only, use the `--geom-kind` filter:

```
cadq features list --type landscape.softscape.tree --geom-kind polygon
cadq features list --type landscape.softscape.tree --geom-kind point
```

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
(extensible per-project via `cadq ingest --rules my.yaml`):

- **Softscape**: `landscape.softscape.{lawn, planting, tree, shrub, hedge}`
- **Hardscape**: `landscape.hardscape.{driveway, path, patio, road, step, fence}`
- **Water**: `landscape.water`
- **Buildings**: `building.{footprint, roof, wall, opening}`
- **Site**: `site.boundary` (matches `SiteBdy`, `AssumedBoundary`,
  `TitleLine`, `RedLine`)
- **Survey**: `survey.{contour.major, contour.minor, surface,
  spot_elevation, bank}`
- **Services**: `services.{drainage, drainage.manhole, drainage.gully,
  street_furniture}`
- **Annotation**: `annotation.{text, grid, north_arrow, scale_bar}`

The classifier handles both dash-style (`L-PLNT-TREE`) and **PascalCase**
(`TreeSpread`, `LevelsSpot`, `AssumedBoundary`) naming — case transitions
are tokenised before matching.

See `references/ontology.md` for full layer-name + block-name regex
patterns and hatch hints.
See `references/workflows.md` for full chat→tool transcripts.

## Geometry kinds

Every feature has a `geom_kind`:

- `polygon` — closed regions (lawns, buildings, canopies, hedges, water).
  Carries `area_du` / `area_m2`.
- `line` — open curves (contours, ridges, site boundaries with open
  geometry). Carries `length_du` / `length_m`.
- `point` — single locations (block inserts: trees, manholes, north
  arrows, etc.).

Filter with `geom_kind="polygon"` (MCP) or `--geom-kind polygon` (CLI)
when you only want one geometric flavour.

## Tree de-duplication (note for the agent)

The ingest pipeline collapses common tree-symbol duplicates:

- Trunk INSERT *inside* a canopy polygon → keep canopy, merge trunk's
  evidence (`merge_rule: trunk-in-canopy`).
- Two co-located trunk points within 0.25 m → keep one.

Implication for the AI: **the count from `trees_tool` / `cadq trees` is
the unique-tree count, not the raw entity count.** When the user
specifically asks "how many tree symbols?" or "how many TreeTrunk
blocks?", use `cadq features list --type landscape.softscape.tree
--geom-kind point` (trunks) or `--geom-kind polygon` (canopies) instead.

## Output etiquette

- Show numbers to a sensible precision (e.g. m² to 0.01).
- When you list features, include `id`, `ontology`, `name` (if any),
  `area_m2` or `length_m`, and `confidence`.
- For boundaries, prefer GeoJSON when the user might paste it into a map,
  WKT for short answers in chat.
- For elevation profiles, summarise `z_min`, `z_max`, `average_grade` first;
  offer the full sample list on request.
- For garden derivations, lead with the headline `garden_area_m2`, then
  the `site_method`, then the subtraction breakdown.

## Installation (if not already set up)

```powershell
# from the repo containing cadq:
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev,mcp]"
# DWG input also requires the free ODA File Converter — see below.
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

## ODA File Converter

DWG is a proprietary binary format; cadq shells out to the free ODA
File Converter to translate DWG → DXF. DXF inputs need none of this.

If a DWG ingest fails, drive these commands:

```
cadq oda status        # is it installed?
cadq oda install       # opens download page + prints steps
cadq oda set-path C:\path\to\ODAFileConverter.exe   # custom location
cadq oda convert in.dwg -o out.dxf                  # one-shot
```

Resolution order: `ODA_FILE_CONVERTER` env var → user config (set via
`cadq oda set-path`) → `ODAFileConverter` on PATH → auto-detection in
`Program Files\ODA\…` and `Program Files\ODAFileConverter*`.

## Guard rails

- **No fabrication.** If a tool returns `null` or `[]`, say so. Do not
  estimate from context.
- **Confidence matters.** Features below `confidence: 0.7` (typically
  polygonized regions, hatch-only matches, or convex-hull-derived
  boundaries) should be flagged when quoted.
- **Cite handles.** The `evidence[].handle` is the original DXF handle —
  useful when the user wants to find the entity in their CAD package.
- **Per-project rules.** If the user's layer scheme differs from the default
  ontology, ask whether they want to supply a custom rules YAML
  (`cadq ingest --rules path/to/rules.yaml`) instead of guessing.
- **Avoid paper space.** cadq currently ingests model space only. If a
  query returns nothing surprising, ask whether the relevant geometry is on
  a paper-space layout.
- **Garden caveats.** When `site_method` is `"convex hull of
  site.boundary lines (open boundary)"`, the site polygon is approximate
  and the garden number is an over-estimate. Same for drawings where the
  building outline is drawn as open wall lines (not yet polygonized into
  `building.footprint` automatically).
