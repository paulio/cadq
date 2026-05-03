# Workflow recipes

End-to-end transcripts for the canonical questions. Use these as templates
when composing tool calls in chat. All examples assume the user has already
provided a DWG/DXF or a `*.cadqcache` is in the workspace.

## 0. First time: ODA File Converter (DWG only)

If the user hands you a `.dwg` and `cadq ingest` fails with
*"DWG input requires the free ODA File Converter"*, drive these
commands rather than guessing:

```
cadq oda status                              # is it installed?
cadq oda install                             # opens download page + steps
cadq oda set-path C:\Path\ODAFileConverter.exe   # only if non-standard
cadq oda convert in.dwg -o out.dxf           # one-shot if you want DXF
```

Then re-run `cadq ingest <file.dwg>`. DXF input never needs ODA.

## 1. Highest point

**User:** *Where is the highest point on the drawing?*

**Plan:**
1. `info` — confirm the drawing has elevation data (`counts.spot_elevations`
   and contour features both > 0 is a good sign).
2. `elevation_max_tool` (MCP) or `cadq elevation max` (CLI).
3. Cross-check the source — `spot` is high-confidence (text label),
   `polyline` is medium (contour vertex).

**Sample reply:**
> The highest sampled point is **17.50** (units: metres) at
> `(x=5.0, y=5.0)`, taken from a spot-elevation text label
> (`source: spot`, confidence 0.9). The next-highest sample is the major
> contour at z=15, so this matches expectations.

## 2. Lawn area

**User:** *How big is the lawned area?*

**Plan:**
1. `features_list` filtered by `landscape.softscape.lawn`.
2. Sum `area_m2` across the returned features.
3. List the contributing features so the user can verify.

**Sample reply:**
> Total lawn area: **184.20 m²** across 2 features:
>
> | id     | name        | area_m² | layer       | confidence |
> | ------ | ----------- | ------- | ----------- | ---------- |
> | lawn-1 | Front Lawn  | 100.00  | L-LAWN-01   | 0.90       |
> | lawn-2 | Rear Lawn   |  84.20  | L-LAWN-REAR | 0.90       |
>
> Both classifications come from layer-name rules (high confidence). Run
> `cadq explain lawn-1` for the rule trail.

## 3. Garden / open-ground area (no explicit lawn polygon)

**User:** *What's the garden area?* (typical UK survey: no lawn layer,
just buildings/walls/paths/hedges drawn separately)

**Plan:**
1. `garden_tool` (MCP) or `cadq garden` (CLI) — does the derivation in
   one call.
2. Read `site_method` to gauge confidence:
   - `"site.boundary polygon feature"` — best, a closed boundary.
   - `"polygonize of site.boundary lines"` — good, lines stitched
     into a ring.
   - `"convex hull of site.boundary lines (open boundary)"` — flag as
     approximate; site is over-stated.
3. Optionally re-run with `--subtract-hedges` if the client treats
   hedges as not-garden.

**Sample reply:**
> Garden area (open ground): **372.33 m²**
>
> Derivation: `site - building - hardscape - water - manholes` from the
> `cadq garden` tool.
>
> | Component            | Area (m²) |
> | -------------------- | --------: |
> | Site (convex hull)   |    394.39 |
> | – Buildings (5)      |     −20.34 |
> | – Hardscape (path, step) |   −1.73 |
> | **Garden**           | **372.33** |
> | Hedges (informational) | 61.28 |
> | Tree canopy (informational) | 61.07 |
>
> ⚠️ The site polygon was derived by **convex hull** because the
> `AssumedBoundary` lines don't form a closed ring. The garden area is
> therefore an upper bound. Same caveat if the building outlines come
> from open wall lines (cadq doesn't yet polygonize walls into a
> footprint).

## 4. Tree count

**User:** *How many trees are there?*

**Plan:**
1. `trees_tool` (MCP) or `cadq trees` (CLI). Returns `{count,
   by_geom_kind, feature_ids}`.
2. The pipeline already collapses trunk-in-canopy duplicates, so this
   is a per-tree count. `by_geom_kind` shows how many were canopy
   polygons vs. standalone trunks.
3. If the user specifically asks "how many tree symbols?" /
   "how many TreeTrunk blocks?", switch to filtered listings:

