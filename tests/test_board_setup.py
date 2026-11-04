"""Board setup check: pattern matching and repair, net groups, size lists;
then the check and its fixes on a copy of the fixture board. The pcbnew
part skips without KiCad's Python.

  python3 tests/test_board_setup.py
"""
import functools
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

from autorouter.engine import board_setup as bs

print = functools.partial(print, flush=True)
HERE = Path(__file__).resolve().parent


def test_helpers():
    # KiCad's matching: wildcards or a regular expression over the whole name, case-sensitive
    assert bs.pattern_matches("USB2*", "USB2_P") and not bs.pattern_matches("usb2*", "USB2_P")
    assert bs.pattern_matches("/CM5_GPIO/TRD2_[PN]", "/CM5_GPIO/TRD2_P")
    assert bs.pattern_matches("USB3-0-D_.", "USB3-0-D_N")            # a regex
    assert not bs.pattern_matches("TRD0_P", "/CM5_GPIO/TRD0_P")       # anchored: the sheet path counts
    names = ["USB2_P", "USB2_N", "/CM5_GPIO/TRD0_P", "/CM5_GPIO/TRD0_N", "GND"]
    stale = bs.stale_patterns([("/USB2*", "90R"), ("*GND*", "Power"), ("/Old sheet/TRD*", "100R")], names)
    assert stale == [("/USB2*", "90R"), ("/Old sheet/TRD*", "100R")], stale
    # a sheet path that was never part of the name, or that changed
    assert bs.repaired_pattern("/USB2*", names) == ("USB2*", ["USB2_P", "USB2_N"])
    fixed, hits = bs.repaired_pattern("/Old sheet/TRD*", names)
    assert fixed == "*/TRD*" and len(hits) == 2
    assert bs.repaired_pattern("/HDMI*", names) is None and bs.repaired_pattern("NOPE*", names) is None
    # power names, but not the control pins around them nor straps named after them
    for n in ("+3V3", "/Power/VIN_PD", "VBUS_IN", "+3.3v", "Net-(D301-VBUS)", "Net-(U401-VCCD)", "/RADAR_5V"):
        assert bs.is_power_name(n), n
    for n in ("HUB_PWR_EN1", "PWR_BUT", "Net-(U401-VBUS_MAX)", "Net-(U304-SUSP_IND{slash}LOCAL_PWR)", "GND",
              "I2C_SDA", "unconnected-(U1-VCC-Pad3)", "LED_PWR"):
        assert not bs.is_power_name(n), n
    assert bs.exact_pattern("Net-(U3-A{slash}B)") == "Net-(U3-A*B)"
    # pair families and the class they belong in
    assert bs.pair_family("/USB_Hub/HUB_DN1_P") == "USB" and bs.pair_family("/CM5_GPIO/TRD0_N") == "Ethernet"
    classes = {"Default": {"diff_pair_width": 200_000}, "90R": {"diff_pair_width": 147_000},
               "100R": {"diff_pair_width": 127_000}, "50R": {"diff_pair_width": 0}}
    assert bs.class_for_family("USB", classes) == "90R" and bs.class_for_family("Ethernet", classes) == "100R"
    assert bs.class_for_family("CAN / RS-485", classes) is None
    assert bs.size_list_additions([0, 200_000], [200_000, 400_000, 0]) == [400_000]
    print("test_board_setup: helpers ok")


def test_board():
    try:
        import pcbnew
    except ImportError:
        print("test_board_setup: board part skipped (needs KiCad's Python)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "obd2-adapter.kicad_pcb"
        shutil.copy(HERE / "fixtures" / "obd2-adapter.kicad_pcb", path)
        board = pcbnew.LoadBoard(str(path))
        # a project whose pattern was written for a sheet the nets are not on
        board.GetDesignSettings().m_NetSettings.SetNetclass("CAN", pcbnew.NETCLASS("CAN"))
        path.with_suffix(".kicad_pro").write_text(json.dumps(
            {"net_settings": {"netclass_patterns": [{"netclass": "CAN", "pattern": "/Old/CAN*"},
                                                    {"netclass": "Gone", "pattern": "/Old/X*"}]}}))
        found = {f.key: f for f in bs.check(board, "JLCPCB 2-layer standard")}
        assert {"outline", "fab", "stale_patterns", "power_default", "predefined_sizes"} <= set(found), list(found)
        assert found["outline"].severity == bs.ERROR and not found["outline"].fixable
        assert bs.blocking(found.values()) == [found["outline"]]
        assert "*/CAN*" in found["stale_patterns"].detail and "no net class Gone" in found["stale_patterns"].detail
        assert not found["power_default"].default_on                  # a new net class: the user opts in
        assert "edge_clearance" not in found                          # the fab template covers it
        done = bs.apply(board, [f for f in found.values() if f.fixable])
        assert any("Power" in line for line in done) and any("pre-defined" in line for line in done), done
        ds = board.GetDesignSettings()
        assert ds.m_MinClearance >= 127_000 and ds.m_CopperEdgeClearance >= 300_000
        widths = [int(w) for w in ds.m_TrackWidthList]
        assert widths[0] == 0 and 400_000 in widths, widths             # index 0 stays "the net class value"
        assert board.FindNet("/VBAT").GetNetClassName() == "Power"
        assert board.FindNet("/CANH").GetNetClassName() == "CAN"
        assert path.with_suffix(".kicad_dru").exists()
        again = {f.key for f in bs.check(board, "JLCPCB 2-layer standard")}
        assert not again & {"fab", "power_default", "predefined_sizes"}, again
        # an empty template choice: the edge clearance is checked on its own
        ds.m_CopperEdgeClearance = 10_000
        assert "edge_clearance" in {f.key for f in bs.check(board)}
    print("test_board_setup: board ok")


if __name__ == "__main__":
    test_helpers()
    test_board()
