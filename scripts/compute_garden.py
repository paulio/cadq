"""Compute the implicit garden polygon for the Vicarage Lane survey.

The drawing has no explicit garden layer.  Strategy:
  1. Polygonize the AssumedBoundary line network -> site polygon (largest).
  2. Subtract Building, Footpath, Step, Wall (closed), and TreeSpread
     polygons.  What's left is the garden / open ground.
  3. Also report the hedge polygons (sometimes those *are* garden beds).
"""

from __future__ import annotations

import sys
from pathlib import Path

from shapely import wkb as shp_wkb
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import polygonize, unary_union

from cadq.store import connect


def _load_polys(con, layer: str) -> list[Polygon]:
    rows = con.execute(
        """
        SELECT wkb FROM entities
        WHERE layer = ? AND kind IN ('LWPOLYLINE','POLYLINE','HATCH')
          AND is_closed = TRUE
        """,
        [layer],
    ).fetchall()
    out: list[Polygon] = []
    for (blob,) in rows:
        try:
            g = shp_wkb.loads(bytes(blob))
        except Exception:
            continue
        if isinstance(g, Polygon) and g.is_valid and g.area > 0:
            out.append(g)
    return out


def _open_lines(con, layer: str):
    rows = con.execute(
        """
        SELECT wkb FROM entities
        WHERE layer = ? AND kind IN ('LINE','LWPOLYLINE','POLYLINE')
          AND is_closed = FALSE
        """,
        [layer],
    ).fetchall()
    geoms = []
    for (blob,) in rows:
        try:
            geoms.append(shp_wkb.loads(bytes(blob)))
        except Exception:
            continue
    return geoms


def main() -> None:
    cache = Path(sys.argv[1])
    con = connect(cache, read_only=True)
    try:
        # 1. Reconstruct site boundary by polygonizing AssumedBoundary.
        bnd_lines = _open_lines(con, "AssumedBoundary")
        if not bnd_lines:
            print("No AssumedBoundary lines found; cannot derive site polygon.")
            return
        merged = unary_union(bnd_lines)
        polys = list(polygonize([merged]))
        if not polys:
            print(
                "AssumedBoundary lines did not form a closed ring "
                "(open boundary). Falling back to convex hull."
            )
            site = unary_union(bnd_lines).convex_hull
            site_method = "convex hull of AssumedBoundary"
        else:
            site = max(polys, key=lambda p: p.area)
            site_method = (
                f"polygonize of AssumedBoundary ({len(polys)} ring(s) found)"
            )
        print(f"Site polygon: area={site.area:,.2f} sq.units"
              f" via {site_method}")

        # 2. Subtract the things that aren't garden.
        subtractions: dict[str, list[Polygon]] = {
            "Building": _load_polys(con, "Building"),
            "Footpath": _load_polys(con, "Footpath"),
            "Step": _load_polys(con, "Step"),
            "Wall": _load_polys(con, "Wall"),
            # Service boxes (manhole covers etc.) -- usually tiny but include
            # for completeness:
            "Service": _load_polys(con, "Service"),
            # TreeSpread = canopy outlines.  Conventional landscape practice
            # would NOT subtract these from lawn (you can have lawn under a
            # tree); we therefore keep them out of the subtraction by
            # default and report them separately.
        }
        garden = site
        breakdown: list[tuple[str, float, float]] = []  # (name, area, after)
        for name, polys in subtractions.items():
            if not polys:
                breakdown.append((name, 0.0, garden.area))
                continue
            sub = unary_union(polys)
            sub_in_site = sub.intersection(site)
            taken = sub_in_site.area
            garden = garden.difference(sub_in_site)
            breakdown.append((name, taken, garden.area))

        print()
        print(f"{'Subtraction':<12} {'this layer':>14} {'garden after':>14}")
        for name, taken, after in breakdown:
            print(f"  {name:<10} {taken:>14,.2f} {after:>14,.2f}")

        # 3. Hedges: report the area of hedge outlines as a separate figure
        #    (some clients count hedges as garden, some don't).
        hedge = _load_polys(con, "Hedge")
        hedge_area = unary_union(hedge).area if hedge else 0.0
        treespread = _load_polys(con, "TreeSpread")
        canopy_area = unary_union(treespread).area if treespread else 0.0

        print()
        print("=== headline ===")
        print(f"Site (within AssumedBoundary):       {site.area:>12,.2f} sq.units")
        print(f"Garden (open ground after sub):      {garden.area:>12,.2f} sq.units")
        print(f"Hedge outlines (informational):      {hedge_area:>12,.2f} sq.units")
        print(f"Tree canopy outlines (informational):{canopy_area:>12,.2f} sq.units")
        print()
        print(
            "NB: drawing reports unitless. Site survey extents span "
            f"{530.7 - 476.3:.1f} x {540.1 - 489.6:.1f} drawing units; "
            "for a typical UK survey of this footprint the units are metres "
            "(spot levels are ~0.5 with two-decimal precision and easting/"
            "northing values are ~500 -- consistent with metres on a local "
            "survey grid). Treat the area numbers as m^2 unless your office "
            "convention differs."
        )

    finally:
        con.close()


if __name__ == "__main__":
    main()
