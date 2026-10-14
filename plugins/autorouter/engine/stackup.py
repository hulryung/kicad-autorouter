"""Board stackups and controlled-impedance track sizes.

A stackup is the list of copper and dielectric layers from top to bottom.
The templates are JLCPCB's standard 1.6 mm builds; the board's own stackup
(Board Setup > Physical Stackup) is read from the .kicad_pcb file.

The impedance formulas are the usual closed forms a fab's calculator starts
from: Hammerstad-Jensen for microstrip (outer layers, with a solder mask
correction), Wheeler for stripline (inner layers, between the two nearest
copper layers), and the IPC-2141 coupling terms for edge-coupled pairs.
They land within a few percent of a field solver for ordinary geometries;
order the board with the fab's own impedance control for the exact widths.
"""
import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

MM = 1_000_000            # nm per mm


@dataclass
class Dielectric:
    thickness_mm: float
    er: float
    kind: str = "core"           # core / prepreg


@dataclass
class Stackup:
    name: str
    description: str
    copper_mm: List[float]               # copper thickness of each layer, top to bottom
    dielectrics: List[Dielectric]        # between copper i and i+1
    mask_mm: float = 0.0127              # solder mask over the outer layers (0: none)
    mask_er: float = 3.8
    approximate: bool = False            # True: copied from published tables, check with the fab

    @property
    def layers(self) -> int:
        return len(self.copper_mm)

    @property
    def thickness_mm(self) -> float:
        return sum(self.copper_mm) + sum(d.thickness_mm for d in self.dielectrics)


def _jlc(name, desc, copper, diel):
    return Stackup(name, desc, copper, [Dielectric(t, er, kind) for t, er, kind in diel], approximate=True)


# JLCPCB's standard 1.6 mm stackups (outer 1 oz finished, inner 0.5 oz).
# Prepreg 7628 er 4.4, 3313 er 4.1, 2116 er 4.16; core er 4.6.
TEMPLATES: Dict[str, Stackup] = {s.name: s for s in [
    _jlc("JLC 2L 1.6mm", "JLCPCB 2-layer 1.6 mm FR4", [0.035, 0.035], [(1.53, 4.5, "core")]),
    _jlc("JLC04161H-7628", "JLCPCB 4-layer 1.6 mm, 7628 prepreg (default)",
         [0.035, 0.0152, 0.0152, 0.035],
         [(0.2104, 4.4, "prepreg"), (1.065, 4.6, "core"), (0.2104, 4.4, "prepreg")]),
    _jlc("JLC04161H-3313", "JLCPCB 4-layer 1.6 mm, 3313 prepreg (thin, narrower 50 ohm tracks)",
         [0.035, 0.0152, 0.0152, 0.035],
         [(0.0994, 4.1, "prepreg"), (1.265, 4.6, "core"), (0.0994, 4.1, "prepreg")]),
    _jlc("JLC04161H-2116", "JLCPCB 4-layer 1.6 mm, 2116 prepreg",
         [0.035, 0.0152, 0.0152, 0.035],
         [(0.1088, 4.16, "prepreg"), (1.265, 4.6, "core"), (0.1088, 4.16, "prepreg")]),
    _jlc("JLC06161H-2116", "JLCPCB 6-layer 1.6 mm, 2116 prepreg (default)",
         [0.035, 0.0152, 0.0152, 0.0152, 0.0152, 0.035],
         [(0.1164, 4.16, "prepreg"), (0.55, 4.6, "core"), (0.1360, 4.16, "prepreg"),
          (0.55, 4.6, "core"), (0.1164, 4.16, "prepreg")]),
    _jlc("JLC06161H-3313", "JLCPCB 6-layer 1.6 mm, 3313 prepreg",
         [0.035, 0.0152, 0.0152, 0.0152, 0.0152, 0.035],
         [(0.0764, 4.1, "prepreg"), (0.55, 4.6, "core"), (0.2172, 4.1, "prepreg"),
          (0.55, 4.6, "core"), (0.0764, 4.1, "prepreg")]),
]}
BOARD = "board"           # the board's own stackup


# ---- reading the board's stackup -------------------------------------------

def _sexpr_block(text: str, head: str) -> Optional[str]:
    """The balanced "(head ...)" block of an s-expression file, or None."""
    i = text.find("(" + head)
    if i < 0:
        return None
    depth = 0
    for j in range(i, len(text)):
        c = text[j]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
        elif c == '"':
            # skip strings (they may hold parentheses)
            k = j + 1
            while k < len(text) and text[k] != '"':
                k += 2 if text[k] == "\\" else 1
    return None


