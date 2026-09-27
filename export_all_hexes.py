"""
Batch-export every hex's road splines from the pak.

Drives Tsekho/fh_map_exporter's Exporter.exe once per hex.  The exporter takes
a single asset per invocation and re-indexes the 24 GB pak each time, so this is
slow (~40 s/hex) but embarrassingly parallel and independent per hex -- a failure
on one hex cannot corrupt another, which is why it runs as separate processes
rather than one long-lived tool.

Skips any hex whose JSON already exists, so the run is resumable: interrupt it
and re-run to continue.

    python3 export_all_hexes.py              # all hexes in the offset table
    python3 export_all_hexes.py AcrithiaHex  # just one
    python3 export_all_hexes.py -j 4         # 4 workers (default: 4)

Each worker re-indexes the pak, so RAM scales with the worker count; the
exporter is not cheap on memory.  Disk: expect roughly 10 MB of meshes per hex,
which is why this can be deleted afterwards -- only _json/ is needed.
"""
import argparse
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import grid

EXPORTER = r"J:\fh_map_exporter\Exporter.exe"
PAKS = r"E:\Program Files (x86)\Steam\steamapps\common\Foxhole\War\Content\Paks"
OUT = r"J:\fh_export"
JSON = os.path.join(OUT, "_json")

# Region names differ in spelling between the app's tables and the exporter's
# region_centers.json (MapStemaLAndingHex vs StemaLandingHex).  The umap name is
# the canonical one, so try the app's spelling, then the variants that show up.
VARIANTS = ("{r}", "{r}Hex", "{r}Region")


def candidates(region):
    base = region
    for suf in ("Hex", "Region", "IslandHex", "IslandsHex"):
        if base.endswith(suf):
            base = base[: -len(suf)]
    return ["%s%s" % (base, s) if s else base for s in ("", "Hex", "Region")]


def export_one(region):
    """Export one hex.  Returns (region, ok, message)."""
    target = os.path.join(JSON, region + ".json")
    if os.path.isfile(target):
        return region, True, "cached"
    for name in candidates(region):
        cmd = [EXPORTER, "-i", PAKS, "-o", OUT,
               "-a", "War/Content/Maps/Master/" + name]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        except subprocess.TimeoutExpired:
            return region, False, "timeout on %s" % name
        if os.path.isfile(target):
            return region, True, "as %s" % name
    return region, False, "no umap matched %s" % candidates(region)


def candidates_mode(names):
    grid.load_offsets(os.path.join(HERE, "scripts", "export_major_locations.sh"))
    for r in names or sorted(grid.OFFSETS):
        print("%-26s -> %s" % (r, " | ".join(candidates(r))))
    return 0


def main():
    # `--candidates` is a dry run: print the umap names that would be tried,
    # without launching the exporter.  Cheap way to check the spelling logic
    # against the one hex already on disk before a long batch.
    if len(sys.argv) > 1 and sys.argv[1] == "--candidates":
        return candidates_mode(sys.argv[2:])

    ap = argparse.ArgumentParser()
    ap.add_argument("hexes", nargs="*")
    ap.add_argument("-j", type=int, default=4)
    a = ap.parse_args()
    grid.load_offsets(os.path.join(HERE, "scripts", "export_major_locations.sh"))
    os.makedirs(JSON, exist_ok=True)

    hexes = a.hexes or sorted(grid.OFFSETS)
    done = cached = failed = 0
    with ThreadPoolExecutor(max_workers=a.j) as ex:
        for region, ok, msg in ex.map(export_one, hexes):
            if not ok:
                failed += 1
                print("  FAIL %-26s %s" % (region, msg), flush=True)
            elif msg == "cached":
                cached += 1
            else:
                done += 1
                print("  ok   %-26s %s" % (region, msg), flush=True)
    print("\n%d exported, %d cached, %d failed" % (done, cached, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
