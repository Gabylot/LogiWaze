"""
Street geometry from the pak's blueprint placements: town sidewalks and bridges.

WHY THIS EXISTS
---------------
`load_splines()` reads ONLY the `splines` key of the exporter output, so two
whole categories of drivable surface are invisible to the road network:

    bridges        477 placements, 47 of 53 hexes
    TownSidewalk*  6,825 placements, 49 of 53 hexes

That is why coverage was uneven and why towns sat stranded from roads that were
plainly visible on the map.  Both live in `blueprints`/`symbols`, which the
loader never opens.  Neither is a spline: both are placed as a 9-element
transform, so they need their own reader.

THE TRANSFORM
-------------
    [x, y, z, sx, sy, sz, roll, yaw, pitch]   UE centimetres and degrees

  x,y   position, UE cm, the same frame as spline knots
  sx    scale on the mesh's LENGTH axis -- the length multiplier
  yaw   heading in degrees; sidewalks sit near multiples of 90, i.e. a grid

sx is almost always +-1.0 (a mirror, not a resize) but is fractional on some
pieces (0.74, 1.15), so it genuinely modulates length and must be used rather
than assumed to be 1.

NATIVE LENGTHS ARE MEASURED, NOT GUESSED
----------------------------------------
The exporter does not carry mesh dimensions, so each piece's length has to come
from somewhere.  It is measured from the data: pieces of one mesh sharing a
bearing are spaced one native length apart, so the modal spacing along a shared
bearing recovers it.  Hard-coding metres per piece type would be a guess; this
is re-derived on every run, so a pak change shows up instead of silently
producing wrong geometry.
"""
import argparse
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import grid
import roads_from_pak as rfp

HERE = os.path.dirname(os.path.abspath(__file__))
PAK_JSON = rfp.PAK_JSON

# Matched on the whole family rather than a hand-kept list, so a new sidewalk
# variant is picked up automatically instead of silently dropped the way the
# splines were.
STREET_RE = re.compile(r"^TownSidewalk", re.I)
BRIDGE_RE = re.compile(r"bridge|drawbridge|pontoon", re.I)


def blueprint_transform(entry):
    """Extract (x, y, sx, yaw) from a placement.

    Prefers the component transform under the mesh's own key and falls back to
    `_self`.  Both carry the same 9 numbers in the observed data; the mesh's own
    is the more specific source.
    """
    if not isinstance(entry, dict):
        return None
    for key, val in entry.items():
        if key == "_self":
            continue
        if (isinstance(val, list) and len(val) >= 8
                and isinstance(val[0], (int, float))):
            return tuple(val[:8])
    s = entry.get("_self")
    if isinstance(s, list) and len(s) >= 8:
        return tuple(s[:8])
    return None


def collect_pieces(region):
    """Return (streets, bridges) for one hex as dicts of raw UE values."""
    path = os.path.join(PAK_JSON, region + ".json")
    if not os.path.isfile(path):
        return [], []
    data = json.load(open(path, encoding="utf-8"))
    streets, bridges = [], []
    for key in ("blueprints", "symbols", "groups"):
        for name, entries in (data.get(key) or {}).items():
            if not hasattr(entries, "__len__"):
                continue
            want_street = bool(STREET_RE.match(name))
            want_bridge = bool(BRIDGE_RE.search(name))
            if not (want_street or want_bridge):
                continue
            for e in entries:
                t = blueprint_transform(e)
                if t is None:
                    continue
                rec = {"mesh": name, "key": key, "x": t[0], "y": t[1],
                       "sx": t[3], "yaw": t[7]}
                (streets if want_street else bridges).append(rec)
    return streets, bridges


def piece_segments(region, pieces, native_cm=None, default_cm=1000.0):
    """Endpoints for each piece, in world units.

    `native_cm` maps mesh name -> that mesh's piece length.  A piece's own
    sx scales its length, and a negative sx is a mirror (the yaw already
    encodes the heading, so the sign only flips which end is which -- the
    segment is the same either way).
    """
    ox, oy = grid.hex_origin(region)
    cx, cy = ox + grid.W / 2.0, oy - grid.K / 2.0
    s = rfp.cm_to_world_scale()
    out = []
    for p in pieces:
        L = (native_cm or {}).get(p["mesh"], default_cm) * (abs(p["sx"]) or 1.0)
        a = math.radians(p["yaw"])
        hx, hy = math.cos(a) * L / 2.0, math.sin(a) * L / 2.0
        out.append(((cx + (p["x"] - hx) * s, cy - (p["y"] - hy) * s),
                    (cx + (p["x"] + hx) * s, cy - (p["y"] + hy) * s)))
    return out


