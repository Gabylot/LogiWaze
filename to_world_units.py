#!/usr/bin/env python3
"""Convert a road GeoJSON from EPSG:3857 to the app's WORLD units.

scripts/roads.js does, per coordinate, before handing the data to the app:

    v[0] = round((x * (128/20037500) + 128) * 1000) / 1000
    v[1] = round((y * (128/20037500) - 128) * 1000) / 1000

The file the app reads must be in that frame.  A hex is 25.6 world units
across; raw mercator values are ~4e6, so every road spans thousands of hexes
and the loader/ownership/tile passes grind over the whole globe - which is what
made the map hang and burned gigabytes of RAM.  The `crs` member in these
files claims EPSG:3857 either way, so it cannot be trusted to identify the
frame; the coordinate magnitudes can (world: |x| < ~260, mercator: ~1e6-1e7).
"""
import argparse, json, math

SCALE = 128.0 / 20037500.0
HALF = 128.0

ap = argparse.ArgumentParser()
ap.add_argument('--in', dest='src', required=True)
ap.add_argument('--out', dest='dst', required=True)
ap.add_argument('--force', action='store_true',
                help='convert even if the input already looks like world units')
a = ap.parse_args()

d = json.load(open(a.src))


def coords_of(g):
    return [g['coordinates']] if g['type'] == 'LineString' else g['coordinates']


def set_coords(f, parts):
    if f['geometry']['type'] == 'LineString':
        f['geometry']['coordinates'] = parts[0] if parts else []
    else:
        f['geometry']['coordinates'] = parts


maxabs = 0.0
for f in d['features']:
    for c in coords_of(f['geometry']):
        for p in c:
            maxabs = max(maxabs, abs(p[0]), abs(p[1]))

already_world = maxabs < 1000.0
if already_world and not a.force:
    print('to_world_units: input already looks like world units (max |coord| = %.1f) '
          '- nothing to do.  Use --force to convert anyway.' % maxabs)
else:
    for f in d['features']:
        parts = []
        for c in coords_of(f['geometry']):
            parts.append([[round(p[0] * SCALE + HALF, 3), round(p[1] * SCALE - HALF, 3)]
                          for p in c])
        set_coords(f, parts)
    xs = [p[0] for f in d['features'] for c in coords_of(f['geometry']) for p in c]
    ys = [p[1] for f in d['features'] for c in coords_of(f['geometry']) for p in c]
    print('to_world_units: max |coord| %.3g -> world  x[%.2f..%.2f]  y[%.2f..%.2f]'
          % (maxabs, min(xs), max(xs), min(ys), max(ys)))

d['crs'] = {'type': 'name', 'properties': {'name': 'urn:ogc:def:crs:EPSG::3857'}}
json.dump(d, open(a.dst, 'w'))
print('wrote %s: %d features' % (a.dst, len(d['features'])))