def parse_stackup(text: str, name: str = "Board stackup") -> Optional[Stackup]:
    """The stackup in a .kicad_pcb file's setup section, None if it has none."""
    block = _sexpr_block(text, "stackup")
    if not block:
        return None
    copper: List[float] = []
    diel: List[Dielectric] = []
    pending: List[Dielectric] = []          # dielectrics since the last copper
    mask_mm, mask_er = 0.0, 3.8
    for m in re.finditer(r'\(layer\s+"([^"]+)"(.*?)\n\t\t\t\)', block, re.S):
        body = m.group(2)
        typ = re.search(r'\(type\s+"([^"]+)"\)', body)
        typ = typ.group(1) if typ else ""
        th = [float(v) for v in re.findall(r"\(thickness\s+([0-9.]+)", body)]
        er = [float(v) for v in re.findall(r"\(epsilon_r\s+([0-9.]+)", body)]
        if typ == "copper":
            if copper:
                if pending:
                    # several dielectric sublayers between two coppers: one layer, thickness-weighted er
                    t = sum(d.thickness_mm for d in pending)
                    e = sum(d.thickness_mm * d.er for d in pending) / t if t else 4.5
                    diel.append(Dielectric(t, e, pending[0].kind))
                else:
                    diel.append(Dielectric(0.1, 4.5))
            copper.append(th[0] if th else 0.035)
            pending = []
        elif typ in ("core", "prepreg") or "dielectric" in m.group(1):
            # a dielectric may list sublayers: (thickness a) (epsilon_r b) ... (addsublayer) ...
            for k, t in enumerate(th or [0.1]):
                pending.append(Dielectric(t, er[k] if k < len(er) else (er[0] if er else 4.5), typ or "core"))
        elif "Solder Mask" in typ:
            mask_mm = th[0] if th else 0.01
            mask_er = er[0] if er else 3.8
    if len(copper) < 2 or len(diel) != len(copper) - 1:
        return None
    return Stackup(name, "from Board Setup > Physical Stackup", copper, diel, mask_mm, mask_er)


def board_stackup(board) -> Optional[Stackup]:
    """The stackup saved in the board file (unsaved edits are not seen)."""
    try:
        path = board.GetFileName()
        with open(path, encoding="utf-8") as f:
            st = parse_stackup(f.read())
    except Exception:
        return None
    if st is not None and st.layers != board.GetCopperLayerCount():
        return None
    return st


def resolve(board, name: str) -> Tuple[Optional[Stackup], str]:
    """The stackup to compute with and a note on where it came from."""
    if name and name != BOARD:
        st = TEMPLATES.get(name)
        if st is None:
            return None, f"unknown stackup template {name!r}"
        if board is not None and st.layers != board.GetCopperLayerCount():
            return st, (f"{name} has {st.layers} copper layers but the board has "
                        f"{board.GetCopperLayerCount()}: widths are computed for the template")
        return st, name
    st = board_stackup(board) if board is not None else None
    if st is None:
        return None, "the board has no physical stackup (Board Setup > Physical Stackup); pick a template"
    return st, "board stackup"


# ---- impedance formulas ----------------------------------------------------

def _z_air_ms(u: float) -> float:
    f = 6 + (2 * math.pi - 6) * math.exp(-((30.666 / u) ** 0.7528))
    return 60 * math.log(f / u + math.sqrt(1 + 4 / (u * u)))


def _eeff_ms(u: float, er: float) -> float:
    a = 1 + math.log((u ** 4 + (u / 52) ** 2) / (u ** 4 + 0.432)) / 49 + math.log(1 + (u / 18.1) ** 3) / 18.7
    b = 0.564 * ((er - 0.9) / (er + 3)) ** 0.053
    return (er + 1) / 2 + (er - 1) / 2 * (1 + 10 / u) ** (-a * b)


def microstrip(w: float, h: float, t: float, er: float, mask: float = 0.0) -> float:
    """Single-ended impedance (ohm) of an outer-layer track: width w over a
    plane h below, copper t thick (all mm); Hammerstad-Jensen with the
    thickness correction, then a solder mask correction."""
    u = w / h
    if t > 0:
        coth = 1 / math.tanh(math.sqrt(6.517 * u))
        dw1 = t / math.pi * math.log(1 + 4 * math.e / (t / h * coth * coth))
        dwr = 0.5 * (1 + 1 / math.cosh(math.sqrt(er - 1))) * dw1
        u1, ur = u + dw1 / h, u + dwr / h
    else:
        u1 = ur = u
    eeff = _eeff_ms(ur, er) * (_z_air_ms(u1) / _z_air_ms(ur)) ** 2
    z = _z_air_ms(ur) / math.sqrt(eeff)
    if mask > 0:
        z *= 1 - 0.35 * (1 - math.exp(-1.55 * mask / h))
    return z


def _stripline_sym(w: float, b: float, t: float, er: float) -> float:
    """Wheeler's centred stripline: plane spacing b, width w, thickness t."""
    x = min(t / b, 0.9)
    if x > 0:
        m = 2 / (1 + 2 / 3 * x / (1 - x))
        dw = t / math.pi * (1 - 0.5 * math.log((x / (2 - x)) ** 2 + (0.0796 * x / (w / b + 1.1 * x)) ** m))
    else:
        dw = 0.0
    wp = w + dw
    k = 4 / math.pi * (b - t) / wp
    return 30 / math.sqrt(er) * math.log(1 + k * (2 * k + math.sqrt((2 * k) ** 2 + 6.27)))


