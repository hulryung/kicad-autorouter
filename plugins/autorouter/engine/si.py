"""Design intent beyond the net classes: controlled impedance, length
matching and the order in which groups of nets are routed.

The intent is kept per board in "<board>.autorouter.json" next to the board
file (the dialog's Signal integrity tab edits it), turned into per-net track
sizes with the stackup's impedance formulas (engine/stackup.py), and can be
written into KiCad itself: a net class per impedance class (track width,
diff pair width and gap, which KiCad's interactive differential pair router
uses) and DRC rules for the per-layer widths, pair gap, skew and lengths
(which KiCad's DRC and its length tuner check against).
"""
import copy
import fnmatch
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import stackup as stk

MM = 1_000_000

# Routing groups, in the default order: what a designer routes first.
GROUP_PAIRS, GROUP_MEMORY, GROUP_HIGH_SPEED, GROUP_CLOCK, GROUP_POWER, GROUP_SIGNAL = (
    "pairs", "memory", "high_speed", "clock", "power", "signal")
DEFAULT_ORDER = [GROUP_PAIRS, GROUP_MEMORY, GROUP_HIGH_SPEED, GROUP_CLOCK, GROUP_POWER, GROUP_SIGNAL]
GROUP_LABELS = {
    GROUP_PAIRS: "Differential pairs",
    GROUP_MEMORY: "Memory buses (nets on DDR/SDRAM/PSRAM/flash parts)",
    GROUP_HIGH_SPEED: "High speed (USB, Ethernet, MIPI..., impedance or length rules)",
    GROUP_CLOCK: "Clocks and crystals",
    GROUP_POWER: "Power (routed power nets, not planes)",
    GROUP_SIGNAL: "Everything else",
}
# Name patterns per group (case-insensitive fnmatch on the net name without
# its sheet path). Memory and pairs are also found from the parts and the
# pair naming; impedance and length rules put their nets in high_speed.
GROUP_PATTERNS: Dict[str, List[str]] = {
    GROUP_MEMORY: ["DDR*", "*_DDR*", "SDRAM*", "*SDRAM*", "DQ*", "DQS*", "*_DQ*", "*_DQS*", "DM[0-9]*",
                   "*_DM[0-9]*", "FMC_*", "FSMC_*", "*_BA[0-9]", "BA[0-9]", "*CKE*", "*ODT*", "*_RAS*",
                   "*_CAS*", "QSPI*", "OSPI*", "*_QSPI*", "*_OSPI*", "*HYPERBUS*", "*PSRAM*", "SPI_FLASH*"],
    GROUP_HIGH_SPEED: ["USB*", "*_USB*", "*USB_D*", "ETH*", "*_ETH*", "RGMII*", "RMII*", "*MDI*", "HDMI*",
                       "MIPI*", "*CSI*", "*DSI*", "PCIE*", "*_PCIE*", "SATA*", "LVDS*", "*SERDES*", "SDIO*",
                       "*SD_D*", "*SD_CMD*", "*EMMC*"],
    GROUP_CLOCK: ["*CLK*", "*SCK", "*SCLK*", "*XTAL*", "*OSC*", "*XIN*", "*XOUT*", "*MCO*", "*REFCLK*"],
}
# Memory parts, by value or footprint/symbol name.
MEMORY_PARTS = ["DDR", "SDRAM", "DRAM", "SRAM", "PSRAM", "HYPERRAM", "LPDDR", "EMMC", "FLASH",
                "MT41", "MT47", "MT48", "MT53", "MT25", "IS42", "IS43", "IS45", "IS46", "IS61", "IS62", "IS66",
                "IS25", "W97", "W98", "W25", "W29", "W9825", "AS4C", "K4B", "K4A", "H5T", "NT5", "MX25", "MX66",
                "GD25", "S25FL", "S27K", "N25Q", "AT25", "APS6404", "LY68", "ESP-PSRAM"]


