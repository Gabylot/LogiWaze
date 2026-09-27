"""
Per-hex length coverage: pak network vs the hand trace, all shared hexes.

WHY
---
Aggregate numbers hid this.  The network is 96% of the hand trace's length overall,
which looks fine, but coverage is wildly uneven per hex -- some hexes are 99.7% and
others are 85%.  A hex-level average cannot see that, so this reports per hex and
ranks them.

THE MEASUREMENT THAT MATTERS MOST
---------------------------------
For each hex this also compares the pak's length against the length of the RAW
spline entries it was built from (`raw_len` vs `emitted_len`).  Those two must be
equal.  If they are, the extractor consumed every segment it was given and the
shortfall is in the pak export itself -- i.e. architectural, not a filter bug.
If they differ, a filter is dropping road and that is fixable here.

Region filtering is valid: only 24 of 3723 features (0.6%) have a vertex outside
their labelled hex, so comparing per-region lengths is sound.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import grid
import roads_from_pak as rfp
import pak_acceptance as pa

HERE = os.path.dirname(os.path.abspath(__file__))


def polylen(pts):
    return sum(((pts[i + 1][0] - pts[i][0]) ** 2 +
                (pts[i + 1][1] - pts[i][1]) ** 2) ** 0.5
               for i in range(len(pts) - 1))


def hand_by_region():
    """Hand-traced length per region, CONVERTED from EPSG:3857 to world units.

    road_source.geojson is mercator; the pak lengths below are world units.
    Comparing them directly inflates the hand figure by ~1.6e5 and makes every
    hex look like 0.0% coverage -- which is exactly what the first run of this
    script printed.  Lengths are not comparable across frames even though
    positions are.
    """
    import pak_acceptance as _pa          # noqa: F401
    d = json.load(open(os.path.join(HERE, "road_source.geojson"),
                       encoding="utf-8"))
    out = {}
    for f in d["features"]:
        r = f.get("properties", {}).get("region")
        if not r:
            continue
        g = f.get("geometry", {})
        cs = g.get("coordinates") or []
        polys = cs if g.get("type") == "MultiLineString" else [cs]
        tot, n = 0.0, 0
        for line in polys:
            if len(line) >= 2:
                w = [merc_to_world(a, b) for a, b in line]
                tot += polylen(w)
                n += 1
        a = out.setdefault(r, [0.0, 0])
        a[0] += tot
        a[1] += n
    return out


def merc_to_world(mx, my):
    """EPSG:3857 -> world units, the inverse of scripts/roads.js."""
    return (mx * pa.MERC_SCALE + pa.HALF_WORLD,
            my * pa.MERC_SCALE - pa.HALF_WORLD)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=float, default=95.0,
                    help="coverage below this is flagged (default 95%%)")
    ap.add_argument("--worst", type=int, default=15,
                    help="how many worst hexes to list in full")
    a = ap.parse_args()
    grid.load_offsets(os.path.join(HERE, "scripts",
                                   "export_major_locations.sh"))

    hand = hand_by_region()
    s = rfp.cm_to_world_scale()
    rows = []
    for reg in sorted(hand):
        try:
            ents = rfp.load_splines(reg)
        except SystemExit:
            continue
        if not ents:
            continue
        raw = polylen([p for _t, e in ents for p in rfp.segment_points(e)]) * s
        out = rfp.hex_lines(reg)
        em = sum(polylen(p) for _t, p in out)
        hl, hn = hand[reg]
        cov = 100.0 * em / hl if hl else float("nan")
        # Extractor loss: how much of the pak the builder actually emitted.
        keep = 100.0 * em / raw if raw else float("nan")
        rows.append((cov, reg, hl, em, raw, keep, len(ents), len(out), hn))

    rows.sort()
    print("per-hex length coverage, pak vs hand trace  (n=%d shared hexes)\n"
          % len(rows))
    print("%-22s %9s %9s %8s %9s %9s %7s"
          % ("hex", "hand u", "pak u", "cov %", "raw u", "kept %", "segs"))
    print("-" * 84)
    for cov, reg, hl, em, raw, keep, ne, nl, hn in rows:
        flag = "  <-- BELOW %.0f%%" % a.min if cov < a.min else ""
        print("%-22s %9.1f %9.1f %7.1f%% %9.1f %8.1f%% %7d%s"
              % (reg, hl, em, cov, raw, keep, ne, flag))

    ok = [r for r in rows if r[0] >= a.min]
    print("\n  %d of %d hexes at or above %.0f%% coverage; %d below"
          % (len(ok), len(rows), a.min, len(rows) - len(ok)))
    lost = [(100.0 - r[5], r[1]) for r in rows if r[5] < 99.9]
    print("  extractor loss (raw -> emitted) below 0.1%%: %d hexes%s"
          % (len(lost),
             "" if not lost else "  <-- " + ", ".join(n for _d, n in lost)))
    if not lost:
        print("  => the builder emits 100%% of the spline length it is given.")
        print("     Any shortfall vs the hand trace is IN THE PAK EXPORT,")
        print("     not in roads_from_pak.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
