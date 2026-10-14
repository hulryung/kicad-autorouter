"""What the model may ask for, and how it is carried out on the board.

The reply schema is deliberately small: options to change, routing to run
on a scope, tracks to remove or add, vias to add, a zone refill, a question
back to the user, and whether the model considers the job done. Everything
geometric is in millimetres and layer names, as a designer would write it.
"""
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

ACTION_TYPES = ("set_options", "route", "remove_tracks", "remove_items_near", "add_track", "add_via",
                "move_footprint", "move_items_near", "set_track_width", "set_layer_near", "set_via_near",
                "inspect_near", "refill_zones", "ask", "note")

OPTION_KEYS = {
    "pitch_mm": float, "via_cost": int, "skip_nets": list, "track_mode": str, "track_width_mm": float,
    "neck_down": bool, "fanout_vias": bool, "stitch_vias": bool, "stitch_pitch_mm": float,
    "diff_pairs": bool, "allow_vias": bool, "routable_layers": list, "horizontal_layers": list,
    "remove_existing": bool, "power_pours": bool, "rip_up": bool, "max_expansions": int,
    "move_existing": bool, "plane_nets": bool, "time_limit_min": float,
    "route_order": list, "tune_lengths": bool, "protect_groups": bool,
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
    "move_existing": _nullable("boolean"), "plane_nets": _nullable("boolean"),
    "time_limit_min": _nullable("number"),
    "route_order": _nullable("array", items={"type": "string", "enum": ["pairs", "memory", "high_speed", "clock",
                                                                         "power", "signal"]}),
    "tune_lengths": _nullable("boolean"), "protect_groups": _nullable("boolean"),
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
    # route hints
    "layers": _nullable("array", items={"type": "string"}),
    "via_cost": _nullable("integer"),
    "avoid_mm": _nullable("array", items={"type": "array", "items": {"type": "number"}}),
    # remove_items_near / move_items_near / set_* / inspect_near
    "radius_mm": _nullable("number"),
    # move_footprint
    "ref": _nullable("string"),
    "rotation_deg": _nullable("number"),
    "side": _nullable("string", enum=["front", "back"]),
    # move_items_near
    "dx_mm": _nullable("number"),
    "dy_mm": _nullable("number"),
    # set_layer_near
    "to_layer": _nullable("string"),
    # keep an edit even if the DRC gets worse (e.g. you will reroute next)
    "force": _nullable("boolean"),
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
        if t == "remove_items_near" and ("x_mm" not in act or "y_mm" not in act):
            problems.append(f"action {i}: remove_items_near needs x_mm and y_mm")
        if t == "add_track" and (not act.get("net") or not act.get("layer") or len(act.get("points_mm") or []) < 2):
            problems.append(f"action {i}: add_track needs net, layer and at least two points")
        if t == "add_via" and (not act.get("net") or "x_mm" not in act or "y_mm" not in act):
            problems.append(f"action {i}: add_via needs net, x_mm and y_mm")
        if t == "move_footprint" and not act.get("ref"):
            problems.append(f"action {i}: move_footprint needs ref")
        if t in ("move_items_near", "set_layer_near", "set_via_near", "inspect_near") and ("x_mm" not in act or "y_mm" not in act):
            problems.append(f"action {i}: {t} needs x_mm and y_mm")
        if t == "set_layer_near" and not act.get("to_layer"):
            problems.append(f"action {i}: set_layer_near needs to_layer")
        if t == "set_track_width" and not act.get("width_mm"):
            problems.append(f"action {i}: set_track_width needs width_mm")
    return problems


@dataclass
class ActionResult:
    action: dict
    ok: bool
    detail: str = ""
    data: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"type": self.action.get("type"), "ok": self.ok, "detail": self.detail, **self.data}


EDITS = ("add_track", "add_via", "remove_tracks", "remove_items_near", "route", "move_footprint",
         "move_items_near", "set_track_width", "set_layer_near", "set_via_near")
# edits that are undone when they make the DRC worse (unless "force": true)
ROLLBACK = ("add_track", "add_via", "move_footprint", "move_items_near", "set_track_width",
            "set_layer_near", "set_via_near")


