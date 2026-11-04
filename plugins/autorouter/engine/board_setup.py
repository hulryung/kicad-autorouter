"""Board setup check: what in Board Setup and the project file keeps the
router (and KiCad's own DRC and interactive router) from doing its best,
and the fixes for it.

check() reads the board and returns Findings, each with a severity, an
explanation and, where there is a safe one, a fix; apply() carries out the
fixes the user ticked. Fixes only ever add or raise: a net class is created
or a pattern assigned, a minimum raised, a pre-defined size added. Nothing
the designer set is lowered or removed.

Board Setup changes made from a plugin are not on KiCad's undo list; they
reach the project file (.kicad_pro) when the board is saved.

The pure helpers (pattern matching, the size lists, the net groups) work
without KiCad and are tested offline; check() and apply() need pcbnew.
"""
import fnmatch
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import fab

MM = 1_000_000
ERROR, WARNING, INFO = "error", "warning", "info"
SEVERITY_ORDER = {ERROR: 0, WARNING: 1, INFO: 2}

POWER_CLASS = "Power"
POWER_TRACK_NM = 400_000          # about 1 A on outer 1 oz copper at a 10 degree rise (IPC-2221)
EDGE_CLEARANCE_NM = 300_000       # copper to a routed edge: what board houses ask without extra cost
MIN_EDGE_CLEARANCE_NM = 200_000   # below this, a routed edge exposes copper
# Power net names: the router's own list (engine/native/problem.py) less the
# grounds, which are poured rather than routed.
POWER_PATTERNS = ["+*", "*VCC*", "*VDD*", "*VBUS*", "*VIN*", "*VOUT*", "*3V3*", "*3.3V*", "*5V*", "*12V*",
                  "*24V*", "*PWR*", "*POWER*", "*VBAT*", "*VSYS*"]
NOT_POWER = ["*EN", "*_EN*", "*nEN*", "*PWR_EN*", "*nOC*", "*_OC*", "*FAULT*", "*GOOD*", "*PG", "*_PG*",
             "*SENSE*", "*DET*", "*_ON", "*CTRL*", "*SDA*", "*SCL*", "*BUT*", "*BTN*", "*KEY*", "*LED*",
             "unconnected-*"]
AUTO_POWER_PINS = {"VBUS", "VCC", "VDD", "VIN", "VOUT", "VBAT", "VSYS", "VCCIO", "VDDIO", "PWR", "VPWR"}
# Differential pair families and the impedance a net class name for them
# usually carries (90R, USB_90, 100_OHM...).
PAIR_FAMILIES: List[Tuple[str, List[str], List[str]]] = [
    ("USB", ["*USB*", "*HUB_D*", "*D+", "*D-", "*DP", "*DN"], ["90"]),
    ("Ethernet", ["*TRD*", "*MDI*", "*TX?_?", "*RX?_?", "*ETH*"], ["100"]),
    ("HDMI", ["*HDMI*", "*TMDS*"], ["HDMI", "100"]),
    ("MIPI / LVDS", ["*MIPI*", "*CSI*", "*DSI*", "*DPHY*", "*LVDS*"], ["100"]),
    ("PCIe", ["*PCIE*", "*REFCLK*"], ["85", "100"]),
    ("CAN / RS-485", ["*CAN*", "*485*", "*RS485*"], ["120"]),
]


@dataclass
class Finding:
    key: str                          # stable id, e.g. "pairs_default"
    severity: str                     # error | warning | info
    title: str                        # one line, for the list
    detail: str                       # what it means and what the fix does
    fix_label: str = ""               # "" = nothing to do automatically
    apply: Optional[Callable[[object], List[str]]] = None   # fn(board) -> what changed
    default_on: bool = True           # ticked when the check is shown

    @property
    def fixable(self) -> bool:
        return self.apply is not None


# ---- helpers that need no KiCad --------------------------------------------

def pattern_matches(pattern: str, name: str) -> bool:
    """KiCad's net class pattern matching: a wildcard (* and ?) or a regular
    expression, either over the whole net name, case-sensitive."""
    if fnmatch.fnmatchcase(name, pattern):
        return True
    try:
        return re.fullmatch(pattern, name) is not None
    except re.error:
        return False


