"""A plane-net pad the pour reaches only through a narrow corridor that a
signal route must take (issue #10).

A two-layer board. A GND pour on F.Cu covers it all; locked tracks of
other nets run down the middle with one 1.2 mm gap. GND pad U1.2 sits right
of the wall, and the pour reaches it only through that gap: at the start it
is joined to U1.1, U1.7 and U1.8 on the left. SIG runs from the left to the
right side through the gap (on F.Cu alone its only way): the route closes
the pour's corridor and the refilled pour no longer reaches U1.2. GND is a
plane net, so the pad is no connection of the job: it used to stay open
without a word.

1. Routed on F.Cu only, the pad cannot be joined again: the job must
   report it as failed for GND (failed, failed_detail naming U1.2 with a
   reason, a status line).
2. Routed on F.Cu and B.Cu, the job routes the cut-off pad as a stray of
   GND (under the wall on B.Cu) and the pad ends up joined, not reported.

Both ways a job can run: the pipeline placing the paths (apply_paths) and
a caller placing them as they come (on_path, as the dialog does) and
running the clean-up and the plane check itself.

Builds the board in memory. Needs KiCad's Python; skips otherwise.

  <KiCad python3> tests/test_plane_pad_cutoff.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

try:
    import pcbnew
except ImportError:
    print("test_plane_pad_cutoff: skipped (needs KiCad's Python)")
    sys.exit(0)

from autorouter.engine.native import apply as native_apply, pipeline
from autorouter.engine.native.problem import NativeOptions

MM = 1_000_000
W, H = 30, 20
GAP = (9.4, 10.6)            # the wall's gap (y, mm): room for SIG or the pour, not both


def pt(x, y):
    return pcbnew.VECTOR2I(int(round(x * MM)), int(round(y * MM)))


def add_pad(fp, number, x, y, net, size=0.8):
    p = pcbnew.PAD(fp)
    p.SetNumber(str(number))
    p.SetShape(pcbnew.PAD_SHAPE_RECT)
    p.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    p.SetLayerSet(p.SMDMask())
    p.SetSize(pcbnew.VECTOR2I(int(size * MM), int(size * MM)))
    p.SetPosition(pt(x, y))
    p.SetNet(net)
    fp.Add(p)
    return p


def add_track(board, net, layer, a, b, w=0.25):
    t = pcbnew.PCB_TRACK(board)
    t.SetStart(pt(*a))
    t.SetEnd(pt(*b))
    t.SetWidth(int(w * MM))
    t.SetLayer(layer)
    t.SetNet(net)
    t.SetLocked(True)
    board.Add(t)
    return t


def make_board():
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(2)
    ds = board.GetDesignSettings()
    ds.m_NetSettings.GetDefaultNetclass().SetTrackWidth(int(0.25 * MM))
    ds.m_NetSettings.GetDefaultNetclass().SetClearance(int(0.2 * MM))
    nets = {}
    for name in ("GND", "SIG", "WALL1", "WALL2"):
        nets[name] = pcbnew.NETINFO_ITEM(board, name)
        board.Add(nets[name])
    for (x1, y1), (x2, y2) in (((0, 0), (W, 0)), ((W, 0), (W, H)), ((W, H), (0, H)), ((0, H), (0, 0))):
        s = pcbnew.PCB_SHAPE(board)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetStart(pt(x1, y1))
        s.SetEnd(pt(x2, y2))
        s.SetLayer(pcbnew.Edge_Cuts)
        s.SetWidth(int(0.1 * MM))
        board.Add(s)
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference("U1")
    board.Add(fp)
    add_pad(fp, 1, 5, 15, nets["GND"])
    add_pad(fp, 7, 9, 17, nets["GND"])            # most of GND's pads are left of the wall
    add_pad(fp, 8, 3, 11, nets["GND"])
    add_pad(fp, 2, 22, 10, nets["GND"])
    add_pad(fp, 3, 5, 5, nets["SIG"])
    add_pad(fp, 4, 25, 5, nets["SIG"])
    # the wall: two locked tracks of other nets down x = 15, the gap between them
    add_track(board, nets["WALL1"], pcbnew.F_Cu, (15, 0.1), (15, GAP[0]))
    add_track(board, nets["WALL2"], pcbnew.F_Cu, (15, GAP[1]), (15, H - 0.1))
    z = pcbnew.ZONE(board)
    z.SetLayer(pcbnew.F_Cu)
    z.SetNetCode(nets["GND"].GetNetCode())
    z.SetMinThickness(int(0.2 * MM))
    z.SetLocalClearance(int(0.25 * MM))
    z.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)
    o = z.Outline()
    o.NewOutline()
    for x, y in ((0.3, 0.3), (W - 0.3, 0.3), (W - 0.3, H - 0.3), (0.3, H - 0.3)):
        o.Append(int(x * MM), int(y * MM))
    board.Add(z)
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    board.BuildConnectivity()
    return board, nets


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL {name} {detail}")
        sys.exit(1)
    print(f"ok   {name}")


def options(layers):
    return NativeOptions(pitch_mm=0.1, routable_layers=layers, time_limit_s=60)


def pad_joined(board, gnd):
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    board.BuildConnectivity()
    groups = native_apply._pad_groups(board, board.GetConnectivity(), gnd)
    return len(groups) == 1


def run(mode, layers):
    """One job on a fresh board: (board, nets, job, status lines)."""
    board, nets = make_board()
    lines = []
    if mode == "apply_paths":
        job = pipeline.run_job(board, options(layers), apply_paths=True, status=lines.append)
        return board, nets, job, lines
    # as the dialog runs it: the caller places the paths, finishes the copper
    # and runs the plane check itself
    got = {}
    added = []

    def on_path(path):
        added.extend(native_apply.add_path(board, got["layers"], path))

    def on_layers(ls):
        got["layers"] = ls

    job = pipeline.run_job(board, options(layers), apply_paths=False, on_path=on_path, on_layers=on_layers,
                           status=lines.append)
    pipeline.finish_copper(board, added, options(layers))
    lines += pipeline.check_plane_pads(board, job)
    check(f"{mode}: the check runs once per job", pipeline.check_plane_pads(board, job) == [])
    return board, nets, job, lines


def main():
    board, nets = make_board()
    check("at the start U1.2 is joined by the pour", pad_joined(board, nets["GND"].GetNetCode()))
    for mode in ("apply_paths", "on_path"):
        # 1. F.Cu only: SIG takes the gap, nothing can reach U1.2 again
        board, nets, job, lines = run(mode, [True, False])
        label = f"{mode}, F.Cu"
        check(f"{label}: SIG routed", "SIG" not in job.failed, f"failed {job.failed}")
        check(f"{label}: SIG's route cut U1.2 off the pour", not pad_joined(board, nets["GND"].GetNetCode()))
        detail = [d for d in job.failed_detail if d.get("net") == "GND"]
        check(f"{label}: GND is in failed", "GND" in job.failed, str(job.failed))
        check(f"{label}: U1.2 (only) is named, with a reason",
              any(d.get("pads") == ["U1.2"] and d.get("reason") for d in detail), str(detail))
        check(f"{label}: a status line names it", any("U1.2" in s for s in lines), str(lines))
        # 2. F.Cu and B.Cu: the cut-off pad is routed to its net
        board, nets, job, lines = run(mode, [True, True])
        label = f"{mode}, F.Cu + B.Cu"
        check(f"{label}: SIG routed", "SIG" not in job.failed, f"failed {job.failed}")
        check(f"{label}: U1.2 is joined to GND again", pad_joined(board, nets["GND"].GetNetCode()),
              str(job.stats.get("cut_off_detail")))
        check(f"{label}: the routes to it counted", job.stats.get("cut_off_routed", 0) >= 1, str(job.stats))
        check(f"{label}: a joined pad is not reported", "GND" not in job.failed, str(job.failed))
    print("test_plane_pad_cutoff: all passed")


if __name__ == "__main__":
    main()