@dataclass
class ImpedanceClass:
    name: str
    nets: List[str] = field(default_factory=list)       # fnmatch patterns on net names
    single_ohm: float = 0.0                              # single-ended target, 0 = none
    diff_ohm: float = 0.0                                # differential target, 0 = none
    gap_mm: float = 0.0                                  # pair gap; 0 = chosen with the width
    skew_mm: float = 0.1                                 # pairs: P/N length difference allowed (0 = no tuning)


@dataclass
class LengthGroup:
    name: str
    nets: List[str] = field(default_factory=list)       # fnmatch patterns
    tolerance_mm: float = 1.0                            # +- around the target
    target_mm: float = 0.0                               # 0 = the longest member


@dataclass
class SiConfig:
    stackup: str = stk.BOARD
    impedance: List[ImpedanceClass] = field(default_factory=list)
    length_groups: List[LengthGroup] = field(default_factory=list)
    route_order: List[str] = field(default_factory=lambda: list(DEFAULT_ORDER))
    group_patterns: Dict[str, List[str]] = field(default_factory=dict)   # extra patterns per group
    tune_lengths: bool = True
    kicad_patterns: bool = True                  # meanders handed to KiCad's length tuner as tuning patterns
    fab: str = ""                                # fabrication rules template (engine/fab.py); "" = the board's own
    rounded: bool = True                         # meander corners as arc fillets (else 45-degree chamfers)
    pair_min_span_mm: float = 3.0                # pair ends closer than this: connected directly, not coupled
    pair_uncoupled_max_mm: float = 5.0           # uncoupled length a pair may have (KiCad rule); shorter pairs: no skew

    @classmethod
    def from_dict(cls, d: dict) -> "SiConfig":
        d = dict(d or {})
        cfg = cls()
        cfg.stackup = str(d.get("stackup") or stk.BOARD)
        cfg.impedance = [ImpedanceClass(**{k: v for k, v in x.items() if k in ImpedanceClass.__dataclass_fields__})
                         for x in d.get("impedance") or []]
        cfg.length_groups = [LengthGroup(**{k: v for k, v in x.items() if k in LengthGroup.__dataclass_fields__})
                             for x in d.get("length_groups") or []]
        order = [g for g in d.get("route_order") or [] if g in DEFAULT_ORDER]
        cfg.route_order = order + [g for g in DEFAULT_ORDER if g not in order]
        cfg.group_patterns = {k: list(v) for k, v in (d.get("group_patterns") or {}).items()}
        cfg.tune_lengths = bool(d.get("tune_lengths", True))
        cfg.kicad_patterns = bool(d.get("kicad_patterns", True))
        cfg.fab = str(d.get("fab") or "")
        cfg.rounded = bool(d.get("rounded", True))
        cfg.pair_min_span_mm = float(d.get("pair_min_span_mm", 3.0))
        cfg.pair_uncoupled_max_mm = float(d.get("pair_uncoupled_max_mm", 5.0))
        return cfg

    def to_dict(self) -> dict:
        return asdict(self)


def config_path(board) -> Optional[Path]:
    name = board.GetFileName() if board is not None else ""
    if not name:
        return None
    p = Path(name)
    return p.with_name(p.stem + ".autorouter.json")


def load(board) -> SiConfig:
    p = config_path(board)
    if p is None or not p.exists():
        return SiConfig()
    try:
        return SiConfig.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return SiConfig()


def save(board, cfg: SiConfig) -> Optional[Path]:
    p = config_path(board)
    if p is None:
        return None
    p.write_text(json.dumps(cfg.to_dict(), indent=2) + "\n", encoding="utf-8")
    return p


def matches(name: str, patterns: List[str]) -> bool:
    """fnmatch, case-insensitive, on the full name or the name without its sheet path."""
    short = name.rsplit("/", 1)[-1]
    for pat in patterns:
        p = pat.upper()
        if fnmatch.fnmatchcase(name.upper(), p) or fnmatch.fnmatchcase(short.upper(), p):
            return True
    return False


def is_memory_part(fp) -> bool:
    text = " ".join(str(x) for x in (fp.GetValue(), fp.GetFPIDAsString())).upper()
    return any(k in text for k in MEMORY_PARTS)


# ---- the plan ----------------------------------------------------------------

