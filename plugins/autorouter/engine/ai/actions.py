"""What the model may ask for, and how it is carried out on the board.

The reply schema is deliberately small: options to change, routing to run
on a scope, tracks to remove or add, vias to add, a zone refill, a question
back to the user, and whether the model considers the job done. Everything
geometric is in millimetres and layer names, as a designer would write it.
"""
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

ACTION_TYPES = ("set_options", "route", "remove_tracks", "add_track", "add_via", "refill_zones", "ask", "note")

OPTION_KEYS = {
    "pitch_mm": float, "via_cost": int, "skip_nets": list, "track_mode": str, "track_width_mm": float,
    "neck_down": bool, "fanout_vias": bool, "stitch_vias": bool, "stitch_pitch_mm": float,
    "diff_pairs": bool, "allow_vias": bool, "routable_layers": list, "horizontal_layers": list,
    "remove_existing": bool, "power_pours": bool, "rip_up": bool, "max_expansions": int,
}

def _nullable(t, **extra):
    d = {"type": [t, "null"]}
    d.update(extra)
    return d


# OpenAI's strict JSON schema mode (what `codex exec --output-schema` uses)
# needs every property listed in "required" and no free-form objects, so
# optional fields are nullable and the option bag spells out every key.
_OPTION_PROPS = {
    "pitch_mm": _nullable("number"), "via_cost": _nullable("integer"),
    "skip_nets": _nullable("array", items={"type": "string"}),
    "track_mode": _nullable("string", enum=["auto", "netclass", "current", "fixed"]),
    "track_width_mm": _nullable("number"), "neck_down": _nullable("boolean"),
    "fanout_vias": _nullable("boolean"), "stitch_vias": _nullable("boolean"),
    "stitch_pitch_mm": _nullable("number"), "diff_pairs": _nullable("boolean"),
    "allow_vias": _nullable("boolean"),
    "routable_layers": _nullable("array", items={"type": "boolean"}),
    "horizontal_layers": _nullable("array", items={"type": "boolean"}),
    "remove_existing": _nullable("boolean"), "power_pours": _nullable("boolean"),
    "rip_up": _nullable("boolean"), "max_expansions": _nullable("integer"),
}

_ACTION_PROPS = {
    "type": {"type": "string", "enum": list(ACTION_TYPES)},
    "options": {"type": ["object", "null"], "additionalProperties": False,
                "required": list(_OPTION_PROPS), "properties": _OPTION_PROPS,
                "description": "set_options only; unused keys null."},
    "scope": _nullable("string", enum=["board", "nets", "sheets", "selection"]),
    "nets": _nullable("array", items={"type": "string"}),
    "sheets": _nullable("array", items={"type": "string"}),
    "net": _nullable("string"),
    "layer": _nullable("string"),
    "width_mm": _nullable("number"),
    "points_mm": _nullable("array", items={"type": "array", "items": {"type": "number"}}),
    "x_mm": _nullable("number"),
    "y_mm": _nullable("number"),
    "diameter_mm": _nullable("number"),
    "drill_mm": _nullable("number"),
    "question": _nullable("string"),
    "text": _nullable("string"),
}

REPLY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "actions", "done"],
    "properties": {
        "summary": {"type": "string", "description": "One or two sentences for the user: what you saw and what you are doing."},
        "done": {"type": "boolean", "description": "true when no further round is needed after these actions."},
        "actions": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": False,
                      "required": list(_ACTION_PROPS), "properties": _ACTION_PROPS},
        },
    },
}


def normalize(reply: dict) -> dict:
    """Drop the nulls strict mode forces the model to write."""
    if not isinstance(reply, dict):
        return reply
    acts = []
    for act in reply.get("actions") or []:
        if not isinstance(act, dict):
            continue
        clean = {k: v for k, v in act.items() if v is not None}
        if isinstance(clean.get("options"), dict):
            clean["options"] = {k: v for k, v in clean["options"].items() if v is not None}
        acts.append(clean)
    out = dict(reply)
    out["actions"] = acts
    return out


def validate(reply: dict) -> List[str]:
    """Problems with a reply, as messages; empty when it is usable."""
    problems = []
    if not isinstance(reply, dict):
        return ["reply is not an object"]
    if not isinstance(reply.get("actions"), list):
        problems.append("'actions' must be a list")
        return problems
    for i, act in enumerate(reply["actions"]):
        t = act.get("type") if isinstance(act, dict) else None
        if t not in ACTION_TYPES:
            problems.append(f"action {i}: unknown type {t!r}")
            continue
        if t == "set_options":
            for key in (act.get("options") or {}):
                if key not in OPTION_KEYS:
                    problems.append(f"action {i}: unknown option {key!r}")
        if t == "route" and act.get("scope") in ("nets", "sheets") and not act.get(act["scope"]):
            problems.append(f"action {i}: route scope {act['scope']} needs a list")
        if t == "remove_tracks" and not act.get("nets"):
            problems.append(f"action {i}: remove_tracks needs nets")
        if t == "add_track" and (not act.get("net") or not act.get("layer") or len(act.get("points_mm") or []) < 2):
            problems.append(f"action {i}: add_track needs net, layer and at least two points")
        if t == "add_via" and (not act.get("net") or "x_mm" not in act or "y_mm" not in act):
            problems.append(f"action {i}: add_via needs net, x_mm and y_mm")
    return problems


