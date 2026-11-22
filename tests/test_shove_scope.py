"""Shove mode in a scoped job, as a headless second pass runs it.

1. Pads a filled pour joins are one group. Three +3V3 pads on F.Cu reach a
   +3V3 pour on In2.Cu through their own (unlocked) stubs and vias; two of
   them sit off the pour, so by position alone they read as strays. The
   job, scoped to +3V3 and a signal, must not tie them together with
   tracks: as a plane net (plane_nets on) and as a routed net (plane_nets
   off). In shove mode the pads used to be grouped by locked copper and
   pads on the fill only, so a plane net's stub-and-via reached nothing.
2. The tracks of a differential pair outside the scope are locked copper
   for shove (not movable pieces), its lengths unchanged by the job; with
   scope_locks_pairs off they are movable again, and a whole-board job
   leaves them movable as before.

Builds the boards in memory. Needs KiCad's Python; skips otherwise.

  <KiCad python3> tests/test_shove_scope.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

try:
    import pcbnew
except ImportError:
    print("test_shove_scope: skipped (needs KiCad's Python)")
    sys.exit(0)

from autorouter.engine.native import extract, pipeline
from autorouter.engine.native.problem import NativeOptions, SCOPE_BOARD, SCOPE_SELECTION

MM = 1_000_000


def pt(x, y):
    return pcbnew.VECTOR2I(int(round(x * MM)), int(round(y * MM)))


def outline(board, w, h):
    for (x1, y1), (x2, y2) in (((0, 0), (w, 0)), ((w, 0), (w, h)), ((w, h), (0, h)), ((0, h), (0, 0))):
        s = pcbnew.PCB_SHAPE(board)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetStart(pt(x1, y1))
        s.SetEnd(pt(x2, y2))
        s.SetLayer(pcbnew.Edge_Cuts)
        s.SetWidth(int(0.1 * MM))
        board.Add(s)


def add_pad(fp, number, x, y, net, w=0.8, h=0.8):
    p = pcbnew.PAD(fp)
    p.SetNumber(str(number))
    p.SetShape(pcbnew.PAD_SHAPE_RECT)
    p.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    p.SetLayerSet(p.SMDMask())
    p.SetSize(pcbnew.VECTOR2I(int(w * MM), int(h * MM)))
    p.SetPosition(pt(x, y))
    p.SetNet(net)
    fp.Add(p)


def add_track(board, net, layer, a, b, w=0.3):
    t = pcbnew.PCB_TRACK(board)
    t.SetStart(pt(*a))
    t.SetEnd(pt(*b))
    t.SetWidth(int(w * MM))
    t.SetLayer(layer)
    t.SetNet(net)
    board.Add(t)
    return t


def add_via(board, net, x, y):
    v = pcbnew.PCB_VIA(board)
    v.SetPosition(pt(x, y))
    v.SetWidth(int(0.6 * MM))
    v.SetDrill(int(0.3 * MM))
    v.SetNet(net)
    board.Add(v)
    return v


def pour_board():
    """40 x 20 mm, 4 layers; a +3V3 pour on In2.Cu over the right 3/4."""
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(4)
    ds = board.GetDesignSettings()
    ns = ds.m_NetSettings
    ns.GetDefaultNetclass().SetTrackWidth(int(0.25 * MM))
    ns.GetDefaultNetclass().SetClearance(int(0.15 * MM))
    vcc = pcbnew.NETINFO_ITEM(board, "+3V3")
    board.Add(vcc)
    sig = pcbnew.NETINFO_ITEM(board, "SIG")
    board.Add(sig)
    outline(board, 40, 20)
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference("U1")
    board.Add(fp)
    # two pads well off the pour, each with a stub east to its via in the pour
    for n, (x, y, vx) in enumerate(((3, 5, 13), (3, 15, 13)), 1):
        add_pad(fp, n, x, y, vcc)
        add_track(board, vcc, pcbnew.F_Cu, (x, y), (vx, y))
        add_via(board, vcc, vx, y)
    # three over the pour (most of the net's pads: a plane net), each with a
    # short stub to its via
    for n, y in ((3, 5), (4, 10), (5, 15)):
        add_pad(fp, n, 30, y, vcc)
        add_track(board, vcc, pcbnew.F_Cu, (30, y), (31.5, y))
        add_via(board, vcc, 31.5, y)
    add_pad(fp, 6, 20, 3, sig)
    add_pad(fp, 7, 20, 17, sig)
    z = pcbnew.ZONE(board)
    z.SetLayer(pcbnew.In2_Cu)
    z.SetNetCode(vcc.GetNetCode())
    z.SetMinThickness(int(0.2 * MM))
    z.SetLocalClearance(int(0.2 * MM))
    o = z.Outline()
    o.NewOutline()
    for x, y in ((10, 0.5), (39.5, 0.5), (39.5, 19.5), (10, 19.5)):
        o.Append(int(x * MM), int(y * MM))
    board.Add(z)
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    board.BuildConnectivity()
    return board, vcc.GetNetCode()


def pair_board():
    """30 x 20 mm, 2 layers: a USB_DP/USB_DN pair already routed straight
    across, and SIG to route from top to bottom across it."""
    board = pcbnew.BOARD()
    ds = board.GetDesignSettings()
    ds.m_NetSettings.GetDefaultNetclass().SetTrackWidth(int(0.2 * MM))
    ds.m_NetSettings.GetDefaultNetclass().SetClearance(int(0.15 * MM))
    nets = {}
    for name in ("USB_DP", "USB_DN", "SIG"):
        nets[name] = pcbnew.NETINFO_ITEM(board, name)
        board.Add(nets[name])
    outline(board, 30, 20)
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference("J1")
    board.Add(fp)
    add_pad(fp, 1, 4, 9.6, nets["USB_DP"], 0.6, 0.3)
    add_pad(fp, 2, 4, 10.4, nets["USB_DN"], 0.6, 0.3)
    fp2 = pcbnew.FOOTPRINT(board)
    fp2.SetReference("U1")
    board.Add(fp2)
    add_pad(fp2, 1, 26, 9.6, nets["USB_DP"], 0.6, 0.3)
    add_pad(fp2, 2, 26, 10.4, nets["USB_DN"], 0.6, 0.3)
    fp3 = pcbnew.FOOTPRINT(board)
    fp3.SetReference("R1")
    board.Add(fp3)
    add_pad(fp3, 1, 15, 3, nets["SIG"])
    add_pad(fp3, 2, 15, 17, nets["SIG"])
    for name, y in (("USB_DP", 9.6), ("USB_DN", 10.4)):
        add_track(board, nets[name], pcbnew.F_Cu, (4, y), (26, y), 0.2)
    board.BuildConnectivity()
    return board, nets


def new_tracks(board, code, before):
    return [t for t in board.GetTracks() if t.GetNetCode() == code and t.GetClass() == "PCB_TRACK"
            and str(t.m_Uuid.AsString()) not in before]


def lengths(board, names):
    return {n: round(sum(t.GetLength() for t in board.GetTracks()
                         if t.GetNetname() == n and t.GetClass() != "PCB_VIA") / MM, 3) for n in names}


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL {name} {detail}")
        sys.exit(1)
    print(f"ok   {name}")


def main():
    for planes in (True, False):
        what = "plane net" if planes else "routed net"
        board, vcc = pour_board()
        opts = NativeOptions(pitch_mm=0.1, shove=True, plane_nets=planes, stitch_vias=False,
                             scope=SCOPE_SELECTION, scope_nets=["+3V3", "SIG"])
        problem = extract.extract(board, opts)
        net = problem.nets[vcc]
        check(f"{what}: +3V3 is {'a plane' if planes else 'routed'}", net.route != planes, str(net.route))
        if planes:
            conns = [c for c in problem.connections if c.net == vcc]
            check(f"{what}: the pads the In2 pour joins need no connection", not conns,
                  f"{len(conns)} connection(s), {len(net.clusters)} groups")
        else:
            # its unlocked stubs and vias are movable, and the pour joins them
            # into one piece that already joins every pad
            joined = {c for ex in problem.existing if ex.path.net == vcc for j in ex.joins for c in j}
            check(f"{what}: one movable piece joins the pads through the pour",
                  joined == set(range(len(net.clusters))), f"{joined} of {len(net.clusters)} groups")
        board, vcc = pour_board()
        before = {str(t.m_Uuid.AsString()) for t in board.GetTracks()}
        job = pipeline.run_job(board, opts)
        added = new_tracks(board, vcc, before)
        check(f"{what}: no +3V3 track joins them again", not added,
              str([(t.GetStart().x / MM, t.GetStart().y / MM, t.GetEnd().x / MM, t.GetEnd().y / MM) for t in added]))
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
        board.BuildConnectivity()
        check(f"{what}: the board is fully connected", board.GetConnectivity().GetUnconnectedCount(True) == 0
              and not job.failed, f"failed {job.failed}")

    names = ["USB_DP", "USB_DN"]
    for label, scope, locks, frozen in (("scoped job", SCOPE_SELECTION, True, True),
                                        ("scoped job, scope_locks_pairs off", SCOPE_SELECTION, False, False),
                                        ("whole-board job", SCOPE_BOARD, True, False)):
        board, nets = pair_board()
        opts = NativeOptions(pitch_mm=0.1, shove=True, scope=scope, scope_nets=["SIG"] if scope != SCOPE_BOARD else [],
                             scope_locks_pairs=locks)
        problem = extract.extract(board, opts)
        pair_codes = {nets[n].GetNetCode() for n in names}
        movable = [ex for ex in problem.existing if ex.path.net in pair_codes]
        check(f"{label}: the pair's tracks are {'locked' if frozen else 'movable'}",
              (not movable) if frozen else bool(movable), f"{len(movable)} movable piece(s)")
    board, nets = pair_board()
    start = lengths(board, names)
    job = pipeline.run_job(board, NativeOptions(pitch_mm=0.1, shove=True, scope=SCOPE_SELECTION, scope_nets=["SIG"]))
    after = {d["pair"]: d["length_mm"] for d in job.stats.get("pair_lengths", [])}
    check("scoped job: SIG is routed", not job.failed, str(job.failed))
    check("scoped job: the pair outside the scope keeps its lengths", lengths(board, names) == start
          and after.get("USB_DN / USB_DP") == [start["USB_DN"], start["USB_DP"]],
          f"{start} -> {lengths(board, names)}, stats {after}")
    print("test_shove_scope: all passed")


if __name__ == "__main__":
    main()