```
cadq features list --type landscape.softscape.tree --geom-kind point
cadq features list --type landscape.softscape.tree --geom-kind polygon
```

**Sample reply:**
> **9 trees** on this site (after de-duplicating trunk inserts that fall
> inside a canopy polygon).
>
> - Canopies: 7
> - Standalone trunks: 2
>
> Trunk-in-canopy collapses are recorded in each merged feature's
> evidence with `merge_rule: trunk-in-canopy`. Run
> `cadq features list --type landscape.softscape.tree` for the full
> list with handles.

## 5. Driveway boundary

**User:** *What is the boundary of the driveway?*

**Plan:**
1. `features_list ontology_prefix="landscape.hardscape.driveway"`.
2. For each driveway feature, call `boundary feature_id="..." fmt="geojson"`
   (or `wkt` if the user wants a one-liner).
3. If the evidence rule is `polygonize`, flag that the boundary was
   inferred from open edge lines and that confidence is reduced.

**Sample reply (polygonized case):**
> Found 1 driveway feature, `driveway-1`, recovered from 4 open edge-lines
> on layer `L-DRIVEWAY-EDGE` (rule: `polygonize`, confidence 0.7). Boundary:
>
> ```wkt
> POLYGON ((24 0, 20 0, 20 20, 24 20, 24 0))
> ```
>
> Note: this region was reconstructed by stitching open lines, not read
> from a closed polyline. If you have a authoritative driveway polygon,
> classifying its layer to `landscape.hardscape.driveway` will give a
> higher-confidence answer.

## 6. Find what's near a feature

**User:** *Which trees are nearest the manhole?*

**Plan:**
1. `features_list ontology_prefix="services.drainage.manhole"` to get the
   manhole id.
2. `nearest_tool feature_id="<manhole-id>" ontology_prefix="landscape.softscape.tree" limit=5`.

## 7. Adjacency / what touches what

**User:** *Does the lawn touch the driveway?*

**Plan:**
1. `features_list ontology_prefix="landscape.softscape.lawn"`.
2. `topology_adjacent_tool feature_id="<lawn-id>" tolerance=0.01`.
3. Filter the result for entries whose `ontology` starts with
   `landscape.hardscape.driveway`.

The `relation` field is one of `touches | overlaps | intersects`. Quote it
verbatim — `intersects` may indicate a drafting error rather than design
intent.

## 8. Elevation profile / driveway gradient

**User:** *How steep is the driveway?*

**Plan:**
1. `features_list ontology_prefix="landscape.hardscape.driveway"` →
   `boundary` → pick two endpoints of the long axis. (For an MVP, ask the
   user for start/end coordinates if the driveway has a complex shape.)
2. `elevation_profile_tool x1 y1 x2 y2 samples=25`.
3. Report `average_grade` (rise/run, drawing units), `z_drop`, `z_min`,
   `z_max`. Only offer the full sample list on request.

## 9. Label search

**User:** *Where is the front entrance labelled?*

**Plan:**
1. `label_search_tool pattern="front*"` (glob: `*` → `%`, `?` → `_`).
2. For each match, optionally call `features_list` and intersect with the
   text point to find the named region.

## 10. "Why is this classified that way?"

**User:** *Why is `lawn-2` a lawn?*

**Plan:**
1. `explain_tool target_id="lawn-2"` returns rows from `ontology_log`
   showing every rule that matched, in order.
2. Quote the matching `rule` (regex) and `confidence`.

## Tips for chat composition

- Always run `info` first in a new session — it's cheap and prevents
  unit/extents confusion later. Watch for `units_name="unitless"`:
  many UK surveys ship that way despite the geometry being metres.
- When in doubt, call `plan_tool "<the user's question verbatim>"` — it
  returns a suggested tool sequence without side effects.
- Never paste raw GeoJSON into chat unless asked — it's noisy. Summarise
  first, offer the GeoJSON on request (`cadq garden --with-geometry`).
- If the user mentions a layer name explicitly (e.g. *"the L-PLAY area"*),
  use `cadq features list --layer L-PLAY` rather than guessing the
  ontology prefix.
- For tree counts, prefer the dedicated `cadq trees` shortcut over
  `features list | wc -l` — it gives the AI-friendly summary already.