def stripline(w: float, h1: float, h2: float, t: float, er: float) -> float:
    """Inner-layer track between planes h1 above and h2 below (mm): the
    off-centre line as two centred striplines in parallel."""
    z1 = _stripline_sym(w, 2 * h1 + t, t, er)
    z2 = _stripline_sym(w, 2 * h2 + t, t, er)
    return 2 * z1 * z2 / (z1 + z2)


@dataclass
class LayerGeometry:
    """What a track on one copper layer sees."""
    index: int                       # copper layer index, top = 0
    outer: bool
    t: float                         # copper thickness, mm
    h: float                         # nearest plane distance (microstrip), or h1 (stripline)
    h2: float = 0.0                  # stripline: the other plane
    er: float = 4.5
    mask: float = 0.0


def geometry(st: Stackup, index: int) -> LayerGeometry:
    n = st.layers
    t = st.copper_mm[index]
    if index == 0 or index == n - 1:
        d = st.dielectrics[0] if index == 0 else st.dielectrics[-1]
        return LayerGeometry(index, True, t, d.thickness_mm, er=d.er, mask=st.mask_mm)
    up, down = st.dielectrics[index - 1], st.dielectrics[index]
    # the neighbouring copper layers are taken as the references; the far
    # side's copper thickness adds to the distance
    h1 = up.thickness_mm
    h2 = down.thickness_mm
    er = (up.er * h1 + down.er * h2) / (h1 + h2)
    return LayerGeometry(index, False, t, h1, h2, er)


def single_ended(g: LayerGeometry, w: float) -> float:
    if g.outer:
        return microstrip(w, g.h, g.t, g.er, g.mask)
    return stripline(w, g.h, g.h2, g.t, g.er)


def differential(g: LayerGeometry, w: float, s: float) -> float:
    """Edge-coupled pair (IPC-2141 coupling terms), w and gap s in mm."""
    z0 = single_ended(g, w)
    if g.outer:
        return 2 * z0 * (1 - 0.48 * math.exp(-0.96 * s / g.h))
    b = g.h + g.h2 + g.t
    return 2 * z0 * (1 - 0.347 * math.exp(-2.9 * s / b))


def _solve(f, target: float, lo: float, hi: float) -> Optional[float]:
    """w in [lo, hi] with f(w) == target; f falls as w grows."""
    flo, fhi = f(lo), f(hi)
    if not (fhi <= target <= flo):
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        if f(mid) > target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


@dataclass
class TrackSize:
    layer: int                       # copper index, top = 0
    width_nm: int
    gap_nm: int = 0                  # pairs only
    ohm: float = 0.0                 # what that size gives (after rounding)
    note: str = ""


def _round_nm(mm: float, step_um: float = 5.0) -> int:
    step = step_um * 1000
    return int(round(mm * MM / step) * step)


def size_for(st: Stackup, index: int, single_ohm: float = 0.0, diff_ohm: float = 0.0,
             min_width_nm: int = 100_000, min_gap_nm: int = 100_000, gap_nm: int = 0) -> TrackSize:
    """The track width (and gap, for a pair) that gives the target impedance
    on copper layer index. A pair keeps its gap at gap_nm, or else at the
    track width up to 0.2 mm, never below min_gap; rounded to 5 um."""
    g = geometry(st, index)
    wmin, wmax = min_width_nm / MM, 5.0
    if diff_ohm > 0:
        def gap_for(w):
            # no gap given: as wide as the track, but not over 0.2 mm (tight coupling)
            return max(gap_nm / MM if gap_nm else min(w, 0.2), min_gap_nm / MM)
        w = _solve(lambda w: differential(g, w, gap_for(w)), diff_ohm, wmin, wmax)
        if w is None:
            note = "too high for the minimum width" if differential(g, wmin, gap_for(wmin)) < diff_ohm \
                else "too low within 5 mm"
            w = wmin if "high" in note else wmax
        else:
            note = ""
        wn, sn = _round_nm(w), _round_nm(gap_for(w))
        wn = max(wn, min_width_nm)
        return TrackSize(index, wn, sn, differential(g, wn / MM, sn / MM), note)
    w = _solve(lambda w: single_ended(g, w), single_ohm, wmin, wmax)
    note = ""
    if w is None:
        note = "too high for the minimum width" if single_ended(g, wmin) < single_ohm else "too low within 5 mm"
        w = wmin if "high" in note else wmax
    wn = max(_round_nm(w), min_width_nm)
    return TrackSize(index, wn, 0, single_ended(g, wn / MM), note)


def describe(st: Stackup) -> List[str]:
    """One line per layer for logs and the dialog."""
    out = [f"{st.name}: {st.layers} layers, {st.thickness_mm:.2f} mm"
           + (" (template values, check with the fab)" if st.approximate else "")]
    for i in range(st.layers):
        out.append(f"  L{i + 1} copper {st.copper_mm[i] * 1000:.1f} um")
        if i < len(st.dielectrics):
            d = st.dielectrics[i]
            out.append(f"     {d.kind} {d.thickness_mm:.4f} mm, er {d.er:g}")
    return out
