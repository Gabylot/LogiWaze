"""Emit extracted roads into the production road_source.geojson format.

This is the step that makes the extractor usable: the app's pipeline is

    road_source.geojson  --scripts/roads.js-->  Roads.json  -->  app

road_source.geojson is EPSG:3857 (Web Mercator) with MultiLineString geometry.
scripts/roads.js converts to game-world units with

    world_x = merc_x * SCALE + 128
    world_y = merc_y * SCALE - 128        SCALE = 128 / 20037500

so the inverse, used here to write mercator from the extractor's world units,
is

    merc_x = (world_x - 128) / SCALE
    merc_y = (world_y + 128) / SCALE

Verified against the real file: 2683936.9688615957 -> 145.145.

Usage
    python3 emit_road_source.py SomeHex [more...]        # print a fragment
    python3 emit_road_source.py --merge out.geojson ...  # replace those hexes
                                                             in a copy of
                                                             road_source.geojson
"""
import sys, os, json, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extract_routes as er
import grid

HERE = os.path.dirname(os.path.abspath(__file__))
HALF_WORLD = 128.0
MERC_HALF = 20037500.0
SCALE = HALF_WORLD / MERC_HALF


def world_to_merc(x, y):
    return ((x - HALF_WORLD) / SCALE, (y + HALF_WORLD) / SCALE)


def hex_features(region):
    """Extracted roads for one hex, in production road_source.geojson form."""
    png = er.find_png(region)
    if png is None:
        raise SystemExit('%s: no map PNG' % region)
    if grid.origin_from_table(region) is None:
        raise SystemExit('%s: no offset table entry' % region)
    feats, _ = er.extract_hex(region)
    out = []
    for f in feats:
        c = f['geometry']['coordinates']
        if len(c) < 2:
            continue
        line = [list(world_to_merc(x, y)) for x, y in c]
        out.append({
            'type': 'Feature',
            'properties': {'region': f['properties']['region'],
                           'tier': f['properties']['tier']},
            'geometry': {'type': 'MultiLineString', 'coordinates': [line]},
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('hexes', nargs='+')
    ap.add_argument('--merge', help='write a merged road_source.geojson here')
    a = ap.parse_args()
    grid.load_offsets()
    made = {}
    for R in a.hexes:
        made[R] = hex_features(R)
        print('%-24s %5d features' % (R, len(made[R])), file=sys.stderr)
    if not a.merge:
        print(json.dumps({'type': 'FeatureCollection',
                          'crs': {'type': 'name', 'properties': {
                              'name': 'urn:ogc:def:crs:EPSG::3857'}},
                          'features': [f for v in made.values() for f in v]}))
        return
    src = json.load(open(os.path.join(HERE, 'road_source.geojson')))
    keep = [f for f in src['features']
            if f.get('properties', {}).get('region') not in made]
    out = src['features'][:0] + keep
    for R in a.hexes:
        out.extend(made[R])
    src['features'] = out
    json.dump(src, open(a.merge, 'w'))
    print('wrote %s: %d features (%d replaced)'
          % (a.merge, len(out), sum(len(v) for v in made.values())),
          file=sys.stderr)


if __name__ == '__main__':
    main()
