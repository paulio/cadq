# Ontology reference

Default rule pack used by `cadq` when classifying layers, hatches, and block
inserts. Override per-project by passing `cadq ingest --rules my.yaml`.

The dotted ontology types are designed for prefix queries — e.g.
`features_list ontology_prefix="landscape.softscape"` returns all soft
landscape features regardless of subtype.

## Classifier note: PascalCase tokenization

Real-world surveys use both naming styles:

- Dash-separated: `L-PLNT-TREE`, `L-LAWN-01`, `L-DRIVEWAY-EDGE`
- PascalCase: `TreeSpread`, `LevelsSpot`, `AssumedBoundary`, `StreetFurniture`

The classifier inserts a `_` at every lowercase→uppercase and letter↔digit
transition before matching, so `TreeSpread` → `Tree_Spread`,
`AssumedBoundary` → `Assumed_Boundary`, `Building3` → `Building_3`. The
same rule pack handles both schemes without doubling rules.

## Layer-name → ontology

Regex is matched case-insensitively against the (tokenized) layer name.
First match wins; all matches are recorded in `ontology_log` for
`cadq explain`.

### Survey / topography

| Pattern (regex)                                          | Ontology type                       | Confidence |
| -------------------------------------------------------- | ----------------------------------- | ---------- |
| `(^|[-_])(spot|sl|rl|levels?)([-_]|$)`                   | `survey.spot_elevation`             | 0.85       |
| `(^|[-_])contour[-_]?(major|index)([-_]|$)`              | `survey.contour.major`              | 0.90       |
| `(^|[-_])contour([-_]|$)`                                | `survey.contour.minor`              | 0.85       |
| `(^|[-_])(tin|dem|surface)([-_]|$)`                      | `survey.surface`                    | 0.70       |
| `(^|[-_])(bank|embankment|topbank|toebank)([-_]|$)`      | `survey.bank`                       | 0.70       |

### Site

| Pattern (regex)                                          | Ontology type | Confidence |
| -------------------------------------------------------- | ------------- | ---------- |
| `(^|[-_])(site[-_]?bdy|boundary|title[-_]?line|assumed[-_]?boundary|red[-_]?line)([-_]|$)` | `site.boundary` | 0.90 |

Matches `SiteBdy`, `AssumedBoundary`, `TitleLine`, `RedLine` and the
dash-separated equivalents.

### Buildings

| Pattern (regex)                                          | Ontology type        | Confidence |
| -------------------------------------------------------- | -------------------- | ---------- |
| `(^|[-_])(bldg|building|footprint|outline)([-_]|$)`      | `building.footprint` | 0.80       |
| `(^|[-_])(roof|ridge)([-_]|$)`                           | `building.roof`      | 0.70       |
| `(^|[-_])(wall|walls)([-_]|$)`                           | `building.wall`      | 0.65       |
| `(^|[-_])(door|window|opening)([-_]|$)`                  | `building.opening`   | 0.70       |

### Landscape — softscape (rule order matters)

| Pattern (regex)                                          | Ontology type                  | Confidence |
| -------------------------------------------------------- | ------------------------------ | ---------- |
| `(^|[-_])(lawn|grass|turf)([-_]|$)`                      | `landscape.softscape.lawn`     | 0.90       |
| `(^|[-_])(tree|trees|treespread|treetrunk|canopy|crown)([-_]|$)` | `landscape.softscape.tree` | 0.85   |
| `(^|[-_])(hedge|hedges|hedging)([-_]|$)`                 | `landscape.softscape.hedge`    | 0.85       |
| `(^|[-_])(shrub|shrubs|bush|bushes)([-_]|$)`             | `landscape.softscape.shrub`    | 0.80       |
| `(^|[-_])(plnt|planting|bed|garden)([-_]|$)`             | `landscape.softscape.planting` | 0.80       |

> Tree/canopy must come **before** the generic planting rule, otherwise
> a layer like `L-PLNT-CANOPY` would classify as planting rather than
> as a tree.

### Landscape — hardscape

