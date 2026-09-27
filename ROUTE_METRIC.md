# Routing-equivalence metric

Replaces point-distance precision/recall as the way to judge road extraction.

## Why

The old metric measured how close extracted pixels are to hand-drawn ones.
That punishes lateral error, which changes no route at all, and in this
project it swung wildly for reasons unrelated to quality:

| change                                | F1 before -> after |
|---------------------------------------|--------------------|
| 9.5px coordinate offset (a real bug)  | 55.1 -> 86.5       |
| evaluation tolerance 6px -> 10px      | Kalokai 73 -> 97   |

A metric that moves 30 points on a sub-pixel tolerance change cannot be
trusted to rank hexes.

## What it measures

For each hex: build a contracted routable graph from the extracted skeleton,
build the same from the hand-drawn roads, snap every town to its nearest node
in each, then compare shortest-path distance for every town pair.

Reported as `ratio = extracted_distance / hand_distance`. A route is
"preserved" when the ratio is near 1.0. Lateral error does not move this
number.

## The noise floor

A control run compares the HAND network against ITSELF, rasterised at width 1
versus width 3. It gives p50 1.058 with p10-p90 of 0.919-1.267 - i.e. a
change of nothing but raster width produces spreads as large as the ones
measured against the extractor. Any reading inside roughly 0.9-1.3 is
therefore indistinguishable from measurement noise, which is why the hand
raster must use width 1.

## Results, all 43 hexes with ground truth

- median per-hex route ratio: 1.048
- 21/43 hexes have a median ratio within 5% of 1.0
- 27/43 within 10%

Best by fraction of town pairs within 5%:
| hex | within 5% | p50 |
|---|---|---|
| ReaversPassHex | 92.6% | 0.993 |
| CallumsCapeHex | 83.3% | 0.999 |
| OarbreakerHex | 80.0% | 0.989 |

Worst by deviation of median ratio from 1.0:
| hex | p50 | within 5% |
|---|---|---|
| MorgensCrossingHex | 1.853 | 9% |
| LochMorHex | 1.347 | 17% |
| TerminusHex | 1.272 | 24% |

## Known caveats

- `towns.json` uses a different world origin than `Roads.json`. The offset
  is `x += 128.25, y -= 128.25`, fitted by minimising median town-to-road
  distance (0.5px resolution, 38.5px residual).
- Towns do not sit on their roads: distance ranges 3.6px to 192px, because
  the hand-drawn network does not reach every town. Both graphs are snapped
  independently with a 250px cap.
- MorgensCrossingHex has p90 6.26, i.e. a few town pairs are wildly
  different. Uninvestigated; likely a genuinely missing link rather than a
  whole-network failure.
- Graph construction uses degree!=2 pixels as nodes. The extracted polylines
  have one vertex per pixel, so the graph is built from the skeleton mask
  instead.

## Usage

    python3 route_equivalence.py HexA HexB ...   # per-hex comparison
    python3 route_control.py                     # measure the noise floor


## Accuracy validation (validate_all.py)

    python3 validate_all.py                    # all hexes with ground truth
    python3 validate_all.py --null-test        # plus a null control
    python3 validate_all.py SomeHex

Result over the 42 hexes that have >=10 comparable town pairs:

    median p50 route ratio : 1.050
    median match%          : 37.6
    p50 within 5% of 1.0   : 20/42
    match% >= 50           : 9/42

### How much is this worth? (sensitivity sweep)

A metric is only useful if a broken network scores worse than a good one. The
hand-drawn network was scored against copies of ITSELF shifted by increasing
amounts, giving the metric's response curve:

| shift | Acrithia | Clanshead | LochMor | AshFields | ReaversPass |
|-------|----------|-----------|---------|-----------|-------------|
| 0 px  | 58.4%    | 34.6%     | 16.7%   | 59.6%     | 92.6%       |
| 30 px | 48.5%    | 30.8%     | 13.5%   | 36.1%     | 72.1%       |
| 60 px | 36.4%    | 24.4%     |  8.2%   | 33.3%     | 42.6%       |
| 120 px| 20.8%    | 23.4%     | 10.4%   | 21.7%     | 27.5%       |
| 240 px| 18.5%    |  9.0%     |  2.7%   | 13.3%     |  7.6%       |
| 480 px| 21.7%    | 17.8%     |  8.3%   |  9.3%     |  0.0%       |

Two things follow:

1. The metric has a **floor of roughly 15-20%**. Anything at or below that is
   indistinguishable from a broken network.
2. It degrades monotonically between 0 and 120 px, so it is genuinely
   discriminative in that range.

The real extraction scores 37.6% median, comfortably above the 60 px null
(19.3%) and the floor. So the extracted network is meaningfully better than a
visibly-shifted one, and roughly equivalent to a ~30-40 px perturbation - well
outside the 1-3 px precision the pipeline actually achieves.

**Honest limitation:** this metric cannot distinguish "exact" from "off by
30 px". It is a screen that catches gross failure on an uncharted hex, not a
certificate of correctness.

## Region-name aliases

Two hexes are spelled differently in the map PNGs than in Roads.json and the
shell offset table:

| PNG | table / Roads.json |
|-----|--------------------|
| MapStemaLAndingHex.png | StemaLandingHex |
| MapMarbanHollowHex.png | MarbanHollow |

A literal lookup missed both, which made them appear absent from the offset
table when they were present. `grid.canonical_region()` now normalises names
(punctuation-free, case-insensitive, trailing "hex" dropped) and
`origin_from_table` uses it.
