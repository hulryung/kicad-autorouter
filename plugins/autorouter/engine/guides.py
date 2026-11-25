"""Routing guides: how a board should be routed, chosen by the user.

A guide is a JSON file with two halves:

  {"id": "outer-signals-planes",
   "name": "Two signal layers over planes",
   "summary": "one or two sentences for the user",           natural language
   "guide": "instructions for the AI assistant",             natural language
   "style": {"layers": {"inner": "plane"}, "class_width_mm": {...}, "class_via_mm": {...}},
   "options": {"via_cost": 40, "route_order": ["power", "signal"]},
   "preview": "outer-signals-planes.png"}

The router executes the JSON half (style and options) exactly, so a guide
gives the same result every time and can be read and edited. The words are
for people and for the model: the assistant gets the guide's text with the
board and works within it, or, with the "ai-decides" guide, chooses the
approach itself. A request in plain words becomes a guide through the model
(describe()), shown to the user as JSON before it is used.

Built-in guides live in plugin_resources/guides/; the user's own (learnt
from a board, written by the model, edited) in the plugin's data folder.
"""
import json
import math
import re
import subprocess
import tempfile
from pathlib import Path
from .persistence import atomic_text
from typing import Dict, List, Optional

from . import style as style_mod
from .paths import data_dir

BUILTIN = Path(__file__).resolve().parents[1] / "plugin_resources" / "guides"
AI_DECIDES = "ai-decides"

# the options a guide may set (RouteOptions / the assistant's set_options keys)
OPTION_KEYS = {"via_cost", "pitch_mm", "route_order", "neck_down", "diff_pairs", "stitch_vias",
               "stitch_pitch_mm", "fanout_vias", "plane_nets", "allow_vias", "max_expansions"}

GUIDE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "summary": {"type": "string"},
        "guide": {"type": "string"},
        "inner_layers": {"type": "string", "enum": ["signal", "plane"]},
        "class_widths": {"type": "array", "items": {
            "type": "object", "properties": {"net_class": {"type": "string"}, "width_mm": {"type": "number"}},
            "required": ["net_class", "width_mm"], "additionalProperties": False}},
        "via_cost": {"type": ["integer", "null"]},
        "route_order": {"type": ["array", "null"], "items": {
            "type": "string", "enum": ["pairs", "memory", "high_speed", "clock", "power", "signal"]}},
    },
    "required": ["name", "summary", "guide", "inner_layers", "class_widths", "via_cost", "route_order"],
    "additionalProperties": False,
}


def user_dir() -> Path:
    return data_dir() / "guides"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "guide"


def validate(g):
    """Reject malformed guide values before they reach native routing options."""
    if not isinstance(g, dict):
        raise ValueError('Guide must be an object')
    for key in ('id', 'name', 'summary', 'guide', 'preview'):
        if key in g and not isinstance(g[key], str):
            raise ValueError(f'Guide {key} must be text')
    ident = g.get('id', '')
    if ident and (not re.fullmatch(r'[A-Za-z0-9_.-]+', ident) or ident in ('.', '..')):
        raise ValueError('Guide id must be a filename, not a path')
    if 'order' in g and (type(g['order']) not in (int, float) or not math.isfinite(g['order'])):
        raise ValueError('Guide order must be finite')
    style, options = g.get('style', {}), g.get('options', {})
    if not isinstance(style, dict) or not isinstance(options, dict):
        raise ValueError('Guide style/options must be objects')
    for key in ('layers', 'class_width_mm', 'class_via_mm'):
        if not isinstance(style.get(key, {}), dict):
            raise ValueError(f'Guide {key} must be an object')
    def number(v, low=0, high=float('inf')):
        return type(v) in (int, float) and math.isfinite(v) and low <= v <= high
    if any(role not in ('plane', 'signal') for role in style.get('layers', {}).values()):
        raise ValueError('Layer roles must be plane or signal')
    if any(not number(w) or w == 0 for w in style.get('class_width_mm', {}).values()):
        raise ValueError('Class widths must be positive and finite')
    for sizes in style.get('class_via_mm', {}).values():
        if not isinstance(sizes, list) or len(sizes) != 2 or not all(number(v) for v in sizes) or not 0 < sizes[1] < sizes[0]:
            raise ValueError('Via sizes must be [diameter, drill] with 0 < drill < diameter')
    bounds = {'via_cost': (0, 100000), 'pitch_mm': (0.025, 2), 'stitch_pitch_mm': (1, 50),
              'max_expansions': (1, 100000000)}
    for key, value in options.items():
        if value is None or key not in OPTION_KEYS:
            continue
        if key in bounds and not number(value, *bounds[key]):
            raise ValueError(f'Guide option {key} is out of range')
        if key in ('via_cost', 'max_expansions') and not float(value).is_integer():
            raise ValueError(f'Guide option {key} must be an integer')
        if key in ('neck_down', 'diff_pairs', 'stitch_vias', 'fanout_vias', 'plane_nets', 'allow_vias') and type(value) is not bool:
            raise ValueError(f'Guide option {key} must be boolean')
        if key == 'route_order':
            from .si import DEFAULT_ORDER
            if not isinstance(value, list) or any(v not in DEFAULT_ORDER for v in value):
                raise ValueError('Invalid routing order')
    return g


