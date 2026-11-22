"""Per-layer custom rules and via clearance to locked inner-layer copper.

(1) A .kicad_dru rule with (layer inner) / (layer outer) / (layer "F.Cu")
holds on those layers only; KiCad tries the rules last to first and the
first whose condition and layer match wins. With an inner 1.0 mm / outer
1.5 mm pair, a track on In1.Cu may pass a locked track of the other domain
at 1.0 mm, but not on F.Cu (the router used to keep 1.5 mm everywhere).

(2) A via spans every layer and keeps each layer's rule from locked tracks
on other layers than the one it is routed on. On a real board (BATTEM2
cell 8) vias came 0.2 mm from a locked In2.Cu lane where the inner rule
asks 1.0 mm: SMD pads on F.Cu sitting over the lane opened the isolation
ring's "pads already inside the gap" corridors on In2.Cu too.

Builds the boards in memory (the .kicad_dru goes next to a temporary board
file). Needs KiCad's Python; the rule checks run without it.

  <KiCad python3> tests/test_layer_rules.py
"""
import functools
import math
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))
print = functools.partial(print, flush=True)

MM = 1_000_000

RULES = """(version 1)
# domain rules by net name, as an isolation board writes them
(rule "HV inner"
  (layer inner)
  (condition "A.NetName == 'HV' && B.NetName != 'HV'")
  (constraint clearance (min 1mm)))
(rule "HV outer"
  (layer outer)
  (condition "A.NetName == 'HV' && B.NetName != 'HV'")
  (constraint clearance (min 1.5mm)))
"""


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL {name} {detail}")
        sys.exit(1)
    print(f"ok   {name}")


