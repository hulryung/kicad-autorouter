"""Builds the dialog against a real board without showing it. Needs KiCad's
Python (pcbnew + wx) and a desktop session: creating a wx.App blocks forever
in a sandboxed or headless shell, so this only runs when
KICAD_AUTOROUTER_GUI_TESTS=1 is set."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

try:
    import pcbnew
    import wx
except ImportError:
    print("test_dialog_smoke: skipped (needs KiCad's Python)")
    sys.exit(0)

HERE = Path(__file__).resolve().parent


def main():
    if os.environ.get("KICAD_AUTOROUTER_GUI_TESTS") != "1":
        print("test_dialog_smoke: skipped (set KICAD_AUTOROUTER_GUI_TESTS=1 in a desktop session)")
        return
    board = pcbnew.LoadBoard(str(HERE / "fixtures" / "obd2-adapter.kicad_pcb"))
    app = wx.App(False)
    from autorouter.dialog import AutorouterDialog
    from autorouter.engine.options import RouteOptions

    dialog = AutorouterDialog(None, board)
    assert dialog.routable.GetCount() == 2 and dialog.skip_classes.GetCount() == 1
    o = dialog._collect_options()
    assert o.routable_layers == [True, True] and o.horizontal_layers is None
    assert o.ignore_net_classes == []
    dialog._load_options(RouteOptions(native_time_limit_min=7, ignore_net_classes=["Default"],
                                      horizontal_layers=[False, True]))
    o = dialog._collect_options()
    assert o.native_time_limit_min == 7 and o.ignore_net_classes == ["Default"]
    assert o.horizontal_layers == [False, True]
    # the Signal integrity tab: a preset class, a template stackup, sizes computed, options carry them
    from autorouter.engine import si
    from autorouter.si_page import PRESETS
    page = dialog.si_page
    page.stackup.SetSelection(page.stackups.index("JLC 2L 1.6mm"))
    page._add_row(page.zgrid, ["Z50", "*", "50", "0", "0", "0"])
    cfg = page.config()
    assert cfg.stackup == "JLC 2L 1.6mm" and cfg.impedance[0].single_ohm == 50 and cfg.route_order == si.DEFAULT_ORDER
    assert "Z50" in page.sizes.GetValue(), page.sizes.GetValue()
    native = dialog.native_options(dialog._collect_options())
    assert native.impedance and native.route_order == si.DEFAULT_ORDER
    assert len(PRESETS) >= 5
    dialog._show_progress("x", 0.5)
    assert dialog.gauge.GetValue() == 500
    dialog.Destroy()
    app.Destroy()
    print("test_dialog_smoke: ok")


if __name__ == "__main__":
    main()
