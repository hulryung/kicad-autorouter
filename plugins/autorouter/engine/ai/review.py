"""Scoring a routed board and improving it in rounds.

The model gets the board context, deterministic metrics, a full-board image
and close-ups of the places it flagged last time, and answers with a score
card (0-100 overall plus per-criterion scores and comments), a list of
issues with positions, and actions that would improve the score. The loop
applies the actions, re-scores, and keeps going while the score rises.
"""
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

from . import actions as act_mod
from . import codex, context, render

CRITERIA = ["connectivity", "drc", "track_length", "vias", "layer_use", "aesthetics", "signal_integrity", "power_and_ground"]

REVIEW_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["score", "criteria", "issues", "summary", "actions", "done"],
    "properties": {
        "score": {"type": "integer", "description": "0-100 overall quality of the routing as a layout reviewer sees it"},
        "criteria": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["name", "score", "comment"],
            "properties": {"name": {"type": "string", "enum": CRITERIA},
                           "score": {"type": "integer"}, "comment": {"type": "string"}}}},
        "issues": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["severity", "net", "x_mm", "y_mm", "description", "suggestion"],
            "properties": {"severity": {"type": "string", "enum": ["high", "medium", "low"]},
                           "net": {"type": ["string", "null"]},
                           "x_mm": {"type": ["number", "null"]}, "y_mm": {"type": ["number", "null"]},
                           "description": {"type": "string"}, "suggestion": {"type": "string"}}}},
        "summary": {"type": "string"},
        "done": {"type": "boolean", "description": "true when you see nothing worth changing"},
        "actions": act_mod.REPLY_SCHEMA["properties"]["actions"],
    },
}

REVIEW_PROMPT = """You are a senior PCB layout reviewer scoring an autorouted board inside KiCad.
Judge it the way you would judge a junior's artwork before release, from the JSON context, the
deterministic metrics, the full-board image and the close-up images (each close-up names its centre in mm).

Score 0-100 overall and per criterion:
- connectivity: everything connected that should be (pour nets excepted)
- drc: no violations, margins respected
- track_length: direct routes, no detours or doubling back
- vias: as few layer changes as the board needs, none wasted
- layer_use: sensible layer choice, no needless inner-layer excursions
- aesthetics: 45-degree style, no zig-zags, parallel runs, clean escapes, nothing under connectors
- signal_integrity: pairs coupled, return paths, no long stubs
- power_and_ground: widths, pour ties, stitching where it helps

List concrete issues with positions (x_mm, y_mm) so close-ups can be made, and propose actions from the
action list that would raise the score (reroute specific nets with hints, remove and reroute, move a part,
change widths, add stitching, ...). Keep ground/power pours as they are unless they are the issue.
Set done when nothing worth changing remains. Answer with the JSON object only.
"""


def metrics(board) -> dict:
    """Deterministic figures the reviewer should not have to estimate."""
    import pcbnew
    tracks = [t for t in board.GetTracks() if t.GetClass() == "PCB_TRACK"]
    vias = [t for t in board.GetTracks() if t.GetClass() == "PCB_VIA"]
    per_net: Dict[str, dict] = defaultdict(lambda: {"length_mm": 0.0, "segments": 0, "vias": 0, "layers": set()})
    for t in tracks:
        d = per_net[t.GetNetname()]
        d["length_mm"] += t.GetLength() / 1e6
        d["segments"] += 1
        d["layers"].add(board.GetLayerName(t.GetLayer()))
    for v in vias:
        per_net[v.GetNetname()]["vias"] += 1
    # detour ratio: routed length vs the straight-line span of the net's pads
    pads: Dict[str, list] = defaultdict(list)
    for pad in board.GetPads():
        if pad.GetNetCode() > 0:
            p = pad.GetPosition()
            pads[pad.GetNetname()].append((p.x / 1e6, p.y / 1e6))
    rows = []
    for name, d in per_net.items():
        pts = pads.get(name, [])
        span = 0.0
        if len(pts) >= 2:
            xs, ys = [q[0] for q in pts], [q[1] for q in pts]
            span = (max(xs) - min(xs)) + (max(ys) - min(ys))      # Manhattan extent of the net's pads
        ratio = round(d["length_mm"] / span, 2) if span > 0.5 else None
        rows.append({"net": name, "length_mm": round(d["length_mm"], 2), "segments": d["segments"],
                     "vias": d["vias"], "layers": sorted(d["layers"]), "detour_ratio": ratio})
    rows.sort(key=lambda r: -(r["detour_ratio"] or 0))
    angles = defaultdict(int)
    for t in tracks:
        d = t.GetEnd() - t.GetStart()
        a = round(math.degrees(math.atan2(d.y, d.x))) % 180
        angles["octilinear" if a in (0, 45, 90, 135) else "other"] += 1
    return {"segments": len(tracks), "vias": len(vias), "length_mm": round(sum(t.GetLength() for t in tracks) / 1e6, 1),
            "unconnected": board.GetConnectivity().GetUnconnectedCount(True),
            "angles": dict(angles), "nets_by_detour": rows[:40]}


