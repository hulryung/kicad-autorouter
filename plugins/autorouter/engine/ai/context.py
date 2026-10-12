"""The board as the model sees it: a compact JSON description."""
import json
import math
import os
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional


def _mm(nm: int) -> float:
    return round(nm / 1e6, 4)


def kicad_cli() -> Optional[str]:
    import shutil
    for cand in (shutil.which("kicad-cli"), "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
                 r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe", r"C:\Program Files\KiCad\9.0\bin\kicad-cli.exe"):
        if cand and os.path.isfile(cand):
            return cand
    return None


def drc_summary(board, workdir: Path, limit: int = 40) -> dict:
    """KiCad's DRC on a saved copy of the board (zones refilled), condensed."""
    import pcbnew
    cli = kicad_cli()
    if not cli:
        return {"available": False}
    copy = workdir / "drc-copy.kicad_pcb"
    report = workdir / "drc.json"
    try:
        pcbnew.SaveBoard(str(copy), board)
        subprocess.run([cli, "pcb", "drc", "--format", "json", "--severity-error", "--refill-zones",
                        "-o", str(report), str(copy)], capture_output=True, text=True, timeout=300)
        data = json.loads(report.read_text(encoding="utf-8"))
    except Exception as e:
        return {"available": False, "error": str(e)}
    kinds = Counter(v.get("type") for v in data.get("violations", []))
    items = []
    for v in data.get("violations", [])[:limit]:
        items.append({"type": v.get("type"), "description": v.get("description", "")[:140],
                      "items": [it.get("description", "")[:120] for it in v.get("items", [])],
                      "pos_mm": (v.get("items") or [{}])[0].get("pos")})
    unconnected = Counter()
    opens = []
    for u in data.get("unconnected_items", []):
        desc = u["items"][0]["description"] if u.get("items") else ""
        if "[" in desc:
            unconnected[desc.split("[")[1].split("]")[0]] += 1
        if len(opens) < limit:
            opens.append({"items": [it.get("description", "")[:100] for it in u.get("items", [])],
                          "pos_mm": [[it.get("pos", {}).get("x"), it.get("pos", {}).get("y")]
                                     for it in u.get("items", [])]})
    return {"available": True, "violations_by_type": dict(kinds), "violations": items,
            "unconnected_total": len(data.get("unconnected_items", [])),
            "unconnected_by_net": dict(unconnected.most_common(40)),
            "unconnected": opens}


def unconnected_edges(board, limit: int = 60) -> List[dict]:
    """Where the open connections are, from the live board: for every net
    whose copper is in more than one piece, the nearest pad pair between the
    largest piece and each other piece (what the ratsnest draws), with pad
    names, positions and whether each side already sits on a pour."""
    from ..native import extract
    conn = board.GetConnectivity()
    pads_by_net: Dict[int, list] = {}
    for pad in board.GetPads():
        if pad.GetNetCode() > 0:
            pads_by_net.setdefault(pad.GetNetCode(), []).append(pad)
    out: List[dict] = []
    for code, pads in sorted(pads_by_net.items()):
        if len(pads) < 2:
            continue
        name = board.FindNet(code).GetNetname()

        def key(pad):
            fp = extract.footprint_of(pad)
            pos = pad.GetPosition()
            return (fp.GetReference() if fp else "?", pad.GetNumber(), pos.x, pos.y)

        index = {key(pad): i for i, pad in enumerate(pads)}
        parent = list(range(len(pads)))
        on_pour = [False] * len(pads)

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i, pad in enumerate(pads):
            try:
                linked = conn.GetConnectedItems(pad)
            except Exception:
                linked = []
            for other in linked:
                cls = other.GetClass()
                if cls == "ZONE":
                    on_pour[i] = True
                elif cls == "PAD":
                    j = index.get(key(other))
                    if j is not None:
                        parent[find(i)] = find(j)
        groups: Dict[int, List[int]] = {}
        for i in range(len(pads)):
            groups.setdefault(find(i), []).append(i)
        if len(groups) < 2:
            continue
        roots = sorted(groups, key=lambda r: -len(groups[r]))
        main = groups[roots[0]]
        for r in roots[1:]:
            best = None
            for i in main:
                pi = pads[i].GetPosition()
                for j in groups[r]:
                    pj = pads[j].GetPosition()
                    d = abs(pi.x - pj.x) + abs(pi.y - pj.y)
                    if best is None or d < best[0]:
                        best = (d, i, j)
            d, i, j = best
            ki, kj = key(pads[i]), key(pads[j])
            out.append({"net": name,
                        "from": f"{kj[0]}.{kj[1]}", "from_mm": [_mm(kj[2]), _mm(kj[3])],
                        "from_on_pour": any(on_pour[k] for k in groups[r]),
                        "to": f"{ki[0]}.{ki[1]}", "to_mm": [_mm(ki[2]), _mm(ki[3])],
                        "to_on_pour": any(on_pour[k] for k in main),
                        "piece_pads": len(groups[r]), "distance_mm": round(math.hypot(ki[2] - kj[2], ki[3] - kj[3]) / 1e6, 2)})
            if len(out) >= limit:
                return out
    return out


def board_context(board, options: dict, last_run: Optional[dict], workdir: Path,
                  selection: Optional[dict] = None, with_drc: bool = True) -> dict:
    """Everything a routing assistant needs to reason about this board."""
    import pcbnew
    from ..native import extract

    ds = board.GetDesignSettings()
    ns = ds.m_NetSettings
    bb = board.GetBoardEdgesBoundingBox()
    layers = [(lid, board.GetLayerName(lid)) for lid in board.GetEnabledLayers().CuStack()]
    sizes = extract.board_sizes(board)

    classes = []
    try:
        names = ["Default"] + [str(k) for k in ns.GetNetclasses().keys()]
    except Exception:
        names = ["Default"]
    for name in names:
        try:
            nc = ns.GetNetClassByName(name) if name != "Default" else ns.GetDefaultNetclass()
            classes.append({"name": name, "track_width_mm": _mm(nc.GetTrackWidth()),
                            "clearance_mm": _mm(nc.GetClearance()), "via_mm": _mm(nc.GetViaDiameter()),
                            "via_drill_mm": _mm(nc.GetViaDrill()), "diff_pair_width_mm": _mm(nc.GetDiffPairWidth()),
                            "diff_pair_gap_mm": _mm(nc.GetDiffPairGap())})
        except Exception:
            pass

    pads_by_net: Dict[int, List[str]] = {}
    sheets_by_net: Dict[int, set] = {}
    for pad in board.GetPads():
        code = pad.GetNetCode()
        if code <= 0:
            continue
        fp = extract.footprint_of(pad)
        ref = fp.GetReference() if fp else "?"
        pads_by_net.setdefault(code, []).append(f"{ref}.{pad.GetNumber()}")
        if fp:
            sheets_by_net.setdefault(code, set()).add(extract.sheet_of(fp))
    tracks_by_net = Counter()
    vias_by_net = Counter()
    for t in board.GetTracks():
        (vias_by_net if t.GetClass() == "PCB_VIA" else tracks_by_net)[t.GetNetCode()] += 1
    zones_by_net: Dict[int, List[str]] = {}
    for z in board.Zones():
        if not z.GetIsRuleArea() and z.GetNetCode() > 0:
            zones_by_net.setdefault(z.GetNetCode(), []).extend(
                board.GetLayerName(l) for l in z.GetLayerSet().CuStack())
    conn = board.GetConnectivity()
    try:
        board.BuildConnectivity()                  # the live board may have changed since the last build
    except Exception:
        pass
    opens = unconnected_edges(board)
    names_all = {n.GetNetname() for c, n in board.GetNetsByNetcode().items() if c > 0}
    nets = []
    for code, net in board.GetNetsByNetcode().items():
        if code <= 0 or code not in pads_by_net:
            continue
        name = net.GetNetname()
        entry = {"name": name, "class": net.GetNetClassName(), "pads": len(pads_by_net[code]),
                 "tracks": tracks_by_net.get(code, 0), "vias": vias_by_net.get(code, 0)}
        if len(pads_by_net[code]) <= 8:
            entry["pins"] = pads_by_net[code]
        if code in zones_by_net:
            entry["zones_on"] = sorted(set(zones_by_net[code]))
        partner = extract.pair_partner(name, names_all)
        if partner:
            entry["pair_with"] = partner
        if sheets_by_net.get(code):
            entry["sheets"] = sorted(sheets_by_net[code])
        nets.append(entry)

    footprints = []
    for fp in board.GetFootprints():
        pos = fp.GetPosition()
        footprints.append({"ref": fp.GetReference(), "value": fp.GetValue(), "sheet": extract.sheet_of(fp),
                           "pads": len(list(fp.Pads())), "at_mm": [_mm(pos.x), _mm(pos.y)],
                           "side": "back" if fp.IsFlipped() else "front"})

    ctx = {
        "board": {"file": os.path.basename(board.GetFileName() or ""),
                  "size_mm": [_mm(bb.GetWidth()), _mm(bb.GetHeight())],
                  "origin_mm": [_mm(bb.GetX()), _mm(bb.GetY())],
                  "copper_layers": [n for _, n in layers],
                  "min_track_mm": _mm(ds.m_TrackMinWidth), "min_clearance_mm": _mm(ds.m_MinClearance),
                  "min_via_mm": _mm(ds.m_ViasMinSize), "hole_clearance_mm": _mm(getattr(ds, "m_HoleClearance", 0)),
                  "predefined_track_widths_mm": [_mm(w) for w in sizes["track_widths"]],
                  "predefined_vias_mm": [[_mm(d), _mm(h)] for d, h in sizes["via_sizes"]],
                  "unconnected_total": len(opens)},
        "unconnected": opens,
        "net_classes": classes,
        "nets": nets,
        "footprints": footprints,
        "sheets": [{"name": n, "footprints": c} for n, c in extract.sheets(board)],
        "options": options,
        "last_run": last_run or {},
        "selection": selection or {},
    }
    if with_drc:
        ctx["drc"] = drc_summary(board, workdir)
    return ctx
