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


if __name__ == "__main__":
    test_comments()
    print("test_dru_comments: ok")
