"""Count trees in a cadq cache.

Heuristic: a tree is any of:
- INSERT block on a Tree* layer (each block instance = one tree),
- a closed polygon on TreeTrunk or Tree (canopy / trunk outline),
- a CIRCLE on Tree / TreeTrunk.

We count *trunks* preferentially (one trunk = one tree) and fall back to
spread/canopy outlines if no trunks are present.  Text labels on TreeText
are reported separately because the same tree often has multiple labels.
"""

from __future__ import annotations

import sys
from pathlib import Path

from cadq.store import connect


def main() -> None:
    cache = Path(sys.argv[1])
    con = connect(cache, read_only=True)
    try:
        print("=== entity counts on Tree* layers ===")
        rows = con.execute(
            """
            SELECT layer, kind, count(*) AS n,
                   sum(CASE WHEN is_closed THEN 1 ELSE 0 END) AS closed
            FROM entities
            WHERE layer LIKE 'Tree%'
            GROUP BY layer, kind
            ORDER BY layer, kind
            """
        ).fetchall()
        print(f"  {'layer':<14} {'kind':<12} {'n':>5} {'closed':>7}")
        for layer, kind, n, closed in rows:
            print(f"  {layer:<14} {kind:<12} {n:>5} {closed or 0:>7}")

        print()
        print("=== block inserts on Tree* layers ===")
        irows = con.execute(
            """
            SELECT layer, block_name, count(*) AS n
            FROM inserts
            WHERE layer LIKE 'Tree%'
            GROUP BY layer, block_name
            ORDER BY n DESC
            """
        ).fetchall()
        if not irows:
            print("  (none)")
        for layer, blk, n in irows:
            print(f"  {layer:<14} block={blk!s:<25} count={n}")

        print()
        # Headline counts
        trunks_circle = con.execute(
            "SELECT count(*) FROM entities WHERE layer='TreeTrunk' AND kind='CIRCLE'"
        ).fetchone()[0]
        trunks_closed = con.execute(
            "SELECT count(*) FROM entities WHERE layer='TreeTrunk' AND is_closed=TRUE AND kind!='CIRCLE'"
        ).fetchone()[0]
        spread_closed = con.execute(
            "SELECT count(*) FROM entities WHERE layer='TreeSpread' AND is_closed=TRUE"
        ).fetchone()[0]
        tree_inserts = con.execute(
            "SELECT count(*) FROM inserts WHERE layer LIKE 'Tree%'"
        ).fetchone()[0]
        treetext = con.execute(
            "SELECT count(*) FROM texts WHERE layer = 'TreeText'"
        ).fetchone()[0]

        print("=== headline ===")
        print(f"  TreeTrunk CIRCLE entities  : {trunks_circle}")
        print(f"  TreeTrunk other closed     : {trunks_closed}")
        print(f"  TreeSpread closed canopies : {spread_closed}")
        print(f"  Block INSERTs on Tree*     : {tree_inserts}")
        print(f"  TreeText labels            : {treetext}")

        # Best-guess single number, in priority order:
        if tree_inserts:
            best = tree_inserts
            via = "block inserts on Tree* layers"
        elif trunks_circle:
            best = trunks_circle
            via = "TreeTrunk CIRCLE entities"
        elif trunks_closed:
            best = trunks_closed
            via = "TreeTrunk closed polygons"
        elif spread_closed:
            best = spread_closed
            via = "TreeSpread canopy outlines"
        else:
            best = 0
            via = "no tree entities found"
        print()
        print(f"  >>> best estimate: {best} trees  ({via})")
    finally:
        con.close()


if __name__ == "__main__":
    main()
