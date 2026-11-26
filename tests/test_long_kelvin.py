import sys, time, json, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
try:
    import pcbnew
except ImportError:
    print("test_long_kelvin: skipped (needs KiCad Python)")
    raise SystemExit(0)
from test_shove_scope import outline, add_pad, add_track, add_via
from autorouter.engine.kelvin import route_kelvin
from autorouter.engine.native.problem import NativeOptions

b = pcbnew.BOARD()
outline(b, 115, 65)
nc = b.GetDesignSettings().m_NetSettings.GetDefaultNetclass()
nc.SetClearance(1500000)
nc.SetTrackWidth(250000)
f = pcbnew.FOOTPRINT(b)
f.SetReference("J1")
b.Add(f)
for i in range(9):
    y = 5 + 6 * i
    x = 40 + 7.5 * i
    n = pcbnew.NETINFO_ITEM(b, "C" + str(i))
    b.Add(n)
    add_pad(f, 2 * i + 1, 10, y, n)
    add_pad(f, 2 * i + 2, x, y, n)
    target = next(p for p in f.Pads() if p.GetNumber() == str(2 * i + 2))
    target.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
    target.SetLayerSet(target.PTHMask())
    target.SetSize(pcbnew.VECTOR2I(1800000, 1800000))
    target.SetDrillSize(pcbnew.VECTOR2I(600000, 600000))
    add_track(b, n, pcbnew.F_Cu, (10, y), (11, y), 0.25)
    add_via(b, n, 11, y)
    add_track(b, n, pcbnew.F_Cu, (x, y), (x, y + 2), 0.25)
    add_track(b, n, pcbnew.F_Cu, (x, y + 2), (10, y + 2), 0.25)
b.BuildConnectivity()
t = time.perf_counter()
reports = []
folder = tempfile.TemporaryDirectory()
root = Path(folder.name)
for i in range(9):
    old_ids = {t.m_Uuid.AsString() for t in b.GetTracks()}
    r = route_kelvin(
        b,
        "J1." + str(2 * i + 1),
        "J1." + str(2 * i + 2),
        root / str(i),
        options=NativeOptions(pitch_mm=0.1, time_limit_s=30),
        separation_mm=0.15,
    )
    reports.append(r)
    b = pcbnew.LoadBoard(r["board"])
    assert r["after_opens"].get("C" + str(i), 0) == 0, r
    added = [
        t
        for t in b.GetTracks()
        if t.m_Uuid.AsString() not in old_ids and t.GetClass() != "PCB_VIA"
    ]
    assert (
        sum(t.GetLength() for t in added) >= (29 + 7.5 * i) * 1e6 - 200000
    ), "route took a same-net force shortcut"
    print("joined", i, round(time.perf_counter() - t, 2), flush=True)
assert not reports[-1]["after_opens"], reports[-1]
folder.cleanup()
print(
    "test_long_kelvin: nine 30-90 mm sense routes, 1.5 mm domain clearance and 0.15 mm force separation passed"
)