@dataclass
class SiPlan:
    stackup: Optional[stk.Stackup]
    source: str
    # net name -> {"class", "widths": {kicad layer id: nm}, "gaps": {...}, "skew": nm, "ohm": {...}}
    impedance: Dict[str, dict] = field(default_factory=dict)
    # [{"name", "nets": [names], "tolerance": nm, "target": nm (0 = longest)}]
    length_groups: List[dict] = field(default_factory=list)
    lines: List[str] = field(default_factory=list)      # a readable report
    warnings: List[str] = field(default_factory=list)


def copper_ids(board) -> List[int]:
    """KiCad layer ids of the copper layers, top to bottom."""
    import pcbnew
    return list(board.GetEnabledLayers().CuStack())[:board.GetCopperLayerCount()] if board is not None else []


def plan(board, cfg: SiConfig, net_names: Optional[List[str]] = None) -> SiPlan:
    """Per-net sizes and length groups for a board (net_names: the board's nets)."""
    from .native.extract import board_sizes, pair_partner
    st, source = stk.resolve(board, cfg.stackup)
    out = SiPlan(st, source)
    if net_names is None:
        net_names = [n.GetNetname() for c, n in board.GetNetsByNetcode().items() if c > 0]
    names = set(net_names)
    sizes = board_sizes(board)
    min_w = max(sizes["min_track_width"], 90_000)          # JLCPCB multilayer minimum: 0.09 mm
    min_gap = max(sizes["min_clearance"], 90_000)
    ids = copper_ids(board)
    if cfg.impedance and st is None:
        out.warnings.append(source)
    if st is not None:
        out.lines += stk.describe(st)
        if st.layers != len(ids):
            out.warnings.append(source)
    for zc in cfg.impedance:
        members = sorted(n for n in names if matches(n, zc.nets))
        if not members:
            out.warnings.append(f"impedance class {zc.name}: no net matches {', '.join(zc.nets)}")
            continue
        if st is None:
            continue
        widths, gaps, ohms = {}, {}, {}
        for i, lid in enumerate(ids):
            si = min(i, st.layers - 1) if i < len(ids) - 1 else st.layers - 1      # bottom maps to bottom
            ts = stk.size_for(st, si, single_ohm=zc.single_ohm, diff_ohm=zc.diff_ohm,
                              min_width_nm=min_w, min_gap_nm=min_gap, gap_nm=int(zc.gap_mm * MM))
            widths[lid], gaps[lid], ohms[lid] = ts.width_nm, ts.gap_nm, ts.ohm
            if ts.note:
                out.warnings.append(f"{zc.name} on L{i + 1}: {ts.note} ({ts.ohm:.0f} ohm)")
        target = f"{zc.diff_ohm:g} ohm differential" if zc.diff_ohm else f"{zc.single_ohm:g} ohm"
        out.lines.append(f"{zc.name} ({target}): " + ", ".join(
            f"L{i + 1} {widths[l] / MM:.3f}" + (f"/{gaps[l] / MM:.3f}" if zc.diff_ohm else "")
            + f" mm = {ohms[l]:.1f}" for i, l in enumerate(ids)) + f"; {len(members)} nets")
        for n in members:
            out.impedance[n] = {"class": zc.name, "widths": dict(widths), "gaps": dict(gaps) if zc.diff_ohm else {},
                                "ohm": dict(ohms), "diff": bool(zc.diff_ohm)}
        if zc.diff_ohm and zc.skew_mm > 0:
            done = set()
            for n in members:
                other = pair_partner(n, names)
                if other and other in members and n not in done:
                    done |= {n, other}
                    out.length_groups.append({"name": f"{zc.name} skew {n.rsplit('/', 1)[-1]}",
                                              "nets": sorted([n, other]),
                                              "tolerance": int(zc.skew_mm * MM), "target": 0, "pair": True})
    for lg in cfg.length_groups:
        members = sorted(n for n in names if matches(n, lg.nets))
        if len(members) < 2 and not lg.target_mm:
            out.warnings.append(f"length group {lg.name}: {len(members)} net(s) match")
            continue
        out.length_groups.append({"name": lg.name, "nets": members, "tolerance": int(lg.tolerance_mm * MM),
                                  "target": int(lg.target_mm * MM), "pair": False})
        out.lines.append(f"length group {lg.name}: {len(members)} nets, "
                         + (f"target {lg.target_mm:g} mm" if lg.target_mm else "match the longest")
                         + f" +- {lg.tolerance_mm:g} mm")
    return out


