"""
Foxhole road extraction: map PNG -> world-coordinate GeoJSON.

  1. colour mask per road tier (white=t1, orange=t2, red=t3)
  2. distance transform drops the wide Frontier-Border overlay band
  3. skeletonise the COMBINED mask so tiers cross-connect at junctions
  4. junction/endpoint detection, then walk each edge between nodes
  5. assign a tier per edge by majority colour along its pixels
  6. Douglas-Peucker simplify, emit LineStrings in world units

Origin comes from grid.hex_origin() (the exact offset table).
"""
import json
import numpy as np
from PIL import Image
from skimage.morphology import skeletonize
from scipy import ndimage as ndi

import grid
from skimage.morphology import binary_dilation, disk

S = grid.W / 2048.0          # world units per pixel (exact: 0.0125)
BORDER_MAX_R = 7.0           # roads reach r<=6px; border band is 10-50px
MIN_EDGE_PX = 12            # drop speckle shorter than this
# Antialiasing leaves hairline breaks where two road segments meet, which
# fragment the network (46 pieces vs the hand-drawn 1).  Dilating the road
# blob by 2px before skeletonising bridges them: on Acrithia that takes the
# largest component from 74% to 99.3% of the network.  Dilating the skeleton
# instead is worse - it distorts geometry and only reaches 80%.
DILATE = 2
MIN_COMP = 100              # discard leftover blobs smaller than this
# Shortest feature worth emitting.  Junction snapping can collapse a fragment
# onto a single point; those are dead ends in the graph, not roads.
MIN_FEATURE_LEN = 0.25      # world units (20 px)
# Longest interior fragment still treated as a dead stub.  Antialiasing stubs
# measured 1-11px; real road segments between junctions are far longer.
STUB_MAX_PX = 14
# Radius used to group neighbouring node pixels into one physical junction.
JUNCTION_RADIUS = 1
# Minimum skeleton degree to seed a junction.  Seeds from every degree!=2
# pixel made the walk stop on antialiasing dropouts mid-road.
FORK_DEGREE = 3
# Fragments touching a single junction are stubs; keep them only if this long.
STUB_MIN_PX = 12
# A skeleton component smaller than this fraction of the largest one is not a
# road: it is antialiasing debris or the rapid decay zone shading, which the
# colour mask catches but which never connects to the network.
ISO_FRACTION = 0.02
# Median inscribed radius above which a blob is a terrain region, not a road.
# Real roads measure 1-3 px; the pale blue-grey tint that produced a false
# road web in KuuraStrand measures 7.1.
MAX_ROAD_RADIUS_PX = 4.0
# A tier1 (white road) pixel must be within this distance of the edge of its own
# colour region.  Measured: 97.8% of hand-drawn tier1 road pixels have radius
# <=4, versus 33.7% of the pale terrain tint that also passes the brightness
# test.  Without it, KuuraStrand produced a false web of roads.
TIER1_MAX_RADIUS_PX = 4
# Blue tint by which a pale terrain colour is distinguished from a neutral-grey
# road.  The false colour (201,202,214) has B-R = 13.
BLUE_TINT_MIN = 6
# Minimum SATURATION (max(RGB) - min(RGB)) for a tier2 pixel.
#
# Tier2 was the dominant source of false edges: 95.9% of the pixels on edges
# that sit >10px from any hand-drawn road.  Two populations share tier2's
# existing R>180 & 110<G<190 & B<110 window and are separated by saturation:
#
#     false  (181,117,97)   sat 84    dark, dull terracotta - terrain shading
#     true   (217,172,108)  sat 109   tan   - a real tier2 road
#     true   (213,129,111)  sat 102   red   - a real tier3-blend road
#
# Measured over all 43 hexes with hand-drawn ground truth (830,889 false and
# 1,486,780 true tier2 px), a cut at sat>=88 keeps 6.4% of the false pixels
# and loses 0.41% of the true ones.  There is a genuine gap: the false p90 is
# 84 and the true p1 is 91, so 88 sits in empty space rather than on a
# boundary.  End to end this lifts micro F1 82.3 -> 91.3 with recall
# UNCHANGED at 97.3% - unlike a green-channel cut (G>=130), which reaches a
# similar precision only by deleting 11 points of recall.
TIER2_MIN_SAT = 88


