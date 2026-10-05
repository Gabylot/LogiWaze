#!/usr/bin/env python3
"""Explode MultiLineString features into one LineString feature per part.

The LogiWaze road loader walks `feature.geometry.coordinates[m][0].toFixed(3)`
- i.e. it assumes every road feature is a LineString whose coordinates are
points.  For a MultiLineString, `coordinates[m]` is itself a line, so `[0]` is
a point ARRAY and `.toFixed` is not a function:

    TypeError: i.geometry.coordinates[m][0].toFixed is not a function

Every road_source.geojson that ever worked in this app is 100% LineString, so
this is a hard format requirement, not a preference.  Splitting here also
gives the router one line per road part instead of one feature holding many.
"""
import argparse, json

ap = argparse.ArgumentParser()
ap.add_argument('--in', dest='src', required=True)
ap.add_argument('--out', dest='dst', required=True)
a = ap.parse_args()

d = json.load(open(a.src))
out = []
split = 0
n_in = len(d['features'])
for f in d['features']:
    g = f['geometry']
    if g['type'] == 'LineString':
        out.append(f)
        continue
    if g['type'] == 'MultiLineString':
        for part in g['coordinates']:
            if len(part) < 2:
                continue
            nf = json.loads(json.dumps(f))
            nf['geometry'] = {'type': 'LineString', 'coordinates': part}
            out.append(nf)
            split += 1
        continue
    out.append(f)
d['features'] = out
json.dump(d, open(a.dst, 'w'))
n_ml = sum(1 for f in out if f['geometry']['type'] == 'MultiLineString')
print('normalize_geometry: %d -> %d features (%d MultiLineString parts split, %d left)'
      % (n_in, len(out), split, n_ml))