class Reviewer:
    """Score, improve, re-score."""

    def __init__(self, board, options: dict, route_fn, refill_fn, workdir: Path, log, binary: str = "",
                 model: str = "", max_rounds: int = 3, poll=None, cancelled=None, verify: bool = True,
                 min_gain: int = 2):
        self.board, self.options, self.route_fn, self.refill_fn = board, dict(options), route_fn, refill_fn
        self.workdir, self.log = workdir, log
        self.binary = binary or codex.find_codex()
        self.model, self.max_rounds, self.poll, self.cancelled = model, max_rounds, poll, cancelled
        self.verify, self.min_gain = verify, min_gain
        self.rounds: List[dict] = []

    def _images(self, round_no: int, issues: List[dict]) -> List[Path]:
        images = []
        full = render.render_board(self.board, self.workdir, name=f"review-round{round_no}.png", size=1800)
        if full:
            images.append(full)
            for i, issue in enumerate([x for x in issues if x.get("x_mm") is not None][:4]):
                crop = render.crop_at(self.board, full, issue["x_mm"], issue["y_mm"], 8.0,
                                      self.workdir / f"review-round{round_no}-closeup{i + 1}.png")
                if crop:
                    images.append(crop)
        return images

    def _verify(self) -> dict:
        drc = context.drc_summary(self.board, self.workdir / "verify")
        if not drc.get("available"):
            return {"violations": 0, "unconnected": self.board.GetConnectivity().GetUnconnectedCount(True)}
        return {"violations": sum(drc.get("violations_by_type", {}).values()),
                "unconnected": drc.get("unconnected_total", 0)}

    def run(self, request: str = "", apply: bool = True) -> List[dict]:
        if not self.binary:
            raise codex.CodexError("The codex CLI was not found.")
        self.workdir.mkdir(parents=True, exist_ok=True)
        last_issues: List[dict] = []
        best = None
        for round_no in range(1, self.max_rounds + 1):
            if self.cancelled and self.cancelled():
                break
            self.log(f"[review] round {round_no}: measuring and rendering...")
            ctx = context.board_context(self.board, self.options, self.rounds[-1] if self.rounds else None,
                                        self.workdir, with_drc=True)
            ctx["metrics"] = metrics(self.board)
            (self.workdir / f"review-context-round{round_no}.json").write_text(json.dumps(ctx, indent=1), encoding="utf-8")
            images = self._images(round_no, last_issues)
            labels = [f"{p.name}" + (f" (close-up around issue {i})" if i else " (full board, top copper)")
                      for i, p in enumerate(images)]
            prompt = "\n".join([REVIEW_PROMPT, "\n## Images attached\n", "\n".join(labels),
                                "\n## What the user asked for\n", request or "Score the routing and improve it.",
                                "\n## Earlier rounds\n", json.dumps(self.rounds, indent=1, default=str)[:12000],
                                "\n## Board context and metrics\n", json.dumps(ctx, indent=1)[:70000],
                                "\n\nAnswer with the JSON object only."])
            self.log(f"[review] round {round_no}: asking Codex to score the board...")
            reply = codex.run(self.binary, prompt, self.workdir, schema=REVIEW_SCHEMA, model=self.model,
                              cancelled=self.cancelled, poll=self.poll, images=images)
            reply = act_mod.normalize(reply)
            score = int(reply.get("score", 0))
            self.log(f"[review] score {score}/100: {reply.get('summary', '').strip()}")
            for c in reply.get("criteria", []):
                self.log(f"[review]   {c.get('name')}: {c.get('score')}  {c.get('comment', '')}")
            for issue in reply.get("issues", [])[:10]:
                self.log(f"[review]   issue ({issue.get('severity')}) {issue.get('net') or ''} at "
                         f"({issue.get('x_mm')}, {issue.get('y_mm')}): {issue.get('description')} -> {issue.get('suggestion')}")
            last_issues = reply.get("issues", [])
            record = {"round": round_no, "score": score, "criteria": reply.get("criteria", []),
                      "issues": last_issues, "summary": reply.get("summary", ""), "results": []}
            acts = [a for a in reply.get("actions", []) if a.get("type") in act_mod.ACTION_TYPES]
            if apply and acts and not reply.get("done"):
                applier = act_mod.BoardActions(self.board, self.options, self.route_fn, self.refill_fn, self.log,
                                               verify_fn=self._verify if self.verify else None)
                record["results"] = [r.as_dict() for r in applier.apply(acts)]
            self.rounds.append(record)
            (self.workdir / "review-rounds.json").write_text(json.dumps(self.rounds, indent=1, default=str), encoding="utf-8")
            if reply.get("done") or not acts or not apply:
                break
            if best is not None and score < best - 10:
                self.log("[review] score dropped sharply; stopping here")
                break
            best = score if best is None else max(best, score)
        return self.rounds
