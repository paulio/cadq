"""Ad-hoc inspector for the Vicarage Lane survey to find garden-like layers."""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

from cadq.queries import list_features, list_layers
from cadq.store import connect


def main() -> None:
    cache = Path(sys.argv[1])
    print("=== layers matching garden vocabulary ===")
    pat = re.compile(
        r"garden|lawn|grass|plnt|turf|plant|soft|hard|drive|patio|path|"
        r"veg|hedge|shrub|tree|border|bed|land",
        re.IGNORECASE,
    )
    layers = list_layers(cache)
    for r in layers:
        name = r["name"] or ""
        if pat.search(name):
            print(
                f"  {name:<45} -> ontology={r['ontology']!s:<40} "
                f"conf={r['confidence']}"
            )

    print()
    print("=== ALL layer names (sorted) ===")
    for r in sorted(layers, key=lambda x: (x["name"] or "").lower()):
        flag = " <-- mapped" if r["ontology"] else ""
        print(f"  {r['name']!s:<55}{flag}")

    print()
    print("=== features summary ===")
    feats = list_features(cache)
    by_onto = Counter(f.ontology for f in feats)
    for k, v in sorted(by_onto.items()):
        print(f"  {k:<45} count={v}")
    print(f"  TOTAL features: {len(feats)}")

    print()
    print("=== entity counts by layer (top 25 by area or count) ===")
    con = connect(cache, read_only=True)
    try:
        rows = con.execute(
            """
            SELECT layer,
                   count(*) AS n,
                   sum(CASE WHEN kind IN ('LWPOLYLINE','POLYLINE')
                              AND is_closed THEN 1 ELSE 0 END) AS closed_poly,
                   sum(CASE WHEN kind = 'HATCH' THEN 1 ELSE 0 END) AS hatches
            FROM entities
            GROUP BY layer
            ORDER BY n DESC
            LIMIT 25
            """
        ).fetchall()
        print(f"  {'layer':<45} {'#':>6} {'closed':>7} {'hatch':>6}")
        for layer, n, cp, h in rows:
            print(f"  {layer!s:<45} {n:>6} {cp:>7} {h:>6}")
    finally:
        con.close()

    print()
    print("=== sample of text labels (first 30) ===")
    con = connect(cache, read_only=True)
    try:
        rows = con.execute(
            "SELECT text, layer FROM texts WHERE text IS NOT NULL "
            "AND length(trim(text)) > 0 LIMIT 30"
        ).fetchall()
        for t, l in rows:
            print(f"  [{l!s:<30}] {t!s:<60}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
