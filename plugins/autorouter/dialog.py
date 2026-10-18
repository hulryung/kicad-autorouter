"""The Autorouter dialog: options, live progress, the AI assistant.

The router reads the board through the PCB API, searches on a worker thread
and puts each connection's copper on the board as soon as it is found.
"""
import threading
import time
from pathlib import Path
from typing import Optional

import pcbnew
import wx

from .engine import fab, session, si
from .engine.options import RouteOptions
from .engine.native import (apply as native_apply, extract as native_extract, kicad_tuning as native_kicad_tuning,
                            pipeline as native_pipeline, router as native_router)
from .engine.ai import assistant as ai_assistant, codex as ai_codex, headless as ai_headless, review as ai_review
from .engine.native.problem import (NativeOptions, SCOPE_BOARD, SCOPE_SELECTION, SCOPE_SHEETS,
                                    SIZE_AUTO, SIZE_CURRENT, SIZE_FIXED, SIZE_NETCLASS)
from .engine.paths import data_dir
from .log import get_logger
from .settings import Settings
from .si_page import SiPage

logger = get_logger("dialog")




class AutorouterDialog(wx.Dialog):
    def __init__(self, parent, board):
        super().__init__(parent, title="Autorouter",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.board = board
        self.settings = Settings.load()
        self.layers = session.copper_layers(board)
        self.net_classes = session.net_class_names(board)
        self.sizes = native_extract.board_sizes(board)
        self.sheets = native_extract.sheets(board)
        try:
            self.sel_refs, self.sel_nets = native_extract.selection_scope(board, pcbnew.GetCurrentSelection())
        except Exception:
            self.sel_refs, self.sel_nets = [], []
        self.job: Optional[session.JobFiles] = None
        self.applied = False
        self.busy = False
        self._cancel = False
        self._native_layers = None
        self._last_refresh = 0.0

        self._build()
        self._load_options(self.settings.options)
        self.SetMinSize((760, 560))
        self.Fit()
        # never taller than the screen it opens on (the log shrinks instead)
        area = wx.Display(wx.Display.GetFromWindow(parent) if parent else 0).GetClientArea()
        w, h = self.GetSize()
        if h > area.height - 40 or w > area.width - 40:
            self.SetSize((min(w, area.width - 40), min(h, area.height - 40)))
        self.CentreOnParent()
        self.Bind(wx.EVT_CLOSE, self._on_close)

    # ---- layout -------------------------------------------------------------

    def _build(self):
        root = wx.BoxSizer(wx.VERTICAL)
        self.notebook = wx.Notebook(self)
        self.notebook.AddPage(self._routing_page(self.notebook), "Routing")
        self.si_page = SiPage(self.notebook, self.board, self._append_log)
        self.notebook.AddPage(self.si_page, "Signal integrity")
        self.notebook.AddPage(self._layers_page(self.notebook), "Layers && nets")
        self.notebook.AddPage(self._ai_page(self.notebook), "AI assistant")
        root.Add(self.notebook, 0, wx.EXPAND | wx.ALL, 8)

        progress = wx.StaticBoxSizer(wx.VERTICAL, self, "Progress")
        self.status = wx.StaticText(self, label="Ready.")
        self.gauge = wx.Gauge(self, range=1000)
        self.log = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL)
        self.log.SetFont(wx.Font(wx.FontInfo(10).Family(wx.FONTFAMILY_TELETYPE)))
        self.log.SetMinSize((-1, 110))
        progress.Add(self.status, 0, wx.EXPAND | wx.ALL, 4)
        progress.Add(self.gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 4)
        progress.Add(self.log, 1, wx.EXPAND | wx.ALL, 4)
        root.Add(progress, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.open_jobs = wx.Button(self, label="Open data folder")
        self.open_jobs.Bind(wx.EVT_BUTTON, lambda e: self._open_folder(
            self.job.directory if self.job else data_dir()))
        buttons.Add(self.open_jobs, 0, wx.RIGHT, 8)
        buttons.AddStretchSpacer()
        self.route_btn = wx.Button(self, label="Route")
        self.route_btn.SetDefault()
        self.route_btn.Bind(wx.EVT_BUTTON, self._on_route)
        self.cancel_btn = wx.Button(self, label="Cancel")
        self.cancel_btn.Bind(wx.EVT_BUTTON, self._on_cancel)
        self.cancel_btn.Disable()
        self.close_btn = wx.Button(self, wx.ID_CLOSE, "Close")
        self.close_btn.Bind(wx.EVT_BUTTON, self._on_close)
        for b in (self.route_btn, self.cancel_btn, self.close_btn):
            buttons.Add(b, 0, wx.LEFT, 6)
        root.Add(buttons, 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(root)

    def _routing_page(self, parent):
        """Two columns of groups, so the page stays short enough for a laptop
        screen: search and sizes on the left, pours/vias and strategy on the
        right, the scope and the existing-tracks choice across the bottom."""
        page = wx.Panel(parent)
        box = wx.BoxSizer(wx.VERTICAL)

        def grid_row(grid, label, ctrl, tip=None):
            text = wx.StaticText(page, label=label)
            if tip:
                text.SetToolTip(tip)
                ctrl.SetToolTip(tip)
            grid.Add(text, 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(ctrl, 0, wx.EXPAND)
            return ctrl

        def check(label, tip):
            c = wx.CheckBox(page, label=label)
            c.SetToolTip(tip)
            return c

        columns = wx.BoxSizer(wx.HORIZONTAL)
        left = wx.BoxSizer(wx.VERTICAL)
        right = wx.BoxSizer(wx.VERTICAL)

        # -- left: the search and the sizes
        search = wx.StaticBoxSizer(wx.VERTICAL, page, "Search")
        grid = wx.FlexGridSizer(cols=2, vgap=4, hgap=8)
        grid.AddGrowableCol(1)
        self.pitch = grid_row(grid, "Grid (mm)", wx.SpinCtrlDouble(page, min=0.025, max=2.0, initial=0.1, inc=0.025),
                              "Track centrelines snap to this grid. Smaller is more precise and slower; "
                              "about half the smallest pad pitch works well.")
        self.pitch.SetDigits(3)
        self.native_via_cost = grid_row(grid, "Via cost (cells)", wx.SpinCtrl(page, min=0, max=100000, initial=25),
                                        "A via costs as much as this many cells of straight track.")
        self.native_budget = grid_row(grid, "Budget per connection (k cells)",
                                      wx.SpinCtrl(page, min=10, max=100000, initial=400),
                                      "A* gives up on a connection after expanding this many cells.")
        self.native_time_limit = grid_row(grid, "Time limit (min, 0 = none)",
                                          wx.SpinCtrl(page, min=0, max=600, initial=10),
                                          "The whole run, all passes. When it runs out, what is routed stays, the "
                                          "rest is reported as not routed, and the pour vias are still placed.")
        self.skip_nets = grid_row(grid, "Nets to leave unrouted", wx.TextCtrl(page),
                                  "Comma-separated net names or patterns, e.g. GND,+3V3,*_PLANE. "
                                  "Use it for nets that get a copper pour.")
        search.Add(grid, 0, wx.EXPAND | wx.ALL, 6)
        left.Add(search, 0, wx.EXPAND | wx.BOTTOM, 8)

        sizes = wx.StaticBoxSizer(wx.VERTICAL, page, "Track and via sizes")
        grid = wx.FlexGridSizer(cols=2, vgap=4, hgap=8)
        grid.AddGrowableCol(1)
        mm = lambda nm: f"{nm / 1e6:g} mm"
        cur_w = self.sizes["current_track_width"]
        cur_v = self.sizes["current_via"]
        self.track_choices = [(SIZE_AUTO, 0, "Automatic (net class, existing tracks, pad width)"),
                              (SIZE_NETCLASS, 0, "Net class width (per net)"),
                              (SIZE_CURRENT, 0, f"Editor's current ({mm(cur_w)})")]
        self.track_choices += [(SIZE_FIXED, w, f"Pre-defined {mm(w)}") for w in self.sizes["track_widths"]]
        self.track_size = grid_row(grid, "Track width", wx.Choice(page, choices=[c[2] for c in self.track_choices]),
                                   "Net class sizes are KiCad's default. The other entries are the board's "
                                   "pre-defined sizes (Board Setup) and the editor's current selection.")
        self.via_choices = [(SIZE_NETCLASS, (0, 0), "Net class via (per net)"),
                            (SIZE_CURRENT, cur_v, f"Editor's current ({mm(cur_v[0])} / {mm(cur_v[1])})")]
        self.via_choices += [(SIZE_FIXED, v, f"Pre-defined {mm(v[0])} / {mm(v[1])}") for v in self.sizes["via_sizes"]]
        self.via_size = grid_row(grid, "Via (diameter / drill)", wx.Choice(page, choices=[c[2] for c in self.via_choices]))
        sizes.Add(grid, 0, wx.EXPAND | wx.ALL, 6)
        left.Add(sizes, 0, wx.EXPAND)

        # -- right: pours and vias, then the strategy
        pours = wx.StaticBoxSizer(wx.VERTICAL, page, "Copper pours and vias")
        self.native_vias = check("Allow vias", "Off: single-layer routing on each connection's pad layer.")
        self.native_fanout = check("Fan out GND/power pads to their planes with vias",
                                   "Nets with a copper zone (pours) get a short stub and a via at each SMD pad "
                                   "on a layer the pour does not reach, so the refilled pour connects them. "
                                   "Works for nets listed under 'Nets to leave unrouted' too.")
        self.native_plane = check("Pour under the pads (a +3V3 plane): vias only, no tracks",
                                  "A net whose pour lies under at least half of its pads is treated as a plane, "
                                  "like GND: each pad gets a via (or a short tie) to the pour and no tracks are "
                                  "drawn for it. Off: such nets are routed with tracks as well.")
        self.native_power = check("Power pours: pad vias and coarse stitching",
                                  "Routed power nets that also have a copper pour (e.g. +3V3 on an inner layer) "
                                  "get a via at each pad on a layer the pour does not cover, and a via grid at "
                                  "twice the stitching pitch through the pour when it spans two or more layers.")
        stitch_row = wx.BoxSizer(wx.HORIZONTAL)
        self.native_stitch = check("Stitch GND pours with vias every",
                                   "Pour nets left unrouted that have zones on two or more layers get a grid "
                                   "of vias through their zones, wherever a via still fits, to shorten return "
                                   "current loops. Placed after all routing.")
        self.stitch_pitch = wx.SpinCtrlDouble(page, min=1.0, max=50.0, initial=4.0, inc=0.5)
        self.stitch_pitch.SetDigits(1)
        stitch_row.Add(self.native_stitch, 0, wx.ALIGN_CENTER_VERTICAL)
        stitch_row.Add(self.stitch_pitch, 0, wx.LEFT | wx.RIGHT, 6)
        stitch_row.Add(wx.StaticText(page, label="mm"), 0, wx.ALIGN_CENTER_VERTICAL)
        for item in (self.native_vias, self.native_fanout, self.native_plane, self.native_power):
            pours.Add(item, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
        pours.Add(stitch_row, 0, wx.ALL, 6)
        right.Add(pours, 0, wx.EXPAND | wx.BOTTOM, 8)

        strategy = wx.StaticBoxSizer(wx.VERTICAL, page, "Strategy")
        self.native_retry = check("Retry failed connections (bigger budget, cheaper vias)",
                                  "Connections still open after the rip-up rounds get one more search with "
                                  "three times the budget and vias at a third of the cost.")
        self.native_pairs = check("Differential pairs coupled (net class pair width and gap)",
                                  "Nets named like D+/D-, TX_P/TX_N or USB_DP/USB_DN are routed side by side "
                                  "at the pair width and gap of their net class, which is where KiCad keeps the "
                                  "impedance choice (Board Setup > Net Classes).")
        self.native_shove = check("Move existing tracks out of the way (shove)",
                                  "When a connection finds no room, unlocked tracks and vias already on the "
                                  "board may be removed and routed again, the way KiCad's interactive router "
                                  "shoves tracks aside. Locked tracks and pour nets stay where they are. "
                                  "Also lets the second pass move what the first pass placed.")
        for item in (self.native_retry, self.native_pairs):
            strategy.Add(item, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
        strategy.Add(self.native_shove, 0, wx.ALL, 6)
        right.Add(strategy, 0, wx.EXPAND)

        columns.Add(left, 1, wx.EXPAND | wx.RIGHT, 8)
        columns.Add(right, 1, wx.EXPAND)
        box.Add(columns, 0, wx.EXPAND | wx.ALL, 8)

        # -- bottom: the scope, and what happens before and after
        bottom = wx.BoxSizer(wx.HORIZONTAL)
        scope = wx.StaticBoxSizer(wx.HORIZONTAL, page, "What to route")
        sel_label = (f"Selected items ({len(self.sel_refs)} footprints, {len(self.sel_nets)} nets)"
                     if (self.sel_refs or self.sel_nets) else "Selected items (nothing selected)")
        self.scope_choice = wx.RadioBox(page, choices=["Whole board", sel_label, "Schematic sheets:"],
                                        majorDimension=1, style=wx.RA_SPECIFY_COLS)
        if not (self.sel_refs or self.sel_nets):
            self.scope_choice.EnableItem(1, False)
        self.scope_choice.SetToolTip("Selected items: select footprints, pads, tracks or nets in the editor "
                                     "before opening the dialog. Only connections touching them are routed.")
        self.scope_choice.Bind(wx.EVT_RADIOBOX, lambda e: self.sheet_list.Enable(self.scope_choice.GetSelection() == 2))
        self.sheet_list = wx.CheckListBox(page, choices=[f"{name}  ({n} footprints)" for name, n in self.sheets])
        self.sheet_list.SetMinSize((-1, 70))
        self.sheet_list.Disable()
        scope.Add(self.scope_choice, 0, wx.ALL, 6)
        scope.Add(self.sheet_list, 1, wx.EXPAND | wx.ALL, 6)
        bottom.Add(scope, 1, wx.EXPAND | wx.RIGHT, 8)

        board_box = wx.StaticBoxSizer(wx.VERTICAL, page, "Existing tracks")
        # Always starts on the safe choice: this one is not remembered between runs.
        self.existing_choice = wx.RadioBox(page, choices=["Keep them: route only the unconnected (ratsnest) connections",
                                                          "Remove all tracks and vias first, route everything"],
                                           majorDimension=1, style=wx.RA_SPECIFY_COLS)
        self.existing_choice.SetToolTip("Keep: what is routed stays as it is (with 'Move existing tracks' on, "
                                        "unlocked tracks may still be shoved aside); only connections KiCad "
                                        "shows in the ratsnest are routed. Remove: a fresh start for the scope.")
        self.refill = check("Refill copper zones when done",
                            "Pours are refilled around the new tracks and vias, as Edit > Fill All "
                            "Zones would; otherwise the DRC reports the stale fills.")
        self.refill.SetValue(True)
        board_box.Add(self.existing_choice, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
        board_box.Add(self.refill, 0, wx.ALL, 6)
        bottom.Add(board_box, 0, wx.EXPAND)
        box.Add(bottom, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        page.SetSizer(box)
        return page

    def _layers_page(self, parent):
        page = wx.Panel(parent)
        box = wx.BoxSizer(wx.HORIZONTAL)
        names = [name for _, name in self.layers]

        left = wx.StaticBoxSizer(wx.VERTICAL, page, "Routable layers")
        self.routable = wx.CheckListBox(page, choices=names)
        for i in range(len(names)):
            self.routable.Check(i, True)
        left.Add(self.routable, 1, wx.EXPAND | wx.ALL, 4)

        middle = wx.StaticBoxSizer(wx.VERTICAL, page, "Preferred direction")
        self.override_dirs = wx.CheckBox(page, label="Override (checked = horizontal)")
        self.horizontal = wx.CheckListBox(page, choices=names)
        for i in range(len(names)):
            self.horizontal.Check(i, i % 2 == 0)
        self.horizontal.Disable()
        self.override_dirs.Bind(wx.EVT_CHECKBOX,
                                lambda e: self.horizontal.Enable(self.override_dirs.GetValue()))
        middle.Add(self.override_dirs, 0, wx.ALL, 4)
        middle.Add(self.horizontal, 1, wx.EXPAND | wx.ALL, 4)

        right = wx.StaticBoxSizer(wx.VERTICAL, page, "Net classes to leave unrouted")
        self.skip_classes = wx.CheckListBox(page, choices=self.net_classes)
        right.Add(self.skip_classes, 1, wx.EXPAND | wx.ALL, 4)

        for sizer in (left, middle, right):
            box.Add(sizer, 1, wx.EXPAND | wx.ALL, 6)
        page.SetSizer(box)
        return page

    def _ai_page(self, parent):
        page = wx.Panel(parent)
        box = wx.BoxSizer(wx.VERTICAL)
        intro = wx.StaticText(page, label=(
            "Describe what you want in plain words. The assistant (OpenAI Codex CLI) reads the board, "
            "the net classes, the last run and the DRC, and answers with routing actions this plugin "
            "carries out live: option changes, routing by net, sheet or selection, removing or adding "
            "tracks and vias. Each round is one undo step with everything else."))
        intro.Wrap(600)
        box.Add(intro, 0, wx.ALL, 10)
        self.ai_request = wx.TextCtrl(page, style=wx.TE_MULTILINE)
        self.ai_request.SetMinSize((-1, 70))
        self.ai_request.SetHint("e.g. Route the whole board, leave GND to the pours, keep the USB pair coupled. "
                                "Then fix anything the DRC reports.")
        box.Add(self.ai_request, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(page, label="Model"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.ai_model = wx.TextCtrl(page, value=self.settings.ai_model, size=(160, -1))
        self.ai_model.SetHint("codex default")
        row.Add(self.ai_model, 0, wx.RIGHT, 12)
        row.Add(wx.StaticText(page, label="Rounds"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.ai_rounds = wx.SpinCtrl(page, min=1, max=10, initial=3)
        row.Add(self.ai_rounds, 0, wx.RIGHT, 12)
        self.ai_drc = wx.CheckBox(page, label="Include DRC results (runs kicad-cli, slower)")
        self.ai_drc.SetValue(True)
        row.Add(self.ai_drc, 0, wx.ALIGN_CENTER_VERTICAL)
        box.Add(row, 0, wx.ALL, 10)
        row2 = wx.BoxSizer(wx.HORIZONTAL)
        self.ai_ask = wx.Button(page, label="Ask the assistant")
        self.ai_ask.Bind(wx.EVT_BUTTON, self._on_ai_ask)
        self.ai_review = wx.Button(page, label="Review current routing")
        self.ai_review.Bind(wx.EVT_BUTTON, lambda e: self._on_ai_ask(None, review=True))
        self.ai_score = wx.Button(page, label="Score and improve")
        self.ai_score.SetToolTip("The model scores the routing (0-100, per criterion) from the context, metrics, "
                                 "a board image and close-ups of what it flags, applies improvements, and re-scores, "
                                 "for up to 'Rounds' rounds or until the score stops rising.")
        self.ai_score.Bind(wx.EVT_BUTTON, self._on_ai_score)
        row2.Add(self.ai_ask, 0, wx.RIGHT, 6)
        row2.Add(self.ai_review, 0, wx.RIGHT, 6)
        row2.Add(self.ai_score, 0, wx.RIGHT, 12)
        binary = ai_codex.find_codex(self.settings.codex_path)
        self.ai_status = wx.StaticText(page, label=(f"codex: {binary}  ({ai_codex.codex_version(binary)})" if binary
                                                    else "codex CLI not found: install with  npm i -g @openai/codex"))
        row2.Add(self.ai_status, 0, wx.ALIGN_CENTER_VERTICAL)
        box.Add(row2, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        page.SetSizer(box)
        return page

    # ---- the assistant ------------------------------------------------------

    def _ai_begin(self, suffix: str):
        """Common start of an assistant session: codex present, settings saved,
        a job folder, the live-display route function. Returns
        (binary, route_fn, selection) or None when it cannot start."""
        binary = ai_codex.find_codex(self.settings.codex_path)
        if not binary:
            wx.MessageBox("The codex CLI was not found. Install it with  npm i -g @openai/codex  and log in once.",
                          "Autorouter", wx.ICON_ERROR, self)
            return None
        self._save_settings()
        options = self._collect_options()
        self.log.Clear()
        self._cancel = False
        self.applied = False
        self._set_busy(True)
        name = Path(self.board.GetFileName()).stem if self.board.GetFileName() else "board"
        self.job = session.new_job(name + suffix)
        base = self.native_options(options)
        selection = {"refs": list(self.sel_refs), "nets": list(self.sel_nets)}
        self._path_items = {}
        self._run_added = []                   # items this run created (for the overlap merge at the end)
        self._run_removed = set()              # UUIDs of items ripped up again
        self._native_layers = None
        return binary, self._ai_route_fn(base, selection), selection

    def _on_ai_score(self, event):
        """Score the current routing, let the model improve it, re-score."""
        started = self._ai_begin("-review")
        if started is None:
            return
        binary, route_fn, selection = started
        reviewer = ai_review.Reviewer(self.board, {}, route_fn, lambda: session.refill_zones(self.board),
                                      self.job.directory, log=self._append_log, binary=binary,
                                      model=self.ai_model.GetValue().strip(), max_rounds=self.ai_rounds.GetValue(),
                                      poll=lambda: wx.SafeYield(self, True), cancelled=lambda: self._cancel)
        self._show_progress("Reviewer: measuring, rendering and scoring the board...", None)
        try:
            rounds = reviewer.run(self.ai_request.GetValue().strip(), apply=True)
        except ai_codex.CodexError as e:
            self._set_busy(False)
            self._show_progress(f"Reviewer stopped: {e}", 0.0)
            return
        except Exception as e:
            logger.exception("Reviewer failed")
            self._set_busy(False)
            self._show_progress(f"Reviewer failed: {e}", 0.0)
            return
        self._set_busy(False)
        pcbnew.Refresh()
        scores = [r["score"] for r in rounds]
        self._show_progress(f"Reviewer: score {' -> '.join(str(x) for x in scores)} over {len(rounds)} round(s). "
                            f"{rounds[-1]['summary'] if rounds else ''}"[:300], 1.0)
        self.applied = self.applied or any(r.get("type") in ("route", "add_track", "add_via", "remove_tracks",
                                                              "remove_items_near", "move_footprint", "move_items_near")
                                           and r.get("ok") for rd in rounds for r in rd["results"])

    def _ai_route_fn(self, base, selection):
        def on_path(path):
            if self._native_layers is not None:
                self._add_native_path(path)

        def route_fn(spec):
            # the router needs the layers for live display; read them per run
            o = ai_headless.apply_scope(ai_headless.native_options_from(spec.get("options", {}), base), spec, selection)
            o = ai_headless.apply_hints(o, spec, [name for _, name in self.layers])
            if spec.get("options", {}).get("remove_existing"):
                session.remove_tracks(self.board)
            started = time.time()

            def progress(done, total, routed, failed, net):
                self._show_progress(f"[assistant] routing {done}/{total}: {routed} routed, {failed} failed "
                                    f"({time.time() - started:.0f}s) {net}", done / total if total else None)
                wx.SafeYield(self, True)

            def set_layers(layers):
                self._native_layers = layers

            self._path_items = {}

            def extract_here(board, opts, status):
                problem = native_extract.extract(board, opts, status=status)
                self._path_items = {ex.path.pid: list(ex.items) for ex in problem.existing}
                return problem

            try:
                job = native_pipeline.run_job(self.board, o, status=self._status_now, progress=progress,
                                              on_path=on_path, on_remove=lambda pid: self._remove_native_path(pid),
                                              cancelled=lambda: self._cancel,
                                              escalate=spec.get("options", {}).get("escalate", True),
                                              apply_paths=False, on_layers=set_layers, extract_fn=extract_here)
            except Exception as e:
                return {"error": f"could not route: {e}"}
            if not job.layers:
                return {"error": "could not read the board"}
            escalated = job.escalated
            result = job
            # as after a dialog run: shared copper written once, then length matching
            try:
                live = [t for t in self._run_added if str(t.m_Uuid.AsString()) not in self._run_removed]
                native_apply.trim_overlaps(self.board, live)
                report = native_pipeline.tune_lengths(self.board, o, status=self._status_now,
                                                      pair_exceptions=job.pair_exceptions)
                if report is not None:
                    job.tuning, job.tuning_lines = report.groups, report.lines()
                    self._kicad_update_wanted = bool(report.kicad_patterns)
            except Exception as e:
                logger.exception("Finishing the assistant's run")
                self._append_log(f"[plugin] could not finish the run (overlaps, length matching): {e!r}")
            self._refill_if_wanted()
            pcbnew.Refresh()
            try:
                self.board.BuildConnectivity()
            except Exception:
                pass
            failed = list(job.failed)
            outcome = {"error": None, "scope": spec.get("scope", "board"),
                       "connections_in_scope": job.stats.get("connections", 0), "routed": result.routed,
                       "failed": failed, "ripped_up": result.ripped, "fanout_vias": result.fanout,
                       "coupled_pairs": result.pairs, "via_swaps": getattr(result, "pair_swaps", 0),
                       "stitching_vias": result.stitched,
                       "failed_detail": getattr(result, "failed_detail", []), "escalated": escalated,
                       "length_matching": job.tuning_lines, "pair_log": [list(x) for x in job.pair_log],
                       "pair_exceptions": job.pair_exceptions,
                       "unconnected_after": self.board.GetConnectivity().GetUnconnectedCount(True)}
            outcome["summary"] = (f"{result.routed} routed ({escalated} on the finer second pass), {len(failed)} failed, "
                                  f"{result.fanout} fanout vias, {result.stitched} stitching vias; "
                                  f"{outcome['unconnected_after']} unconnected left")
            return outcome

        return route_fn

    def _on_ai_ask(self, event, review: bool = False):
        request = self.ai_request.GetValue().strip()
        if review:
            request = ("Review the current routing of this board as a senior layout engineer: point out "
                       "problems (unrouted nets, DRC items, questionable widths, pairs not coupled, missing "
                       "return vias) and what you would change. Use only 'note' actions and set done to true."
                       + (f"\nUser notes: {request}" if request else ""))
        if not request:
            wx.MessageBox("Write what the assistant should do first.", "Autorouter", wx.ICON_INFORMATION, self)
            return
        started = self._ai_begin("-ai")
        if started is None:
            return
        binary, route_fn, selection = started

        def poll():
            wx.SafeYield(self, True)

        helper = ai_assistant.Assistant(self.board, {}, route_fn, lambda: session.refill_zones(self.board),
                                        self.job.directory, log=self._append_log, binary=binary,
                                        model=self.ai_model.GetValue().strip(), max_rounds=self.ai_rounds.GetValue(),
                                        with_drc=self.ai_drc.GetValue(), poll=poll,
                                        cancelled=lambda: self._cancel, selection=selection)
        self._show_progress("Assistant: reading the board and asking Codex...", None)
        try:
            history = helper.run(request)
        except ai_codex.CodexError as e:
            self._set_busy(False)
            self._show_progress(f"Assistant stopped: {e}", 0.0)
            return
        except Exception as e:
            logger.exception("Assistant failed")
            self._set_busy(False)
            self._show_progress(f"Assistant failed: {e}", 0.0)
            return
        self._set_busy(False)
        pcbnew.Refresh()
        asked = [a for h in helper.transcript for a in h["reply"].get("actions", []) if a.get("type") == "ask"]
        if asked:
            self._show_progress("Assistant asks: " + asked[-1].get("question", ""), 0.0)
        else:
            last = history[-1]["summary"] if history else ""
            self._show_progress(f"Assistant done after {len(history)} round(s). {last}"[:300], 1.0)
        self.applied = self.applied or any(r.get("type") in ("route", "add_track", "add_via", "remove_tracks") and r.get("ok")
                                           for h in history for r in h["results"])

    # ---- options <-> widgets ------------------------------------------------

    def _load_options(self, o: RouteOptions):
        self.pitch.SetValue(o.native_pitch_mm)
        self.native_via_cost.SetValue(o.native_via_cost)
        self.native_budget.SetValue(o.native_max_expansions_k)
        self.native_time_limit.SetValue(o.native_time_limit_min)
        self.skip_nets.SetValue(o.skip_nets)
        self.native_vias.SetValue(o.native_allow_vias)
        self.native_retry.SetValue(o.native_retry)
        self.native_fanout.SetValue(o.native_fanout_vias)
        self.native_pairs.SetValue(o.native_diff_pairs)
        self.native_stitch.SetValue(o.native_stitch_vias)
        self.stitch_pitch.SetValue(o.native_stitch_pitch_mm)
        self.native_power.SetValue(o.native_power_pours)
        self.native_plane.SetValue(o.native_plane_nets)
        self.native_shove.SetValue(o.native_shove)
        self.track_size.SetSelection(0)
        for i, (mode, value, _) in enumerate(self.track_choices):
            if mode == o.native_track_mode and (mode != SIZE_FIXED or value == o.native_track_width):
                self.track_size.SetSelection(i)
        self.via_size.SetSelection(0)
        for i, (mode, value, _) in enumerate(self.via_choices):
            if mode == o.native_via_mode and (mode != SIZE_FIXED or value == (o.native_via_diameter, o.native_via_drill)):
                self.via_size.SetSelection(i)
        self.existing_choice.SetSelection(0)             # never remembered: keep the tracks
        self.refill.SetValue(o.refill_zones)
        for i, name in enumerate(self.net_classes):
            self.skip_classes.Check(i, name in o.ignore_net_classes)
        if o.routable_layers and len(o.routable_layers) == len(self.layers) and not all(o.routable_layers):
            for i, v in enumerate(o.routable_layers):
                self.routable.Check(i, bool(v))
        else:
            # nothing chosen yet: an inner ground plane carries no tracks
            for i in self._ground_planes():
                self.routable.Check(i, False)
        if o.horizontal_layers and len(o.horizontal_layers) == len(self.layers):
            self.override_dirs.SetValue(True)
            self.horizontal.Enable()
            for i, v in enumerate(o.horizontal_layers):
                self.horizontal.Check(i, bool(v))

    def _ground_planes(self):
        try:
            planes = native_extract.ground_plane_layers(self.board)
        except Exception:
            logger.exception("Ground plane check failed")
            return []
        return [i for i in planes if i < len(self.layers)]

    def _collect_options(self) -> RouteOptions:
        n = len(self.layers)
        return RouteOptions(
            native_pitch_mm=self.pitch.GetValue(),
            native_via_cost=self.native_via_cost.GetValue(),
            native_allow_vias=self.native_vias.GetValue(),
            native_retry=self.native_retry.GetValue(),
            native_fanout_vias=self.native_fanout.GetValue(),
            native_diff_pairs=self.native_pairs.GetValue(),
            native_stitch_vias=self.native_stitch.GetValue(),
            native_stitch_pitch_mm=self.stitch_pitch.GetValue(),
            native_power_pours=self.native_power.GetValue(),
            native_plane_nets=self.native_plane.GetValue(),
            native_shove=self.native_shove.GetValue(),
            native_max_expansions_k=self.native_budget.GetValue(),
            native_time_limit_min=self.native_time_limit.GetValue(),
            skip_nets=self.skip_nets.GetValue().strip(),
            native_track_mode=self.track_choices[self.track_size.GetSelection()][0],
            native_track_width=self.track_choices[self.track_size.GetSelection()][1],
            native_via_mode=self.via_choices[self.via_size.GetSelection()][0],
            native_via_diameter=self.via_choices[self.via_size.GetSelection()][1][0],
            native_via_drill=self.via_choices[self.via_size.GetSelection()][1][1],
            ignore_net_classes=[self.net_classes[i] for i in self.skip_classes.GetCheckedItems()],
            routable_layers=[self.routable.IsChecked(i) for i in range(n)],
            horizontal_layers=[self.horizontal.IsChecked(i) for i in range(n)]
            if self.override_dirs.GetValue() else None,
            remove_existing_tracks=self.existing_choice.GetSelection() == 1,
            refill_zones=self.refill.GetValue(),
        )

    def native_options(self, o: RouteOptions) -> NativeOptions:
        scope = (SCOPE_BOARD, SCOPE_SELECTION, SCOPE_SHEETS)[self.scope_choice.GetSelection()]
        native = NativeOptions(
            pitch_mm=o.native_pitch_mm, via_cost=o.native_via_cost, allow_vias=o.native_allow_vias,
            max_expansions=o.native_max_expansions_k * 1000, retry_failed=o.native_retry,
            time_limit_s=o.native_time_limit_min * 60.0,
            fanout_vias=o.native_fanout_vias, diff_pairs=o.native_diff_pairs,
            stitch_vias=o.native_stitch_vias, stitch_pitch_mm=o.native_stitch_pitch_mm,
            power_pours=o.native_power_pours, shove=o.native_shove, plane_nets=o.native_plane_nets,
            skip_nets=[s.strip() for s in o.skip_nets.split(",") if s.strip()],
            ignore_net_classes=list(o.ignore_net_classes),
            routable_layers=o.routable_layers, horizontal_layers=o.horizontal_layers,
            scope=scope, scope_refs=list(self.sel_refs), scope_nets=list(self.sel_nets),
            scope_sheets=[self.sheets[i][0] for i in self.sheet_list.GetCheckedItems()],
            track_mode=o.native_track_mode, track_width=o.native_track_width,
            via_mode=o.native_via_mode, via_diameter=o.native_via_diameter, via_drill=o.native_via_drill)
        if o.routable_layers and not all(o.routable_layers):
            planes = set(self._ground_planes())
            off = [f"{name}{' (ground plane)' if i in planes else ''}"
                   for i, (_, name) in enumerate(self.layers) if i < len(o.routable_layers) and not o.routable_layers[i]]
            self._append_log("[plugin] no tracks on " + ", ".join(off) + " (Layers tab)")
        # the Signal integrity tab: impedance widths, length groups, routing order
        cfg = self.si_page.config()
        if cfg.fab:
            # the fab's rules first: the net classes they raise are what the
            # impedance plan and the router read
            try:
                for line in fab.apply(self.board, cfg.fab):
                    self._append_log(f"[plugin] {cfg.fab}: {line}")
                fab.write_drc_rules(self.board, cfg.fab)
            except Exception as e:
                logger.exception("Fabrication rules")
                self._append_log(f"[plugin] fabrication rules not applied: {e!r}")
        try:
            plan = si.plan(self.board, cfg)
        except Exception as e:
            logger.exception("Design intent")
            self._append_log(f"[plugin] signal integrity settings ignored: {e!r}")
            return native
        self._si_plan = plan
        return si.apply_to_options(native, cfg, plan)

    def _save_settings(self):
        if hasattr(self, "ai_model"):
            self.settings.ai_model = self.ai_model.GetValue().strip()
        self.settings.options = self._collect_options()
        self.settings.options.remove_existing_tracks = False       # a per-run choice, not a preference
        try:
            self.settings.save()
        except OSError as e:
            logger.warning("Could not save settings: %s", e)

    # ---- starting a run -----------------------------------------------------

    def _on_route(self, event):
        self._save_settings()
        options = self._collect_options()
        if options.routable_layers and not any(options.routable_layers):
            wx.MessageBox("Enable at least one routable layer.", "Autorouter", wx.ICON_WARNING, self)
            return
        self._start_native()

    def _prepare(self, options: RouteOptions) -> bool:
        self.log.Clear()
        self.applied = False
        self._cancel = False
        try:
            name = Path(self.board.GetFileName()).stem if self.board.GetFileName() else "board"
            self.job = session.new_job(name)
            if options.remove_existing_tracks:
                removed = session.remove_tracks(self.board)
                self._append_log(f"[plugin] removed {removed} existing tracks and vias")
                pcbnew.Refresh()
            return True
        except Exception as e:
            logger.exception("Preparing the run failed")
            wx.MessageBox(str(e), "Autorouter", wx.ICON_ERROR, self)
            return False

    # ---- the built-in router ------------------------------------------------

    def _start_native(self):
        options = self._collect_options()
        if not self._prepare(options):
            return
        native = self.native_options(options)
        self.si_page.save()
        self._native_opts = native
        plan = getattr(self, "_si_plan", None)
        if plan is not None and (native.impedance or native.length_groups):
            for line in [l for l in plan.lines if not l.startswith("  ")] + [f"warning: {w}" for w in plan.warnings]:
                self._append_log("[plugin] " + line)
        if native.scope == SCOPE_SHEETS and not native.scope_sheets:
            wx.MessageBox("Tick at least one schematic sheet, or route the whole board.",
                          "Autorouter", wx.ICON_WARNING, self)
            return
        self._set_busy(True)
        self._show_progress("Reading the board...", None)
        started = time.time()

        last_logged = [0.0]

        def progress(done, total, routed, failed, name):
            text = f"Routing {done}/{total}: {routed} routed, {failed} failed  ({time.time() - started:.0f}s)  {name}"
            wx.CallAfter(self._show_progress, text, done / total if total else None)
            # the job log keeps a trace of the run (one line a second at most)
            # so a run that seems stuck can be read afterwards
            now = time.time()
            if now - last_logged[0] >= 1.0:
                last_logged[0] = now
                self._log_file("[router] " + text)

        def on_path(path):
            wx.CallAfter(self._add_native_path, path)

        def on_remove(pid):
            wx.CallAfter(self._remove_native_path, pid)

        def extract_on_ui(board, opts, status):
            # pcbnew work belongs on the UI thread; no yielding in there
            def read():
                problem = native_extract.extract(board, opts, status=lambda t: self._append_log("[plugin] " + t))
                # shove mode: the board's own pieces are removable by path id
                self._path_items = {ex.path.pid: list(ex.items) for ex in problem.existing}
                st = problem.stats
                self._append_log(f"[plugin] grid {problem.grid.w}x{problem.grid.h}x{problem.grid.layers} cells at "
                                 f"{opts.pitch_mm:g} mm, {st['nets']} nets, {st['connections']} of "
                                 f"{st['all_connections']} connections in scope, {st.get('fanout_vias', 0)} fanout vias, "
                                 f"{len(problem.stitch)} stitch sites, {st.get('movable_paths', 0)} movable pieces, "
                                 f"read in {st['extract_seconds']:.1f}s")
                return problem
            return self._on_main(read)

        def set_layers(layers):
            self._native_layers = layers

        def status(text):
            wx.CallAfter(self._show_progress, text, None)
            self._log_file(f"[router] {text}  ({time.time() - started:.0f}s)")

        self._path_items = {}
        self._run_added = []                   # items this run created (for the overlap merge at the end)
        self._run_removed = set()              # UUIDs of items ripped up again
        self._native_layers = None

        def work():
            try:
                job = native_pipeline.run_job(self.board, native, status=status, progress=progress,
                                              on_path=on_path, on_remove=on_remove,
                                              cancelled=lambda: self._cancel, escalate=True, apply_paths=False,
                                              extract_fn=extract_on_ui, on_layers=set_layers)
            except Exception as e:
                logger.exception("Routing failed")
                self._log_file(f"[router] failed after {time.time() - started:.0f}s: {e!r}")
                wx.CallAfter(self._native_done, None, e)
                return
            self._log_file(f"[router] finished after {time.time() - started:.0f}s")
            wx.CallAfter(self._native_done, job, None)

        threading.Thread(target=work, name="autorouter-native", daemon=True).start()

    def _on_main(self, fn):
        """Run fn on the UI thread (pcbnew calls) and wait for its result."""
        done = threading.Event()
        box = {}

        def call():
            try:
                box["value"] = fn()
            except Exception as e:
                logger.exception("Main-thread call failed")
                box["error"] = e
                self._append_log(f"[plugin] error: {e!r}")
            done.set()

        wx.CallAfter(call)
        done.wait()
        return box.get("value")

    def _remove_native_path(self, pid):
        """Rip-up: take a path's tracks and vias off the board again."""
        for item in self._path_items.pop(pid, []):
            try:
                self._run_removed.add(str(item.m_Uuid.AsString()))
                self.board.Remove(item)
                item.thisown = 0                   # KiCad's undo list owns it now
                session._REMOVED.append(item)
            except Exception as e:
                logger.warning("Could not remove a ripped-up item: %s", e)

    def _add_native_path(self, path):
        try:
            self._path_items[path.pid] = native_apply.add_path(self.board, self._native_layers, path)
            self._run_added += self._path_items[path.pid]
            self.applied = True
        except Exception as e:
            logger.exception("Adding a routed path failed")
            self._append_log(f"[plugin] could not add a path: {e!r}")
        now = time.time()
        if now - self._last_refresh > 0.5:
            self._last_refresh = now
            pcbnew.Refresh()

    def _native_done(self, result, error):
        """result is a pipeline.JobResult."""
        self._set_busy(False)
        if error is not None:
            pcbnew.Refresh()
            self._show_progress(f"Routing failed: {error}", 0.0)
            self._append_log(f"[plugin] routing failed: {error!r}")
            return
        if result is not None:
            # shared copper (a stub two routes leave by) is written once, now
            # that nothing will be ripped up any more
            try:
                live = [t for t in self._run_added if str(t.m_Uuid.AsString()) not in self._run_removed]
                n = native_apply.trim_overlaps(self.board, live)
                if n:
                    self._append_log(f"[plugin] {n} overlapping segments merged")
            except Exception as e:
                logger.exception("Trimming overlaps failed")
                self._append_log(f"[plugin] could not merge overlapping segments: {e!r}")
            # length matching on the finished copper (meanders)
            if not result.cancelled and getattr(self, "_native_opts", None) is not None:
                try:
                    self._show_progress("Length matching...", None)
                    self.si_page.pair_exceptions = list(result.pair_exceptions)
                    for e in result.pair_exceptions:
                        where = f" ({e['where']})" if e.get("where") else ""
                        self._append_log(f"[plugin] pair {e['nets'][0]} / {e['nets'][1]}{where} connected directly: "
                                         f"{e['text']}")
                    report = native_pipeline.tune_lengths(self.board, self._native_opts,
                                                          pair_exceptions=result.pair_exceptions)
                    if report is not None:
                        result.tuning, result.tuning_lines = report.groups, report.lines()
                        for line in report.lines():
                            self._append_log("[plugin] length matching: " + line)
                        self._kicad_update_wanted = bool(report.kicad_patterns)
                except Exception as e:
                    logger.exception("Length matching failed")
                    self._append_log(f"[plugin] length matching failed: {e!r}")
            # a pass that broke (pad vias, the finer pass, the pours) is
            # recorded, not fatal: say so instead of quietly leaving its work undone
            for key in ("fanout_error", "escalation_error", "pour_error"):
                if result.stats.get(key):
                    self._append_log(f"[plugin] {key.replace('_', ' ')}: {result.stats[key]}")
        self._refill_if_wanted()
        pcbnew.Refresh()
        if result is None or not result.layers:
            self._show_progress("Could not read the board (see the log).", 0.0)
            return
        if result.cancelled:
            text = f"Cancelled after {result.routed} connections; they stay on the board (Ctrl+Z removes them)."
            self._show_progress(text, 0.0)
            self._append_log("[plugin] " + text)
            return
        if not result.routed and not result.fanout and not result.stitched and not result.failed:
            self._show_progress("Nothing to route in this scope: every connection is done or skipped.", 1.0)
            return
        failed = list(result.failed)
        text = f"Done: {result.routed} connections routed in {result.seconds:.0f}s"
        if result.timed_out:
            text = f"Time limit reached: {result.routed} connections routed in {result.seconds:.0f}s"
        if result.escalated:
            text += f" ({result.escalated} on the finer second pass)"
        if result.fanout:
            text += f", {result.fanout} fanout vias"
        if result.pairs:
            text += f", {result.pairs} coupled pairs"
            if getattr(result, "pair_swaps", 0):
                text += f" ({result.pair_swaps} with a via swap)"
        if result.tuning:
            text += f", {sum(1 for g in result.tuning if g['ok'])}/{len(result.tuning)} length groups matched"
        if result.stitched:
            text += f", {result.stitched} stitching vias"
        if result.ripped:
            text += f" ({result.ripped} rerouted)"
        if result.shoved:
            text += f", {result.shoved} existing pieces moved"
        if result.failed:
            text += f", {len(result.failed)} not routed ({', '.join(failed[:6])}{'...' if len(failed) > 6 else ''})"
            self._append_log("[plugin] unrouted: " + ", ".join(failed))
        text += ".  Undo (Ctrl+Z) reverts it."
        self._show_progress(text, 1.0)
        self._append_log("[plugin] " + text)

    def _refill_if_wanted(self):
        if not self.refill.GetValue():
            return
        try:
            self._show_progress("Refilling copper zones...", None)
            wx.SafeYield(self, True)
            n = session.refill_zones(self.board)
            if n:
                self._append_log(f"[plugin] refilled {n} copper zones")
        except Exception as e:
            logger.exception("Zone refill failed")
            self._append_log(f"[plugin] zone refill failed: {e!r}")

    # ---- common -------------------------------------------------------------

    def _on_cancel(self, event):
        self._cancel = True
        self._show_progress("Cancelling after the current connection...", None)

    def _on_close(self, event):
        if self.busy:
            if wx.MessageBox("Routing is still running. Stop it and close?", "Autorouter",
                             wx.YES_NO | wx.ICON_QUESTION, self) != wx.YES:
                return
            self._cancel = True
        self._save_settings()
        self.si_page.save()
        if getattr(self, "_kicad_update_wanted", False):
            # KiCad's length tuner rebuilds the meanders as soon as the editor has the board back
            self._kicad_update_wanted = False
            if native_kicad_tuning.request_update():
                logger.info("Asked KiCad to update all tuning patterns")
        self.EndModal(wx.ID_OK if self.applied else wx.ID_CANCEL)

    def _set_busy(self, busy: bool):
        self.busy = busy
        self.route_btn.Enable(not busy)
        for b in (getattr(self, "ai_ask", None), getattr(self, "ai_review", None), getattr(self, "ai_score", None)):
            if b is not None:
                b.Enable(not busy)
        self.cancel_btn.Enable(busy)
        self.notebook.Enable(not busy)
        if not busy:
            self.gauge.SetValue(self.gauge.GetValue())

    def _status_now(self, text: str):
        """Status updates from main-thread work (board reading): show them immediately."""
        self._show_progress(text, None)
        self._append_log("[plugin] " + text)
        wx.SafeYield(self, True)

    def _show_progress(self, text: str, fraction: Optional[float]):
        self.status.SetLabel(text)
        if fraction is None:
            self.gauge.Pulse()
        else:
            self.gauge.SetValue(int(max(0.0, min(1.0, fraction)) * 1000))

    def _append_log(self, line: str):
        try:
            self.log.AppendText(line + "\n")
        except RuntimeError:
            pass                                   # the dialog is gone; the file still gets it
        self._log_file(line)

    def _log_file(self, line: str):
        """The job's dialog.log, with a clock: callable from any thread."""
        job = getattr(self, "job", None)
        if job is not None:
            try:
                with open(job.directory / "dialog.log", "a", encoding="utf-8") as f:
                    f.write(time.strftime("%H:%M:%S ") + line + "\n")
            except OSError:
                pass

    def _open_folder(self, path: Path):
        path.mkdir(parents=True, exist_ok=True)
        wx.LaunchDefaultBrowser(path.as_uri())