def measure_native_cm_all(regions, verbose=False):
    """Native length per sidewalk mesh, measured across ALL hexes.

    Two defects in the previous estimator, both of which fragment the network:

    1. It was POOLED over every piece in a hex, so a single length was applied
       to every mesh.  But the meshes genuinely differ: TownSidewalk01/03/04/05
       measured 206-2127 cm within one hex.  Pooling averaged distinct lengths
       together, so most pieces were drawn at the wrong length and their
       endpoints missed the pieces they were supposed to meet.

    2. It was computed PER HEX.  A hex like PariPeakHex has 20 pieces spread
       over 7 meshes, so each mesh's estimate came from 1-6 samples and was
       noise.  Pooling per mesh across the whole 53-hex export gives hundreds
       of samples for the common meshes.

    The estimator itself: within one mesh, pieces tile, so the distance between
    two piece centres is a near-integer multiple of the native length L.  Every
    candidate L is scored by how many observed gaps it explains.  Among
    candidates that explain equally well, the one whose multiples are SMALLEST
    is chosen -- because if the true L is 1000 cm then 500 cm also explains
    every gap (as multiples 2, 4, 6) and 200 cm would too (5, 10, 15), so
    "best score" alone is degenerate and must be broken by preferring tight
    multiples.

    Only pieces with |sx| == 1 are used: a piece with sx 0.74 is shorter than
    native, and including it corrupts the centre-to-centre spacing that the
    whole method depends on.
    """
    by_mesh = defaultdict(list)
    for region in regions:
        streets, _ = collect_pieces(region)
        for p in streets:
            if abs(abs(p["sx"]) - 1.0) < 1e-6:
                by_mesh[p["mesh"]].append((p["x"], p["y"]))

    out = {}
    for mesh, pts in by_mesh.items():
        n = len(pts)
        if n < 4:
            continue
        gaps = []
        for i in range(n):
            best = None
            for j in range(i + 1, n):
                d = math.hypot(pts[j][0] - pts[i][0], pts[j][1] - pts[i][1])
                if best is None or d < best:
                    best = d
            if best:
                gaps.append(best)
        if len(gaps) < 3:
            continue
        gmin = min(gaps)
        if gmin < 1.0:
            continue
        cands = set()
        for g in gaps:
            for m in (1, 2, 3, 4, 5, 6):
                c = g / m
                if c >= 20.0:            # ignore absurdly fine lattices
                    cands.add(round(c))
        best = None
        for c in cands:
            L = float(c)
            tol = max(0.06 * L, 15.0)
            hit = mult = 0
            for g in gaps:
                m = g / L
                mi = round(m)
                if 1 <= mi <= 8 and abs(m - mi) * L <= tol:
                    hit += 1
                    mult += mi
            if not hit:
                continue
            score = hit / len(gaps)
            # tie-break: same explanatory power, prefer the tightest multiples
            key = (-score, mult / hit)
            if best is None or key < best[0]:
                best = (key, L, score, hit, len(gaps))
        if best is not None and best[2] >= 0.5:
            out[mesh] = best[1]
            if verbose:
                print("    %-34s L=%6.0f cm  explains %d/%d gaps (%.0f%%)"
                      % (mesh[:34], best[1], best[3], best[4],
                         100 * best[2]))
    return out



