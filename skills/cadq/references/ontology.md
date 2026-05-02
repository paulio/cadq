# Ontology reference

Default rule pack used by `cadq` when classifying layers, hatches, and block
inserts. Override per-project by passing `cadq ingest --rules my.yaml`.

The dotted ontology types are designed for prefix queries — e.g.
`features_list ontology_prefix="landscape.softscape"` returns all soft
landscape features regardless of subtype.

## Layer-name → ontology

Regex is matched case-insensitively against the layer name. First match
wins; all matches are recorded in `ontology_log` for `cadq explain`.

| Pattern (regex)                                          | Ontology type                       | Confidence |
| -------------------------------------------------------- | ----------------------------------- | ---------- |
| `(^|[-_])(spot|sl|rl|levels?)([-_]|$)`                   | `survey.spot_elevation`             | 0.85       |
| `(^|[-_])contour[-_]?(major|index)([-_]|$)`              | `survey.contour.major`              | 0.90       |
| `(^|[-_])contour([-_]|$)`                                | `survey.contour.minor`              | 0.85       |
| `(^|[-_])(tin|dem|surface)([-_]|$)`                      | `survey.surface`                    | 0.70       |
| `(^|[-_])(site[-_]?bdy|boundary|title[-_]?line)([-_]|$)` | `site.boundary`                     | 0.90       |
| `(^|[-_])(bldg|building|footprint|outline)([-_]|$)`      | `building.footprint`                | 0.80       |
| `(^|[-_])(roof|ridge)([-_]|$)`                           | `building.roof`                     | 0.70       |
| `(^|[-_])(lawn|grass|turf)([-_]|$)`                      | `landscape.softscape.lawn`          | 0.90       |
| `(^|[-_])(plnt|planting|bed|shrub|garden)([-_]|$)`       | `landscape.softscape.planting`      | 0.80       |
| `(^|[-_])(tree|trees)([-_]|$)`                           | `landscape.softscape.tree`          | 0.85       |
| `(^|[-_])(driveway|drive)([-_]|$)`                       | `landscape.hardscape.driveway`      | 0.90       |
| `(^|[-_])(path|footpath|walk|paving|paved)([-_]|$)`      | `landscape.hardscape.path`          | 0.80       |
| `(^|[-_])(patio|terrace|deck)([-_]|$)`                   | `landscape.hardscape.patio`         | 0.80       |
| `(^|[-_])(road|carriageway|kerb|curb)([-_]|$)`           | `landscape.hardscape.road`          | 0.75       |
| `(^|[-_])(pond|lake|water|pool)([-_]|$)`                 | `landscape.water`                   | 0.80       |
| `(^|[-_])(drain|gully|sw|fw|ic|mh|manhole)([-_]|$)`      | `services.drainage`                 | 0.70       |
| `(^|[-_])(text|anno|annotation|label|dim|dimension)([-_]|$)` | `annotation.text`               | 0.60       |
| `(^|[-_])(grid|axis|axes)([-_]|$)`                       | `annotation.grid`                   | 0.70       |

## Block-name → ontology (point features)

| Pattern (regex)                                | Ontology type                     | Confidence |
| ---------------------------------------------- | --------------------------------- | ---------- |
| `(^|[-_])tree(s)?([-_]|$)`                     | `landscape.softscape.tree`        | 0.85       |
| `(^|[-_])(shrub|bush)([-_]|$)`                 | `landscape.softscape.shrub`       | 0.80       |
| `(^|[-_])(mh|manhole|ic|inspection)([-_]|$)`   | `services.drainage.manhole`       | 0.85       |
| `(^|[-_])(gully|drain)([-_]|$)`                | `services.drainage.gully`         | 0.80       |
| `(^|[-_])(spot|level|rl|ffl)([-_]|$)`          | `survey.spot_elevation`           | 0.85       |
| `(^|[-_])(north|n[-_]?arrow)([-_]|$)`          | `annotation.north_arrow`          | 0.95       |
| `(^|[-_])(scale[-_]?bar|scalebar)([-_]|$)`     | `annotation.scale_bar`            | 0.95       |

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
