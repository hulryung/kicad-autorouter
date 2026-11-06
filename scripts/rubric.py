"""A fixed rubric for a benchmark result: the overall score from criteria
with fixed thresholds instead of a reviewer's free 0-100 judgement.

Nine of the eleven criteria are measured from the benchmark (metrics.json,
the routed board and the reference) by the formulas below, so the same board
always gets the same numbers. Only the two that need eyes, similarity to the
reference and the visual half of neatness, come from reviewer evaluations:
the median over every evaluation given, which damps one reviewer's swing.
The overall score is the criteria's sum scaled to 0-100.

Run with KiCad's Python (pcbnew):
  python3 scripts/rubric.py --bench DIR --reference REF.kicad_pcb [EVAL.json ...]
"""
import argparse
import fnmatch
import json
import statistics
import sys
from pathlib import Path

CRITERIA = ["connectivity", "drc", "similarity_to_reference", "escape_and_fanout", "directness", "via_use", "neatness",
            "impedance_widths", "diff_pairs", "length_matching", "routing_priority"]
REVIEWED = ("similarity_to_reference", "neatness")

POWER_PATTERNS = ["+*", "V*", "*VCC*", "*VDD*", "*VBUS*", "*VIN*", "*VOUT*", "*3V3*", "*5V*", "*12V*", "*24V*",
                  "*PWR*", "*POWER*", "*VBAT*"]
CONTROL_WORDS = {"EN", "BUT", "LED", "OC", "NOC", "GOOD", "PG", "SENSE", "FAULT", "FLT", "ALERT", "INT", "RST",
                 "NRST", "DET"}
RAIL_CAP_NM = 500_000          # a rail at the designer's width, or this wide, counts as sized right


def clamp(v: float) -> float:
    return round(max(0.0, min(10.0, v)), 2)


def is_rail(name: str) -> bool:
    name = name.split("/")[-1].upper()
    if not any(fnmatch.fnmatchcase(name, p.upper()) for p in POWER_PATTERNS):
        return False
    words = {w for w in name.replace("-", "_").split("_") if w}
    return not (words & CONTROL_WORDS or any(w.rstrip("0123456789") in CONTROL_WORDS for w in words))


def rail_width_share(routed_path: Path, reference_path: Path):
    """Share of the routed power-rail track length that is at least as wide as
    the designer made that rail (the length-weighted median of the reference's
    widths for the net, capped at RAIL_CAP_NM). None without rails to compare."""
    import pcbnew

    def widths(board):
        out = {}
        for t in board.GetTracks():
            if t.GetClass() == "PCB_TRACK" and is_rail(t.GetNetname()):
                out.setdefault(t.GetNetname(), []).append((t.GetWidth(), t.GetLength()))
        return out

    ref = widths(pcbnew.LoadBoard(str(reference_path)))
    got = widths(pcbnew.LoadBoard(str(routed_path)))
    ok = total = 0.0
    for net, segs in got.items():
        if net not in ref:
            continue
        rs = sorted(ref[net])
        half, acc, target = sum(l for _, l in rs) / 2, 0.0, rs[-1][0]
        for w, l in rs:
            acc += l
            if acc >= half:
                target = w
                break
        target = min(target, RAIL_CAP_NM)
        for w, l in segs:
            total += l
            if w >= target - 1_000:
                ok += l
    return ok / total if total else None