def stale_patterns(assignments: Sequence[Tuple[str, str]], net_names: Iterable[str]) -> List[Tuple[str, str]]:
    """The (pattern, class) assignments that match no net on the board."""
    names = list(net_names)
    return [(p, c) for p, c in assignments if not any(pattern_matches(p, n) for n in names)]


def repaired_pattern(pattern: str, net_names: Iterable[str]) -> Optional[Tuple[str, List[str]]]:
    """A pattern that matches what a stale one was meant to: the usual cause
    is a sheet path that changed or was never part of the name (a pattern
    "/USB2*" for global nets named "USB2_P"). Tries the last path element on
    its own and under any sheet; returns (pattern, nets it matches) or None."""
    names = list(net_names)
    if "/" not in pattern:
        return None
    tail = pattern.rsplit("/", 1)[-1]
    if not tail:
        return None
    for candidate in (tail, "*/" + tail):
        hits = [n for n in names if pattern_matches(candidate, n)]
        if hits:
            return candidate, hits
    return None


def is_power_name(name: str) -> bool:
    short = name.rsplit("/", 1)[-1]
    up = short.upper()
    if any(fnmatch.fnmatchcase(up, p.upper()) for p in NOT_POWER):
        return False
    auto = re.fullmatch(r"NET-\(.*-([^-]*)\)", up)
    if auto:
        # a net KiCad named after one pin ("Net-(D301-VBUS)"): only a supply
        # pin's own name counts, not a strap like VBUS_MAX or LOCAL_PWR
        pin = auto.group(1)
        return pin.startswith("+") or pin in AUTO_POWER_PINS or pin.startswith(("VCC", "VDD"))
    return any(fnmatch.fnmatchcase(up, p.upper()) for p in POWER_PATTERNS)


def exact_pattern(name: str) -> str:
    """A pattern for one net. KiCad's escapes in a name ("{slash}" for a "/"
    inside a pin name) do not survive as pattern text: they become "*"."""
    return re.sub(r"\{[a-z]+\}", "*", name)


def pair_family(name: str) -> Optional[str]:
    up = name.rsplit("/", 1)[-1].upper()
    for family, patterns, _ in PAIR_FAMILIES:
        if any(fnmatch.fnmatchcase(up, p.upper()) for p in patterns):
            return family
    return None


def class_for_family(family: str, classes: Dict[str, dict]) -> Optional[str]:
    """The existing net class a pair family belongs in: one named for its
    impedance or interface (90R for USB, 100R for Ethernet) that sets a diff
    pair width; None if the board has none."""
    hints = next((h for f, _, h in PAIR_FAMILIES if f == family), [])
    for hint in hints:
        for name, c in sorted(classes.items()):
            if name != "Default" and hint.upper() in name.upper() and c.get("diff_pair_width", 0) > 0:
                return name
    return None


def size_list_additions(existing: Iterable[int], wanted: Iterable[int]) -> List[int]:
    """The widths (nm) to add to a pre-defined list so it holds every wanted one."""
    have = {int(v) for v in existing if int(v) > 0}
    return sorted({int(v) for v in wanted if int(v) > 0} - have)


