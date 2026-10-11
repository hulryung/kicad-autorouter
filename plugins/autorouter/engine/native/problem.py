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
    anchors: List[Tuple[int, int]] = field(default_factory=list)   # (cx, cy) pad centres, for the heuristic
    refs: Set[str] = field(default_factory=set)           # footprints whose pads are in here


@dataclass
class NetInfo:
    code: int
    name: str
    rules: NetRules
    clusters: List[Cluster] = field(default_factory=list)


@dataclass
class Connection:
    net: int
    a: int                  # cluster indices in NetInfo.clusters
    b: int
    estimate: int           # cells, for ordering


@dataclass
class RoutedPath:
    net: int
    width: int
    segments: List[Tuple[int, int, int, int, int]]   # (layer index, x1, y1, x2, y2) in nm
    vias: List[Tuple[int, int]]                      # (x, y) in nm
    via_diameter: int
    via_drill: int


SCOPE_BOARD, SCOPE_SELECTION, SCOPE_SHEETS = "board", "selection", "sheets"
SIZE_NETCLASS, SIZE_CURRENT, SIZE_FIXED = "netclass", "current", "fixed"


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
    track_mode: str = SIZE_NETCLASS
    track_width: int = 0
    via_mode: str = SIZE_NETCLASS
    via_diameter: int = 0
    via_drill: int = 0
    via_cost: int = 50                 # in straight cells
    allow_vias: bool = True
    max_expansions: int = 400_000
    retry_failed: bool = True
    skip_nets: List[str] = field(default_factory=list)       # names or fnmatch patterns
    ignore_net_classes: List[str] = field(default_factory=list)
    routable_layers: Optional[List[bool]] = None             # over the board's copper stack
    horizontal_layers: Optional[List[bool]] = None
    turn_cost: int = 2


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
    hole_to_hole: int = 0                         # nm, board's hole-to-hole minimum
    max_via_drill: int = 0                        # nm, the largest via drill any net uses
