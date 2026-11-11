# Routing policies

The router's habits are taken from how boards are laid out by hand. This page
lists them, says which are in the code, and records how each was checked.

A policy goes in only when it holds up on real boards: the same board is
routed by a designer and by the router, both are measured with
`scripts/conventions.py`, and the benchmark boards must not lose connections
or gain DRC errors.

## How a policy is checked

```
<KiCad python3> scripts/conventions.py designer.kicad_pcb
<KiCad python3> scripts/improve_bench.py --reference designer.kicad_pcb --plugins plugins --out out
<KiCad python3> scripts/conventions.py out/routed.kicad_pcb
```

Boards used so far: the CM5 edge gateway (four layers, QFN, a dense
two-row module connector) and three KiCad demo boards with a designer's
routing: `kit-dev-coldfire-xilinx_5213` (TQFP-100 and TQFP-144 at 0.5 mm),
`stickhub` (two layers) and `video`.

## IC pins (QFP, QFN, SOIC)

| Policy | State |
| --- | --- |
| A pin leaves outward on its own layer, also when the net's other pad lies across the chip. It turns under the body only when the way out is nearly shut, or for pairs, memory and clock nets whose target is well behind the pad. | In the code (escape planning) |
| The track runs straight on past the pad before it turns: 0.3 mm more than clearance needs, when that lane is free. | In the code since 0.5.6 |
| A coarse body's pin (SOIC) is entered from its chosen side only; the half of the pad behind it is closed. | In the code |
| The room under a coarse body is for its own pins' vias, not a channel along the inside of a pin row. | In the code (a cost per cell) |
| The first 0.6 mm past a pin's exit is kept for that pin; a fine-pitch pin's via site at its exit is closed to other nets. | In the code |
| A pin's exit never lands on another pad of the same net on the same part. | In the code |
| Pins of a dense two-row connector alternate between the outside and the channel, with vias at two depths. | In the code |

Measured on the ColdFire board (0.5 mm TQFP pins with a track):

| | Designer | 0.5.5 | 0.5.6 |
| --- | --- | --- | --- |
| Leaves outward | 61 % | 53 % | 56 % |
| Leaves under the body | 31 % | 41 % | 35 % |
| Straight run of an outward exit (median) | 0.95 mm | 0.46 mm | 0.64 mm |

and on the CM5 board:

| | Designer | 0.5.5 | 0.5.6 |
| --- | --- | --- | --- |
| Straight run of an outward exit (median) | 0.86 mm | 0.43 mm | 0.70 mm |
| First via, distance along the track (median) | 0.73 mm | 0.42 mm | 0.66 mm |

## Passives and small parts

| Policy | State |
| --- | --- |
| A two-pin part may be left straight out of its end, not only along the pad's long axis. | In the code |
| A pad's exit lane is checked against neighbouring copper exactly, with a 45-degree jog when the grid cell is off the pad axis. | In the code |
| Track width necks down at pads closer together than the track with its clearance. | In the code |

## Vias

| Policy | State |
| --- | --- |
| A plane pin gets its own via next to the pad (fanout), on the side with room. Big exposed pads get a 2 x 2 array. | In the code |
| No via in another pin's outlet or via site. | In the code |
| A via that joins nothing is removed with its stub. | In the code |
| A via keeps a mask dam's width (0.15 mm) from its own SMD pad. | Not in the code: see below |

## Copper and mask

| Policy | State |
| --- | --- |
| No track or via under an opening drawn on a solder mask layer (a logo, an exposed strip), unless the opening sits over that net's own pad or pour. KiCad's DRC reports such an opening when it uncovers two nets. | In the code since 0.5.6 |
| Copper drawings and text are obstacles. | In the code |
| No track on an inner ground plane layer by default. | In the code |
| Routes are drawn at 0, 45 and 90 degrees, with acute corners cut and junctions welded to shared end points. | In the code |
| A route ends at the edge of its net's pour instead of crossing the fill. | In the code |
| Power rails are widened after routing where the board has room. | In the code |

## Tried and not adopted

| Idea | Result |
| --- | --- |
| Close the body-side half of every IC pin's pad (as for SOICs), so a pin planned outward cannot be left from under the chip. | CM5 0 -> 2 unconnected; ColdFire 22 -> 27. |
| A cost on the strip just past the body-side end of an outward-leaving pin. | CM5 unchanged; ColdFire 22 -> 26 unconnected, pins leaving under the body 41 -> 38 %. |
| Fanout vias 0.15 mm off their own pad when a spot that far is free. | Vias within 0.15 mm of their pad 15 -> 8 % on CM5, but 0 -> 1 unconnected there and 22 -> 28 on ColdFire. |
| A 0.2 mm straight neck instead of 0.3 mm. | ColdFire 22 -> 26 unconnected (0.3 mm: 24). |

## Seen in the measurements, no policy yet

- Designers differ on joining neighbouring pins of one net pad to pad
  (94 % on `video`, about 10 % elsewhere).
- Tracks leaving a chip resistor or capacitor toward its other pad, and other
  nets' tracks under such a part, are as common on the designers' boards as
  in the router's results.
- The ColdFire and `video` boards do not finish within the ten-minute
  budget (22 and 194 connections left); that is a search-speed limit, not a
  convention.