| Pattern (regex)                                          | Ontology type                       | Confidence |
| -------------------------------------------------------- | ----------------------------------- | ---------- |
| `(^|[-_])(driveway|drive)([-_]|$)`                       | `landscape.hardscape.driveway`      | 0.90       |
| `(^|[-_])(path|footpath|walk|paving|paved)([-_]|$)`      | `landscape.hardscape.path`          | 0.80       |
| `(^|[-_])(patio|terrace|deck)([-_]|$)`                   | `landscape.hardscape.patio`         | 0.80       |
| `(^|[-_])(road|carriageway|kerb|curb)([-_]|$)`           | `landscape.hardscape.road`          | 0.75       |
| `(^|[-_])(step|steps|stair|stairs)([-_]|$)`              | `landscape.hardscape.step`          | 0.75       |
| `(^|[-_])(fence|fencing|railing|railings)([-_]|$)`       | `landscape.hardscape.fence`         | 0.80       |

### Water

| Pattern (regex)                                          | Ontology type      | Confidence |
| -------------------------------------------------------- | ------------------ | ---------- |
| `(^|[-_])(pond|lake|water|pool)([-_]|$)`                 | `landscape.water`  | 0.80       |

### Services

| Pattern (regex)                                          | Ontology type                  | Confidence |
| -------------------------------------------------------- | ------------------------------ | ---------- |
| `(^|[-_])(drain|gully|sw|fw|ic|mh|manhole)([-_]|$)`      | `services.drainage`            | 0.70       |
| `(^|[-_])(street[-_]?furniture|streetfurniture|sf|bench|bin|bollard|signpost|lamp|lighting)([-_]|$)` | `services.street_furniture` | 0.70 |

### Annotations

| Pattern (regex)                                          | Ontology type        | Confidence |
| -------------------------------------------------------- | -------------------- | ---------- |
| `(^|[-_])(text|anno|annotation|label|dim|dimension|legend|format)([-_]|$)` | `annotation.text` | 0.60 |
| `(^|[-_])(grid|axis|axes|control|north|viewport)([-_]|$)`| `annotation.grid`    | 0.70       |

## Block-name → ontology (point features)

Block inserts whose block name matches one of these become point
features in the ontology. After classification, the **tree
de-duplication pass** collapses trunk inserts that fall inside a canopy
polygon, so the resulting feature count is per-tree, not per-symbol.

| Pattern (regex)                                          | Ontology type                     | Confidence |
| -------------------------------------------------------- | --------------------------------- | ---------- |
| `(^|[-_])(tree|trees|treespread|treetrunk|conifer|canopy|crown)([-_]|$)` | `landscape.softscape.tree` | 0.85 |
| `(^|[-_])(shrub|bush)([-_]|$)`                           | `landscape.softscape.shrub`       | 0.80       |
| `(^|[-_])(hedge)([-_]|$)`                                | `landscape.softscape.hedge`       | 0.85       |
| `(^|[-_])(mh|manhole|ic|inspection)([-_]|$)`             | `services.drainage.manhole`       | 0.85       |
| `(^|[-_])(gully|drain)([-_]|$)`                          | `services.drainage.gully`         | 0.80       |
| `(^|[-_])(spot|level|rl|ffl)([-_]|$)`                    | `survey.spot_elevation`           | 0.85       |
| `(^|[-_])(north|n[-_]?arrow)([-_]|$)`                    | `annotation.north_arrow`          | 0.95       |
| `(^|[-_])(scale[-_]?bar|scalebar)([-_]|$)`               | `annotation.scale_bar`            | 0.95       |
| `(^|[-_])(bench|bin|bollard|post|lamp|signpost)([-_]|$)` | `services.street_furniture`       | 0.70       |

## Hatch pattern hints (secondary signal)

| Pattern name | Ontology hint                  |
| ------------ | ------------------------------ |
| `GRASS`      | `landscape.softscape.lawn`     |
| `EARTH`      | `landscape.softscape.planting` |
| `GRAVEL`     | `landscape.hardscape.path`     |
| `AR-CONC`    | `landscape.hardscape.driveway` |
| `AR-SAND`    | `landscape.hardscape.path`     |
| `AR-BRSTD`   | `landscape.hardscape.patio`    |
| `WATER`      | `landscape.water`              |

When layer ontology and hatch hint agree, confidence is boosted by +0.10.

## How to override per project

```yaml
# my-rules.yaml
version: 1
rules:
  - pattern: '(^|[-_])(LAWN|GRS)\d*([-_]|$)'
    ontology: landscape.softscape.lawn
    confidence: 0.95
hatch_hints:
  GRASS-PRO: landscape.softscape.lawn
block_rules:
  - pattern: '^OAK[-_]'
    ontology: landscape.softscape.tree
    confidence: 0.95
```

Run with: `cadq ingest site.dxf --rules my-rules.yaml`.
