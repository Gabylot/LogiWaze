#!/usr/bin/env python3
"""Tests for graphkit.  Run: python3 test_graphkit.py

These exist because the graph model was got wrong three times in development,
each time producing a confident but false conclusion about the network.  Each
test pins one of those mistakes so it cannot come back.
"""
import math
import sys

import graphkit as gk


def net(*lines):
    """Build a Network from (region, [(x,y),...]) tuples."""
    return gk.Network(list(lines))


def eq(a, b, msg=''):
    if a != b:
        print('FAIL %s: got %r expected %r' % (msg or 'value', a, b))
        return 1
    return 0


def near(a, b, tol=1e-9, msg=''):
    if abs(a - b) > tol:
        print('FAIL %s: got %r expected ~%r' % (msg or 'value', a, b))
        return 1
    return 0


def t_frames():
    """world <-> mercator must round-trip, and the scale must be the multiply."""
    fails = 0
    x, y = 12.5, -7.25
    mx, my = gk.w2m(x, y)
    bx, by = gk.m2w((mx, my))
    fails += near(bx, x, 1e-9, 'w2m/m2w x round trip')
    fails += near(by, y, 1e-9, 'w2m/m2w y round trip')
    # the multiplier, named as a multiplier so it cannot be used as a divisor
    fails += near(gk.SCALE * 20037500.0, 128.0, 1e-9, 'SCALE is 128/20037500')
    return fails


def t_components_share_node():
    """Two lines meeting at an identical endpoint are one component."""
    n = net(('A', [(0, 0), (1, 0)]), ('A', [(1, 0), (2, 0)]))
    return eq(n.n_components, 1, 'lines sharing a node join') + \
           eq(n.largest_component, 4, 'all four endpoints in one component')


def t_components_not_merged_by_interior():
    """A road ending MID-LINE is not a connection.  Pinned mistake #2.

    Line 1 spans (0,0)-(10,0).  Line 2 ends at (5,0), which lies on line 1's
    interior.  A graph that joined on shared geometry would merge these; an
    endpoint graph must not.
    """
    n = net(('A', [(0, 0), (10, 0)]), ('A', [(5, 0), (5, 3)]))
    return eq(n.n_components, 2, 'mid-line ending must not connect')


def t_near_miss_excludes_same_component():
    """Coincident endpoints already joined must not be reported as gaps.

    Pinned mistake #3: this defect reported 17,812 phantom near-misses in the
    hand-traced network, which is actually well connected.
    """
    # two lines that share a node, plus a third line 0.1 away from that node
    n = net(('A', [(0, 0), (1, 0)]), ('A', [(1, 0), (2, 0)]), ('A', [(1.1, 0), (3, 0)]))
    misses = n.near_misses(1.0)
    return eq(len(misses), 1, 'only the genuinely unjoined pair is reported')


def t_near_miss_reports_real_gap():
    """A genuine gap between two disconnected lines is reported once."""
    n = net(('A', [(0, 0), (1, 0)]), ('A', [(1.1, 0), (2, 0)]))
    misses = n.near_misses(1.0)
    return eq(len(misses), 1, 'one gap found') + \
           near(misses[0][0], 0.1, 1e-6, 'gap distance')


def t_near_miss_respects_threshold():
    n = net(('A', [(0, 0), (1, 0)]), ('A', [(1.5, 0), (2.5, 0)]))
    return eq(len(n.near_misses(0.2)), 0, 'beyond threshold') + \
           eq(len(n.near_misses(1.0)), 1, 'within threshold')


def t_join_kind_pass_through():
    """Two collinear lines, one ending where the other starts: 0 degrees.

    ends[1] is line 0's END at (1,0); ends[2] is line 1's START at (1,0).
    Those are the pair that forms the through-path.
    """
    n = net(('A', [(0, 0), (1, 0)]), ('A', [(1, 0), (2, 0)]))
    kind, ang = n.join_kind(1, 2)
    return eq(kind, 'pass-through', 'kind') + near(ang, 0.0, 1e-6, 'straight through is 0 deg')


def t_join_kind_hairpin():
    """A pass-through join that turns sharply reports a large angle."""
    n = net(('A', [(0, 0), (1, 0)]), ('B', [(1, 0), (0, 1)]))
    kind, ang = n.join_kind(1, 2)
    bad = 1 if ang <= 40 else 0
    if bad:
        print('FAIL hairpin: expected >40 deg, got %.1f' % ang)
    return eq(kind, 'pass-through', 'kind') + bad


def t_join_kind_fork_is_not_a_hairpin():
    """Pinned mistake #4: fork/merge joins were counted as hairpins.

    Two lines both STARTING at a junction, running in opposite directions, are
    a road passing straight through.  Using >=3 vertices per line matters: on
    a 2-point line the "outward" vector points at the other endpoint, so the
    direction is degenerate and the test would decide for the wrong reason.
    """
    n = net(('A', [(-2, 0), (-1, 0), (0, 0)]), ('B', [(0, 0), (1, 0), (2, 0)]))
    kind, ang = n.join_kind(1, 2)          # A's END meets B's START
    fails = eq(kind, 'pass-through', 'kind') + near(ang, 0.0, 1e-6, 'straight is 0 deg')
    m = net(('A', [(1, 0), (0.5, 0), (0, 0)]), ('B', [(-1, 0), (-0.5, 0), (0, 0)]))
    kind2, ang2 = m.join_kind(0, 2)        # A's START meets B's START
    fails += eq(kind2, 'fork/merge', 'both start => fork/merge')
    if kind2 == 'fork/merge' and ang2 < 120:
        fails += 1
        print('FAIL fork: opposite arms should be ~180 deg, got %.1f' % ang2)
    return fails


def t_nearest_seg_vs_endpoint():
    """A town mid-road is far from any endpoint but sits on the segment."""
    n = net(('A', [(0, 0), (5, 0), (10, 0)]))
    dist, seg = n.nearest_seg(5, 0)
    ep = n.comp_at(5, 0)[1]               # distance to nearest ENDPOINT
    fails = 0
    if dist > 1e-9:
        fails += 1
        print('FAIL nearest_seg: expected 0, got %r' % dist)
    if ep <= 4:
        fails += 1
        print('FAIL endpoint distance: expected >4, got %r' % ep)
    return fails


def t_weld_reduces_components():
    """Welding a genuine gap must reduce the component count.

    Uses the network's own union-find rather than reimplementing it, which was
    itself a source of wrong answers earlier.
    """
    n = net(('A', [(0, 0), (1, 0)]), ('A', [(1.1, 0), (2, 0)]))
    before = n.n_components
    dsu = n.dsu
    dsu.union(('e', 1), ('e', 2))         # line 0's end to line 1's start
    after = len({dsu.find(('e', i)) for i in range(len(n.ends))})
    return eq(before, 2, 'two components before') + eq(after, 1, 'one after welding')


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith('t_')]
    fails = 0
    for t in tests:
        f = t()
        fails += f
        print('%-42s %s' % (t.__name__, 'ok' if f == 0 else 'FAIL'))
    print()
    print('%d test(s), %d failure(s)' % (len(tests), fails))
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
