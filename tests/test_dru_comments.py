"""A '#' comment line in a .kicad_dru is a comment, whatever it holds: one
with "Net-(" in it once made the router read no rule at all (and route with
net-class clearances only, an 8-cell board's isolation gaps gone).

  python3 tests/test_dru_comments.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

import importlib.util
spec = importlib.util.spec_from_file_location(
    "dru", Path(__file__).resolve().parents[1] / "plugins/autorouter/engine/native/dru.py")
dru = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dru)

RULE = "(rule \"x\" (condition \"A.NetName == 'a'\") (constraint clearance (min 1mm)))"
CLASSES = {"Default": 150_000}


def test_comments():
    for head in ("", "# note: Net-( names\n", "   # indented ( comment\n", "# balanced (comment)\n",
                 "# '(' and \"(\" in quotes\n"):
        rules = dru.ClearanceRules.parse("(version 1)\n" + head + RULE, CLASSES).rules
        assert len(rules) == 1 and rules[0].clearance == 1_000_000, (head, rules)
    # a '#' inside a quoted condition is not a comment
    r2 = "(rule \"y\" (condition \"A.NetName == '#1'\") (constraint clearance (min 2mm)))"
    rules = dru.ClearanceRules.parse("(version 1)\n" + r2, CLASSES)
    a, b = dru.Item(netname="#1"), dru.Item(netname="z")
    assert rules.between(a, b) == 2_000_000
    print("comments: ok")


class _Board:
    """Just enough of a pcbnew BOARD for dru.for_board."""
    def __init__(self, path):
        self.path = path

    def GetFileName(self):
        return self.path

    def GetDesignSettings(self):
        class NC:
            def GetClearance(self):
                return 150_000

        class NS:
            def GetNetclasses(self):
                return {}

            def GetDefaultNetclass(self):
                return NC()

        class DS:
            m_NetSettings = NS()
            m_MinClearance = 100_000
        return DS()


def test_problem_only_for_lost_clearances():
    """A file of fabrication rules only (the plugin's own fab presets) has
    nothing for the router: no warning. Clearance rules it cannot read: one."""
    import tempfile
    fab = ('(version 1)\n# --- autorouter fab begin ---\n'
           '(rule "fab: plated pad hole to copper" (constraint hole_clearance (min 0.3mm)) '
           '(condition "A.Type == \'Pad\' && A.isPlated()"))\n'
           '(rule "fab: silk" (layer "?.Silkscreen") (constraint text_thickness (min 0.15mm)))\n')
    lost = '(version 1)\n(rule "iso" (condition "A.Foo(\'x\')") (constraint clearance (min 8mm)))\n'
    with tempfile.TemporaryDirectory() as d:
        for text, warned in ((fab, False), (lost, True)):
            Path(d, "b.kicad_dru").write_text(text)
            assert dru.for_board(_Board(str(Path(d, "b.kicad_pcb")))) is None
            assert bool(dru.LAST_PROBLEM) == warned, (warned, dru.LAST_PROBLEM)
    print("only lost clearance rules are reported: ok")


if __name__ == "__main__":
    test_problem_only_for_lost_clearances()
    test_comments()
    print("test_dru_comments: ok")
