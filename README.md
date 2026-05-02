# cadq — Semantic query for DWG/DXF drawings

A CLI + MCP server that turns CAD drawings (DWG via DXF) into a queryable,
semantically enriched index, so AI harnesses (Copilot, Claude Desktop, Cursor)
can answer questions like:

- *Where is the highest point on the drawing?*
- *How big is the lawned area?*
- *What is the boundary of the driveway?*
- *How many trees are on this site?*
- *What's the open garden area once the building and driveway are removed?*

## Pipeline

```
DWG ──► DXF ──► ezdxf parse ──► DuckDB index ──► CLI/MCP tools ──► AI harness
              (ODA converter)   (+ rule-based ontology mapping
                                  + topology / dedup / labels)
```

The cache is a single DuckDB file written next to the source drawing. All
queries read from the cache so they're fast and deterministic — every
numeric answer is traceable back to the original DXF entity handles via
each feature's `evidence` field.

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev,mcp]"
```

`.dxf` input works out of the box. `.dwg` input needs the free
[ODA File Converter](https://www.opendesign.com/guestfiles/oda_file_converter)
— see [ODA setup](#oda-file-converter-setup) below.

## Quickstart

```powershell
cadq ingest .\samples\site.dxf
cadq info
cadq layers list
cadq features list --type landscape.softscape.lawn
cadq area --feature lawn-1
cadq boundary --feature driveway-1 --format geojson
cadq elevation max
cadq trees                           # one-line tree count
cadq garden                          # derive open-ground area
```

## CLI surface

### Core ingest / inspection

| Command                                              | Purpose                                  |
| ---------------------------------------------------- | ---------------------------------------- |
| `cadq ingest <file>`                                 | Parse + build `.cadqcache` (DuckDB)      |
| `cadq info`                                          | Drawing metadata, units, extents         |
| `cadq layers list [--filter ...]`                    | All layers + ontology mapping            |
| `cadq features list [--type ...] [--layer ...] [--geom-kind ...]` | Semantic features        |
| `cadq feature <id>`                                  | Full feature record + evidence           |
| `cadq area --feature <id>`                           | Area in drawing units (+ m²)             |
| `cadq boundary --feature <id> --format wkt\|geojson` | Boundary geometry                        |
| `cadq explain <layer\|feature_id>`                   | Why was this classified as X             |

### Spatial queries

| Command                                              | Purpose                                  |
| ---------------------------------------------------- | ---------------------------------------- |
| `cadq elevation max\|min`                            | Highest / lowest elevation sample        |
| `cadq elevation at <x> <y>`                          | IDW elevation at a point                 |
| `cadq elevation profile <x1> <y1> <x2> <y2>`         | Sampled profile + min/max/grade          |
| `cadq nearest --to <id> [--type ...] [--limit N]`    | Nearest features by distance             |
| `cadq topology adjacent --to <id>`                   | Features that touch / overlap            |
| `cadq label search '<glob>'`                         | Search drawing text                      |
| `cadq plan "<question>"`                             | Suggest a tool sequence (no execution)   |

### Domain shortcuts

| Command                                              | Purpose                                  |
| ---------------------------------------------------- | ---------------------------------------- |
| `cadq trees`                                         | Count unique trees (after trunk-in-canopy de-dup) |
| `cadq garden [--subtract-hedges] [--subtract-canopies] [--subtract <prefix>] [--with-geometry]` | Derive open-ground area from `site.boundary` minus built features |

### ODA File Converter

| Command                                              | Purpose                                  |
| ---------------------------------------------------- | ---------------------------------------- |
| `cadq oda status`                                    | Detection report (env / config / PATH / auto) |
| `cadq oda where`                                     | Print resolved path; non-zero exit if missing |
| `cadq oda install`                                   | Print steps + open the download page     |
| `cadq oda set-path <full path>`                      | Persist a custom converter path          |
| `cadq oda clear`                                     | Forget the persisted path                |
| `cadq oda convert <file.dwg> [-o out.dxf]`           | One-shot DWG → DXF                       |

## What gets recognised

cadq's default rule pack maps both dash-style (`L-PLNT-TREE`) and
PascalCase (`TreeSpread`, `LevelsSpot`, `AssumedBoundary`) layer names —
the classifier inserts word breaks at case transitions before matching.
Common ontologies:

- `landscape.softscape.{lawn, planting, tree, shrub, hedge}`
- `landscape.hardscape.{driveway, path, patio, road, step, fence}`
- `landscape.water`
- `building.{footprint, roof, wall, opening}`
- `site.boundary`
- `survey.{contour.major, contour.minor, surface, spot_elevation, bank}`
- `services.{drainage, drainage.manhole, drainage.gully, street_furniture}`
- `annotation.{text, grid, north_arrow, scale_bar}`

Per-project overrides via `cadq ingest --rules my-rules.yaml`. See
[`rules/layers.default.yaml`](rules/layers.default.yaml) for the full
default pack and the YAML format.

## Pipeline passes

After parsing the DXF with `ezdxf`, ingest runs these passes in order:

1. **Layer + block classification** against the rule pack.
2. **Polygon features** from closed polylines.
3. **Hatch features** (with hatch-pattern hints).
4. **Block insert features** (point geometry: trees, manholes, etc.).
5. **Polygonize pass** — recover regions from open edge-line networks
   (e.g. a driveway drawn as four lines instead of a closed polyline).
6. **Line features** for contours, boundaries, roof lines.
7. **Spot elevations** from text + block attributes (`RL 12.34`, `FFL`,
   `IL`, `+`/`-` levels).
8. **Label join** — text inside a polygon becomes the feature's `name`,
   skipping numeric labels and contour features.
9. **Tree de-duplication** — trunk point inside canopy polygon is
   collapsed; canopy wins, evidence is merged with `merge_rule:
   trunk-in-canopy`.

Every classification is logged to `ontology_log` so `cadq explain` can
show you *why*.

## MCP server

```powershell
cadq-mcp
```

Exposes the CLI surface as MCP tools so Copilot / Claude / Cursor can
call them. Sample registration for an MCP harness:

```json
{
  "mcpServers": {
    "cadq": { "command": "cadq-mcp", "env": { "CADQ_CACHE": "" } }
  }
}
```

A reusable Agent Skill is shipped under [`skills/cadq/`](skills/cadq/) —
SKILL.md, ontology + workflow references, and a self-verifying quickstart
script. Drop the folder into any agent that supports the
[Agent Skills spec](https://agentskills.io/specification).

## ODA File Converter setup

DWG is a proprietary binary format; cadq shells out to the free ODA
converter (which is licensed separately and isn't bundled). DXF inputs
need none of this.

```powershell
cadq oda install      # opens the download page + prints steps
# 1. Sign in / register a free ODA account
# 2. Download the Windows 64-bit installer
# 3. Run it, accept the licence
cadq oda status       # confirms detection (auto-finds versioned folders)
```

Resolution order (first hit wins):
1. `ODA_FILE_CONVERTER` environment variable
2. User config (`%APPDATA%\cadq\config.json`, written by `cadq oda set-path`)
3. `ODAFileConverter` on `PATH`
4. Auto-detection in `Program Files\ODA\…` and
   `Program Files\ODAFileConverter*` (versioned folders)

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

The smoke suite builds an in-memory DXF (lawn, driveway-by-edge-lines,
contours, RL spot levels, tree trunk inside canopy, manhole, site
boundary) and exercises the full pipeline end-to-end. **22 tests** cover
classification, polygonize, label join, tree de-dup, garden derivation,
elevation queries, topology, plan/explain, and PascalCase + survey-style
layer recognition.

## Status

The MVP described in the original brainstorm is complete:

- ✅ Ingest, ontology rules, polygonize, line features, spot levels.
- ✅ CLI + MCP for info / features / area / boundary / elevation /
  nearest / topology / labels / plan / explain.
- ✅ DWG support via ODA File Converter, with first-class management
  commands (`cadq oda …`).
- ✅ Domain shortcuts (`cadq trees`, `cadq garden`).
- ✅ PascalCase / UK-survey layer vocabulary.
- ✅ Tree de-duplication.

Roadmap (`# TODO` markers in code):

- TIN-based `elevation profile` (currently IDW).
- Polygonize closed wall networks into a building footprint.
- Xref resolution.
- Paper-space layout ingest.
- Confidence-weighted label join (multiple texts per polygon).