def apply_to_options(options, cfg: SiConfig, si_plan: Optional[SiPlan]):
    """Copy the intent into a NativeOptions (returns a new one)."""
    o = copy.copy(options)
    o.route_order = list(cfg.route_order)
    o.route_groups = {k: list(v) for k, v in cfg.group_patterns.items()}
    o.impedance = dict(si_plan.impedance) if si_plan else {}
    o.length_groups = list(si_plan.length_groups) if (si_plan and cfg.tune_lengths) else []
    o.kicad_tuning_patterns = cfg.kicad_patterns
    from . import fab
    rules = fab.get(cfg.fab)
    o.pth_hole_clearance_mm = rules.pth_hole_clearance if rules else 0.0
    o.tune_rounded = cfg.rounded
    o.pair_min_span_mm = cfg.pair_min_span_mm
    o.pair_uncoupled_max_mm = cfg.pair_uncoupled_max_mm
    return o


# ---- into KiCad --------------------------------------------------------------

def apply_net_classes(board, cfg: SiConfig, si_plan: SiPlan) -> List[str]:
    """A KiCad net class per impedance class: track width and diff pair
    width/gap for the top layer (the per-layer widths go in the DRC rules),
    clearance and vias from the Default class; one pattern assignment per
    pattern. KiCad's interactive router and diff pair router then use the
    same sizes. Returns what was done."""
    import pcbnew
    ds = board.GetDesignSettings()
    ns = ds.m_NetSettings
    default = ns.GetDefaultNetclass()
    done = []
    ids = copper_ids(board)
    for zc in cfg.impedance:
        nets = [n for n, z in si_plan.impedance.items() if z["class"] == zc.name]
        if not nets or not ids:
            continue
        z = si_plan.impedance[nets[0]]
        top = ids[0]
        nc = pcbnew.NETCLASS(zc.name)
        nc.SetClearance(default.GetClearance())
        nc.SetViaDiameter(default.GetViaDiameter())
        nc.SetViaDrill(default.GetViaDrill())
        nc.SetTrackWidth(z["widths"][top])
        if z["diff"]:
            nc.SetDiffPairWidth(z["widths"][top])
            nc.SetDiffPairGap(z["gaps"][top])
            nc.SetDiffPairViaGap(z["gaps"][top])
        else:
            nc.SetDiffPairWidth(default.GetDiffPairWidth())
            nc.SetDiffPairGap(default.GetDiffPairGap())
        nc.SetDescription(f"Autorouter: {zc.diff_ohm or zc.single_ohm:g} ohm "
                          f"{'differential' if zc.diff_ohm else 'single-ended'} ({si_plan.source})")
        ns.SetNetclass(zc.name, nc)
        for pat in zc.nets:
            ns.SetNetclassPatternAssignment(pat, zc.name)
        done.append(f"net class {zc.name}: {z['widths'][top] / MM:.3f} mm"
                    + (f", gap {z['gaps'][top] / MM:.3f} mm" if z["diff"] else "") + f" ({len(nets)} nets)")
    try:
        ns.ClearAllCaches()
        ns.RecomputeEffectiveNetclasses()
    except Exception:
        pass
    try:
        board.SynchronizeNetsAndNetClasses(False)
    except Exception:
        pass
    return done


BEGIN = "# --- autorouter begin: generated by the Autorouter plugin, edits inside are overwritten ---"
END = "# --- autorouter end ---"


def _mm(nm: int) -> str:
    return f"{nm / MM:.4f}".rstrip("0").rstrip(".") + "mm"


def _net_condition(nets: List[str]) -> str:
    return " || ".join(f"A.NetName == '{n}'" for n in nets)


