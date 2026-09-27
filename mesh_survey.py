"""
Enumerate every spline mesh name present in the pak export.

WHY
---
`classify()` accepts a road only if its mesh name is in TIER_MESHES, TIER_SUFFIXES
or SPECIAL_MESHES -- matched EXACTLY, apart from the Snow suffix.  Anything else is
dropped silently: `load_splines()` does `if tier is None: continue`, so a road
built from a mesh this table does not know about produces no error, no warning and
no feature.  It just is not there.

That makes the whitelist the first thing to check when roads go missing.  A hex
with a low coverage number and a short segment count is the signature of whole
mesh families being discarded rather than of geometry being read slightly wrong.

    python3 mesh_survey.py            # all hexes, counts per mesh name
    python3 mesh_survey.py --unknown  # only names classify() rejects
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import roads_from_pak as rfp


def survey(json_dir):
    """Return (per-mesh Counter of segment count, hexes using each mesh)."""
    seg = Counter()
    hexes = defaultdict(set)
    for fn in sorted(os.listdir(json_dir)):
        if not fn.endswith(".json"):
            continue
        reg = fn[:-5]
        try:
            sp = json.load(open(os.path.join(json_dir, fn),
                                encoding="utf-8")).get("splines", {})
        except (ValueError, OSError) as e:
            print("  %s: unreadable (%s)" % (reg, e))
            continue
        for name, entries in sp.items():
            seg[name] += len(entries)
            hexes[name].add(reg)
    return seg, hexes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("json_dir", nargs="?",
                    default=os.environ.get("PAK_JSON_DIR", rfp.PAK_JSON))
    ap.add_argument("--unknown", action="store_true",
                    help="only print names classify() rejects")
    ap.add_argument("--hexes", action="store_true",
                    help="list the hexes using each mesh")
    a = ap.parse_args()

    if not os.path.isdir(a.json_dir):
        raise SystemExit("no such directory: %s" % a.json_dir)
    seg, hexes = survey(a.json_dir)
    if not seg:
        raise SystemExit("no splines found in %s" % a.json_dir)

    total = sum(seg.values())
    kept = sum(n for nm, n in seg.items() if rfp.classify(nm) is not None)
    print("%d mesh names, %d spline segments across %d hexes"
          % (len(seg), total, len({h for v in hexes.values() for h in v})))
    print("classify() keeps %d segments (%.1f%%), drops %d (%.1f%%)\n"
          % (kept, 100.0 * kept / total, total - kept,
             100.0 * (total - kept) / total))
    print("%-8s %-9s %-6s %s" % ("tier", "segments", "hexes", "mesh name"))
    print("-" * 78)
    for nm, n in sorted(seg.items(), key=lambda kv: -kv[1]):
        t = rfp.classify(nm)
        if a.unknown and t is not None:
            continue
        extra = ("  [%s]" % ",".join(sorted(hexes[nm])[:3])
                 if a.hexes and len(hexes[nm]) <= 3 else "")
        print("%-8s %-9d %-6d %s%s"
              % (("T%d" % t) if t else "DROPPED", n, len(hexes[nm]), nm, extra))

    dropped = [(n, nm) for nm, n in seg.items() if rfp.classify(nm) is None]
    if dropped:
        print("\nDROPPED by classify(), largest first:")
        for n, nm in sorted(dropped, reverse=True):
            # Editor spline mesh is deliberately ignored, so it is not a bug.
            note = ""
            if any(s in nm for s in rfp.IGNORE_SUBSTR) or nm in rfp.IGNORE_EXACT:
                note = "  (intentional: editor visualisation)"
            print("  %6d segments  %-60s%s" % (n, nm, note))
    return 0


if __name__ == "__main__":
    sys.exit(main())