@dataclass
class ActionResult:
    action: dict
    ok: bool
    detail: str = ""
    data: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"type": self.action.get("type"), "ok": self.ok, "detail": self.detail, **self.data}


class BoardActions:
    """Carries actions out on an open board. Routing goes through the
    built-in router with the dialog's current options (plus overrides);
    the host supplies the routing function so the dialog keeps its live
    display and undo behaviour."""

    def __init__(self, board, options: dict, route_fn: Callable[[dict], dict],
                 refill_fn: Callable[[], int], log: Callable[[str], None]):
        self.board = board
        self.options = options              # mutable dict of option overrides
        self.route_fn = route_fn
        self.refill_fn = refill_fn
        self.log = log

    def apply(self, actions: List[dict]) -> List[ActionResult]:
        results = []
        for act in actions:
            try:
                results.append(self._one(act))
            except Exception as e:           # one bad action must not stop the rest
                results.append(ActionResult(act, False, f"{type(e).__name__}: {e}"))
            self.log(f"[ai] {results[-1].action.get('type')}: {'ok' if results[-1].ok else 'FAILED'} {results[-1].detail}")
        return results

    def _one(self, act: dict) -> ActionResult:
        t = act["type"]
        if t == "note":
            return ActionResult(act, True, act.get("text", ""))
        if t == "ask":
            return ActionResult(act, True, act.get("question", ""))
        if t == "set_options":
            changed = {}
            for key, value in (act.get("options") or {}).items():
                if key in OPTION_KEYS:
                    self.options[key] = value
                    changed[key] = value
            return ActionResult(act, True, f"set {changed}")
        if t == "refill_zones":
            return ActionResult(act, True, f"refilled {self.refill_fn()} zones")
        if t == "remove_tracks":
            return ActionResult(act, True, f"removed {self._remove_tracks(act['nets'])} items")
        if t == "add_track":
            return ActionResult(act, True, f"added {self._add_track(act)} segments")
        if t == "add_via":
            self._add_via(act)
            return ActionResult(act, True, "via added")
        if t == "route":
            spec = {"scope": act.get("scope", "board"), "nets": act.get("nets") or [],
                    "sheets": act.get("sheets") or [], "options": dict(self.options)}
            outcome = self.route_fn(spec)
            ok = outcome.get("error") is None
            return ActionResult(act, ok, outcome.get("error") or outcome.get("summary", ""), outcome)
        return ActionResult(act, False, "unknown action")

    # ---- pcbnew edits -------------------------------------------------------

    def _net(self, name: str):
        net = self.board.FindNet(name)
        if net is None or net.GetNetCode() <= 0:
            raise ValueError(f"no net named {name!r}")
        return net

    def _layer(self, name: str) -> int:
        lid = self.board.GetLayerID(name)
        if lid < 0:
            raise ValueError(f"no layer named {name!r}")
        return lid

    def _remove_tracks(self, nets: List[str]) -> int:
        codes = {self._net(n).GetNetCode() for n in nets}
        items = [t for t in self.board.GetTracks() if t.GetNetCode() in codes]
        for item in items:
            self.board.Remove(item)
        return len(items)

    def _add_track(self, act: dict) -> int:
        import pcbnew
        net = self._net(act["net"])
        layer = self._layer(act["layer"])
        ds = self.board.GetDesignSettings()
        width = int(round(float(act.get("width_mm") or 0) * 1e6)) or \
            ds.m_NetSettings.GetEffectiveNetClass(net.GetNetname()).GetTrackWidth()
        pts = [(int(round(x * 1e6)), int(round(y * 1e6))) for x, y in act["points_mm"]]
        n = 0
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            if (x1, y1) == (x2, y2):
                continue
            track = pcbnew.PCB_TRACK(self.board)
            track.SetStart(pcbnew.VECTOR2I(x1, y1))
            track.SetEnd(pcbnew.VECTOR2I(x2, y2))
            track.SetWidth(width)
            track.SetLayer(layer)
            track.SetNetCode(net.GetNetCode())
            self.board.Add(track)
            n += 1
        return n

    def _add_via(self, act: dict) -> None:
        import pcbnew
        net = self._net(act["net"])
        ds = self.board.GetDesignSettings()
        nc = ds.m_NetSettings.GetEffectiveNetClass(net.GetNetname())
        dia = int(round(float(act.get("diameter_mm") or 0) * 1e6)) or nc.GetViaDiameter()
        drill = int(round(float(act.get("drill_mm") or 0) * 1e6)) or nc.GetViaDrill()
        via = pcbnew.PCB_VIA(self.board)
        via.SetPosition(pcbnew.VECTOR2I(int(round(act["x_mm"] * 1e6)), int(round(act["y_mm"] * 1e6))))
        via.SetViaType(pcbnew.VIATYPE_THROUGH)
        via.SetWidth(dia)
        via.SetDrill(drill)
        via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        via.SetNetCode(net.GetNetCode())
        self.board.Add(via)