def chain_segments(segs, tol):
    """Join segment endpoints within `tol` and return merged polylines."""
    parent = list(range(len(segs)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    cell = max(tol, 1e-6)
    buckets = defaultdict(list)
    for i, (a, b) in enumerate(segs):
        for p in (a, b):
            buckets[(int(math.floor(p[0] / cell)),
                    int(math.floor(p[1] / cell)))].append((i, p))
    for (cx, cy), members in buckets.items():
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i, pi in members:
                    for j, pj in buckets.get((cx + dx, cy + dy), ()):
                        if i == j:
                            continue
                        if math.hypot(pi[0] - pj[0], pi[1] - pj[1]) <= tol:
                            union(i, j)
    groups = defaultdict(list)
    for i in range(len(segs)):
        groups[find(i)].append(i)
    out = []
    for members in groups.values():
        pts = []
        for i in members:
            a, b = segs[i]
            if not pts:
                pts.append(a)
            elif math.hypot(a[0] - pts[-1][0], a[1] - pts[-1][1]) > tol:
                pts.append(a)          # branch: start a fresh run
            pts.append(b)
        # drop zero-length repeats
        ded = [pts[0]] if pts else []
        for q in pts[1:]:
            if math.hypot(q[0] - ded[-1][0], q[1] - ded[-1][1]) > 1e-12:
                ded.append(q)
        if len(ded) >= 2:
            out.append(ded)
    return out


def hex_features_streets(region, join_world=0.03, native=None):
    """Street polylines for one hex, in world units.

    `native` is the per-mesh length table from measure_native_cm_all(), measured
    across the whole export.  It is a PARAMETER rather than being recomputed per
    hex because a per-hex estimate is built from too few pieces to be reliable
    (PariPeakHex: 20 pieces over 7 meshes) and because the table must be
    identical for every hex or a street at a hex edge would not meet its
    neighbour across the border.
    """
    streets, _bridges = collect_pieces(region)
    if not streets:
        return []
    if native is None:
        # Fallback for single-hex use only.  build_network() always passes the
        # global table; a per-hex value here is only a convenience for tooling.
        native = measure_native_cm_all([region])
    segs = piece_segments(region, streets, native, default_cm=1000.0)
    return chain_segments(segs, join_world)


def build_network(out_path, hexes, streets=True, tier_streets=2,
                  join_world=0.03, verbose=False):
    """Merge spline roads + street pieces into one FeatureCollection.

    Streets are emitted at `tier_streets` (default 2 = dirt).  They are a
    distinct surface in the game, but the app's tier vocabulary is 1/2/3 and a
    street is drivable by anything, so tier 2 keeps them routable without
    pretending to be main roads.  A `source` property records the distinction so
    the choice is visible downstream rather than hidden here.

    The per-mesh length table is measured ONCE across every hex being built and
    shared, for the reasons in measure_native_cm_all().  Measuring per hex both
    under-samples sparse hexes and gives neighbouring hexes different lengths
    for the same mesh, which would leave street ends unable to meet across a
    border -- the exact failure that fragmented the graph before.
    """
    grid.load_offsets(os.path.join(HERE, "scripts",
                                   "export_major_locations.sh"))
    feats = []
    n_road = n_street = 0
    native = None
    if streets:
        if verbose:
            print("measuring per-mesh street piece lengths across %d hexes..."
                  % len(hexes))
        native = measure_native_cm_all(hexes, verbose=verbose)
        if verbose:
            print("  %d meshes measured" % len(native))
        missing = set()
        for region in hexes:
            streets_h, _ = collect_pieces(region)
            for p in streets_h:
                if p["mesh"] not in native:
                    missing.add(p["mesh"])
        if verbose and missing:
            print("  WARNING: %d meshes have no measured length and will fall "
                  "back to 1000 cm: %s" % (len(missing),
                                           ", ".join(sorted(missing)[:6])))


    def merc(line):
        """world -> EPSG:3857, the frame every other stage of the chain uses.

        The old pak_roads.geojson is mercator, scripts/roads.js consumes
        mercator, and Hermes embeds mercator.  Emitting world units here put
        every town ~5 km from its own road and made the acceptance harness
        report nonsense, so the conversion belongs here rather than being left
        to a downstream step that has to guess.
        """
        return [list(rfp.world_to_merc(p[0], p[1])) for p in line]

    for region in hexes:
        for tier, line in rfp.hex_lines(region):
            if len(line) >= 2:
                feats.append({"type": "Feature",
                              "properties": {"region": region, "tier": tier,
                                             "source": "road"},
                              "geometry": {"type": "LineString",
                                           "coordinates": merc(line)}})
                n_road += 1
        if not streets:
            continue
        for line in hex_features_streets(region, join_world=join_world,
                                         native=native):
            if len(line) >= 2:
                feats.append({"type": "Feature",
                              "properties": {"region": region,
                                             "tier": tier_streets,
                                             "source": "street"},
                              "geometry": {"type": "LineString",
                                           "coordinates": merc(line)}})
                n_street += 1
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f)
    return n_road, n_street, len(feats)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("hexes", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--out", required=True,
                    help="output path; the build is staged by design, never "
                         "written over a deployed file")
    ap.add_argument("--no-streets", action="store_true",
                    help="spline roads only, for A/B against the old build")
    ap.add_argument("--street-tier", type=int, default=2)
    ap.add_argument("--street-join", type=float, default=0.03,
                    help="street endpoint join tolerance, world units "
                         "(0.03 is about 2.6 m)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print the measured per-mesh lengths")

    a = ap.parse_args()
    # The offset table must be loaded BEFORE the --all list is built.  Doing it
    # only inside build_network() left grid.OFFSETS empty here, so --all matched
    # nothing and the run exited with "give hex names or --all".
    grid.load_offsets(os.path.join(HERE, "scripts",
                                   "export_major_locations.sh"))
    if a.all:
        hexes = sorted(r for r in grid.OFFSETS
                       if os.path.isfile(os.path.join(PAK_JSON, r + ".json")))
    else:
        hexes = a.hexes
    if not hexes:
        raise SystemExit("give hex names or --all")
    if os.path.abspath(a.out) == os.path.abspath("/tmp/rx/snapped.geojson"):
        raise SystemExit("refusing to write the deployed file")
    n_road, n_street, n = build_network(a.out, hexes,
                                        streets=not a.no_streets,
                                        tier_streets=a.street_tier,
                                        join_world=a.street_join,
                                        verbose=a.verbose)

    print("wrote %s" % a.out)
    print("  %d hexes, %d road features, %d street features, %d total"
          % (len(hexes), n_road, n_street, n))
    return 0


if __name__ == "__main__":
    sys.exit(main())


