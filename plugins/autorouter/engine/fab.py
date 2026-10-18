"""Fabrication rules: a board house's standard capabilities as KiCad design
rules, so the router and KiCad's DRC both work to what the fab can build
without extra cost.

A template raises Board Setup > Constraints (minimum clearance, track width,
via size and drill, hole clearances, copper to edge) and the net classes
to its minimums and default via, and adds the rules the constraints page
cannot say (plated pad holes keep further from copper than via holes) to the
project's .kicad_dru.

The JLCPCB values are their published capabilities
(jlcpcb.com/capabilities/pcb-capabilities), kept to the no-extra-cost range:
a 0.2 mm via drill is standard only with a pad of 0.45 mm or more. Check the
page before ordering; the fab changes them now and then.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

MM = 1_000_000
NONE = ""                         # the board's own rules

BEGIN = "# --- autorouter fab begin: fabrication rules from the Autorouter plugin, edits inside are overwritten ---"
END = "# --- autorouter fab end ---"


@dataclass(frozen=True)
class FabRules:
    description: str
    min_clearance: float          # mm, track/pad/via copper to copper
    min_track: float
    net_clearance: float          # what the net classes get at least (a little over the minimum)
    via_diameter: float           # the net classes' via
    via_drill: float
    min_via_diameter: float
    min_drill: float
    min_annular: float            # via annular ring
    hole_clearance: float         # via hole to other copper
    hole_to_hole: float           # via holes
    edge_clearance: float         # copper to the routed board edge
    pth_hole_clearance: float     # plated pad hole to other copper (inner layers are the stricter)
    pad_hole_to_hole: float       # between pad holes
    silk_width: float = 0.15
    text_height: float = 1.0


TEMPLATES: Dict[str, FabRules] = {
    "JLCPCB 2-layer standard": FabRules(
        "JLCPCB 2 layers, 1 oz: 0.127 mm clearance, 0.6/0.3 mm vias",
        min_clearance=0.127, min_track=0.127, net_clearance=0.15, via_diameter=0.6, via_drill=0.3,
        min_via_diameter=0.5, min_drill=0.3, min_annular=0.1, hole_clearance=0.25, hole_to_hole=0.25,
        edge_clearance=0.3, pth_hole_clearance=0.3, pad_hole_to_hole=0.5),
    "JLCPCB 4-layer standard": FabRules(
        "JLCPCB 4 layers (outer 1 oz, inner 0.5 oz): 0.1 mm clearance, 0.45/0.2 mm vias",
        min_clearance=0.1, min_track=0.1, net_clearance=0.127, via_diameter=0.45, via_drill=0.2,
        min_via_diameter=0.45, min_drill=0.2, min_annular=0.1, hole_clearance=0.2, hole_to_hole=0.2,
        edge_clearance=0.3, pth_hole_clearance=0.3, pad_hole_to_hole=0.45),
    "JLCPCB 6-layer standard": FabRules(
        "JLCPCB 6 layers (outer 1 oz, inner 0.5 oz): 0.1 mm clearance, 0.45/0.2 mm vias",
        min_clearance=0.1, min_track=0.1, net_clearance=0.127, via_diameter=0.45, via_drill=0.2,
        min_via_diameter=0.45, min_drill=0.2, min_annular=0.1, hole_clearance=0.2, hole_to_hole=0.2,
        edge_clearance=0.3, pth_hole_clearance=0.3, pad_hole_to_hole=0.45),
}


def get(name: str) -> Optional[FabRules]:
    return TEMPLATES.get(name or "")


def _nm(mm: float) -> int:
    return int(round(mm * MM))


def apply(board, name: str) -> List[str]:
    """Raise the board's constraints and net classes to the template;
    returns what changed. Nothing is ever lowered: a stricter rule, a wider
    impedance track or a larger clearance the designer chose stays."""
    rules = get(name)
    if rules is None:
        return []
    ds = board.GetDesignSettings()
    out = []

    def setting(attr, mm, label):
        if not hasattr(ds, attr):
            return
        old = int(getattr(ds, attr))
        new = _nm(mm)
        if old < new:                  # a stricter rule the designer set stays
            setattr(ds, attr, new)
            out.append(f"{label} {old / MM:.3f} -> {mm:.3f} mm")

    setting("m_MinClearance", rules.min_clearance, "minimum clearance")
    setting("m_TrackMinWidth", rules.min_track, "minimum track width")
    setting("m_ViasMinSize", rules.min_via_diameter, "minimum via diameter")
    setting("m_MinThroughDrill", rules.min_drill, "minimum through hole")
    setting("m_ViasMinAnnularWidth", rules.min_annular, "minimum via annular ring")
    setting("m_HoleClearance", rules.hole_clearance, "hole clearance")
    setting("m_HoleToHoleMin", rules.hole_to_hole, "hole to hole")
    setting("m_CopperEdgeClearance", rules.edge_clearance, "copper to edge")

    ns = ds.m_NetSettings
    # by name, one live object at a time: the netclass map's values are
    # temporaries in the scripting API (holding them crashes KiCad)
    try:
        names = [str(n) for n in ns.GetNetclasses().keys()]
    except Exception:
        names = []

    def netclasses():
        yield "Default", ns.GetDefaultNetclass()
        for n in names:
            yield n, ns.GetNetClassByName(n)

    def has(nc, what):
        # a class may leave a value to the Default class (KiCad 9+); reading it then is not safe
        f = getattr(nc, "Has" + what, None)
        return f is None or f()

    for cname, nc in netclasses():
        if nc is None:
            continue
        changed = []
        if has(nc, "Clearance") and nc.GetClearance() < _nm(rules.net_clearance):
            nc.SetClearance(_nm(rules.net_clearance))
            changed.append(f"clearance {rules.net_clearance}")
        if has(nc, "TrackWidth") and nc.GetTrackWidth() < _nm(rules.min_track):
            nc.SetTrackWidth(_nm(rules.min_track))
            changed.append(f"track {rules.min_track}")
        # the via: at least the template's, and never a drill the fab charges extra for
        if has(nc, "ViaDiameter") and has(nc, "ViaDrill") and \
                (nc.GetViaDiameter() < _nm(rules.via_diameter) or nc.GetViaDrill() < _nm(rules.via_drill)):
            nc.SetViaDiameter(max(nc.GetViaDiameter(), _nm(rules.via_diameter)))
            nc.SetViaDrill(max(nc.GetViaDrill(), _nm(rules.via_drill)))
            changed.append(f"via {nc.GetViaDiameter() / MM:.2f}/{nc.GetViaDrill() / MM:.2f}")
        try:
            if nc.HasDiffPairWidth() and nc.GetDiffPairWidth() < _nm(rules.min_track):
                nc.SetDiffPairWidth(_nm(rules.min_track))
                changed.append("pair width")
            if nc.HasDiffPairGap() and nc.GetDiffPairGap() < _nm(rules.min_clearance):
                nc.SetDiffPairGap(_nm(rules.min_clearance))
                changed.append("pair gap")
        except Exception:
            pass
        if changed:
            out.append(f"net class {cname}: " + ", ".join(changed))
    try:
        ns.RecomputeEffectiveNetclasses()
    except Exception:
        pass
    return out


def drc_rules(name: str) -> str:
    """The rules the Constraints page cannot express, as a .kicad_dru block."""
    rules = get(name)
    if rules is None:
        return ""
    def mm(v):
        return f"{v:g}mm"
    return "\n".join([
        BEGIN,
        f"# {name}: {rules.description}",
        f'(rule "fab: plated pad hole to copper"',
        f"\t(constraint hole_clearance (min {mm(rules.pth_hole_clearance)}))",
        "\t(condition \"A.Type == 'Pad' && A.isPlated()\"))",
        f'(rule "fab: pad hole to pad hole"',
        f"\t(constraint hole_to_hole (min {mm(rules.pad_hole_to_hole)}))",
        "\t(condition \"A.Type == 'Pad' && B.Type == 'Pad'\"))",
        f'(rule "fab: silkscreen line width"',
        f"\t(layer \"?.Silkscreen\")",
        f"\t(constraint text_thickness (min {mm(rules.silk_width)}))",
        f"\t(constraint text_height (min {mm(rules.text_height)})))",
        END,
    ]) + "\n"


def write_drc_rules(board, name: str) -> Optional[Path]:
    """Put (or replace, or with no template remove) the fab block in the
    project's .kicad_dru, leaving everything else in it alone."""
    fname = board.GetFileName()
    if not fname:
        return None
    p = Path(fname).with_suffix(".kicad_dru")
    old = p.read_text(encoding="utf-8") if p.exists() else ""
    text = drc_rules(name)
    if BEGIN in old and END in old:
        head, rest = old.split(BEGIN, 1)
        tail = rest.split(END, 1)[1].lstrip("\n")
        new = head + text + tail
    elif text:
        if not old.strip():
            old = "(version 1)\n"
        new = old.rstrip("\n") + "\n\n" + text
    else:
        return None
    p.write_text(new, encoding="utf-8")
    return p
