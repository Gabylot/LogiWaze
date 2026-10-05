#!/usr/bin/env python3
"""build_hybrid.py -- map-image road, with the pak data filling hexes it lacks.

Why
---
The two extraction routes fail in opposite ways, and neither alone is usable:

  map-image   connected (95.4% in one piece) but only 43 hexes
  pak         53 hexes but 327 fragments, largest 3.2%

The map-image network is therefore the backbone, and the pak data is used only
for the hexes it does not have at all -- including PariPeak and KuuraStrand,
where Pariah and Martti live.  That yields 53 hexes at 90.7% connected without
drawing a single new road.

Merging both sources everywhere is NOT the same thing and is much worse
(63.9% connected): the pak lines arrive as 327 fragments, so adding them to a
working network only adds islands.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import graphkit as gk


def hexes(path):
    return {f['properties'].get('region', '?') for f in json.load(open(path))['features']}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--map-image', required=True, help='connected road network')
    ap.add_argument('--pak', required=True, help='game-data road network')
    ap.add_argument('--out', required=True)
    a = ap.parse_args()

    base = json.load(open(a.map_image))
    fill = json.load(open(a.pak))
    have = {f['properties'].get('region', '?') for f in base['features']}
    pak_hexes = {f['properties'].get('region', '?') for f in fill['features']}
    missing = sorted(pak_hexes - have)
    print('map-image: %d features, %d hexes' % (len(base['features']), len(have)))
    print('pak:       %d features, %d hexes' % (len(fill['features']), len(pak_hexes)))
    print('filling from pak: %d hexes -> %s' % (len(missing), ', '.join(missing)))

    out = list(base['features'])
    added = 0
    for f in fill['features']:
        if f['properties'].get('region', '?') in missing:
            g = dict(f)
            g['properties'] = dict(f['properties'])
            g['properties']['source'] = 'pak'
            out.append(g)
            added += 1
    for f in out:
        f['properties'].setdefault('source', 'map-image')
    base['features'] = out
    json.dump(base, open(a.out, 'w'))
    print('wrote %s: %d features (%d from pak) across %d hexes'
          % (a.out, len(out), added, len({f['properties'].get('region') for f in out})))


if __name__ == '__main__':
    main()
