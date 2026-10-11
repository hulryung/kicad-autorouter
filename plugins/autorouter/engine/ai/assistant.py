"""The assistant loop: request -> board context -> Codex -> actions -> results,
repeated until the model says it is done or the round limit is reached."""
import json
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import actions as act_mod
from . import codex, context

SYSTEM_PROMPT = """You are a PCB layout engineer working inside KiCad through an autorouter plugin.
You see the open board as JSON (file board-context.json in the working directory and inline below)
and you answer ONLY with a JSON object matching the given schema. Do not write prose outside it.

What you can do, through "actions":
- set_options: change routing options for the next route action. Keys: pitch_mm (grid, 0.05-0.5),
  via_cost (cells, 5-200), skip_nets (list of net names/patterns left to copper pours), track_mode
  ("auto" | "netclass" | "current" | "fixed"), track_width_mm (with "fixed"), neck_down (bool),
  fanout_vias (bool), stitch_vias (bool), stitch_pitch_mm, diff_pairs (bool), allow_vias (bool),
  routable_layers (list of bool per copper layer, top to bottom), horizontal_layers (list of bool),
  remove_existing (bool: strip existing tracks first), power_pours (bool), rip_up (bool), max_expansions (int).
- route: run the built-in router on a scope: "board", "nets" (+ nets), "sheets" (+ sheets) or "selection".
  Existing tracks are kept unless remove_existing is set; only open connections are routed.
- remove_tracks: delete all tracks and vias of the given nets (then usually route them again).
- add_track: draw a track for a net on a layer through points_mm (a polyline), width_mm optional.
- add_via: place a through via for a net at x_mm, y_mm (net class size unless given).
- refill_zones: refill copper pours (done automatically after route).
- ask: a question for the user when you cannot decide; the loop stops.
- note: a remark shown to the user.

Guidelines: prefer routing by scope over drawing tracks by hand; leave ground/power pour nets to the
pours (skip_nets) and let fanout/stitching vias connect them; respect the net classes and the board
minimums shown; keep differential pairs coupled; explain in "summary" what you changed and why. After a
route action you will get its result (routed/failed counts, DRC) in the next round; set "done": true
when the request is satisfied or nothing more can be done.
"""


class Assistant:
    def __init__(self, board, options: dict, route_fn: Callable[[dict], dict], refill_fn: Callable[[], int],
                 workdir: Path, log: Callable[[str], None], binary: str = "", model: str = "",
                 max_rounds: int = 3, with_drc: bool = True, poll: Optional[Callable[[], None]] = None,
                 cancelled: Optional[Callable[[], bool]] = None, selection: Optional[dict] = None):
        self.board = board
        self.options = dict(options)
        self.route_fn = route_fn
        self.refill_fn = refill_fn
        self.workdir = workdir
        self.log = log
        self.binary = binary or codex.find_codex()
        self.model = model
        self.max_rounds = max_rounds
        self.with_drc = with_drc
        self.poll = poll
        self.cancelled = cancelled
        self.selection = selection
        self.transcript: List[dict] = []
        self.last_run: Optional[dict] = None

    def run(self, request: str) -> List[dict]:
        if not self.binary:
            raise codex.CodexError("The codex CLI was not found. Install it (npm i -g @openai/codex) or set its path.")
        self.workdir.mkdir(parents=True, exist_ok=True)
        history: List[dict] = []
        for round_no in range(1, self.max_rounds + 1):
            if self.cancelled and self.cancelled():
                break
            self.log(f"[ai] round {round_no}: reading the board...")
            ctx = context.board_context(self.board, self.options, self.last_run, self.workdir,
                                        selection=self.selection, with_drc=self.with_drc)
            (self.workdir / "board-context.json").write_text(json.dumps(ctx, indent=1), encoding="utf-8")
            prompt = self._prompt(request, ctx, history, round_no)
            self.log(f"[ai] round {round_no}: asking Codex ({self.model or 'default model'})...")
            reply = codex.run(self.binary, prompt, self.workdir, schema=act_mod.REPLY_SCHEMA, model=self.model,
                              on_output=lambda line: None, cancelled=self.cancelled, poll=self.poll)
            problems = act_mod.validate(reply)
            self.transcript.append({"round": round_no, "reply": reply, "problems": problems})
            self.log(f"[ai] {reply.get('summary', '').strip()}")
            if problems:
                self.log("[ai] reply problems: " + "; ".join(problems))
            acts = [a for a in reply.get("actions", []) if isinstance(a, dict) and a.get("type") in act_mod.ACTION_TYPES]
            applier = act_mod.BoardActions(self.board, self.options, self.route_fn, self.refill_fn, self.log)
            results = applier.apply(acts)
            for r in results:
                if r.action.get("type") == "route" and r.ok:
                    self.last_run = r.data
            history.append({"round": round_no, "summary": reply.get("summary", ""),
                            "results": [r.as_dict() for r in results]})
            if any(a.get("type") == "ask" for a in acts):
                break
            if reply.get("done", False) or not acts:
                break
        return history

    def _prompt(self, request: str, ctx: dict, history: List[dict], round_no: int) -> str:
        parts = [SYSTEM_PROMPT, "\n## The user's request\n", request.strip(), "\n"]
        if history:
            parts.append("\n## What happened in earlier rounds\n")
            parts.append(json.dumps(history, indent=1)[:12000])
        parts.append(f"\n## Board context (round {round_no})\n")
        text = json.dumps(ctx, indent=1)
        if len(text) > 60000:
            slim = dict(ctx)
            slim["footprints"] = slim["footprints"][:80] + [{"note": "truncated; see board-context.json"}]
            text = json.dumps(slim, indent=1)
        parts.append(text)
        parts.append("\n\nAnswer with the JSON object only.")
        return "\n".join(parts)
