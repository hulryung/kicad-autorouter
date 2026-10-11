"""The Freerouting engine: which release, where its jar is, and the command line."""
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, List, Optional

from .download import download
from .paths import data_dir


@dataclass(frozen=True)
class FreeroutingRelease:
    version: str
    sha256: str
    min_java: int            # the jar's class-file level

    @property
    def jar_name(self) -> str:
        return f"freerouting-{self.version}.jar"

    @property
    def url(self) -> str:
        return (f"https://github.com/freerouting/freerouting/releases/download/"
                f"v{self.version}/{self.jar_name}")


# Pinned so every user runs the same engine the plugin was tested with.
# Freerouting 2.5.0 is compiled for Java 25 (class file 69). Its native CLI
# builds are not used: 2.5.0's crash on board serialization at the first pass.
RELEASE = FreeroutingRelease(
    version="2.5.0",
    sha256="f6f51bb02245e8e717f9359bd260cc9c5c0b1bc0acc8b7cb2cd5b8ffeb5de3c7",
    min_java=25,
)

UPDATE_STRATEGIES = ("greedy", "global", "hybrid")
SELECTION_STRATEGIES = ("prioritized", "sequential", "random")


ENGINE_BUILTIN, ENGINE_FREEROUTING = "builtin", "freerouting"


@dataclass
class RouteOptions:
    """Everything the user can set for a run; persisted between runs.

    The engine choice and the native_* fields drive the built-in router; the
    rest configure Freerouting. Layer and net-class choices apply to both.
    """
    engine: str = ENGINE_BUILTIN
    native_pitch_mm: float = 0.1          # routing grid
    native_via_cost: int = 25             # in grid cells of straight track
    native_allow_vias: bool = True
    native_retry: bool = True             # second pass over failed connections
    native_fanout_vias: bool = True       # vias from pads of pour nets (GND, power) to their planes
    native_diff_pairs: bool = True        # coupled routing for +/- pairs at the net class pair width/gap
    native_max_expansions_k: int = 400    # A* search budget per connection, thousands
    skip_nets: str = ""                   # comma-separated names or patterns, e.g. GND,+3V3,*_PLANE
    native_track_mode: str = "auto"       # auto | netclass | current | fixed
    native_track_width: int = 0           # nm, for fixed
    native_via_mode: str = "netclass"     # netclass | current | fixed
    native_via_diameter: int = 0          # nm, for fixed
    native_via_drill: int = 0
    max_passes: int = 100                 # 0 = unlimited (Freerouting's own default)
    optimizer_threads: int = 0            # 0 = Freerouting's default (cores - 1); -1 = no optimizer
    improvement_threshold: float = 2.5    # optimizer stop threshold, percent
    via_costs: int = 50
    plane_via_costs: int = 5
    start_ripup_costs: int = 100
    fanout: bool = True
    update_strategy: str = "greedy"
    selection_strategy: str = "prioritized"
    hybrid_ratio: str = "1:1"
    ignore_net_classes: List[str] = field(default_factory=list)
    routable_layers: Optional[List[bool]] = None        # top to bottom; None = all
    horizontal_layers: Optional[List[bool]] = None      # top to bottom; None = engine default
    remove_existing_tracks: bool = False
    refill_zones: bool = True             # refill pours after routing
    max_heap_mb: int = 0                  # JVM -Xmx; 0 = JVM default (1/4 of RAM)
    extra_args: str = ""                  # anything else, verbatim

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RouteOptions":
        options = cls()
        for key, value in (data or {}).items():
            if hasattr(options, key):
                setattr(options, key, value)
        return options


def managed_jar_path(release: FreeroutingRelease = RELEASE) -> Path:
    return data_dir() / "freerouting" / release.jar_name


def find_jar(preferred: Optional[str] = None, release: FreeroutingRelease = RELEASE) -> Optional[Path]:
    """A usable jar: the one from the settings if it exists, else the managed download."""
    if preferred and os.path.isfile(preferred):
        return Path(preferred)
    managed = managed_jar_path(release)
    return managed if managed.is_file() else None


def install_jar(progress: Optional[Callable[[str, int, Optional[int]], None]] = None,
                cancelled: Optional[Callable[[], bool]] = None,
                release: FreeroutingRelease = RELEASE) -> Path:
    target = managed_jar_path(release)
    label = f"Downloading {release.jar_name}"
    return download(release.url, target, release.sha256,
                    progress=(lambda done, total: progress(label, done, total)) if progress else None,
                    cancelled=cancelled)


def dsn_net_class_name(kicad_name: str) -> str:
    """KiCad's DSN export renames the Default net class."""
    return "kicad_default" if kicad_name == "Default" else kicad_name


def _bools(values: List[bool]) -> str:
    return ",".join("true" if v else "false" for v in values)


def build_command(java: str, jar: Path, options: RouteOptions, dsn: Path, ses: Path,
                  host_version: str = "", user_data: Optional[Path] = None) -> List[str]:
    """The headless Freerouting command line for one run."""
    cmd = [java, "-Djava.awt.headless=true"]
    if options.max_heap_mb and options.max_heap_mb > 0:
        cmd.append(f"-Xmx{int(options.max_heap_mb)}m")
    cmd += ["-jar", str(jar),
            "-de", str(dsn), "-do", str(ses),
            "--gui.enabled=false",
            "--logging.file.enabled=false",
            "-da",
            "-host", f"KiCad-Autorouter {host_version}".strip()]
    if user_data is not None:
        cmd.append(f"--user_data_path={user_data}")
    cmd.append(f"--router.autorouter.max_passes={max(0, int(options.max_passes))}")
    if options.optimizer_threads < 0:
        cmd += ["-mt", "0"]
    elif options.optimizer_threads > 0:
        cmd += ["-mt", str(int(options.optimizer_threads))]
    cmd.append(f"--router.optimizer.improvement_threshold={float(options.improvement_threshold):g}")
    cmd.append(f"--router.via_costs={int(options.via_costs)}")
    cmd.append(f"--router.plane_via_costs={int(options.plane_via_costs)}")
    cmd.append(f"--router.start_ripup_costs={int(options.start_ripup_costs)}")
    if not options.fanout:
        cmd.append("--router.fanout.enabled=false")
    if options.update_strategy in UPDATE_STRATEGIES and options.update_strategy != "greedy":
        cmd += ["-us", options.update_strategy]
        if options.update_strategy == "hybrid" and options.hybrid_ratio:
            cmd += ["-hr", options.hybrid_ratio]
    if options.selection_strategy in SELECTION_STRATEGIES and options.selection_strategy != "prioritized":
        cmd += ["-is", options.selection_strategy]
    classes = [dsn_net_class_name(c) for c in options.ignore_net_classes if c]
    if classes:
        cmd += ["-inc", ",".join(classes)]
    if options.routable_layers and not all(options.routable_layers):
        cmd.append(f"--router.layers.routable={_bools(options.routable_layers)}")
    if options.horizontal_layers:
        cmd.append(f"--router.layers.preferred_direction_horizontal={_bools(options.horizontal_layers)}")
    if options.extra_args.strip():
        import shlex
        cmd += shlex.split(options.extra_args, posix=(os.name != "nt"))
    return cmd
