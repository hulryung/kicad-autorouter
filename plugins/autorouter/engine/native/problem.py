"""What the router works on, independent of pcbnew."""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .grid import Grid


@dataclass
class LayerInfo:
    index: int              # 0..n-1, top to bottom, among the routable layers
    kicad_id: int
    name: str
    horizontal: bool        # preferred direction
    routable: bool = True   # False: copper is modelled (vias must clear it) but no track may go there


@dataclass(frozen=True)
class NetRules:
    width: int              # nm
    clearance: int          # nm
    via_diameter: int
    via_drill: int

    @property
    def key(self) -> Tuple[int, int]:
        """Owner maps are shared by nets with the same inflation."""
        return (self.width, self.clearance)


@dataclass
class Cluster:
    """Copper of one net that is already connected together."""
    cells: Set[int] = field(default_factory=set)          # grid indices (all layers)
    copper: Set[int] = field(default_factory=set)         # cells inside the pads' copper, on their layers
    anchors: List[Tuple[int, int]] = field(default_factory=list)   # (cx, cy) pad centres, for the heuristic
    refs: Set[str] = field(default_factory=set)           # footprints whose pads are in here
    tight: bool = False                                   # holds a necked-down (fine-pitch) escape


@dataclass
class NetInfo:
    code: int
    name: str
    rules: NetRules
    clusters: List[Cluster] = field(default_factory=list)
    # grid cell -> (exit x, exit y, pad x, pad y) in nm: a route that starts or
    # ends on this cell continues off-grid to the exit point and then along
    # the pad's axis into the pad centre, the way a hand-routed escape does.
    endpoints: Dict[int, Tuple[int, int, int, int, int]] = field(default_factory=dict)   # + stub width
    route: bool = True              # False: pour net, only fanned out to its plane with vias
    partner: int = 0                # the other net of a differential pair, 0 if none
    pair_gap: int = 0               # nm, gap between the pair's tracks
    pair_width: int = 0             # nm, each track of the coupled run (0 = rules.width)
    layer_widths: Dict[int, int] = field(default_factory=dict)   # layer index -> nm (controlled impedance)
    group: str = ""                 # routing group (see engine/si.py)
    rank: int = 0                   # the group's place in the routing order (0 = first)


@dataclass
class Connection:
    net: int
    a: int                  # cluster indices in NetInfo.clusters
    b: int
    estimate: int           # cells, for ordering
    priority: int = 1       # 0 = touches a fine-pitch escape: routed first, like a designer would
    width: int = 0          # nm; 0 = the net's width
    neck: int = 0           # nm; >0: near fine-pitch ends the track is this narrow, then widens
    pair: int = 0           # >0: id shared by the two connections of a differential pair
    rank: int = 0           # the net's routing group's place in the order (0 = routed first)


@dataclass
class RoutedPath:
    net: int
    width: int
    # (layer index, x1, y1, x2, y2) in nm, or with a sixth item: a width that
    # differs from the path's width (necked-down pad escapes)
    segments: List[Tuple]
    vias: List[Tuple[int, int]]                      # (x, y) in nm
    via_diameter: int
    via_drill: int
    pid: int = 0                     # set by the router; rip-up removes paths by id


@dataclass
class ExistingPath:
    """Tracks and vias already on the board that the router may rip up and
    reroute (shove mode): one connected piece of one net, with the clusters
    it joins and the board items behind it."""
    path: RoutedPath
    joins: List[Tuple[int, int]]     # cluster index pairs this piece connects
    cluster: int                     # one cluster it touches (-1: a dangling piece)
    cells: List[int]                 # centreline cells, where a route may end on it
    items: list                      # the pcbnew items, for removal on rip-up
    pad_exits: List[List[int]] = field(default_factory=list)   # per pad it touches: the pad's exit cells


