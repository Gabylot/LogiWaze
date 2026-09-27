# Network-difference metric (netdiff.py)

The metric that actually says what is going on.

## Why the earlier metrics were weak

- **Point-distance precision/recall** punished lateral error that changes no
  route, and swung 30 F1 points on a 9.5px coordinate change and 24 points on a
  tolerance change from 6px to 10px.
- **Route-length comparison** ("how far is A from B?") is nearly blind to
  *where* the routes go: two networks can have identical total length and
  share no road at all.

## What this measures

Treat each network as a set of pixels and measure the bidirectional distance
between them:

- **forward** - for every extracted pixel, distance to the nearest hand-drawn
  pixel. Large values mean *we drew roads that are not there*.
- **backward** - for every hand-drawn pixel, distance to the nearest extracted
  pixel. Large values mean *we missed roads that are there*.

Keeping the directions separate is the point: one symmetric number hides the
difference between inventing a motorway and missing a village road.

## Proof that it is discriminative

Each scenario corrupts the extracted network in a known way. Mean over
Acrithia, ClansheadValley, AshFields, ReaversPass.

| scenario | fwd<=4px | bwd<=4px |
|---|---|---|
| real | 66.5% | 73.0% |
| shift 15px | 22.9% | 25.5% |
| shift 30px | 15.4% | 17.1% |
| erase 11% | 70.0% | 69.1% |
| erase 25% | 66.6% | 56.1% |
| erase 40% | 62.2% | 42.8% |

- Positional error is caught hard: a 15px shift collapses both directions.
- Missing roads are caught by **backward**, which falls 73% -> 43% as 40% of
  the network is erased.
- Random pixel-drop is *not* a valid test (survivors still lie on the same
  lines, so nothing moves); erasing whole areas is.

## Baseline across all 43 hand-charted hexes

| statistic | min | median | max |
|---|---|---|---|
| fwd median distance | 1.00px | 2.24px | 5.00px |
| bwd median distance | 1.00px | 2.00px | 4.12px |
| fwd within 4px | 43.3% | 76.5% | 95.2% |
| bwd within 4px | 49.2% | 86.7% | 96.1% |
| fwd within 10px | 71.5% | **97.3%** | 100% |
| bwd within 10px | 75.5% | **96.3%** | 99.4% |

The two networks agree to a median of ~2px, and 96-97% of all network pixels
lie within 10px of the other network.

## Usage

    python3 netdiff.py                    # all charted hexes
    python3 netdiff.py SomeHex OtherHex

## Honest limits

- Sub-4px agreement is only 76-87%, so the metric is not certifying precision
  at the pixel level. Hand drawing is not exact, and part of that gap is
  irreducible.
- Forward and backward answer different questions. A hex can be perfect in
  one and poor in the other; always read both.
