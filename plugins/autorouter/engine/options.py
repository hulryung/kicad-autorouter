"""What the user sets for a run; persisted between runs."""
from dataclasses import dataclass, field, asdict
from typing import List, Optional


@dataclass
class RouteOptions:
    native_pitch_mm: float = 0.1          # routing grid
    native_via_cost: int = 25             # in grid cells of straight track
    native_allow_vias: bool = True
    native_retry: bool = True             # last-chance pass over failed connections
    native_fanout_vias: bool = True       # vias from pads of pour nets (GND, power) to their planes
    native_diff_pairs: bool = True        # coupled routing for +/- pairs at the net class pair width/gap
    native_stitch_vias: bool = True       # via stitching across multi-layer pour nets (GND)
    native_stitch_pitch_mm: float = 8.0
    native_power_pours: bool = True       # power nets with pours: pad vias to the plane and coarse stitching
    native_plane_nets: bool = True        # nets whose pour covers their pads: vias to the pour, no tracks
    native_shove: bool = False            # existing tracks may be ripped up and rerouted to make room
    native_max_expansions_k: int = 400    # A* search budget per connection, thousands
    native_time_limit_min: int = 10       # the whole run; 0 = no limit
    skip_nets: str = ""                   # comma-separated names or patterns, e.g. GND,+3V3,*_PLANE
    native_track_mode: str = "auto"       # auto | netclass | current | fixed
    native_track_width: int = 0           # nm, for fixed
    native_via_mode: str = "netclass"     # netclass | current | fixed
    native_via_diameter: int = 0          # nm, for fixed
    native_via_drill: int = 0
    ignore_net_classes: List[str] = field(default_factory=list)
    routable_layers: Optional[List[bool]] = None        # top to bottom; None = all
    horizontal_layers: Optional[List[bool]] = None      # top to bottom; None = engine default
    remove_existing_tracks: bool = False  # a per-run choice; the dialog never remembers it
    refill_zones: bool = True             # refill pours after routing

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RouteOptions":
        options = cls()
        for key, value in (data or {}).items():
            if hasattr(options, key):
                setattr(options, key, value)
        return options