class BoardActions:
    """Carries actions out on an open board. Routing goes through the
    built-in router with the dialog's current options (plus overrides);
    the host supplies the routing function so the dialog keeps its live
    display and undo behaviour. With a verify function (DRC + unconnected
    count), every board edit is measured before and after, and hand-drawn
    copper that makes the DRC worse is rolled back."""

    def __init__(self, board, options: dict, route_fn: Callable[[dict], dict],
                 refill_fn: Callable[[], int], log: Callable[[str], None],
                 verify_fn: Optional[Callable[[], dict]] = None):
        self.board = board
        self.options = options              # mutable dict of option overrides
        self.route_fn = route_fn
        self.refill_fn = refill_fn
        self.log = log
        self.verify_fn = verify_fn
        self._added: List[Any] = []         # items the last edit created, for rollback
        self._undo: List[Callable[[], None]] = []   # how to put the last edit back
        # Items removed from the board outside KiCad's action-plugin wrapper
        # become Python-owned; dropping them while the connectivity still
        # points at them crashes KiCad's Python. Keep them for the session.
        self._graveyard: List[Any] = []

    def apply(self, actions: List[dict]) -> List[ActionResult]:
        results = []
        for act in actions:
            before = None
            if self.verify_fn and act.get("type") in EDITS:
                before = self._measure()
            self._added = []
            self._undo = []
            try:
                result = self._one(act)
            except Exception as e:           # one bad action must not stop the rest
                result = ActionResult(act, False, f"{type(e).__name__}: {e}")
            if before is not None and result.ok:
                after = self._measure()
                result.data["verify"] = {"before": before, "after": after}
                worse = (after.get("violations", 0) > before.get("violations", 0))
                if worse and act.get("type") in ROLLBACK and not act.get("force") and (self._added or self._undo):
                    for item in self._added:
                        try:
                            self.board.Remove(item)
                            item.thisown = 0
                            self._graveyard.append(item)
                        except Exception:
                            pass
                    try:
                        self.board.BuildConnectivity()
                    except Exception:
                        pass
                    for undo in reversed(self._undo):
                        try:
                            undo()
                        except Exception:
                            pass
                    result.ok = False
                    result.detail += (f" -> ROLLED BACK: DRC violations {before.get('violations')} -> "
                                      f"{after.get('violations')}")
                    result.data["verify"]["rolled_back"] = True
                elif worse:
                    result.detail += f" (DRC violations {before.get('violations')} -> {after.get('violations')})"
                else:
                    result.detail += (f" (DRC {before.get('violations')}->{after.get('violations')}, "
                                      f"unconnected {before.get('unconnected')}->{after.get('unconnected')})")
            results.append(result)
            self.log(f"[ai] {result.action.get('type')}: {'ok' if result.ok else 'FAILED'} {result.detail}")
        return results

    def _measure(self) -> dict:
        try:
            return self.verify_fn() or {}
        except Exception as e:
            return {"error": str(e)}

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
        if t == "remove_items_near":
            return ActionResult(act, True, f"removed {self._remove_near(act)} items")
        if t == "add_track":
            return ActionResult(act, True, f"added {self._add_track(act)} segments")
        if t == "add_via":
            self._add_via(act)
            return ActionResult(act, True, "via added")
        if t == "move_footprint":
            return ActionResult(act, True, self._move_footprint(act))
        if t == "move_items_near":
            return ActionResult(act, True, f"moved {self._move_near(act)} items")
        if t == "set_track_width":
            return ActionResult(act, True, f"changed width of {self._set_width(act)} segments")
        if t == "set_layer_near":
            return ActionResult(act, True, f"moved {self._set_layer(act)} segments to {act['to_layer']}")
        if t == "set_via_near":
            return ActionResult(act, True, f"changed {self._set_via(act)} vias")
        if t == "inspect_near":
            items = self._inspect(act)
            return ActionResult(act, True, f"{len(items)} items", {"items": items})
        if t == "route":
            hints = {k: act.get(k) for k in ("layers", "width_mm", "via_cost", "avoid_mm") if act.get(k) is not None}
            spec = {"scope": act.get("scope", "board"), "nets": act.get("nets") or [],
                    "sheets": act.get("sheets") or [], "options": dict(self.options), "hints": hints}
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

    # ---- freedom: footprints, items, widths, layers, vias ------------------

    def _items_near(self, act: dict, default_radius: float = 0.5):
        """Tracks and vias within radius_mm of x_mm,y_mm (of one net if given)."""
        import math
        x, y = act["x_mm"] * 1e6, act["y_mm"] * 1e6
        r = float(act.get("radius_mm") or default_radius) * 1e6
        code = self._net(act["net"]).GetNetCode() if act.get("net") else None
        found = []
        for t in self.board.GetTracks():
            if code is not None and t.GetNetCode() != code:
                continue
            if t.GetClass() == "PCB_VIA":
                p = t.GetPosition()
                d = math.hypot(p.x - x, p.y - y)
            else:
                a, b = t.GetStart(), t.GetEnd()
                dx, dy = b.x - a.x, b.y - a.y
                l2 = dx * dx + dy * dy
                tt = 0.0 if l2 == 0 else max(0.0, min(1.0, ((x - a.x) * dx + (y - a.y) * dy) / l2))
                d = math.hypot(x - (a.x + tt * dx), y - (a.y + tt * dy))
            if d <= r:
                found.append(t)
        return found

    def _footprint(self, ref: str):
        for fp in self.board.GetFootprints():
            if fp.GetReference() == ref:
                return fp
        raise ValueError(f"no footprint {ref!r}")

    def _move_footprint(self, act: dict) -> str:
        import pcbnew
        fp = self._footprint(act["ref"])
        old_pos = pcbnew.VECTOR2I(fp.GetPosition())
        old_rot = fp.GetOrientationDegrees()
        old_flipped = fp.IsFlipped()
        self._undo.append(lambda: (fp.SetPosition(old_pos), fp.SetOrientationDegrees(old_rot),
                                   fp.IsFlipped() != old_flipped and fp.Flip(fp.GetPosition(), False)))
        changes = []
        if act.get("x_mm") is not None and act.get("y_mm") is not None:
            fp.SetPosition(pcbnew.VECTOR2I(int(round(act["x_mm"] * 1e6)), int(round(act["y_mm"] * 1e6))))
            changes.append(f"to ({act['x_mm']}, {act['y_mm']}) mm")
        if act.get("rotation_deg") is not None:
            fp.SetOrientationDegrees(float(act["rotation_deg"]))
            changes.append(f"rotation {act['rotation_deg']} deg")
        if act.get("side") in ("front", "back"):
            want_back = act["side"] == "back"
            if fp.IsFlipped() != want_back:
                fp.Flip(fp.GetPosition(), False)
                changes.append(f"flipped to {act['side']}")
        return f"{act['ref']} " + (", ".join(changes) if changes else "unchanged")

    def _move_near(self, act: dict) -> int:
        import pcbnew
        dx = int(round(float(act.get("dx_mm") or 0) * 1e6))
        dy = int(round(float(act.get("dy_mm") or 0) * 1e6))
        items = self._items_near(act)
        delta = pcbnew.VECTOR2I(dx, dy)
        for t in items:
            t.Move(delta)
        self._undo.append(lambda: [t.Move(pcbnew.VECTOR2I(-dx, -dy)) for t in items])
        return len(items)

    def _set_width(self, act: dict) -> int:
        width = int(round(float(act["width_mm"]) * 1e6))
        if act.get("x_mm") is not None and act.get("y_mm") is not None:
            items = [t for t in self._items_near(act) if t.GetClass() != "PCB_VIA"]
        else:
            code = self._net(act["net"]).GetNetCode()
            items = [t for t in self.board.GetTracks() if t.GetNetCode() == code and t.GetClass() != "PCB_VIA"]
        olds = [(t, t.GetWidth()) for t in items]
        for t in items:
            t.SetWidth(width)
        self._undo.append(lambda: [t.SetWidth(w) for t, w in olds])
        return len(items)

    def _set_layer(self, act: dict) -> int:
        layer = self._layer(act["to_layer"])
        items = [t for t in self._items_near(act) if t.GetClass() == "PCB_TRACK"]
        olds = [(t, t.GetLayer()) for t in items]
        for t in items:
            t.SetLayer(layer)
        self._undo.append(lambda: [t.SetLayer(l) for t, l in olds])
        return len(items)

    def _set_via(self, act: dict) -> int:
        vias = [t for t in self._items_near(act) if t.GetClass() == "PCB_VIA"]
        olds = [(v, v.GetWidth(), v.GetDrillValue()) for v in vias]
        for v in vias:
            if act.get("diameter_mm"):
                v.SetWidth(int(round(float(act["diameter_mm"]) * 1e6)))
            if act.get("drill_mm"):
                v.SetDrill(int(round(float(act["drill_mm"]) * 1e6)))
        self._undo.append(lambda: [(v.SetWidth(w), v.SetDrill(d)) for v, w, d in olds])
        return len(vias)

    def _inspect(self, act: dict) -> List[dict]:
        """What is around a point: tracks, vias, pads and footprints, in mm."""
        import math
        out = []
        for t in self._items_near(act, default_radius=1.0):
            if t.GetClass() == "PCB_VIA":
                p = t.GetPosition()
                out.append({"kind": "via", "net": t.GetNetname(), "at_mm": [p.x / 1e6, p.y / 1e6],
                            "diameter_mm": t.GetWidth() / 1e6, "drill_mm": t.GetDrillValue() / 1e6})
            else:
                a, b = t.GetStart(), t.GetEnd()
                out.append({"kind": "track", "net": t.GetNetname(), "layer": self.board.GetLayerName(t.GetLayer()),
                            "from_mm": [a.x / 1e6, a.y / 1e6], "to_mm": [b.x / 1e6, b.y / 1e6],
                            "width_mm": t.GetWidth() / 1e6})
        x, y = act["x_mm"] * 1e6, act["y_mm"] * 1e6
        r = float(act.get("radius_mm") or 1.0) * 1e6
        for pad in self.board.GetPads():
            p = pad.GetPosition()
            if math.hypot(p.x - x, p.y - y) <= r:
                fp = pad.GetParentFootprint()
                ref = fp.GetReference() if fp is not None and hasattr(fp, "GetReference") else "?"
                out.append({"kind": "pad", "footprint": ref, "pad": pad.GetNumber(), "net": pad.GetNetname(),
                            "at_mm": [p.x / 1e6, p.y / 1e6], "size_mm": [pad.GetSize().x / 1e6, pad.GetSize().y / 1e6],
                            "layers": [self.board.GetLayerName(l) for l in pad.GetLayerSet().CuStack()]})
        for fp in self.board.GetFootprints():
            p = fp.GetPosition()
            if math.hypot(p.x - x, p.y - y) <= r:
                out.append({"kind": "footprint", "ref": fp.GetReference(), "value": fp.GetValue(),
                            "at_mm": [p.x / 1e6, p.y / 1e6], "rotation_deg": fp.GetOrientationDegrees(),
                            "side": "back" if fp.IsFlipped() else "front"})
        return out[:60]

    def _remove_near(self, act: dict) -> int:
        """Tracks and vias within radius_mm (default 0.5) of a point, of one
        net when given: the way to take out a specific bad segment."""
        import math
        x, y = act["x_mm"] * 1e6, act["y_mm"] * 1e6
        r = float(act.get("radius_mm") or 0.5) * 1e6
        code = self._net(act["net"]).GetNetCode() if act.get("net") else None
        victims = []
        for t in self.board.GetTracks():
            if code is not None and t.GetNetCode() != code:
                continue
            if t.GetClass() == "PCB_VIA":
                p = t.GetPosition()
                d = math.hypot(p.x - x, p.y - y)
            else:
                a, b = t.GetStart(), t.GetEnd()
                ax, ay, bx, by = a.x, a.y, b.x, b.y
                dx, dy = bx - ax, by - ay
                l2 = dx * dx + dy * dy
                tt = 0.0 if l2 == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / l2))
                d = math.hypot(x - (ax + tt * dx), y - (ay + tt * dy))
            if d <= r:
                victims.append(t)
        self._forget(victims)
        return len(victims)

    def _forget(self, items) -> None:
        for item in items:
            self.board.Remove(item)
            # never let Python delete a board item: KiCad's bindings cannot
            # destroy them safely (inside KiCad the undo list owns them anyway)
            try:
                item.thisown = 0
            except Exception:
                pass
        self._graveyard.extend(items)
        try:
            self.board.BuildConnectivity()
        except Exception:
            pass

    def _remove_tracks(self, nets: List[str]) -> int:
        codes = {self._net(n).GetNetCode() for n in nets}
        items = [t for t in self.board.GetTracks() if t.GetNetCode() in codes]
        self._forget(items)
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
            self._added.append(track)
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
        self._added.append(via)
