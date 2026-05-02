# cadq — Developer Guide

This guide is for engineers maintaining or extending **cadq**. It explains
the architecture, the ingest pipeline, where to add new behaviour, the
data model on disk, the test strategy, and the operational gotchas you'll
hit. The companion [README.md](README.md) is the user-facing introduction;
this document is the codebase tour.

---

## 1. What cadq is, in one paragraph

cadq is a deterministic, file-based query engine for CAD drawings. The
`ingest` command takes a `.dwg` (via the ODA File Converter) or `.dxf`,
parses it with [ezdxf](https://ezdxf.readthedocs.io/), classifies its
layers and blocks against a YAML rule pack, runs a sequence of geometry
passes (polygonize / label join / tree-dedup / …), and writes everything
into a single DuckDB file (`*.cadqcache`) sitting next to the source. All
read-side commands (CLI + MCP) operate on that cache. There is **no**
hidden state and **no** runtime LLM call — answers are computed by
geometric / SQL operations and returned as stable JSON.

The whole system is intentionally narrow: every feature carries an
`evidence` blob with the original DXF entity handle, the rule that
matched, and a confidence score. AI harnesses use cadq through the CLI
or the MCP server in `cadq-mcp`; numeric answers come from cadq, never
from the model.

---

## 2. Repository layout

```
.
├── pyproject.toml            # Hatchling build, console scripts, deps
├── README.md                 # User-facing
├── DeveloperGuide.md         # (this file)
├── rules/
│   └── layers.default.yaml   # Default ontology rule pack
├── samples/                  # Hand-rolled sample DXFs (gitignored cache files)
├── scripts/
│   ├── inspect_drawing.py    # Ad-hoc cache inspector
│   ├── compute_garden.py     # Original garden-area derivation prototype
│   └── count_trees.py        # Tree-count diagnostic
├── skills/
│   └── cadq/                 # Agent Skill (SKILL.md + references + scripts)
├── src/
│   └── cadq/
│       ├── __init__.py
│       ├── __main__.py       # python -m cadq entry point
│       ├── cli.py            # Typer CLI
│       ├── mcp_server.py     # FastMCP server (mirrors CLI surface)
│       ├── config.py         # %APPDATA%/cadq/config.json persistence
│       ├── store.py          # DuckDB schema + connection helper
│       ├── ontology.py       # Rule pack loader + classifier
│       ├── ingest.py         # Pipeline (DXF → cache)
│       └── queries.py        # Read-side queries used by CLI + MCP
└── tests/
    └── test_smoke.py         # End-to-end pipeline tests
```

Two console entry points are declared in `pyproject.toml`:

- `cadq` → [`cadq.cli:app`](src/cadq/cli.py)
- `cadq-mcp` → [`cadq.mcp_server:main`](src/cadq/mcp_server.py)

---

## 3. Architecture at a glance

```
                ┌──────────────────────┐
   *.dwg ──►    │  ODA File Converter  │  (external; resolved via cadq.config)
                └──────────┬───────────┘
                           │ *.dxf
                ┌──────────▼───────────┐
                │  ezdxf parse         │
                ├──────────────────────┤
   Rule pack ──►│  ingest.py passes:   │
   (rules/      │   1. classify        │
    layers      │   2. polygons        │
    .default    │   3. hatches         │
    .yaml)      │   4. block inserts   │
                │   5. polygonize      │
                │   6. line features   │
                │   7. spot levels     │
                │   8. label join      │
                │   9. tree de-dup     │
                ├──────────────────────┤
                │  DuckDB cache        │  ← single file *.cadqcache
                └──────────┬───────────┘
                           │
        ┌──────────────────┴──────────────────┐
        ▼                                     ▼
 ┌────────────┐                       ┌──────────────┐
 │ cadq CLI   │                       │  cadq-mcp    │
 │ (Typer)    │                       │  (FastMCP)   │
 └────────────┘                       └──────────────┘
        ▲                                     ▲
        └──────────── stable JSON ────────────┘
                       │
                       ▼
             AI harness / shell user
```

Three invariants the codebase enforces:

1. **Cache is the source of truth.** Once `ingest` finishes, no read
   command needs the original DWG/DXF or the rule pack again.
2. **Every numeric answer is sourced.** Features carry `evidence`
   (handle, layer, rule, confidence). Tools refuse to guess.
3. **CLI ≡ MCP.** Every MCP tool wraps a function from `queries.py`.
   New behaviour added to `queries.py` should appear in both surfaces.

---

## 4. Module-by-module tour

### 4.1 `cadq.store` — the DuckDB schema

[`src/cadq/store.py`](src/cadq/store.py) owns the on-disk format. Tables:

| Table | Purpose |
|---|---|
| `meta` | `schema_version` and other key/value flags |
| `drawing` | One row per cache: source path, units, extents, georef flag |
| `layers` | Layer metadata + ontology mapping (type, confidence, rule) |
| `entities` | All model-space entities (line, polyline, hatch, text, insert, …) with WKB geometry, per-vertex `z_values`, and a JSON `attrs` blob |
| `texts` | Text/MTEXT/ATTRIB rows linked back to `entities.id` |
| `inserts` | Block inserts + transform |
| `spot_elevations` | Promoted RL/FFL/IL labels and block-attached Z |
| `features` | Semantic features (the AI-facing layer) |
| `ontology_log` | Every classification attempt — used by `cadq explain` |

**Geometry storage.** Two-dimensional WKB in a `BLOB` column with a
parallel `z_values DOUBLE[]` for per-vertex elevation. This keeps the
spatial extension optional (we read with shapely) and keeps the file
small. Bounding box columns (`bbox_min_x` etc.) let DuckDB filter
without unpickling WKB.

**Schema versioning.** `SCHEMA_VERSION` is an integer constant. Bump it
when you change DDL; ingest writes the version into `meta` on every
build. `connect()` re-applies all `DDL` statements idempotently
(`CREATE TABLE IF NOT EXISTS`) so old caches won't crash, but any
breaking change needs a migration helper. Today there isn't one — caches
older than the current version should be regenerated by re-running
`cadq ingest`.

**Cache discovery.** `cache_path_for(source)` returns
`<source>.cadqcache`. `find_default_cache(cwd)` is what powers "no
`--cache` flag means use the closest cache".

### 4.2 `cadq.ontology` — the classifier

[`src/cadq/ontology.py`](src/cadq/ontology.py) loads
[`rules/layers.default.yaml`](rules/layers.default.yaml) (or a user
override) and exposes:

- `Ontology.classify_layer(name) → Match | None`
- `Ontology.all_layer_matches(name)` — used by `explain` to show every
  matching rule.
- `Ontology.classify_block(name)` — block-name rules.
- `Ontology.hatch_hint(pattern_name)` — secondary signal for hatch fills.

**PascalCase tokenization.** Real surveys use names like `TreeSpread`,
`LevelsSpot`, `AssumedBoundary`. The original rules used `(^|[-_])`
word boundaries which never matched mid-word. The classifier now passes
every name through `_tokenize()`, which inserts `_` at:

- lowercase → uppercase transitions (`TreeSpread` → `Tree_Spread`)
- letter ↔ digit transitions (`Building3` → `Building_3`)

The same rule pack therefore handles both dash-style and PascalCase
schemas without doubling the rule count.

**Rule precedence.** Rules are first-match-wins. More-specific rules go
first. Crucially: tree/canopy rules now appear *before* the generic
`PLNT|planting|bed|garden` rule, so a layer like `L-PLNT-CANOPY`
classifies as a tree rather than generic planting.

**Adding a vocabulary.** Edit `rules/layers.default.yaml`. The format
documents itself; pattern is a Python regex (case-insensitive). Test
the rule with one of the unit-test cases under
`test_pascalcase_layer_classification` to lock it in.

### 4.3 `cadq.ingest` — the pipeline

[`src/cadq/ingest.py`](src/cadq/ingest.py) is the largest module. The
top-level entry point is `ingest(source, *, rules=None, cache=None) →
Path`. The shape is:

```python
def ingest(source, *, rules=None, cache=None) -> Path:
    dxf_path = _dwg_to_dxf(source) if .dwg else source
    onto = Ontology.load(rules)
    doc = ezdxf.readfile(dxf_path)
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
```

Notes:

- **`_dwg_to_dxf`** shells out to ODA via `find_oda_converter_with_source()`
  (resolution order: env var → user config → PATH → versioned install
  folders under `Program Files`). The error message points users at
  `cadq oda install` / `cadq oda set-path`.
- **`_write_entities`** is the only place we touch raw ezdxf entities. It
  is wrapped in a per-entity `try/except` so one malformed entity doesn't
  abort the whole ingest. New entity-type support goes here.
- **`_build_features_from_polylines`** explicitly skips contour /
  surface / roof ontologies — those are *line-natured* and handled later.
- **`_build_features_from_polygonize`** stitches open line networks into
  polygons. It only runs for ontologies where a region makes sense
  (`landscape.hardscape`, `landscape.softscape`, `landscape.water`,
  `site.boundary`, `building.footprint`) and suppresses duplicates whose
  intersection is ≥95% of the smaller polygon.
- **`_label_features_from_text`** restricts to "nameable" ontologies
  (`landscape.`, `building.`, `site.`) so a stray text inside a contour
  doesn't get attached as the contour's name.
- **`_dedupe_tree_features`** collapses trunks inside canopies, keeping
  the canopy and merging evidence with `merge_rule: trunk-in-canopy`.
  Co-located trunks within 0.25 m are also collapsed.
- **`_update_extents_from_entities`** is a final sweep — many DWGs have
  stale `$EXTMIN/$EXTMAX` values (e.g. `1e+20`); we recompute from the
  geometry actually loaded.

**Adding a new pass.** Three rules:

1. Insert a single function call in `ingest()` between existing passes,
   in the right ordering. Passes are not commutative — label join must
   run *after* features exist, dedup must run *after* label join (so the
   merged feature carries the label).
2. Use the `_Counters` object for stable, per-ontology feature ids
   (`lawn-1`, `lawn-2`, …). Don't roll your own.
3. Write to `ontology_log` whenever you make a classification decision.
   It's how `cadq explain` works.

**Feature ID naming.** `_new_feature_id` slugs the leaf of the ontology
type and per-ontology counts (`lawn-1`, `tree-3`, `manhole-5`). The
counters are reset per-ingest so ids are stable for a given drawing.

### 4.4 `cadq.queries` — the read side

[`src/cadq/queries.py`](src/cadq/queries.py) is the API surface
consumed by both the CLI and MCP. Everything here is pure-read, takes a
`Path` to the cache, and returns plain dicts / dataclasses.

Public surface:

| Function | What it does |
|---|---|
| `info(cache)` | Drawing metadata + entity counts |
| `list_layers(cache, name_filter=None)` | Layers + ontology mapping |
| `list_features(cache, ontology_prefix=None, layer=None, geom_kind=None)` | Filtered feature listing |
| `get_feature(cache, id)` / `feature_to_dict(f)` | Single feature record |
| `feature_area(cache, id)` | Area in DU + m² |
| `feature_boundary(cache, id, fmt="geojson")` | GeoJSON or WKT |
| `elevation_extreme(cache, mode)` | min / max sample point |
| `elevation_at(cache, x, y)` | IDW on nearest 3 samples |
| `elevation_profile(cache, x1,y1,x2,y2, samples)` | Sampled profile + min/max/grade |
| `nearest_features(cache, *, to, type_prefix=None, limit=5)` | Distance-sorted neighbours |
| `adjacent_features(cache, *, to, tolerance=1e-6)` | touches/overlaps/intersects |
| `label_search(cache, pattern)` | Glob over text labels |
| `garden_area(cache, *, subtract_hedges=False, subtract_canopies=False, extra_subtract=())` | Site − built features |
| `plan(question)` | Suggest a tool sequence (rule-based) |
| `explain(cache, target_id)` | Pull rows from `ontology_log` |

**Elevation samples.** `_all_elevation_points` aggregates `(x, y, z,
source)` from `spot_elevations` (text-derived) and from polyline
vertices on elevation-bearing layers (`survey.contour.*`,
`survey.surface`, `survey.spot_elevation`, `building.roof`). Anything
else with `z=0` is excluded — otherwise lawn polygons drown out real
contours. If you add a new elevation-bearing ontology, update
`_ELEVATION_ONTOLOGY_PREFIXES`.

**Garden derivation.** `garden_area()` looks for a site polygon in this
priority order: largest closed `site.boundary` polygon → polygonized
union of `site.boundary` lines → convex hull (with a warning in the
returned `site_method` field). It then subtracts `building.*`,
`landscape.hardscape.*`, `landscape.water`, and
`services.drainage.manhole` by default. Hedges and canopies are
optional.

### 4.5 `cadq.cli` — the Typer CLI

[`src/cadq/cli.py`](src/cadq/cli.py) is mostly thin wrappers over
`queries.py`. Conventions:

- Every command supports `--format json|text` (default JSON).
- `_resolve_cache(cache)` finds the right cache — explicit `--cache`,
  otherwise the closest `*.cadqcache` in the current tree. No env var
  fallback (that's for MCP).
- Subcommand groups (`features`, `elevation`, `topology`, `label`,
  `oda`) are individual `typer.Typer` instances composed via
  `app.add_typer(...)`.
- Error messages follow the pattern `typer.echo("...", err=True);
  raise typer.Exit(1)` — no exceptions leak to the user except for
  unexpected bugs.

**Adding a new command.** Wrap a function from `queries.py`, accept a
`cache` option for explicit override, call `_resolve_cache()`, and
`_emit()` the result. Then mirror the same call in
[`mcp_server.py`](src/cadq/mcp_server.py) as a `@server.tool()`.

### 4.6 `cadq.mcp_server` — the MCP server

[`src/cadq/mcp_server.py`](src/cadq/mcp_server.py) uses
[`FastMCP`](https://github.com/modelcontextprotocol) to expose the same
surface over MCP stdio. Unlike the CLI, the active drawing is sticky:

- Calling `ingest(...)` sets `_active_cache`.
- Otherwise, `CADQ_CACHE` env var is consulted, then `find_default_cache(cwd)`.

Each tool docstring is what the AI sees, so keep them short and
intentional. The server's top-level `instructions` field explains the
expected workflow ("call `info` first; numeric answers must come from
tools; do not invent values").

### 4.7 `cadq.config` — user-level settings

[`src/cadq/config.py`](src/cadq/config.py) persists settings to
`%APPDATA%\cadq\config.json` (Windows) or `~/.config/cadq/config.json`
(POSIX). Only one key is currently used: `oda_file_converter`. The four
levels of resolution are documented in the module docstring.

---

## 5. Data flow for a typical question

User asks: *"How big is the lawned area?"*

```
1. CLI: cadq features list --type landscape.softscape.lawn
        ─► queries.list_features(cache, ontology_prefix="landscape.softscape.lawn")
            ─► SELECT id, ontology, ... FROM features WHERE ontology LIKE ?
2. CLI: cadq area --feature lawn-1
        ─► queries.feature_area(cache, "lawn-1")
            ─► SELECT area_du, area_m2, confidence FROM features WHERE id=?
3. AI/User sums area_m2 across all returned features.
```

No LLM involvement; every step is a deterministic SQL query against the
cache. The `evidence` blob on each feature lets the AI quote the source
DXF handle if challenged.

---

## 6. Testing

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

`tests/test_smoke.py` builds a synthetic DXF in-memory (lawn,
driveway-as-edge-lines, contours, RL spot levels, tree trunk inside
canopy, manhole, site boundary), ingests it, and asserts on every public
function. The tests are intentionally end-to-end — they catch
classifier regressions, polygonize bugs, dedup bugs, and label-join
bugs that pure unit tests would miss.

Categories:

- **Classification** — both dash-style and PascalCase rules are pinned
  in `test_pascalcase_layer_classification`.
- **Pipeline shape** — feature counts per ontology, geom_kind filter,
  trunk-in-canopy de-dup.
- **Geometry** — exact areas, profile slopes, polygonized boundary
  reconstruction, IDW elevation snap.
- **Domain** — `garden_area` numeric correctness, `plan()` keyword
  matches.

When fixing a bug, **add the regression test first**. Real surveys
exposed the PascalCase gap and the trunk-double-count bug; both have
named test cases now.

A second smoke surface is the [`skills/cadq/scripts/quickstart.ps1`](skills/cadq/scripts/quickstart.ps1)
script — it generates a sample DXF using `tests.test_smoke._make_sample_dxf`,
runs the CLI top-to-bottom, and prints a green banner. Useful after
install changes.

---

## 7. Adding new behaviour — recipes

### 7.1 New ontology vocabulary

1. Edit [`rules/layers.default.yaml`](rules/layers.default.yaml). Be
   careful with rule order — first match wins.
2. If your rule needs to override a more general rule (e.g. tree
   beating planting), put it *above* the general rule.
3. Add a case to `test_pascalcase_layer_classification`.
4. Re-run pytest.

### 7.2 New ingest pass

1. Write `_my_pass(con, doc, onto, counters)` in
   [`ingest.py`](src/cadq/ingest.py). Read what you need from
   `entities` / `features`, write into `features` and `ontology_log`.
2. Insert it in the pipeline in `ingest()` at the right point. Earlier
   passes only see entities; later passes see features. Dedup must come
   last.
3. Use `_new_feature_id(con, ontology, counters)` for stable IDs.
4. Add a test that exercises both the positive case and a non-matching
   layer (so the pass doesn't over-fire).

### 7.3 New read query

1. Add a function to [`queries.py`](src/cadq/queries.py). Always take
   `cache: Path` as the first argument. Open with `_open(cache)` and
   close in a `finally`.
2. Wrap in a CLI command in [`cli.py`](src/cadq/cli.py). Use
   `_resolve_cache(cache)` and `_emit(payload, fmt)`.
3. Wrap as an `@server.tool()` in
   [`mcp_server.py`](src/cadq/mcp_server.py). Tool docstring becomes
   AI-visible documentation.
4. Add a test.

### 7.4 New entity type from ezdxf

1. Add a branch in `_write_entities` in
   [`ingest.py`](src/cadq/ingest.py). Capture `wkb`, `z_values`,
   `bbox_*`, `attrs`. Wrap in `try/except` — never abort the whole
   ingest.
2. If the new entity should produce features, write a
   `_build_features_from_<kind>` pass.

### 7.5 Per-project rule overrides

Users can pass `cadq ingest --rules my-rules.yaml`. The YAML uses the
same schema as the default. Document any required custom mappings in
the project's own README, not in cadq itself.

---

## 8. Operational gotchas

- **DWG ↔ DXF roundtrip.** Some DWG features don't survive
  the ODA conversion cleanly (paper-space layouts, complex linetypes).
  cadq currently ingests model-space only. Open layouts won't appear.
- **Stale extents in DXFs.** `$EXTMIN`/`$EXTMAX` are often `1e+20`.
  `_update_extents_from_entities` recomputes from real geometry.
- **Unitless drawings.** Many UK surveys ship with `$INSUNITS=0`. cadq
  reports `units_name="unitless"`, `units_to_m=1.0`, and `area_m2 ==
  area_du`. Document this to the AI: when units are unitless, the
  drawing-unit numbers are usually metres but cadq won't *assume* it.
- **Incomplete site boundaries.** `AssumedBoundary` is frequently drawn
  as open lines. `garden_area` falls back to a convex hull and reports
  `site_method` so callers can flag it.
- **Buildings as wall lines.** Many surveys draw houses as open `Wall`
  polylines. The current pipeline doesn't polygonize walls into a
  footprint, so `building.footprint` may be incomplete on those
  drawings — see the roadmap.
- **PowerShell quoting.** Avoid inline Python (`-c "..."`) in PowerShell
  one-liners — the parser eats `*` and `[]` aggressively. Use script
  files in [`scripts/`](scripts/) instead.
- **DuckDB editable installs.** Re-running `pip install -e .` after
  changing `pyproject.toml` is required when adding new console scripts
  or extras. Code-only changes don't need a reinstall (editable mode).

---

## 9. Releasing

There isn't an automated release pipeline yet. Manual checklist:

1. Bump `version` in [`pyproject.toml`](pyproject.toml) and
   `__version__` in [`src/cadq/__init__.py`](src/cadq/__init__.py).
2. Run the full test suite (`pytest -q`) — must be all green.
3. Run the skill quickstart (`powershell -File
   skills\cadq\scripts\quickstart.ps1`).
4. Tag the release: `git tag -a v0.x.y -m "..."; git push --tags`.
5. (Future) `pipx` packaging or a GitHub release containing a wheel.

---

## 10. Where the design intentionally stops

These are non-goals — please don't add them without discussion:

- **No CAD authoring.** cadq reads drawings; it doesn't write them.
- **No LLM calls inside cadq.** All reasoning is rule-based. AI lives
  in the harness, not the tool.
- **No web service.** Stdio + filesystem only. If you need a service,
  wrap `cadq-mcp`.
- **No bundled ODA converter.** Licence prevents redistribution.
  `cadq oda install` is the maximum we do.
- **No GPL dependencies.** cadq is MIT and we want it to stay
  embeddable. `libredwg` would mean a relicense — see README.

---

## 11. Code of conduct (for the codebase)

- **Stable JSON schemas.** AI harnesses parse our output. Don't
  rename or remove fields without a deprecation cycle. Adding new
  optional fields is fine.
- **Provenance on every claim.** Every feature row keeps `evidence` and
  every classification writes to `ontology_log`. Skipping that breaks
  `cadq explain`.
- **Per-entity exceptions are non-fatal.** Real CAD files are messy.
  One bad entity must not abort the ingest. `_write_entities` shows the
  pattern.
- **Tests before fixes.** Especially for things found on real
  drawings. The PascalCase + tree-dedup work both started as failing
  tests.
- **Keep the surface narrow.** Each new CLI verb should map to one
  well-defined query. Compound verbs ("nearest tree to manhole within
  5 m") belong in the AI harness, not in cadq.