def drc_rules(board, cfg: SiConfig, si_plan: SiPlan, lengths: Optional[Dict[str, int]] = None,
              pair_exceptions: Optional[List[dict]] = None) -> str:
    """KiCad custom rules for the intent: per-layer track width (and pair
    gap) of each impedance class, the skew of each pair, the length window of
    each length group (targets of "match the longest" groups from lengths)."""
    import pcbnew
    out = [BEGIN]
    ids = copper_ids(board)
    for zc in cfg.impedance:
        nets = [n for n, z in si_plan.impedance.items() if z["class"] == zc.name]
        if not nets:
            continue
        z = si_plan.impedance[nets[0]]
        for lid in ids:
            w = z["widths"][lid]
            lname = board.GetLayerName(lid)
            tol = max(5_000, w // 20)
            out.append(f'(rule "{zc.name} width {lname}"\n  (layer "{lname}")\n'
                       f"  (condition \"A.NetClass == '{zc.name}'\")\n"
                       f"  (constraint track_width (min {_mm(w - tol)}) (opt {_mm(w)}) (max {_mm(w + tol)})))")
            if z["diff"]:
                g = z["gaps"][lid]
                out.append(f'(rule "{zc.name} pair gap {lname}"\n  (layer "{lname}")\n'
                           f"  (condition \"A.NetClass == '{zc.name}' && A.inDiffPair('*')\")\n"
                           f"  (constraint diff_pair_gap (min {_mm(g - tol)}) (opt {_mm(g)}) (max {_mm(g + 3 * tol)}))\n"
                           f"  (constraint diff_pair_uncoupled (max {_mm(int(cfg.pair_uncoupled_max_mm * MM))})))")
        if zc.diff_ohm and zc.skew_mm > 0:
            out.append(f'(rule "{zc.name} skew"\n'
                       f"  (condition \"A.NetClass == '{zc.name}' && A.inDiffPair('*')\")\n"
                       f"  (constraint skew (max {_mm(int(zc.skew_mm * MM))})))")
    for g in si_plan.length_groups:
        if g.get("pair"):
            continue
        target = g["target"] or max((lengths or {}).get(n, 0) for n in g["nets"])
        if not target:
            continue
        tol = g["tolerance"]
        out.append(f'(rule "length {g["name"]}"\n  (condition "{_net_condition(g["nets"])}")\n'
                   f"  (constraint length (min {_mm(target - tol)}) (opt {_mm(target)}) (max {_mm(target + tol)})))")
    # pairs the router connected directly (coupling impossible or unnecessary):
    # later rules win in KiCad, so these lift the coupling limit for them, and
    # the skew limit too where coupling was not needed at all
    for e in pair_exceptions or []:
        cond = _net_condition(e["nets"])
        rule = (f'(rule "pair exception {e["nets"][0].rsplit("/", 1)[-1]}"\n'
                f'  (condition "{cond}")\n  (constraint diff_pair_uncoupled (max 1000mm))\n'
                f'  (constraint diff_pair_gap (min 0mm) (max 1000mm))')
        if e.get("skew_waived"):
            rule += "\n  (constraint skew (max 1000mm))"
        out.append(f"# {e['nets'][0]} / {e['nets'][1]}: {e.get('text', e.get('reason', ''))}\n" + rule + ")")
    out.append(END)
    return "\n".join(out) + "\n"


def write_drc_rules(board, text: str) -> Optional[Path]:
    """Put the generated block into the project's .kicad_dru, keeping
    everything else in it. KiCad reads it on the next DRC run."""
    name = board.GetFileName()
    if not name:
        return None
    p = Path(name).with_suffix(".kicad_dru")
    old = p.read_text(encoding="utf-8") if p.exists() else ""
    if BEGIN in old and END in old:
        head, rest = old.split(BEGIN, 1)
        tail = rest.split(END, 1)[1].lstrip("\n")
        new = head + text + tail
    else:
        if not old.strip():
            old = "(version 1)\n"
        new = old.rstrip("\n") + "\n\n" + text
    p.write_text(new, encoding="utf-8")
    return p
