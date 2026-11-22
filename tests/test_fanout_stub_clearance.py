"""A plane net's pin on a 0.5 mm pitch part keeps its clearance to the next
pin. The +3V3 rail is a 0.5 mm PWR class on a board with no pre-defined
track widths: the neck used to stay at 0.5 mm (nothing narrower was
"pre-defined"), both escape lanes grazed pin 2, and the fanout via was then
dropped from the pad centre with a 0.5 mm stub that came within 0.075 mm of
pin 2 (MCP4728 U7.1 / U8.1 on a real board). The stub is now checked at the
pad centre and the neck falls back to the narrowest net-class width.
Builds the board in memory. Needs KiCad's Python; skips otherwise.

  <KiCad python3> tests/test_fanout_stub_clearance.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

try:
    import pcbnew
except ImportError:
    print("test_fanout_stub_clearance: skipped (needs KiCad's Python)")
    sys.exit(0)

from autorouter.engine.native import extract, pipeline
from autorouter.engine.native.problem import NativeOptions, NetRules

MM = 1_000_000
CLR = 0.15


def pt(x, y):
    return pcbnew.VECTOR2I(int(round(x * MM)), int(round(y * MM)))


def add_pad(fp, number, x, y, net, w, h):
    p = pcbnew.PAD(fp)
    p.SetNumber(str(number))
    p.SetShape(pcbnew.PAD_SHAPE_RECT)
    p.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    p.SetLayerSet(p.SMDMask())
    p.SetSize(pcbnew.VECTOR2I(int(w * MM), int(h * MM)))
    p.SetPosition(pt(x, y))
    if net is not None:
        p.SetNet(net)
    fp.Add(p)


def make_board():
    board = pcbnew.BOARD()
    ds = board.GetDesignSettings()
    ns = ds.m_NetSettings
    ns.GetDefaultNetclass().SetTrackWidth(int(0.2 * MM))
    ns.GetDefaultNetclass().SetClearance(int(CLR * MM))
    pwr = pcbnew.NETCLASS("PWR")
    pwr.SetTrackWidth(int(0.5 * MM))
    pwr.SetClearance(int(CLR * MM))
    pwr.SetViaDiameter(int(0.7 * MM))
    pwr.SetViaDrill(int(0.3 * MM))
    ns.SetNetclass("PWR", pwr)
    ns.SetNetclassPatternAssignment("+3V3", "PWR")
    ds.m_TrackMinWidth = int(0.15 * MM)
    nets = {}
    for name in ("+3V3", "SCL", "SDA"):
        nets[name] = pcbnew.NETINFO_ITEM(board, name)
        board.Add(nets[name])
    for (x1, y1), (x2, y2) in (((0, 0), (30, 0)), ((30, 0), (30, 20)), ((30, 20), (0, 20)), ((0, 20), (0, 0))):
        s = pcbnew.PCB_SHAPE(board)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetStart(pt(x1, y1))
        s.SetEnd(pt(x2, y2))
        s.SetLayer(pcbnew.Edge_Cuts)
        s.SetWidth(int(0.1 * MM))
        board.Add(s)
    # an MSOP-10: two rows of five 1.5 x 0.35 mm pads at 0.5 mm pitch, 4.4 mm apart
    u = pcbnew.FOOTPRINT(board)
    u.SetReference("U7")
    u.SetPosition(pt(12.2, 11.0))
    board.Add(u)
    row = {1: "+3V3", 2: "SCL", 3: "SDA"}
    for i in range(5):
        add_pad(u, i + 1, 10.0, 10.0 + 0.5 * i, nets.get(row.get(i + 1)), 1.5, 0.35)
        add_pad(u, 10 - i, 14.4, 10.0 + 0.5 * i, None, 1.5, 0.35)
    # the far ends: the I2C pull-ups, and a +3V3 decoupling pad
    r = pcbnew.FOOTPRINT(board)
    r.SetReference("R1")
    board.Add(r)
    add_pad(r, 1, 4.0, 15.0, nets["SCL"], 0.8, 0.8)
    add_pad(r, 2, 4.0, 17.0, nets["SDA"], 0.8, 0.8)
    c = pcbnew.FOOTPRINT(board)
    c.SetReference("C1")
    board.Add(c)
    add_pad(c, 1, 22.0, 5.0, nets["+3V3"], 0.8, 0.8)
    # +3V3 is poured on B.Cu: a plane net, its pads tied down by fanout vias
    z = pcbnew.ZONE(board)
    z.SetLayer(pcbnew.B_Cu)
    z.SetNetCode(nets["+3V3"].GetNetCode())
    z.SetMinThickness(int(0.2 * MM))
    z.SetLocalClearance(int(0.25 * MM))
    o = z.Outline()
    o.NewOutline()
    for x, y in ((1, 1), (29, 1), (29, 19), (1, 19)):
        o.Append(int(x * MM), int(y * MM))
    board.Add(z)
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    board.BuildConnectivity()
    return board, nets


def violations(board, code):
    """(track/via, pad, gap mm) for every piece of the net's copper on F.Cu
    closer than the clearance to another net's pad."""
    out = []
    need = int(CLR * MM) - 1_000
    pads = [p for p in board.GetPads() if p.GetNetCode() != code and p.IsOnLayer(pcbnew.F_Cu)]
    for t in board.GetTracks():
        if t.GetNetCode() != code or not t.IsOnLayer(pcbnew.F_Cu):
            continue
        shape = t.GetEffectiveShape(pcbnew.F_Cu)
        for p in pads:
            if shape.Collide(p.GetEffectiveShape(pcbnew.F_Cu), need):
                out.append((t.GetClass(), p.GetParentFootprint().GetReference() + "." + p.GetNumber()))
    return out


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL {name} {detail}")
        sys.exit(1)
    print(f"ok   {name}")


def main():
    board, nets = make_board()
    rules = NetRules(int(0.5 * MM), int(CLR * MM), int(0.7 * MM), int(0.3 * MM))
    check("with no pre-defined widths the neck is the narrowest class width",
          extract.neck_width(board, rules) == int(0.2 * MM), str(extract.neck_width(board, rules)))

    opts = NativeOptions(pitch_mm=0.1, track_mode="netclass", via_mode="netclass", stitch_vias=False)
    job = pipeline.run_job(board, opts)
    code = nets["+3V3"].GetNetCode()
    tracks = [t for t in board.GetTracks() if t.GetNetCode() == code]
    check("U7.1 gets its +3V3 copper", any(t.GetClass() == "PCB_VIA" for t in tracks),
          f"failed {job.failed}")
    bad = violations(board, code)
    check("no +3V3 stub comes within clearance of another net's pad", not bad, str(bad))
    near = [t for t in tracks if t.GetClass() == "PCB_TRACK"
            and min(abs(t.GetStart().x - 10 * MM) + abs(t.GetStart().y - 10 * MM),
                    abs(t.GetEnd().x - 10 * MM) + abs(t.GetEnd().y - 10 * MM)) < 50_000]
    check("the stub at the fine-pitch pin is necked", near and all(t.GetWidth() <= 0.35 * MM for t in near),
          str([(t.GetWidth(), t.GetStart(), t.GetEnd()) for t in near]))
    print("test_fanout_stub_clearance: all passed")


if __name__ == "__main__":
    main()