def load_all() -> List[dict]:
    """Built-in guides first (in their file order), then the user's."""
    out: List[dict] = []
    for folder, builtin in ((BUILTIN, True), (user_dir(), False)):
        if not folder.is_dir():
            continue
        for f in sorted(folder.glob("*.json")):
            try:
                g = validate(json.loads(f.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
            g.setdefault("id", f.stem)
            g["builtin"] = builtin
            g["path"] = str(f)
            out.append(g)
    out.sort(key=lambda g: (not g["builtin"], g.get("order", 50), g.get("name", "")))
    return out


def find(guide_id: str) -> Optional[dict]:
    return next((g for g in load_all() if g.get("id") == guide_id), None)


def preview_path(g: dict) -> Optional[Path]:
    name = g.get("preview")
    if not name:
        return None
    for folder in (Path(g.get("path", "")).parent, BUILTIN):
        p = folder / name
        if p.is_file():
            return p
    return None


def save(g: dict) -> Path:
    """Write a user guide (never over a built-in one)."""
    validate(g)
    g = {k: v for k, v in g.items() if k not in ("builtin", "path")}
    g["id"] = g.get("id") or _slug(g.get("name", "guide"))
    if (BUILTIN / f"{g['id']}.json").exists():
        g["id"] = g["id"] + "-mine"
    folder = user_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{g['id']}.json"
    atomic_text(path, json.dumps(g, indent=1, ensure_ascii=False))
    return path


def as_text(g: dict) -> str:
    """The guide as the assistant reads it."""
    if g.get("id") == AI_DECIDES:
        return ("No routing approach is fixed: choose it yourself from the board (layer use, what goes to "
                "pours, widths, order), say in the summary which approach you chose and why, and carry it out.")
    parts = [f"Routing guide: {g.get('name', '')}", g.get("summary", ""), g.get("guide", "")]
    machine = {k: g[k] for k in ("style", "options") if g.get(k)}
    if machine:
        parts.append("The router already applies these settings of the guide; keep to them:\n" +
                     json.dumps(machine, indent=1))
    return "\n".join(p for p in parts if p)


def apply(board, g: dict, native_options) -> List[str]:
    """The guide's style and options on a run's NativeOptions."""
    validate(g)
    lines = style_mod.apply_to_options(board, g.get("style") or {}, native_options)
    for k, v in (g.get("options") or {}).items():
        if k not in OPTION_KEYS or v is None:
            continue
        if k == "route_order":
            native_options.route_order = list(v)
        elif hasattr(native_options, k):
            setattr(native_options, k, v)
        elif k == "max_expansions":
            native_options.max_expansions = int(v)
        lines.append(f"{k} = {v}")
    return lines


def learn(path: str) -> dict:
    """Read a reference project in another process, preserving the editor project."""
    from .isolated import python_executable
    binary = python_executable()
    with tempfile.TemporaryDirectory(prefix='autorouter-guide-') as tmp:
        output = Path(tmp) / 'guide.json'
        code = ("import json,sys; from pathlib import Path; "
                "sys.path.insert(0, sys.argv[1]); "
                "from autorouter.engine.guides import _learn_in_process; "
                "Path(sys.argv[3]).write_text(json.dumps(_learn_in_process(sys.argv[2])), encoding='utf-8')")
        result = subprocess.run([binary, '-c', code, str(Path(__file__).resolve().parents[2]),
                                 str(Path(path).resolve()), str(output)],
                                capture_output=True, text=True, timeout=120)
        if result.returncode != 0 or not output.exists():
            raise RuntimeError(f"Reference board could not be read: {result.stderr[-1500:]}")
        return json.loads(output.read_text(encoding='utf-8'))


def _learn_in_process(path: str) -> dict:
    """Worker only: pcbnew.LoadBoard must never run in the editor process."""
    import pcbnew
    board = pcbnew.LoadBoard(path)
    st = style_mod.learn(board)
    planes = [n for n, r in st["layers"].items() if r == "plane"]
    widths = ", ".join(f"{k} {v:g} mm" for k, v in st["class_width_mm"].items())
    name = Path(path).stem
    summary = (f"As routed on {name}: " +
               (f"{', '.join(planes)} kept for pours; " if planes else "every layer carries tracks; ") +
               (f"drawn widths {widths}." if widths else "net-class widths."))
    return {"id": _slug(f"learnt-{name}"), "name": f"Like {name}", "summary": summary,
            "guide": ("Route the way this board's designer did: " + summary +
                      " Keep signals on the signal layers, leave planes whole, use the given widths."),
            "style": st, "options": {}, "source": path}


def describe(backend, request: str, board, workdir: Path, poll=None, cancelled=None) -> dict:
    """A guide from the user's words, written by the model for this board."""
    from .ai import llm
    ns = board.GetDesignSettings().m_NetSettings
    classes = {str(n): round(c.GetTrackWidth() / 1e6, 3) for n, c in ns.GetNetclasses().items()}
    classes["Default"] = round(ns.GetDefaultNetclass().GetTrackWidth() / 1e6, 3)
    layers = [board.GetLayerName(l) for l in board.GetEnabledLayers().CuStack()]
    prompt = (
        "You write a routing guide for a PCB autorouter from the user's description.\n"
        "The router executes inner_layers ('plane' keeps the inner copper layers free of tracks), "
        "class_widths (track width per net class, only where the user wants it different from the class), "
        "via_cost (cells of straight track one via costs; 25 is the default, higher = fewer vias; null = default) "
        "and route_order (null = default). 'guide' is the instruction text an AI assistant gets while routing; "
        "'summary' is one or two sentences for the user. Write in the user's language.\n\n"
        f"Board: copper layers {layers}; net classes and their widths in mm {json.dumps(classes)}.\n\n"
        f"The user's description:\n{request.strip()}\n")
    reply = llm.run(backend, prompt, workdir, schema=GUIDE_SCHEMA, poll=poll, cancelled=cancelled)
    st: Dict[str, dict] = {"layers": {"inner": reply.get("inner_layers", "signal")}}
    widths = {w["net_class"]: w["width_mm"] for w in reply.get("class_widths") or []
              if w.get("net_class") in classes and w.get("width_mm")}
    if widths:
        st["class_width_mm"] = widths
    options = {}
    if reply.get("via_cost") is not None:
        options["via_cost"] = int(reply["via_cost"])
    if reply.get("route_order"):
        options["route_order"] = reply["route_order"]
    return {"id": _slug(reply.get("name", "described")), "name": reply.get("name", "Described"),
            "summary": reply.get("summary", ""), "guide": reply.get("guide", ""), "style": st,
            "options": options, "request": request}