def tier_masks(a):
    """Colour tests for the three road tiers.

    Tier 1 needed a second condition.  The plain brightness test
    (R>200 & G>200 & B>190) also accepts broad PALE REGIONS: in KuuraStrand
    the colour (201,202,214) is 146k px of terrain tint that passes it, and
    tracing that produced a dense web of "roads" which do not exist.  Hand-drawn
    roads never sit on it (0.0%).

    A road is a THIN streak, so a tier1 pixel must also lie near the edge of its
    own colour region - measured by the distance transform.  Across the
    hand-drawn ground truth, 97.8% of real tier1 road pixels have an inscribed
    radius <= 4px, against 33.7% for the false region.  The cut is applied to
    tier 1 only; tiers 2 and 3 are strongly chromatic and unambiguous.

    Tier 2 additionally requires SATURATION >= TIER2_MIN_SAT.  Its existing
    window admits dark dull terracotta terrain shading as well as real tan and
    red roads; saturation separates them with no recall cost (see
    TIER2_MIN_SAT).
    """
    R = a[:, :, 0].astype(int)
    G = a[:, :, 1].astype(int)
    B = a[:, :, 2].astype(int)
    t1 = (R > 200) & (G > 200) & (B > 190)
    # A road is neutral grey - (236,236,236), (235,235,235).  Pale terrain
    # tints are the same brightness but BLUE-tinted, e.g. (201,202,214), a
    # 146k-px region in KuuraStrand whose edges trace ridge lines and read as a
    # web of roads that are not there.  Measured over the six hexes with
    # hand-drawn ground truth, only 4 of 165,563 kept pixels (0.00%) are
    # blue-tinted pale, so excluding them costs nothing.  (An earlier test
    # appeared to show 1.85% and looked like a real road colour, but that was
    # measuring hand-drawn VERTICES, which drift off the painted road.)
    t1 = t1 & ~((B > R + BLUE_TINT_MIN) & (np.minimum(np.minimum(R, G), B) > 195))
    if t1.any():
        dist = ndi.distance_transform_edt(t1)
        t1 = t1 & (dist <= TIER1_MAX_RADIUS_PX)
    t2 = (R > 180) & (G > 110) & (G < 190) & (B < 110)
    # Reject the dark, dull terracotta that shades terrain.  Saturation is
    # max(RGB)-min(RGB); real tier2 roads are vivid (tan or red), the false
    # shading is not.
    sat = np.maximum(np.maximum(R, G), B) - np.minimum(np.minimum(R, G), B)
    t2 = t2 & (sat >= TIER2_MIN_SAT)
    return {
        1: t1,
        2: t2,
        3: (R > 170) & (G < 110) & (B < 110),
    }