def read_patterns(board) -> List[Tuple[str, str]]:
    """The project's net class pattern assignments, from the saved .kicad_pro
    (the scripting API does not expose them)."""
    name = board.GetFileName() if board is not None else ""
    if not name:
        return []
    pro = Path(name).with_suffix(".kicad_pro")
    try:
        data = json.loads(pro.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for item in (data.get("net_settings") or {}).get("netclass_patterns") or []:
        if isinstance(item, dict) and item.get("pattern") and item.get("netclass"):
            out.append((str(item["pattern"]), str(item["netclass"])))
    return out


def _mm(nm: int) -> str:
    return f"{nm / MM:g} mm"


def _list(names: Sequence[str], limit: int = 8) -> str:
    shown = ", ".join(names[:limit])
    return shown + (f" and {len(names) - limit} more" if len(names) > limit else "")


# ---- reading the board -----------------------------------------------------

def _classes(board) -> Dict[str, dict]:
    """Each net class's settings, by name (Default included). A value the
    class leaves to Default (KiCad 9+) is Default's."""
    ns = board.GetDesignSettings().m_NetSettings
    default = ns.GetDefaultNetclass()

    def read(nc):
        def val(what):
            has = getattr(nc, "Has" + what, None)
            src = nc if has is None or has() else default
            return int(getattr(src, "Get" + what)())
        return {"clearance": val("Clearance"), "track": val("TrackWidth"), "via_diameter": val("ViaDiameter"),
                "via_drill": val("ViaDrill"), "diff_pair_width": val("DiffPairWidth") if nc.HasDiffPairWidth() else 0,
                "diff_pair_gap": val("DiffPairGap")}
    out = {"Default": read(default)}
    out["Default"]["diff_pair_width"] = int(default.GetDiffPairWidth())
    try:
        names = [str(n) for n in ns.GetNetclasses().keys()]
    except Exception:
        names = []
    for name in names:
        nc = ns.GetNetClassByName(name)
        if nc is not None:
            out[name] = read(nc)
    return out


def _nets(board) -> List[Tuple[str, str]]:
    """(net name, effective class) for the board's nets."""
    out = []
    for code, net in board.GetNetsByNetcode().items():
        if code > 0 and net.GetNetname():
            out.append((net.GetNetname(), net.GetNetClassName()))
    return out


def _has_outline(board) -> bool:
    import pcbnew
    try:
        poly = pcbnew.SHAPE_POLY_SET()
        return bool(board.GetBoardPolygonOutlines(poly, False)) and poly.OutlineCount() > 0
    except Exception:
        return any(d.GetLayer() == pcbnew.Edge_Cuts for d in board.GetDrawings())


def _refresh_classes(board) -> None:
    ns = board.GetDesignSettings().m_NetSettings
    for fn in ("ClearAllCaches", "RecomputeEffectiveNetclasses"):
        try:
            getattr(ns, fn)()
        except Exception:
            pass
    try:
        board.SynchronizeNetsAndNetClasses(False)
    except Exception:
        pass


def _assign(board, assignments: Sequence[Tuple[str, str]]) -> None:
    ns = board.GetDesignSettings().m_NetSettings
    for pattern, cls in assignments:
        ns.SetNetclassPatternAssignment(pattern, cls)
    _refresh_classes(board)


def _missing_sizes(board, classes: Dict[str, dict]) -> Tuple[List[int], List[Tuple[int, int]]]:
    """Net class track widths and vias the pre-defined size lists lack."""
    ds = board.GetDesignSettings()
    tracks = [int(w) for w in ds.m_TrackWidthList]
    vias = {(int(v.m_Diameter), int(v.m_Drill)) for v in ds.m_ViasDimensionsList}
    want_tracks = size_list_additions(tracks, [c["track"] for c in classes.values()])
    want_vias = sorted({(c["via_diameter"], c["via_drill"]) for c in classes.values() if c["via_diameter"] > 0}
                       - {v for v in vias if v[0] > 0})
    return want_tracks, want_vias


def _add_sizes(board) -> List[str]:
    """The fix for predefined_sizes, worked out when it is applied: fixes
    applied before it (a new Power class) add sizes of their own."""
    import pcbnew
    tw, vs = _missing_sizes(board, _classes(board))
    d = board.GetDesignSettings()
    # the first entry of each list stands for "the net class value"
    if tw and d.m_TrackWidthList.size() == 0:
        d.m_TrackWidthList.push_back(0)
    if vs and d.m_ViasDimensionsList.size() == 0:
        d.m_ViasDimensionsList.push_back(pcbnew.VIA_DIMENSION(0, 0))
    for w in tw:
        d.m_TrackWidthList.push_back(w)
    for dia, drill in vs:
        d.m_ViasDimensionsList.push_back(pcbnew.VIA_DIMENSION(dia, drill))
    return ([f"pre-defined track widths: + {', '.join(_mm(w) for w in tw)}"] if tw else []) + \
        ([f"pre-defined vias: + {', '.join(f'{_mm(a)}/{_mm(b)}' for a, b in vs)}"] if vs else [])


# ---- the checks --------------------------------------------------------------

def check(board, fab_name: str = "") -> List[Finding]:
    """Everything worth knowing about the board's setup before routing,
    most serious first."""
    from .native.extract import pair_partner
    ds = board.GetDesignSettings()
    out: List[Finding] = []
    nets = _nets(board)
    names = [n for n, _ in nets]
    classes = _classes(board)

    if not board.GetFileName():
        out.append(Finding("unsaved", WARNING, "The board has not been saved yet",
                           "Net classes and pre-defined sizes live in the project file and DRC rules in the "
                           ".kicad_dru next to the board: save the board once so they have somewhere to go."))
    if not _has_outline(board):
        out.append(Finding("outline", ERROR, "No closed board outline on Edge.Cuts",
                           "The router keeps copper inside the board outline and the copper-to-edge clearance "
                           "from it. Draw the outline on Edge.Cuts (closed) before routing."))

    # -- net classes against the board's minimums: DRC flags every item they size
    min_via, min_drill = int(ds.m_ViasMinSize), int(ds.m_MinThroughDrill)
    min_ring, min_track, min_clear = int(ds.m_ViasMinAnnularWidth), int(ds.m_TrackMinWidth), int(ds.m_MinClearance)
    low: Dict[str, Dict[str, int]] = {}
    for cname, c in classes.items():
        want = {}
        drill = max(c["via_drill"], min_drill)
        dia = max(c["via_diameter"], min_via, drill + 2 * min_ring)
        if dia > c["via_diameter"]:
            want["via_diameter"] = dia
        if drill > c["via_drill"]:
            want["via_drill"] = drill
        if c["track"] < min_track:
            want["track"] = min_track
        if c["clearance"] < min_clear:
            want["clearance"] = min_clear
        if want:
            low[cname] = want
    if low:
        lines = []
        for cname, want in low.items():
            c = classes[cname]
            parts = [f"{k.replace('_', ' ')} {_mm(c[k])} -> {_mm(v)}" for k, v in want.items()]
            lines.append(f"  {cname}: " + ", ".join(parts))

        def raise_classes(b, low=low):
            ns = b.GetDesignSettings().m_NetSettings
            done = []
            for cname, want in low.items():
                nc = ns.GetDefaultNetclass() if cname == "Default" else ns.GetNetClassByName(cname)
                if nc is None:
                    continue
                for key, setter in (("via_diameter", "SetViaDiameter"), ("via_drill", "SetViaDrill"),
                                    ("track", "SetTrackWidth"), ("clearance", "SetClearance")):
                    if key in want:
                        getattr(nc, setter)(want[key])
                done.append(f"net class {cname}: " + ", ".join(f"{k.replace('_', ' ')} {_mm(v)}"
                                                               for k, v in want.items()))
            _refresh_classes(b)
            return done
        out.append(Finding("class_minimums", ERROR, f"{len(low)} net class(es) below the board's minimum rules",
                           "Every track and via these classes size breaks the Constraints page's minimums, so "
                           "the router either cannot place them or the DRC flags each one:\n" + "\n".join(lines)
                           + "\nThe fix raises the class values to the minimums.",
                           "Raise the net class values", raise_classes))

    # -- fabrication rules
    if fab_name and fab.get(fab_name) is not None:
        try:
            pending = fab.apply(board, fab_name, dry_run=True)
        except Exception as e:
            pending = [f"(could not compare: {e!r})"]
        if pending:
            def apply_fab(b, name=fab_name):
                done = fab.apply(b, name)
                path = fab.write_drc_rules(b, name)
                if path is not None:
                    done.append(f"fab rules written to {path.name}")
                return done
            out.append(Finding("fab", WARNING, f"Board rules are below {fab_name}",
                               f"{fab.get(fab_name).description}.\nThese settings are under what the fab "
                               "builds without extra cost:\n  " + "\n  ".join(pending)
                               + "\nThe fix raises them (a stricter value stays) and adds the hole rules the "
                                 "Constraints page cannot say to the .kicad_dru.",
                               f"Apply {fab_name}", apply_fab))
    elif int(ds.m_CopperEdgeClearance) < MIN_EDGE_CLEARANCE_NM:
        def raise_edge(b):
            b.GetDesignSettings().m_CopperEdgeClearance = EDGE_CLEARANCE_NM
            return [f"copper to edge clearance {_mm(EDGE_CLEARANCE_NM)}"]
        out.append(Finding("edge_clearance", WARNING,
                           f"Copper to edge clearance is only {_mm(int(ds.m_CopperEdgeClearance))}",
                           "Tracks and pours may run right up to the board edge, where routing the board out "
                           "cuts into them. Board houses ask for about 0.3 mm (pick a fabrication template to "
                           "set all their rules at once).",
                           f"Set it to {_mm(EDGE_CLEARANCE_NM)}", raise_edge))

    # -- net class patterns that match nothing
    patterns = read_patterns(board)
    stale = stale_patterns(patterns, names)
    if stale:
        repairs = []
        lines = []
        class_of = dict(nets)
        for pat, cls in stale:
            fixed = repaired_pattern(pat, names)
            if cls not in classes:
                lines.append(f"  {pat} -> {cls}: matches no net, and there is no net class {cls}")
            elif fixed and any(class_of.get(n) != cls for n in fixed[1]):
                repairs.append((fixed[0], cls))
                lines.append(f"  {pat} -> {cls}: matches no net; {fixed[0]} matches {_list(fixed[1], 4)}")
            elif fixed:
                # a corrected pattern (or another assignment) already puts its nets there
                lines.append(f"  {pat} -> {cls}: matches no net (its nets are in {cls} already; delete it)")
            else:
                lines.append(f"  {pat} -> {cls}: matches no net")
        detail = ("These assignments (Board Setup > Net Classes, from the saved project file) match no net; "
                  "the nets they were written for stay in Default with its width and clearance unless "
                  "another pattern catches them:\n" + "\n".join(lines))
        if repairs:
            detail += ("\nThe fix adds the corrected patterns next to the old ones (the old ones are left for "
                       "you to delete).")
            out.append(Finding("stale_patterns", WARNING, f"{len(stale)} net class pattern(s) match no net", detail,
                               f"Add {len(repairs)} corrected pattern(s)",
                               lambda b, r=repairs: (_assign(b, r), [f"pattern {p} -> {c}" for p, c in r])[1]))
        else:
            out.append(Finding("stale_patterns", INFO, f"{len(stale)} net class pattern(s) match no net", detail))

    # -- differential pairs left in Default
    name_set = set(names)
    by_class = dict(nets)
    pairs: Dict[str, List[Tuple[str, str]]] = {}
    seen = set()
    for n in names:
        if n in seen or n.startswith("unconnected-"):
            continue
        other = pair_partner(n, name_set)
        if other is None:
            continue
        seen.update((n, other))
        if by_class.get(n) == "Default" and by_class.get(other) == "Default":
            pairs.setdefault(pair_family(n) or "other", []).append(tuple(sorted((n, other))))
    if pairs:
        assign, lines, unmatched = [], [], []
        for family, members in sorted(pairs.items()):
            cls = class_for_family(family, classes) if family != "other" else None
            shown = _list([f"{a.rsplit('/', 1)[-1]}/{b.rsplit('/', 1)[-1]}" for a, b in members], 5)
            if cls:
                assign += [(exact_pattern(n), cls) for pair in members for n in pair]
                lines.append(f"  {family}: {shown} -> {cls}")
            else:
                unmatched.append(family)
                lines.append(f"  {family}: {shown} (no net class with a diff pair width for it)")
        count = sum(len(m) for m in pairs.values())
        detail = (f"{count} differential pair(s), found by their names, are in the Default net class, so KiCad's "
                  "diff pair router and the impedance rules do not size them:\n" + "\n".join(lines))
        if unmatched:
            detail += ("\nFor the rest, add an impedance class on the Signal integrity tab (presets: USB 2.0, "
                       "Ethernet, LVDS...) and use Write to KiCad there.")
        if assign:
            out.append(Finding("pairs_default", WARNING, f"{count} differential pair(s) in the Default net class",
                               detail, f"Assign {len(assign) // 2} pair(s) to their net classes",
                               lambda b, a=assign: (_assign(b, a), [f"net {n} -> {c}" for n, c in a])[1]))
        else:
            out.append(Finding("pairs_default", INFO, f"{count} differential pair(s) in the Default net class",
                               detail))

    # -- power nets at signal width
    default_track = classes["Default"]["track"]
    power = sorted(n for n, cls in nets if cls == "Default" and is_power_name(n))
    if power and default_track < POWER_TRACK_NM:
        existing = POWER_CLASS in classes

        def power_class(b, nets_=power, existing=existing):
            import pcbnew
            ns = b.GetDesignSettings().m_NetSettings
            done = []
            if not existing:
                default = ns.GetDefaultNetclass()
                nc = pcbnew.NETCLASS(POWER_CLASS)
                nc.SetClearance(default.GetClearance())
                nc.SetTrackWidth(max(POWER_TRACK_NM, default.GetTrackWidth()))
                nc.SetViaDiameter(default.GetViaDiameter())
                nc.SetViaDrill(default.GetViaDrill())
                nc.SetDiffPairWidth(default.GetDiffPairWidth())
                nc.SetDiffPairGap(default.GetDiffPairGap())
                nc.SetDescription("Autorouter: routed power nets")
                ns.SetNetclass(POWER_CLASS, nc)
                done.append(f"net class {POWER_CLASS}: track {_mm(max(POWER_TRACK_NM, default.GetTrackWidth()))}")
            _assign(b, [(exact_pattern(n), POWER_CLASS) for n in nets_])
            done.append(f"{len(nets_)} nets -> {POWER_CLASS}: {_list(nets_, 6)}")
            return done
        out.append(Finding("power_default", WARNING,
                           f"{len(power)} power net(s) routed at the Default width ({_mm(default_track)})",
                           f"Nets named like supplies ({_list([n.rsplit('/', 1)[-1] for n in power], 40)}) are in "
                           f"Default, so every track the router or you draw for them is {_mm(default_track)} wide. "
                           + (f"The fix assigns them to the existing {POWER_CLASS} class."
                              if existing else
                              f"The fix adds a {POWER_CLASS} net class ({_mm(POWER_TRACK_NM)} tracks, Default's "
                              "clearance and vias) and assigns them to it; widen it in Board Setup for higher "
                              "currents.")
                           + " Nets left to pours (GND, planes) are not affected by their class width.",
                           f"Put them in the {POWER_CLASS} net class", power_class, default_on=False))

    # -- pre-defined sizes: what the editor's track and via drop-downs offer
    want_tracks, want_vias = _missing_sizes(board, classes)
    if want_tracks or want_vias:
        parts = []
        if want_tracks:
            parts.append("track widths " + ", ".join(_mm(w) for w in want_tracks))
        if want_vias:
            parts.append("vias " + ", ".join(f"{_mm(a)}/{_mm(b)}" for a, b in want_vias))
        out.append(Finding("predefined_sizes", INFO, "Net class sizes missing from Pre-defined Sizes",
                           "The editor's track and via drop-downs (and this dialog's size choices) offer the "
                           "pre-defined sizes. Missing: " + "; ".join(parts) + ".",
                           "Add them to Pre-defined Sizes", _add_sizes))

    # -- pours
    stale_zones = [z for z in board.Zones() if not z.GetIsRuleArea() and not z.IsFilled()]
    if stale_zones:
        def refill(b):
            from . import session
            return [f"refilled {session.refill_zones(b)} copper zones"]
        out.append(Finding("zones_unfilled", WARNING, f"{len(stale_zones)} copper zone(s) not filled",
                           "The router reads pours as they are filled: an unfilled zone counts as no copper, so "
                           "its pads get fanout vias they may not need and DRC sees nothing to check.",
                           "Fill all zones", refill))

    # -- stackup, for impedance
    if not bool(ds.m_HasStackup) and board.GetCopperLayerCount() > 2:
        out.append(Finding("stackup", INFO, "No physical stackup in Board Setup",
                           "Impedance widths need the layer thicknesses: set Board Setup > Physical Stackup, or "
                           "pick a JLCPCB template on the Signal integrity tab."))

    out.sort(key=lambda f: SEVERITY_ORDER.get(f.severity, 9))
    return out


def apply(board, findings: Iterable[Finding]) -> List[str]:
    """Carry out the fixes; returns what changed, one line per change."""
    done: List[str] = []
    for f in findings:
        if f.apply is None:
            continue
        try:
            done += [f"{f.key}: {line}" for line in f.apply(board) or []]
        except Exception as e:
            done.append(f"{f.key}: could not apply ({e!r})")
    return done


def blocking(findings: Iterable[Finding]) -> List[Finding]:
    """What should stop a run until the user has looked at it."""
    return [f for f in findings if f.severity == ERROR]