SCOPE_BOARD, SCOPE_SELECTION, SCOPE_SHEETS = "board", "selection", "sheets"
SIZE_AUTO, SIZE_NETCLASS, SIZE_CURRENT, SIZE_FIXED = "auto", "netclass", "current", "fixed"
POWER_PATTERNS = ["+*", "V*", "*VCC*", "*VDD*", "*VBUS*", "*VIN*", "*VOUT*", "*3V3*", "*5V*", "*12V*", "*24V*",
                  "*PWR*", "*POWER*", "*VBAT*", "GND*", "*GND"]


@dataclass
class NativeOptions:
    pitch_mm: float = 0.1
    # What to route: the whole board, only connections touching the given
    # footprints / nets (the editor selection), or footprints from the given
    # schematic sheets.
    scope: str = SCOPE_BOARD
    scope_refs: List[str] = field(default_factory=list)
    scope_nets: List[str] = field(default_factory=list)
    scope_sheets: List[str] = field(default_factory=list)
    # Track width and via size: per net from the net class (KiCad's default),
    # the editor's current track/via selection, or one fixed size (nm).
    neck_down: bool = True          # narrower escape stubs at fine-pitch pads
    escape_sides: Dict[Tuple[str, str], Tuple[int, int]] = field(default_factory=dict)
    # (reference, pad number) -> the exit point chosen for that pad by an earlier pass of the same job, so
    # the vias that pass dropped do not flip a later pass's choice of side
    dual_escapes: bool = False      # SMD pads get an exit on both ends (the search picks one); the open-net pass
    neck_length_mm: float = 1.5     # how far from a fine-pitch pin the track stays at the neck width
    fanout_vias: bool = True        # pads of nets with a copper zone get a via to that plane
    diff_pairs: bool = True         # route +/- pairs coupled, at the net class pair width and gap
    stitch_vias: bool = True        # via stitching across pour nets that span several layers, beside signal layer changes only
    stitch_pitch_mm: float = 4.0    # grid pitch of the candidate stitching spots (a via lands within 3/4 of it of a layer change)
    power_pours: bool = True        # routed nets with pours on 2+ layers: pad fanout vias and coarser stitching
    plane_nets: bool = True         # nets whose pour covers their pads are planes: vias to the pour, no tracks
    fanout_first: bool = True       # drop the plane nets' pad vias before the signals are routed (decoupling first)
    pours_only: bool = False          # the pipeline's last pass: fanout/stitch, no signal routing
    shove: bool = False             # existing (unlocked) tracks of routable nets may be ripped up and rerouted
    # UUIDs of tracks and vias an earlier pass of the same job dropped (the plane
    # nets' pad vias): the signal passes may rip them up when one walls in a
    # connection's only corridor; the pour pass places them again where room is left
    movable_uuids: List[str] = field(default_factory=list)
    locked_uuids: List[str] = field(default_factory=list)    # board items shove mode may not move (a coupled pair's lines)
    keepouts: List[Tuple[int, int, int, int]] = field(default_factory=list)   # nm rects blocked for this run
    # ...only where the pour on the pad's layer does not reach it, or reaches it on an island with no
    # through-hole pin or via of the net (a designer lets the pour join the pads it covers; the pour
    # pass refills the pours first, so an island the new tracks cut off gets its via)
    fanout_only_unconnected: bool = True
    track_mode: str = SIZE_AUTO
    power_patterns: List[str] = field(default_factory=lambda: list(POWER_PATTERNS))
    track_width: int = 0
    via_mode: str = SIZE_NETCLASS
    via_diameter: int = 0
    via_drill: int = 0
    via_cost: int = 25                 # in straight cells
    allow_vias: bool = True
    flat_first: bool = True            # a route with vias is tried on one outer layer alone; the flat route wins when nearly direct
    max_expansions: int = 400_000
    fine_budget_s: Optional[float] = None   # the finer second pass's own time budget; None = 60 s + 20 s per
                                            # open connection, 0 = none (benchmarks: same input, same result)
    conn_time_s: float = 20.0          # one connection's searches (a rip-up attempt: twice); 0 = no limit
    time_limit_s: float = 600.0        # the whole job (all passes); 0 = no limit. Rip-up on a big board
                                       # can otherwise grind for hours with the counter standing still
    retry_failed: bool = True
    rip_up: bool = True                # rip up and reroute paths that block a failed connection
    rip_rounds: int = 8                # how many rip-up rounds over the failed connections
    rip_limit: int = 5                 # how often one connection may be ripped up
    fine_shove: bool = True            # the finer second pass may rip up and reroute the first pass's tracks
    skip_nets: List[str] = field(default_factory=list)       # names or fnmatch patterns
    ignore_net_classes: List[str] = field(default_factory=list)
    routable_layers: Optional[List[bool]] = None             # over the board's copper stack
    horizontal_layers: Optional[List[bool]] = None
    turn_cost: int = 2
    # Design intent (engine/si.py): the order of the routing groups, extra name
    # patterns per group, per-net controlled-impedance sizes and length groups.
    route_order: List[str] = field(default_factory=list)                 # empty = si.DEFAULT_ORDER
    route_groups: Dict[str, List[str]] = field(default_factory=dict)
    impedance: Dict[str, dict] = field(default_factory=dict)             # net name -> sizes per KiCad layer id
    length_groups: List[dict] = field(default_factory=list)              # tuned after routing
    tune_lengths: bool = True
    kicad_tuning_patterns: bool = True  # meanders become KiCad tuning patterns (the length tuner can retune them)
    pth_hole_clearance_mm: float = 0.0  # plated pad holes to other copper, when stricter than the hole clearance (fab rules)
    tune_rounded: bool = True          # meander corners are arc fillets (KiCad's round style), else 45-degree chamfers
    cut_acute: bool = True             # no corner sharper than 90 degrees: cut it with a 0/45/90 run
    outward_exits: bool = True         # two-pin parts may also be left straight out of their ends
    direct_links: bool = True          # close pads with a clear line between them: a direct track first
    direct_max_mm: float = 5.0         # ...up to this far apart
    pair_min_span_mm: float = 3.0      # pair ends closer than this: coupling is unnecessary, connect directly
    pair_uncoupled_max_mm: float = 5.0  # a pair connected directly whose nets are no longer than this: skew waived
    pair_direct_via_factor: int = 3    # a pair connected directly (an exception) pays this many times the via cost
    pair_via_swap: bool = True         # pins in opposite order at the two ends: one line crosses under the other through two vias
    protect_groups: bool = True        # rip-up may not move a pair/memory/high-speed route for a later group