def rdp(pts, eps):
    """Douglas-Peucker, iterative (edges can be long)."""
    if len(pts) < 3:
        return pts
    keep = np.zeros(len(pts), bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        p, q = pts[i], pts[j]
        d = q - p
        n = float(np.hypot(*d))
        seg = pts[i + 1:j] - p
        if n == 0:
            dist = np.hypot(*seg.T)
        else:
            t = np.clip((seg @ d) / (n * n), 0, 1)
            # seg is RELATIVE to p, so the projection must be too.  Adding p
            # back here compared an offset against an absolute point, so every
            # distance came out as |p| (~231 units) and the test `dist > eps`
            # was true for every point at every eps: rdp() never removed
            # anything.  The OCR network therefore carried one vertex per
            # skeleton pixel (738,923 vertices vs the hand-traced 156,214)
            # and the map burned enormous RAM in the router.
            proj = t[:, None] * d
            dist = np.hypot(*(seg - proj).T)
        k = int(np.argmax(dist))
        if dist[k] > eps:
            k += i + 1
            keep[k] = True
            stack.append((i, k))
            stack.append((k, j))
    return pts[keep]


def neighbours8(sk):
    """8-neighbour lookup: dict (y,x) -> list of neighbour (y,x)."""
    ys, xs = np.nonzero(sk)
    s = set(zip(ys.tolist(), xs.tolist()))
    nb = {}
    for (y, x) in s:
        lst = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy == 0 and dx == 0:
                    continue
                if (y + dy, x + dx) in s:
                    lst.append((y + dy, x + dx))
        nb[(y, x)] = lst
    return nb


def build_junctions(comb, branch_px=None):
    """Locate graph nodes and give each one a single canonical pixel.

    Antialiasing makes a single physical fork span several pixels (3229 raw
    node pixels collapse to ~300 real junctions on Acrithia).  Each cluster
    becomes ONE junction, and every edge ending there is snapped to that same
    pixel - otherwise the two halves of a road terminate on different pixels
    and the graph never rejoins.

    `branch_px` restricts the seed set to genuine forks.  Passing it matters:
    seeding with every degree!=2 pixel (including antialiasing dropouts in the
    middle of a road) made the walk stop early and cut real roads short.

    Returns (node_of, canon): skeleton pixel -> junction id, and the canonical
    pixel for each junction.
    """
    if branch_px is None:
        nb = neighbours8(comb)
        branch_px = {p for p, v in nb.items() if len(v) != 2}
    if not branch_px:
        return {}, []
    nm = np.zeros(comb.shape, bool)
    for (y, x) in branch_px:
        nm[y, x] = True
    cl, ncl = ndi.label(binary_dilation(nm, disk(JUNCTION_RADIUS)),
                        structure=np.ones((3, 3)))
    on_skel = set(zip(*np.nonzero(comb)))
    node_of = {}
    canon = []
    for li in range(1, ncl + 1):
        ys, xs = np.nonzero(cl == li)
        if len(ys) == 0:
            continue
        # Only SEED pixels count as junction members.  Dilation pulls in
        # neighbouring degree-2 pixels, and promoting those made a road end
        # at a one-sided "junction" with nothing on the other side - 50 of the
        # 61 dead ends.  Those pixels stay in the edge body so the walk
        # continues through the fork area.
        seed = nm[ys, xs]
        if not seed.any():
            continue
        sy, sx = ys[seed], xs[seed]
        # representative = seed pixel nearest the cluster centroid
        cy, cx = sy.mean(), sx.mean()
        k = int(np.argmin((sy - cy) ** 2 + (sx - cx) ** 2))
        j = len(canon)
        canon.append((float(sy[k]), float(sx[k])))
        for y, x in zip(sy, sx):
            if (int(y), int(x)) in on_skel:
                node_of[(int(y), int(x))] = j
    return node_of, canon


def road_skeleton(a, min_comp=MIN_COMP, dilate=DILATE):
    """Colour mask -> dilate -> medial axis -> drop non-road blobs.

    Dilating the road BLOB (not the skeleton) bridges the hairline breaks that
    antialiasing leaves where two segments meet.  Dilating the skeleton
    instead distorts the centreline and only reached 80% connectivity versus
    99.3% this way, on Acrithia.
    """
    tm = tier_masks(a)
    comb = np.zeros(tm[1].shape, bool)
    for t, m in tm.items():
        d = ndi.distance_transform_edt(m)
        road = m & (d <= BORDER_MAX_R * 3)   # whole road cross-section
        comb |= binary_dilation(road, disk(dilate))
    comb = skeletonize(comb)

    # ---- filter order matters.
    #
    # THICKNESS first, then size.  The thickness cut removes broad pale regions
    # that the T1 brightness test accepts - in KuuraStrand the colour
    # (201,202,214) covers 76k px in one blob with median inscribed radius 7.6.
    # If the size cut ran first that blob became "the largest component" and set
    # the threshold, so the real road network was judged against a false
    # reference.  Running thickness first drops it, and the largest surviving
    # blob is then a real road.
    stats = {'thick_px': 0}
    rad = ndi.distance_transform_edt(comb)
    lab, nlab = ndi.label(comb, structure=np.ones((3, 3)))
    if nlab:
        thin = np.zeros(comb.shape, bool)
        for i in range(1, nlab + 1):
            m = lab == i
            if m.sum() < 300:
                continue
            if float(np.median(rad[m])) <= MAX_ROAD_RADIUS_PX:
                thin |= m
        stats['thick_px'] = int(comb.sum() - thin.sum())
        comb = thin

    lab, nlab = ndi.label(comb, structure=np.ones((3, 3)))
    if nlab:
        sizes = np.bincount(lab.ravel())
        biggest = int(sizes[1:].max()) if len(sizes) > 1 else 0
        # Drop blobs too small to be roads.  Besides antialiasing specks this
        # removes the rapid decay zone: the map draws it with a red tint close
        # to the mud-road colour, so the colour mask picks it up, but it never
        # joins the network - it sits in isolated fragments.  Keying the cutoff
        # to the LARGEST component (rather than a fixed pixel count) is what
        # separates a real short road from decay-zone shading.
        # Validated against the hand-drawn roads across seven hexes: this keeps
        # 99.6-100% of real road while removing 1.4-10.2% of the skeleton.
        keep = np.zeros(sizes.shape, bool)
        keep[1:] = sizes[1:] >= max(min_comp, biggest * ISO_FRACTION)
        stats['noise_comps'] = int((~keep[1:]).sum())
        stats['iso_px'] = int((~keep[lab]).sum())
        comb = keep[lab]

    stats['px'] = int(comb.sum())
    return comb, tm, stats


def extract(png, region, eps=0.06, min_comp=MIN_COMP, dilate=DILATE):
    """Return (features, stats) in world units relative to the hex origin."""
    a = np.array(Image.open(png).convert('RGB'))
    comb, tm, stats = road_skeleton(a, min_comp, dilate)
    stats.update({'edges': 0, 'dropped': 0, 'degenerate': 0, 'tiers': {}})
    if not comb.any():
        return [], stats

    nb = neighbours8(comb)
    on_skel = set(zip(*np.nonzero(comb)))

    # A junction is where the road really FORKS.  Antialiasing leaves stray
    # degree-1 pixels mid-road; seeding junctions with those made the walk stop
    # early and cut real roads short.
    branch_px = {p for p, v in nb.items() if len(v) >= FORK_DEGREE}
    node_of, canon = build_junctions(comb, branch_px)

    # Remove the WHOLE junction cluster, not just its member pixels.  Leaving
    # the cluster's inner pixels in place keeps neighbouring roads joined
    # through the junction, so the walk has no clean end to stop at.
    removed = np.zeros(comb.shape, bool)
    for p in node_of:
        removed[p] = True
    edges_px = comb & ~removed

    lab2, nlab2 = ndi.label(edges_px, structure=np.ones((3, 3)))

    feats = []
    for li in range(1, nlab2 + 1):
        pix = {(int(y), int(x)) for y, x in zip(*np.nonzero(lab2 == li))}
        # A fragment touches a junction when one of its pixels is 8-adjacent to
        # a junction-cluster pixel.  (Fragment pixels were carved OUT of the
        # clusters, so testing membership in node_of never matched and this
        # produced zero edges.)
        #
        # NO size filter here.  A 2-11px fragment linking two junctions is a
        # real road: the clusters either side are only a few pixels apart, so
        # the snapped polyline is short but it is the ONLY thing joining those
        # junctions.  Filtering on size dropped all 347 of them, left 53
        # junctions one-sided, and that alone is why the graph came out in 36
        # components instead of 1.  Stubs (one junction only) are filtered
        # below, once we know how many junctions a fragment touches.
        ends = []
        for p in pix:
            y, x = p
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    q = (y + dy, x + dx)
                    if q in on_skel and q in node_of and q not in pix:
                        ends.append((p, node_of[q]))
        if not ends:
            stats['dropped'] += len(pix)
            continue
        njunc = len({j for _, j in ends})
        if njunc < 2 and len(pix) < STUB_MIN_PX:
            stats['dropped'] += len(pix)
            continue

        start, start_j = ends[0]
        order = [start]
        seen = {start}
        cur = start
        end_j = None
        while True:
            nxt = next((q for q in nb[cur] if q in pix and q not in seen), None)
            if nxt is None:
                break
            seen.add(nxt)
            order.append(nxt)
            cur = nxt
            if cur in node_of:
                end_j = node_of[cur]
                break
        # The junction PIXELS were carved out of the edge set, so the walk can
        # never actually step onto one: it always stops on the last fragment
        # pixel, which merely ADJACENT to the junction.  Looking only for
        # `cur in node_of` therefore left end_j None for every single fragment
        # (534 of 534), so only the start end was ever snapped and no two edges
        # shared an endpoint.  Resolve the far junction from the final pixel's
        # neighbours instead.
        if end_j is None and len(order) > 1:
            y, x = order[-1]
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    q = (y + dy, x + dx)
                    j = node_of.get(q)
                    if j is not None and j != start_j:
                        end_j = j
                        break
                if end_j is not None:
                    break
        if len(order) < 2:
            stats['dropped'] += len(pix)
            continue

        votes = {}
        for y, x in order:
            for t, m in tm.items():
                if m[y, x]:
                    votes[t] = votes.get(t, 0) + 1
        tier = max(votes, key=votes.get) if votes else 3

        pts = np.array([[x * S, -y * S] for y, x in order], float)
        # Snap both ends onto their junction's canonical pixel so every edge
        # meeting at a fork shares one coordinate exactly.
        #
        # Use the junction ids captured in `ends`, NOT node_of.get(start):
        # `start` is a FRAGMENT pixel, never a member of node_of, so that lookup
        # always returned None and no two edges ever shared an endpoint - 0 of
        # 1050 shared, which is why the graph stayed in 30+ components.
        sj = start_j
        if sj is not None:
            pts[0] = [canon[sj][1] * S, -canon[sj][0] * S]
        if end_j is not None:
            pts[-1] = [canon[end_j][1] * S, -canon[end_j][0] * S]
        simp = rdp(pts, eps)
        if len(simp) < 2:
            stats['dropped'] += len(pix)
            continue
        length = float(sum(np.hypot(simp[i + 1][0] - simp[i][0],
                                    simp[i + 1][1] - simp[i][1])
                           for i in range(len(simp) - 1)))
        # Only stubs are length-filtered.  A short fragment that links two
        # junctions IS a real road: the junction clusters sit a couple of
        # pixels apart, so after snapping the polyline measures <0.14 units
        # even though it is the only thing joining those junctions.  Filtering
        # these on length removed 347 real links and left the graph in 30+
        # fragments (Acrithia) instead of 1.
        if njunc < 2 and length < MIN_FEATURE_LEN:
            stats['degenerate'] += 1
            continue
        # Re-assert the snapped endpoints AFTER simplify, and round both from
        # the same canonical value.  rdp() can drop the original last point,
        # and rounding each coordinate independently made two edges sitting on
        # the same junction differ in the last decimal - 0 of 264 endpoints were
        # shared, which left the graph as 38 fragments.
        coords = [[round(float(x), 3), round(float(y), 3)] for x, y in simp]
        if sj is not None:
            coords[0] = [round(canon[sj][1] * S, 3), round(-canon[sj][0] * S, 3)]
        if end_j is not None:
            coords[-1] = [round(canon[end_j][1] * S, 3),
                          round(-canon[end_j][0] * S, 3)]
        stats['edges'] += 1
        stats['tiers'][tier] = stats['tiers'].get(tier, 0) + 1
        feats.append({
            'type': 'Feature',
            'properties': {'region': region, 'tier': int(tier)},
            'geometry': {'type': 'LineString', 'coordinates': coords},
        })
    return feats, stats


def find_png(region, d='/var/www/LogiWaze/MapHexes'):
    """Resolve a region name to its map PNG, tolerating filename typos.

    Two shipped filenames disagree with the region name used everywhere else:
    MapMarbanHollowHex.png serves region 'MarbanHollow', and
    MapStemaLAndingHex.png has a stray capital A.  Fall back to a
    case-insensitive, punctuation-insensitive match.
    """
    import os
    import re
    direct = f'{d}/Map{region}.png'
    if os.path.exists(direct):
        return direct
    def key(s):
        return re.sub(r'[^a-z0-9]', '', s.lower())

    k = key(region)
    # The shipped filenames add a "Hex" suffix, and StemaLandingHex even has a
    # stray capital A, so compare on a normalised key with that suffix dropped.
    for f in sorted(os.listdir(d)):
        if not f.endswith('.png'):
            continue
        fk = key(f[3:-4])
        if fk == k or fk.removesuffix('hex') == k.removesuffix('hex'):
            return f'{d}/{f}'
    return None


def extract_hex(region, png=None, eps=0.06):
    if png is None:
        png = find_png(region)
    grid.load_offsets()
    tx, ty = grid.origin_from_table(region)
    feats, stats = extract(png, region, eps=eps)
    for f in feats:
        f['geometry']['coordinates'] = [
            [round(c[0] + tx, 3), round(c[1] + ty, 3)]
            for c in f['geometry']['coordinates']]
    stats['origin'] = (tx, ty)
    return feats, stats


if __name__ == '__main__':
    import os
    import sys
    region = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else f'{region}.geojson'
    feats, st = extract_hex(region)
    print(f"{region}: {st['edges']} edges, {st['px']} skeleton px, "
          f"tiers={st.get('tiers')}, origin={st['origin']}")
    json.dump({'type': 'FeatureCollection', 'features': feats},
              open(out, 'w'))
    print(f"wrote {os.path.abspath(out)}")