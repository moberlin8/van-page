# Gauge design — measured from `dashboardMock.png`

Source: `/home/ada/van/design/dashboardMock.png` (2326 x 1152, RGBA).
All numbers below are pixel measurements, not guesses. Circle fit on the outer
boundary of each arc: residual std 0.29 px.

## The two gauges

Right-hand column, stacked vertically, same column centre x = 2065:

| # | Label | Secondary | Value | Value colour |
|---|---|---|---|---|
| 1 | `Obstructed` | — | `0.8%` | green |
| 2 | `RTT` | `Last 15 minutes` (clock icon, cyan) | `115 ms` | red |

## Geometry (identical for both)

- Arc centre: `(2065, 726)` gauge 1, `(2065, 1071)` gauge 2.
- Outer radius **R = 190 px**.
- Crown (top of arc) at y = 536 / 881.
- Arc ends at y = 787 / 1132, x = 1885 and 2245.
- Ends sit **18.7 deg below horizontal** → arc spans **217.4 deg**
  (180 + 2 x 18.7). Not a true semicircle — the ends point down-and-out,
  roughly 8 o'clock and 4 o'clock.

## Structure — three concentric parts

Radial profile at the crown (r measured from the arc centre):

| r (px) | width | what it is | colour |
|---|---|---|---|
| < 131 | — | hollow centre | page bg `rgb(33,33,36)` |
| 132–180 | 48 | **thick track band** | `rgb(47,47,50)` = `#2F2F32` |
| 181–182 | 2 | dark gap | separates track from zone ring |
| 183–190 | 8 | **thin zone ring**, outer edge | green / yellow / red |
| > 191 | — | background | |

So: a thick grey track, and a thin coloured ring floating just outside it.

## Zone ring (the thin outer band)

Measured boundaries, in degrees measured from the right end (0 deg) going
counter-clockwise to the left end (198 deg):

| Gauge | green | yellow | red |
|---|---|---|---|
| Obstructed | 198 → 177 (9.7% of arc) | — | 177 → -18 (90.3%) |
| RTT | 198 → 127 (32.7%) | 127 → 55 (32.7%) | 55 → -18 (33.2%) |

RTT's three zones are equal thirds. Obstructed has green only as a sliver at
the start.

## Value fill

No needle. The current value is drawn as a **fill on the thick track**, starting
at the left end (198 deg) and running to the value's position, coloured with the
zone colour at that value:

| Gauge | fill extent | fill colour | implied position |
|---|---|---|---|
| Obstructed | 198 → 177 | green | 9.7% of range |
| RTT | 198 → -9 | red | 95.4% of range |

Both fills end inside the zone their colour names, which is what makes this
reading consistent (a track-coloured-by-zone reading fails: gauge 1's track is
grey while its value sits in the green zone).

## Palette (measured, not from a Tailwind ramp)

| role | rgb | hex |
|---|---|---|
| page background | 33, 33, 36 | `#212124` |
| track | 47, 47, 50 | `#2F2F32` |
| zone green | 133, 189, 114 | `#85BD72` |
| zone yellow | 244, 223, 85 | `#F4DF55` |
| zone red | 224, 86, 96 | `#E05660` |

## Text

- Value text centred on the arc centre x, sitting in the hollow:
  - `0.8%` bbox 109 x 38 px, baseline ~y 756
  - `115 ms` bbox 149 x 33 px, baseline ~y 1098
  - coloured to match the fill.
- Label above the arc, small caps-ish; gauge 2's label row is
  `RTT` (x 2033–2064) with `Last 15 minutes` in cyan to its right
  (x 2135–2259, y 834–848).

## Open question

The mock is ambiguous on one point: whether the thick band is (a) a grey track
with the value drawn as a fill on top, or (b) the track itself taking the colour
of the zone the value falls in. The measurements support (a) — gauge 1's band is
grey while its value is in the green zone. Confirm before building.