@dataclass
class RoutingProblem:
    grid: Grid
    layers: List[LayerInfo]
    owners: Dict[Tuple[int, int], "array"]        # rules.key -> owner map
    via_block: bytearray                          # per 2-D cell: 1 = no via here
    nets: Dict[int, NetInfo]
    connections: List[Connection]
    options: NativeOptions
    stats: Dict[str, float] = field(default_factory=dict)
    fanout: List[Tuple[int, int]] = field(default_factory=list)   # (net, exit cell) wanting a via to a plane
    fanout_alt: Dict[int, List[int]] = field(default_factory=dict)   # exit cell -> the pad's other exits to try
    fanout_goals: Dict[int, Set[int]] = field(default_factory=dict)  # exit cell -> cells on the net's pour, within reach
    stitch: Dict[int, List[Tuple[int, int]]] = field(default_factory=dict)   # net -> candidate (cx, cy) via spots
    board_vias: Dict[int, List[Tuple[int, int]]] = field(default_factory=dict)   # net -> via centres (nm) on the board already
    escape_sides: Dict[Tuple[str, str], Tuple[int, int]] = field(default_factory=dict)   # see NativeOptions
    hole_to_hole: int = 0                         # nm, board's hole-to-hole minimum
    max_via_drill: int = 0                        # nm, the largest via drill any net uses
    safety: int = 0                               # nm added to every obstacle radius
    existing: List[ExistingPath] = field(default_factory=list)   # shove mode: movable board copper
    via_guard: Optional["array"] = None           # per 2-D cell: net whose pad outlet a via here would block (-1: several)
    lane_guard: Optional["array"] = None          # per cell: net whose pad outlet this is; other centrelines keep out
