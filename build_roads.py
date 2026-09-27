"""Build an updated road network from map images, with verification at each step.

This ties the pieces together into one gated command:

    1. screen      check_new_hex.py  - structural sanity + live-API town anchors
    2. emit        extract_routes    - mask, skeletonise, walk to polylines
    3. verify      netdiff.py        - compare against hand-drawn data, when
                                       the hex has any
    4. merge       write a road_source.geojson with those hexes replaced

A hex only reaches the output if it passes the screen.  The network-difference
check is reported but does not block, because it needs ground truth that only
43 of the hexes have - the thresholds for it are in NETDIFF.md and the noise
floor is high enough that a hard gate would reject good hexes.

Usage
    python3 build_roads.py --out new_roads.geojson HexA HexB ...
    python3 build_roads.py --out new_roads.geojson --all      # every map PNG
    python3 build_roads.py --dry-run HexA                    # screen only
"""
import sys, os, json, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from PIL import Image
import extract_routes as er
import grid
import check_new_hex
import netdiff
import emit_road_source as emit

HERE = os.path.dirname(os.path.abspath(__file__))


def all_regions():
    d = os.path.join(HERE, 'MapHexes')
    out = []
    for f in sorted(os.listdir(d)):
        if f.startswith('Map') and f.endswith('.png'):
            out.append(f[3:-4])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('hexes', nargs='*')
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--out', help='merged road_source.geojson to write')
    ap.add_argument('--dry-run', action='store_true', help='screen only, emit nothing')
    ap.add_argument('--no-api', action='store_true', help='skip live town anchors')
    ap.add_argument('--merge', action='store_true',
                    help='replace these hexes in a copy of road_source.geojson')
    a = ap.parse_args()
    grid.load_offsets()
    regions = a.hexes or (all_regions() if a.all else [])
    if not regions:
        ap.error('give hex names, or --all')

    roads_path = os.path.join(HERE, 'Roads.json')
    hand = {}
    if os.path.exists(roads_path):
        lz = json.load(open(roads_path))
        for f in lz['features']:
            hand.setdefault(f['properties']['region'], []).append(f)

    print('%-24s %-8s %9s %9s %9s %9s' %
          ('hex', 'screen', 'features', 'overlap<=4', 'missed<=4', 'polylines'))
    print('-' * 76)
    accepted, rejected, made = [], [], {}
    for R in regions:
        rep = check_new_hex.check(R, with_api=not a.no_api)
        if rep['problems']:
            print('%-24s %-8s %s' % (R, 'FAIL', '; '.join(rep['problems'])))
            rejected.append(R)
            sys.stdout.flush()
            continue
        if a.dry_run:
            print('%-24s %-8s %9s' % (R, 'pass', rep.get('polylines', '-')))
            accepted.append(R)
            sys.stdout.flush()
            continue
        feats = emit.hex_features(R)
        made[R] = feats
        # verify against hand data if this hex has any
        ov = miss = None
        if hand.get(R):
            base = np.array(Image.open(er.find_png(R)).convert('RGB'))
            comb, _, _ = er.road_skeleton(base)
            hm = netdiff.rasterise(hand[R], *comb.shape, *grid.origin_from_table(R))
            c = netdiff.compare(comb, hm)
            if c:
                ov, miss = c['bwd_within4'], c['fwd_within4']
        print('%-24s %-8s %9d %9s %9s %9d' % (
            R, 'pass', len(feats),
            '%.1f%%' % ov if ov is not None else '-',
            '%.1f%%' % miss if miss is not None else '-',
            rep.get('polylines', '-')))
        accepted.append(R)
        sys.stdout.flush()

    print('-' * 76)
    print('%d accepted, %d rejected' % (len(accepted), len(rejected)))
    if rejected:
        print('  rejected: %s' % ', '.join(rejected))
    if a.dry_run or not a.out:
        return 0 if accepted else 1

    frag = {'type': 'FeatureCollection',
            'crs': {'type': 'name', 'properties': {'name': 'urn:ogc:def:crs:EPSG::3857'}},
            'features': [f for R in accepted for f in made[R]]}
    json.dump(frag, open(a.out, 'w'))
    print('wrote %s: %d features from %d hexes'
          % (a.out, len(frag['features']), len(accepted)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