def test_rule_layers():
    from autorouter.engine.native import dru
    I = dru.Item
    classes = {"Default": 200_000}
    r = dru.ClearanceRules.parse(RULES, classes)
    check("layered rules are used, not skipped", not r.skipped and len(r.rules) == 2, str(r.skipped))
    hv, sig = I("Default", "HV"), I("Default", "SIG")
    got = {L: r.between(hv, sig, L) for L in ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")}
    check("inner 1.0 mm on In1/In2, outer 1.5 mm on F/B",
          got == {"F.Cu": 1_500_000, "In1.Cu": 1_000_000, "In2.Cu": 1_000_000, "B.Cu": 1_500_000}, str(got))
    check("no layer (a via): the strictest", r.between(hv, sig) == 1_500_000)
    check("both orders", r.between(sig, hv, "In1.Cu") == 1_000_000)
    check("no rule: net-class clearance", r.between(sig, I("Default", "X"), "In1.Cu") == 200_000)
    # a later layered rule does not shadow an earlier rule on the other layers
    text = RULES + """(rule "HV fine part" (layer "F.Cu")
  (condition "A.NetName == 'HV' && B.NetName == 'SIG'") (constraint clearance (min 0.3mm)))"""
    r2 = dru.ClearanceRules.parse(text, classes)
    got = {L: r2.between(hv, sig, L) for L in ("F.Cu", "In1.Cu", "B.Cu")}
    check("a later (layer \"F.Cu\") rule wins on F.Cu only",
          got == {"F.Cu": 300_000, "In1.Cu": 1_000_000, "B.Cu": 1_500_000}, str(got))
    check("layer names match as KiCad's", dru.layer_matches("inner", "In7.Cu") and not dru.layer_matches("inner", "B.Cu")
          and dru.layer_matches("outer", "B.Cu") and dru.layer_matches("F.Cu", "F.Cu")
          and not dru.layer_matches("F.Cu", "B.Cu") and dru.layer_matches(None, "In1.Cu"))
    print("rule layers: ok")


# ---- boards ---------------------------------------------------------------------

def _pt(x, y):
    import pcbnew
    return pcbnew.VECTOR2I(int(round(x * MM)), int(round(y * MM)))


def _board(tmp, name, w=30, h=20):
    import pcbnew
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(4)
    ds = board.GetDesignSettings()
    ns = ds.m_NetSettings
    ns.GetDefaultNetclass().SetTrackWidth(int(0.25 * MM))
    ns.GetDefaultNetclass().SetClearance(int(0.2 * MM))
    ns.GetDefaultNetclass().SetViaDiameter(int(0.6 * MM))
    ns.GetDefaultNetclass().SetViaDrill(int(0.3 * MM))
    nets = {}
    for n in ("HV", "SIG"):
        nets[n] = pcbnew.NETINFO_ITEM(board, n)
        board.Add(nets[n])
    for (x1, y1), (x2, y2) in (((0, 0), (w, 0)), ((w, 0), (w, h)), ((w, h), (0, h)), ((0, h), (0, 0))):
        s = pcbnew.PCB_SHAPE(board)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetStart(_pt(x1, y1))
        s.SetEnd(_pt(x2, y2))
        s.SetLayer(pcbnew.Edge_Cuts)
        s.SetWidth(int(0.1 * MM))
        board.Add(s)
    path = os.path.join(tmp, name + ".kicad_pcb")
    with open(os.path.join(tmp, name + ".kicad_dru"), "w") as f:
        f.write(RULES)
    board.SetFileName(path)
    return board, nets


def _track(board, net, layer, a, b, width=0.3):
    import pcbnew
    t = pcbnew.PCB_TRACK(board)
    t.SetStart(_pt(*a))
    t.SetEnd(_pt(*b))
    t.SetWidth(int(width * MM))
    t.SetLayer(layer)
    t.SetNet(net)
    t.SetLocked(True)
    board.Add(t)
    return t


def _smd(board, ref, net, x, y, w=1.0, h=0.6):
    import pcbnew
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference(ref)
    fp.SetPosition(_pt(x, y))
    board.Add(fp)
    p = pcbnew.PAD(fp)
    p.SetNumber("1")
    p.SetShape(pcbnew.PAD_SHAPE_RECT)
    p.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    p.SetLayerSet(p.SMDMask())
    p.SetSize(pcbnew.VECTOR2I(int(w * MM), int(h * MM)))
    p.SetPosition(_pt(x, y))
    p.SetNet(net)
    fp.Add(p)
    return p


def _opts(**kw):
    from autorouter.engine.native.problem import NativeOptions
    o = NativeOptions(pitch_mm=0.1, track_mode="netclass", via_mode="netclass", stitch_vias=False, **kw)
    return o


def test_track_per_layer(tmp):
    """SIG's owner map: 1.2 mm from a locked HV track is free on In1.Cu
    (inner 1.0 mm) and blocked on F.Cu (outer 1.5 mm)."""
    import pcbnew
    from autorouter.engine.native import extract
    board, nets = _board(tmp, "track_layers")
    _track(board, nets["HV"], pcbnew.F_Cu, (5, 5), (25, 5))
    _track(board, nets["HV"], pcbnew.In1_Cu, (5, 15), (25, 15))
    _smd(board, "R1", nets["SIG"], 3, 10)
    _smd(board, "R2", nets["SIG"], 27, 10)
    p = extract.extract(board, _opts(skip_nets=["HV"]), status=lambda m: None)
    g = p.grid
    sig = next(n for n in p.nets.values() if n.name == "SIG")
    own = p.owners[sig.rules.key]
    li = {l.kicad_id: l.index for l in p.layers}
    half = sig.rules.width / 2 / MM

    def free(layer, x, y):
        return own[g.index(li[layer], g.cell_x(int(x * MM)), g.cell_y(int(y * MM)))] in (0, sig.code)
    # centreline offsets from the HV track centre: edge gap = off - 0.15 - half width
    d_in = 0.15 + half + 1.0 + 0.1          # 1.0 mm rule, one cell to spare
    d_out = 0.15 + half + 1.5 - 0.1         # inside the 1.5 mm rule
    check("In1.Cu: a SIG track may pass the HV track at the inner 1.0 mm", free(pcbnew.In1_Cu, 15, 15 + d_in))
    check("In1.Cu: ...but not closer", not free(pcbnew.In1_Cu, 15, 15 + 0.15 + half + 1.0 - 0.1))
    check("F.Cu: the outer 1.5 mm holds", not free(pcbnew.F_Cu, 15, 5 + d_out) and not free(pcbnew.F_Cu, 15, 5 + d_in))
    check("F.Cu: free past 1.5 mm", free(pcbnew.F_Cu, 15, 5 + 0.15 + half + 1.5 + 0.1))
    # a via spans both: the via map takes each layer's own rule
    vown = p.owners[(sig.rules.via_diameter, sig.rules.clearance)]
    vr = sig.rules.via_diameter / 2 / MM
    ok_in = 15 + 0.15 + vr + 1.0 + 0.1
    check("via map: In1.Cu keeps 1.0 mm from the In1 track",
          vown[g.index(li[pcbnew.In1_Cu], g.cell_x(15 * MM), g.cell_y(int((15 + 0.15 + vr + 0.9) * MM)))] not in (0, sig.code)
          and vown[g.index(li[pcbnew.In1_Cu], g.cell_x(15 * MM), g.cell_y(int(ok_in * MM)))] in (0, sig.code))
    print("track per layer: ok")


def _via_gaps(board, code, track):
    """Edge gap (mm) of each of the net's vias to the locked track."""
    import pcbnew
    out = []
    a, b = track.GetStart(), track.GetEnd()
    for t in board.GetTracks():
        if t.GetClass() != "PCB_VIA" or t.GetNetCode() != code:
            continue
        p = t.GetPosition()
        dx, dy = b.x - a.x, b.y - a.y
        L2 = dx * dx + dy * dy
        u = 0 if L2 == 0 else max(0.0, min(1.0, ((p.x - a.x) * dx + (p.y - a.y) * dy) / L2))
        d = math.hypot(p.x - (a.x + u * dx), p.y - (a.y + u * dy))
        out.append(round((d - track.GetWidth() / 2 - t.GetWidth(pcbnew.F_Cu) / 2) / MM, 3))
    return out


def test_via_near_inner_lane(tmp):
    """SIG must cross an HV wall on F.Cu, so it drops vias. An HV lane runs
    on In2.Cu right under SIG's F.Cu pads: no SIG via may come within the
    inner rule (1.0 mm) of that lane, though the pads above it are closer
    on F.Cu (where the lane is not)."""
    import pcbnew
    from autorouter.engine.native import extract, pipeline
    board, nets = _board(tmp, "via_lane")
    _track(board, nets["HV"], pcbnew.F_Cu, (15, 0.3), (15, 19.7))         # a wall across the board on F.Cu
    lane = _track(board, nets["HV"], pcbnew.In2_Cu, (11, 2), (11, 18))     # the locked inner lane
    _smd(board, "R1", nets["SIG"], 11, 10, 1.2, 0.6)                       # sits over the lane on F.Cu
    _smd(board, "R3", nets["SIG"], 11, 6, 1.2, 0.6)
    _smd(board, "R2", nets["SIG"], 22, 10)
    # the model first: no via site within the inner rule of the In2 lane
    p = extract.extract(board, _opts(skip_nets=["HV"]), status=lambda m: None)
    g = p.grid
    sig = next(n for n in p.nets.values() if n.name == "SIG")
    vown = p.owners[(sig.rules.via_diameter, sig.rules.clearance)]
    vr = sig.rules.via_diameter / 2
    need = 150_000 + vr + 1_000_000                       # lane half width + via radius + inner rule
    bad = []
    for cy in range(g.cell_y(3 * MM), g.cell_y(17 * MM)):
        for cx in range(g.cell_x(8 * MM), g.cell_x(14 * MM)):
            rest = cy * g.w + cx
            if p.via_block[rest]:
                continue
            if all(vown[l * g.plane + rest] in (0, sig.code) for l in range(g.layers)):
                d = abs(g.ox + cx * g.pitch - 11 * MM)
                if d < need - g.pitch:
                    bad.append((round((g.ox + cx * g.pitch) / MM, 2), round((g.oy + cy * g.pitch) / MM, 2)))
    check("no free via site within 1.0 mm of the In2 lane", not bad, f"{len(bad)} sites, e.g. {bad[:4]}")
    job = pipeline.run_job(board, _opts(skip_nets=["HV"]))
    code = nets["SIG"].GetNetCode()
    gaps = _via_gaps(board, code, lane)
    check("SIG is routed through vias", gaps and not job.failed, f"vias {gaps} failed {job.failed}")
    check("every SIG via keeps 1.0 mm from the locked In2 lane", min(gaps) >= 1.0 - 0.005, str(gaps))
    print("via near inner lane: ok")


def main():
    test_rule_layers()
    try:
        import pcbnew  # noqa: F401
    except ImportError:
        print("test_layer_rules: board checks skipped (needs KiCad's Python)")
        return
    tmp = tempfile.mkdtemp(prefix="layer_rules_")
    try:
        test_track_per_layer(tmp)
        test_via_near_inner_lane(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("test_layer_rules: all passed")


if __name__ == "__main__":
    main()
