#!/usr/bin/env python3
"""merge_roads.py -- road network from pak splines plus map-image OCR.

WHY
---
Two extraction routes that fail in opposite ways, neither usable alone:

  pak splines   exact design geometry and a true `tier` taken from the mesh
                name, but the network fragments badly: junctions and hex
                borders are not joined, and road inside towns is not a road
                spline at all (it is TownSidewalk*/TownW* geometry, which
                lives under `symbols`/`blueprints`, so classify() never sees it)
  map-image OCR sees the whole painted network, so it covers ground the
                splines miss, but it is pixel-derived

The pak data is authoritative; OCR supplies coverage and connectivity that
the splines lack.  Neither source is hand-edited, so this re-runs when the
game updates.

FRAMES
------
`extract_routes.py` emits the app's *world* frame directly.  The deployed
road_source.geojson is EPSG:3857 metres.  graphkit.w2m is the exact inverse of
the m2w that graphkit.load applies on read, so the conversion is one call and
NOT a fitted affine.

An affine was tried first, on the belief that the two frames were related only
by per-hex placement.  That is wrong, and the mistake was quiet: gk.load
applies m2w to whatever it reads, so feeding it world-frame coordinates runs
them through SCALE=6.4e-6 and collapses the entire OCR network -- every
endpoint lands within 0.002 units of (128, -128).  The result still parsed and
still produced plausible-looking counts, which is exactly how a frame bug
survives.  Any output of this script must be checked by round-tripping one
coordinate: w2m then m2w must return the input exactly.
"""
import argparse
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import graphkit as gk


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["features"]


def lines_of(feat):
    g = feat["geometry"]
    c = g["coordinates"]
    return c if g["type"] == "MultiLineString" else [c]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pak", required=True,
                    help="app-frame (EPSG:3857) geojson from the pak")
    ap.add_argument("--ocr", required=True,
                    help="extract_routes.py output, in world units")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    pak, ocr = load(a.pak), load(a.ocr)

    # Fail loudly rather than emit a collapsed network.
    probe = lines_of(ocr[0])[0][0]
    back = gk.m2w(gk.w2m(*probe))
    if max(abs(back[0] - probe[0]), abs(back[1] - probe[1])) > 1e-6:
        raise SystemExit("world->3857 round trip failed: %r -> %r" % (probe, back))

    out = [{"type": "Feature",
            "properties": dict(f["properties"], source="pak"),
            "geometry": f["geometry"]} for f in pak]
    npak = len(out)

    nocr = 0
    for f in ocr:
        region = f["properties"].get("region", "?")
        g = {"type": "LineString",
             "coordinates": [list(gk.w2m(x, y)) for x, y in lines_of(f)[0]]}
        out.append({"type": "Feature",
                    "properties": {"region": region,
                                   "tier": f["properties"].get("tier", 1),
                                   "source": "ocr"},
                    "geometry": g})
        nocr += 1

    fc = {"type": "FeatureCollection", "name": "road_source",
          "crs": {"type": "name",
                  "properties": {"name": "urn:ogc:def:crs:EPSG::3857"}},
          "features": out}
    with open(a.out, "w") as fh:
        json.dump(fc, fh)
    hexes = {f["properties"]["region"] for f in out}
    sys.stderr.write("wrote %s: %d features (%d pak + %d ocr) across %d hexes\n"
                     % (a.out, len(out), npak, nocr, len(hexes)))


if __name__ == "__main__":
    main()