def measured(m: dict, rail_share) -> dict:
    """The nine criteria a board's numbers decide. Thresholds are fixed:
    each costs points per problem as written, from 10 down to 0."""
    c = {}
    c["connectivity"] = clamp(10 - 2 * (m.get("unconnected_after") or 0))
    warnings = sum(v for k, v in (m.get("drc") or {}).items() if k.endswith("/warning"))
    c["drc"] = clamp(10 - 2 * (m.get("drc_errors") or 0) - (m.get("rule_errors") or 0) - 0.5 * warnings)
    agree = str(m.get("escape_side_agreement") or "0 of 0").split(" of ")
    c["escape_and_fanout"] = clamp(10 * int(agree[0]) / int(agree[1])) if int(agree[1]) else 10.0
    ref = m.get("reference") or {}
    longer = m.get("nets_much_longer_than_reference") or []
    ratio = (m.get("length_mm") or 0) / ref["length_mm"] if ref.get("length_mm") else 1.0
    c["directness"] = clamp(10 - 0.5 * len(longer) - 10 * max(0.0, ratio - 1))
    via_ratio = (m.get("vias") or 0) / ref["vias"] if ref.get("vias") else 1.0
    extra_via_nets = sum(1 for n in longer if (n.get("vias") or 0) > (n.get("reference_vias") or 0))
    c["via_use"] = clamp(10 - 0.5 * extra_via_nets - 10 * max(0.0, via_ratio - 1))
    c["neatness_measured"] = clamp(10 - 0.25 * (m.get("off_45") or 0) - (m.get("acute_corners") or 0)
                                   - (m.get("doubled_back") or 0) - (m.get("dangling") or 0))
    intent = m.get("intent") or {}
    imp = intent.get("impedance_width_ok_pct")
    if imp is not None:
        c["impedance_widths"] = clamp(imp / 10)
    else:
        c["impedance_widths"] = clamp(10 * rail_share) if rail_share is not None else 10.0
    pairs = intent.get("pairs") or []
    c["diff_pairs"] = clamp(statistics.mean(p["coupled_pct"] for p in pairs) / 10) if pairs else 10.0
    groups = str(intent.get("length_groups_ok") or "").split(" of ")
    c["length_matching"] = clamp(10 * int(groups[0]) / int(groups[1])) if len(groups) == 2 and int(groups[1]) else 10.0
    opens = sum((intent.get("open_by_group") or {}).values()) if isinstance(intent.get("open_by_group"), dict) else 0
    c["routing_priority"] = clamp(10 - 2 * opens)
    return c


def score(m: dict, rail_share, evaluations: list) -> dict:
    c = measured(m, rail_share)
    reviewed = {}
    for k in REVIEWED:
        vals = [e["criteria"][k] for e in evaluations if k in (e.get("criteria") or {})]
        reviewed[k] = statistics.median(vals) if vals else None
    c["similarity_to_reference"] = reviewed["similarity_to_reference"]
    seen = reviewed["neatness"]
    c["neatness"] = clamp((c["neatness_measured"] + seen) / 2) if seen is not None else c["neatness_measured"]
    missing = [k for k in CRITERIA if c.get(k) is None]
    total = sum(c[k] for k in CRITERIA if c.get(k) is not None)
    count = len(CRITERIA) - len(missing)
    return {"score": round(100 * total / (10 * count), 1) if count else None,
            "criteria": {k: c.get(k) for k in CRITERIA}, "neatness_measured": c["neatness_measured"],
            "reviews": len(evaluations), "reviewer_scores": [e.get("score") for e in evaluations],
            "rail_width_share": None if rail_share is None else round(rail_share, 3), "missing": missing}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bench", required=True, help="benchmark directory (metrics.json, routed.kicad_pcb)")
    ap.add_argument("--reference", required=True)
    ap.add_argument("evaluations", nargs="*", help="reviewer evaluation JSON files")
    args = ap.parse_args(argv)
    bench = Path(args.bench)
    m = json.loads((bench / "metrics.json").read_text())
    evaluations = []
    for f in args.evaluations:
        text = Path(f).read_text()
        evaluations.append(json.loads(text[text.find("{"):text.rfind("}") + 1]))
    share = rail_width_share(bench / "routed.kicad_pcb", Path(args.reference))
    out = score(m, share, evaluations)
    (bench / "rubric.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out))


if __name__ == "__main__":
    sys.exit(main())
