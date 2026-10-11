"""The Autorouter dialog: engine choice, options, live progress.

Two engines share the dialog. The built-in router reads the board through the
PCB API, searches on a worker thread and puts each connection's copper on the
board as soon as it is found. Freerouting runs as an external process on a
DSN export and its session is imported at the end.
"""
import threading
import time
from pathlib import Path
from typing import List, Optional

import pcbnew
import wx

from . import __version__
from .engine import freerouting, java, session
from .engine.freerouting import (ENGINE_BUILTIN, ENGINE_FREEROUTING, RELEASE, RouteOptions,
                                 SELECTION_STRATEGIES, UPDATE_STRATEGIES)
from .engine.native import apply as native_apply, extract as native_extract, router as native_router
from .engine.ai import assistant as ai_assistant, codex as ai_codex, headless as ai_headless
from .engine.native.problem import (NativeOptions, SCOPE_BOARD, SCOPE_SELECTION, SCOPE_SHEETS,
                                    SIZE_AUTO, SIZE_CURRENT, SIZE_FIXED, SIZE_NETCLASS)
from .engine.paths import data_dir
from .engine.progress import ProgressParser
from .engine.runner import EngineRun
from .log import get_logger
from .settings import Settings

logger = get_logger("dialog")
ENGINES = ((ENGINE_BUILTIN, "Built-in router (KiCad PCB API, routes live on the board)"),
           (ENGINE_FREEROUTING, f"Freerouting {RELEASE.version} (external Java engine, DSN/SES)"))


def _mb(n: Optional[int]) -> str:
    return f"{n / 1048576:.0f} MB" if n else "?"


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
        self.run: Optional[EngineRun] = None
        self.parser: Optional[ProgressParser] = None
        self.job: Optional[session.JobFiles] = None
        self.engine_log = None
        self.applied = False
        self.busy = False
        self._cancel = False
        self._java: Optional[java.JavaInfo] = None
        self._jar: Optional[Path] = None
        self._native_layers = None
        self._last_refresh = 0.0

        self._build()
        self._load_options(self.settings.options)
        self._refresh_engine_status()
        self._on_engine_choice(None)
        self.SetMinSize((660, 680))
        self.Fit()
        self.CentreOnParent()
        self.Bind(wx.EVT_CLOSE, self._on_close)

    # ---- layout -------------------------------------------------------------

    def _build(self):
        root = wx.BoxSizer(wx.VERTICAL)
        self.notebook = wx.Notebook(self)
        self.notebook.AddPage(self._routing_page(self.notebook), "Routing")
        self.notebook.AddPage(self._layers_page(self.notebook), "Layers && nets")
        self.notebook.AddPage(self._freerouting_page(self.notebook), "Freerouting")
        self.notebook.AddPage(self._ai_page(self.notebook), "AI assistant")
        root.Add(self.notebook, 0, wx.EXPAND | wx.ALL, 8)

        progress = wx.StaticBoxSizer(wx.VERTICAL, self, "Progress")
        self.status = wx.StaticText(self, label="Ready.")
        self.gauge = wx.Gauge(self, range=1000)
        self.log = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL)
        self.log.SetFont(wx.Font(wx.FontInfo(10).Family(wx.FONTFAMILY_TELETYPE)))
        self.log.SetMinSize((-1, 150))
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
        page = wx.Panel(parent)
        box = wx.BoxSizer(wx.VERTICAL)

        self.engine_choice = wx.RadioBox(page, label="Engine", choices=[label for _, label in ENGINES],
                                         majorDimension=1, style=wx.RA_SPECIFY_COLS)
        self.engine_choice.Bind(wx.EVT_RADIOBOX, self._on_engine_choice)
        box.Add(self.engine_choice, 0, wx.EXPAND | wx.ALL, 10)

        native = wx.StaticBoxSizer(wx.VERTICAL, page, "Built-in router")
        grid = wx.FlexGridSizer(cols=2, vgap=6, hgap=10)
        grid.AddGrowableCol(1)

        def row(label, ctrl, tip=None):
            text = wx.StaticText(page, label=label)
            if tip:
                text.SetToolTip(tip)
                ctrl.SetToolTip(tip)
            grid.Add(text, 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(ctrl, 0, wx.EXPAND)
            return ctrl

        self.pitch = row("Routing grid (mm)", wx.SpinCtrlDouble(page, min=0.025, max=2.0, initial=0.1, inc=0.025),
                         "Track centrelines snap to this grid. Smaller is more precise and slower; "
                         "about half the smallest pad pitch works well.")
        self.pitch.SetDigits(3)
        self.native_via_cost = row("Via cost (grid cells)", wx.SpinCtrl(page, min=0, max=100000, initial=25),
                                   "A via costs as much as this many cells of straight track.")
        self.native_budget = row("Search budget per connection (k cells)",
                                 wx.SpinCtrl(page, min=10, max=100000, initial=400),
                                 "A* gives up on a connection after expanding this many cells.")
        self.skip_nets = row("Nets to leave unrouted", wx.TextCtrl(page),
                             "Comma-separated net names or patterns, e.g. GND,+3V3,*_PLANE. "
                             "Use it for nets that get a copper pour.")
        # Track width and via size: KiCad's own settings, chosen the way the
        # editor's toolbar offers them.
        mm = lambda nm: f"{nm / 1e6:g} mm"
        cur_w = self.sizes["current_track_width"]
        cur_v = self.sizes["current_via"]
        self.track_choices = [(SIZE_AUTO, 0, "Automatic: net class, existing tracks, pad width for power nets"),
                              (SIZE_NETCLASS, 0, "Net class width (per net)"),
                              (SIZE_CURRENT, 0, f"Editor's current track width ({mm(cur_w)})")]
        self.track_choices += [(SIZE_FIXED, w, f"Pre-defined {mm(w)}") for w in self.sizes["track_widths"]]
        self.track_size = row("Track width", wx.Choice(page, choices=[c[2] for c in self.track_choices]),
                              "Net class sizes are KiCad's default. The other entries are the board's "
                              "pre-defined sizes (Board Setup) and the editor's current selection.")
        self.via_choices = [(SIZE_NETCLASS, (0, 0), "Net class via (per net)"),
                            (SIZE_CURRENT, cur_v, f"Editor's current via ({mm(cur_v[0])} / {mm(cur_v[1])})")]
        self.via_choices += [(SIZE_FIXED, v, f"Pre-defined {mm(v[0])} / {mm(v[1])}") for v in self.sizes["via_sizes"]]
        self.via_size = row("Via size (diameter / drill)", wx.Choice(page, choices=[c[2] for c in self.via_choices]))
        native.Add(grid, 0, wx.EXPAND | wx.ALL, 6)
        self.native_vias = wx.CheckBox(page, label="Allow vias")
        self.native_retry = wx.CheckBox(page, label="Retry failed connections with a bigger budget and cheaper vias")
        self.native_fanout = wx.CheckBox(page, label="Fan out GND/power pads to their copper planes with vias")
        self.native_fanout.SetToolTip("Nets with a copper zone (pours) get a short stub and a via at each SMD pad "
                                      "on a layer the pour does not reach, so the refilled pour connects them. "
                                      "Works for nets listed under 'Nets to leave unrouted' too.")
        self.native_pairs = wx.CheckBox(page, label="Route differential pairs coupled (net class pair width and gap)")
        self.native_pairs.SetToolTip("Nets named like D+/D-, TX_P/TX_N or USB_DP/USB_DN are routed side by side "
                                     "at the pair width and gap of their net class, which is where KiCad keeps the "
                                     "impedance choice (Board Setup > Net Classes).")
        native.Add(self.native_vias, 0, wx.LEFT | wx.BOTTOM, 6)
        native.Add(self.native_retry, 0, wx.LEFT | wx.BOTTOM, 6)
        native.Add(self.native_fanout, 0, wx.LEFT | wx.BOTTOM, 6)
        native.Add(self.native_pairs, 0, wx.LEFT | wx.BOTTOM, 6)
        stitch_row = wx.BoxSizer(wx.HORIZONTAL)
        self.native_stitch = wx.CheckBox(page, label="Stitch GND pours with vias every")
        self.native_stitch.SetToolTip("Pour nets left unrouted that have zones on two or more layers get a grid "
                                      "of vias through their zones, wherever a via still fits, to shorten return "
                                      "current loops. Placed after all routing.")
        self.stitch_pitch = wx.SpinCtrlDouble(page, min=1.0, max=50.0, initial=4.0, inc=0.5)
        self.stitch_pitch.SetDigits(1)
        stitch_row.Add(self.native_stitch, 0, wx.ALIGN_CENTER_VERTICAL)
        stitch_row.Add(self.stitch_pitch, 0, wx.LEFT | wx.RIGHT, 6)
        stitch_row.Add(wx.StaticText(page, label="mm"), 0, wx.ALIGN_CENTER_VERTICAL)
        native.Add(stitch_row, 0, wx.LEFT | wx.BOTTOM, 6)
        self.native_power = wx.CheckBox(page, label="Power pours: tie pads to the plane with vias, stitch at twice the pitch")
        self.native_power.SetToolTip("Routed power nets that also have a copper pour (e.g. +3V3 on an inner layer) "
                                     "get a via at each pad on a layer the pour does not cover, and a coarser via "
                                     "grid through the pour when it spans two or more layers.")
        native.Add(self.native_power, 0, wx.LEFT | wx.BOTTOM, 6)
        box.Add(native, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        scope = wx.StaticBoxSizer(wx.HORIZONTAL, page, "What to route (built-in router)")
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
        self.sheet_list.SetMinSize((-1, 90))
        self.sheet_list.Disable()
        scope.Add(self.scope_choice, 0, wx.ALL, 6)
        scope.Add(self.sheet_list, 1, wx.EXPAND | wx.ALL, 6)
        box.Add(scope, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self.remove_existing = wx.CheckBox(page, label="Remove existing tracks and vias first")
        self.remove_existing.SetToolTip("Otherwise existing routing is kept and only the "
                                        "unrouted connections are routed.")
        box.Add(self.remove_existing, 0, wx.LEFT | wx.BOTTOM, 6)
        self.refill = wx.CheckBox(page, label="Refill copper zones when done")
        self.refill.SetToolTip("Pours are refilled around the new tracks and vias, as Edit > Fill All "
                               "Zones would; otherwise the DRC reports the stale fills.")
        self.refill.SetValue(True)
        box.Add(self.refill, 0, wx.LEFT | wx.BOTTOM, 10)
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

    def _freerouting_page(self, parent):
        page = wx.Panel(parent)
        box = wx.BoxSizer(wx.VERTICAL)
        grid = wx.FlexGridSizer(cols=2, vgap=5, hgap=10)
        grid.AddGrowableCol(1)

        def row(label, ctrl, tip=None):
            text = wx.StaticText(page, label=label)
            if tip:
                text.SetToolTip(tip)
                ctrl.SetToolTip(tip)
            grid.Add(text, 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(ctrl, 0, wx.EXPAND)
            return ctrl

        self.passes = row("Max routing passes", wx.SpinCtrl(page, min=0, max=100000, initial=100),
                          "Upper limit for autorouter passes. 0 = no limit.")
        self.threads = row("Optimizer threads", wx.SpinCtrl(page, min=-1, max=256, initial=0),
                           "0 = engine default (CPU cores - 1), -1 = skip the optimizer stage.")
        self.threshold = row("Optimizer stop threshold (%)",
                             wx.SpinCtrlDouble(page, min=0.0, max=100.0, initial=2.5, inc=0.5))
        self.threshold.SetDigits(1)
        self.via_costs = row("Via cost", wx.SpinCtrl(page, min=0, max=100000, initial=50))
        self.plane_via_costs = row("Plane via cost", wx.SpinCtrl(page, min=0, max=100000, initial=5))
        self.ripup_costs = row("Start rip-up cost", wx.SpinCtrl(page, min=0, max=100000, initial=100))
        self.update_strategy = row("Optimizer update strategy", wx.Choice(page, choices=list(UPDATE_STRATEGIES)))
        self.hybrid_ratio = row("Hybrid ratio (global:prioritized)", wx.TextCtrl(page, value="1:1"))
        self.selection_strategy = row("Optimizer item selection", wx.Choice(page, choices=list(SELECTION_STRATEGIES)))
        self.heap = row("JVM max heap (MB, 0 = default)", wx.SpinCtrl(page, min=0, max=262144, initial=0))
        self.extra = row("Extra engine arguments", wx.TextCtrl(page),
                         "Passed to Freerouting verbatim, e.g. --router.job_timeout=00:30:00")
        self.fanout = wx.CheckBox(page, label="Fan out SMD pads before routing")
        box.Add(grid, 0, wx.EXPAND | wx.ALL, 10)
        box.Add(self.fanout, 0, wx.LEFT | wx.BOTTOM, 10)

        setup = wx.StaticBoxSizer(wx.VERTICAL, page, "Engine setup")
        sgrid = wx.FlexGridSizer(cols=3, vgap=5, hgap=8)
        sgrid.AddGrowableCol(1)
        sgrid.Add(wx.StaticText(page, label="Java runtime"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.java_path = wx.TextCtrl(page, value=self.settings.java_path)
        self.java_path.SetHint("auto-detect")
        sgrid.Add(self.java_path, 1, wx.EXPAND)
        b = wx.Button(page, label="Browse...")
        b.Bind(wx.EVT_BUTTON, lambda e: self._browse(self.java_path, "java executable"))
        sgrid.Add(b)
        sgrid.Add(wx.StaticText(page, label="Freerouting jar"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.jar_path = wx.TextCtrl(page, value=self.settings.jar_path)
        self.jar_path.SetHint(f"managed download of {RELEASE.jar_name}")
        sgrid.Add(self.jar_path, 1, wx.EXPAND)
        b = wx.Button(page, label="Browse...")
        b.Bind(wx.EVT_BUTTON, lambda e: self._browse(self.jar_path, "Freerouting jar", "*.jar"))
        sgrid.Add(b)
        setup.Add(sgrid, 0, wx.EXPAND | wx.ALL, 6)
        self.engine_status = wx.StaticText(page, label="")
        setup.Add(self.engine_status, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)
        srow = wx.BoxSizer(wx.HORIZONTAL)
        self.install_btn = wx.Button(page, label="Download missing components")
        self.install_btn.Bind(wx.EVT_BUTTON, lambda e: self._ensure_engine(then=None))
        recheck = wx.Button(page, label="Re-check")
        recheck.Bind(wx.EVT_BUTTON, lambda e: self._refresh_engine_status())
        srow.Add(self.install_btn, 0, wx.RIGHT, 6)
        srow.Add(recheck, 0)
        setup.Add(srow, 0, wx.ALL, 6)
        box.Add(setup, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
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
        row2.Add(self.ai_ask, 0, wx.RIGHT, 6)
        row2.Add(self.ai_review, 0, wx.RIGHT, 12)
        binary = ai_codex.find_codex(self.settings.codex_path)
        self.ai_status = wx.StaticText(page, label=(f"codex: {binary}  ({ai_codex.codex_version(binary)})" if binary
                                                    else "codex CLI not found: install with  npm i -g @openai/codex"))
        row2.Add(self.ai_status, 0, wx.ALIGN_CENTER_VERTICAL)
        box.Add(row2, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        page.SetSizer(box)
        return page

    # ---- the assistant ------------------------------------------------------

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
        binary = ai_codex.find_codex(self.settings.codex_path)
        if not binary:
            wx.MessageBox("The codex CLI was not found. Install it with  npm i -g @openai/codex  and log in once.",
                          "Autorouter", wx.ICON_ERROR, self)
            return
        self._save_settings()
        options = self._collect_options()
        self.log.Clear()
        self._cancel = False
        self.applied = False
        self._set_busy(True)
        name = Path(self.board.GetFileName()).stem if self.board.GetFileName() else "board"
        self.job = session.new_job(name + "-ai")
        base = self.native_options(options)
        selection = {"refs": list(self.sel_refs), "nets": list(self.sel_nets)}
        self._path_items = {}
        self._native_layers = None

        def on_path(path):
            if self._native_layers is not None:
                self._add_native_path(path)

        def route_fn(spec):
            # the router needs the layers for live display; read them per run
            o = ai_headless.apply_scope(ai_headless.native_options_from(spec.get("options", {}), base), spec, selection)
            if spec.get("options", {}).get("remove_existing"):
                session.remove_tracks(self.board)
            try:
                problem = native_extract.extract(self.board, o, status=self._status_now)
            except Exception as e:
                return {"error": f"could not read the board: {e}"}
            self._native_layers = problem.layers
            started = time.time()

            def progress(done, total, routed, failed, net):
                self._show_progress(f"[assistant] routing {done}/{total}: {routed} routed, {failed} failed "
                                    f"({time.time() - started:.0f}s) {net}", done / total if total else None)
                wx.SafeYield(self, True)

            result = native_router.run(problem, progress=progress, on_path=on_path,
                                       cancelled=lambda: self._cancel,
                                       on_remove=lambda pid: self._remove_native_path(pid))
            self._refill_if_wanted()
            pcbnew.Refresh()
            failed = sorted({problem.nets[c.net].name for c in result.failed})
            outcome = {"error": None, "scope": spec.get("scope", "board"),
                       "connections_in_scope": problem.stats["connections"], "routed": result.routed,
                       "failed": failed, "ripped_up": result.ripped, "fanout_vias": result.fanout,
                       "coupled_pairs": result.pairs, "stitching_vias": result.stitched,
                       "unconnected_after": self.board.GetConnectivity().GetUnconnectedCount(True)}
            outcome["summary"] = (f"{result.routed} routed, {len(failed)} failed, {result.fanout} fanout vias, "
                                  f"{result.stitched} stitching vias; {outcome['unconnected_after']} unconnected left")
            return outcome

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
        self.engine_choice.SetSelection(1 if o.engine == ENGINE_FREEROUTING else 0)
        self.pitch.SetValue(o.native_pitch_mm)
        self.native_via_cost.SetValue(o.native_via_cost)
        self.native_budget.SetValue(o.native_max_expansions_k)
        self.skip_nets.SetValue(o.skip_nets)
        self.native_vias.SetValue(o.native_allow_vias)
        self.native_retry.SetValue(o.native_retry)
        self.native_fanout.SetValue(o.native_fanout_vias)
        self.native_pairs.SetValue(o.native_diff_pairs)
        self.native_stitch.SetValue(o.native_stitch_vias)
        self.stitch_pitch.SetValue(o.native_stitch_pitch_mm)
        self.native_power.SetValue(o.native_power_pours)
        self.track_size.SetSelection(0)
        for i, (mode, value, _) in enumerate(self.track_choices):
            if mode == o.native_track_mode and (mode != SIZE_FIXED or value == o.native_track_width):
                self.track_size.SetSelection(i)
        self.via_size.SetSelection(0)
        for i, (mode, value, _) in enumerate(self.via_choices):
            if mode == o.native_via_mode and (mode != SIZE_FIXED or value == (o.native_via_diameter, o.native_via_drill)):
                self.via_size.SetSelection(i)
        self.passes.SetValue(o.max_passes)
        self.threads.SetValue(o.optimizer_threads)
        self.threshold.SetValue(o.improvement_threshold)
        self.via_costs.SetValue(o.via_costs)
        self.plane_via_costs.SetValue(o.plane_via_costs)
        self.ripup_costs.SetValue(o.start_ripup_costs)
        self.update_strategy.SetSelection(
            UPDATE_STRATEGIES.index(o.update_strategy) if o.update_strategy in UPDATE_STRATEGIES else 0)
        self.hybrid_ratio.SetValue(o.hybrid_ratio or "1:1")
        self.selection_strategy.SetSelection(
            SELECTION_STRATEGIES.index(o.selection_strategy) if o.selection_strategy in SELECTION_STRATEGIES else 0)
        self.heap.SetValue(o.max_heap_mb)
        self.extra.SetValue(o.extra_args)
        self.fanout.SetValue(o.fanout)
        self.remove_existing.SetValue(o.remove_existing_tracks)
        self.refill.SetValue(o.refill_zones)
        for i, name in enumerate(self.net_classes):
            self.skip_classes.Check(i, name in o.ignore_net_classes)
        if o.routable_layers and len(o.routable_layers) == len(self.layers):
            for i, v in enumerate(o.routable_layers):
                self.routable.Check(i, bool(v))
        if o.horizontal_layers and len(o.horizontal_layers) == len(self.layers):
            self.override_dirs.SetValue(True)
            self.horizontal.Enable()
            for i, v in enumerate(o.horizontal_layers):
                self.horizontal.Check(i, bool(v))

    def _collect_options(self) -> RouteOptions:
        n = len(self.layers)
        return RouteOptions(
            engine=ENGINES[self.engine_choice.GetSelection()][0],
            native_pitch_mm=self.pitch.GetValue(),
            native_via_cost=self.native_via_cost.GetValue(),
            native_allow_vias=self.native_vias.GetValue(),
            native_retry=self.native_retry.GetValue(),
            native_fanout_vias=self.native_fanout.GetValue(),
            native_diff_pairs=self.native_pairs.GetValue(),
            native_stitch_vias=self.native_stitch.GetValue(),
            native_stitch_pitch_mm=self.stitch_pitch.GetValue(),
            native_power_pours=self.native_power.GetValue(),
            native_max_expansions_k=self.native_budget.GetValue(),
            skip_nets=self.skip_nets.GetValue().strip(),
            native_track_mode=self.track_choices[self.track_size.GetSelection()][0],
            native_track_width=self.track_choices[self.track_size.GetSelection()][1],
            native_via_mode=self.via_choices[self.via_size.GetSelection()][0],
            native_via_diameter=self.via_choices[self.via_size.GetSelection()][1][0],
            native_via_drill=self.via_choices[self.via_size.GetSelection()][1][1],
            max_passes=self.passes.GetValue(),
            optimizer_threads=self.threads.GetValue(),
            improvement_threshold=self.threshold.GetValue(),
            via_costs=self.via_costs.GetValue(),
            plane_via_costs=self.plane_via_costs.GetValue(),
            start_ripup_costs=self.ripup_costs.GetValue(),
            fanout=self.fanout.GetValue(),
            update_strategy=UPDATE_STRATEGIES[self.update_strategy.GetSelection()],
            selection_strategy=SELECTION_STRATEGIES[self.selection_strategy.GetSelection()],
            hybrid_ratio=self.hybrid_ratio.GetValue().strip() or "1:1",
            ignore_net_classes=[self.net_classes[i] for i in self.skip_classes.GetCheckedItems()],
            routable_layers=[self.routable.IsChecked(i) for i in range(n)],
            horizontal_layers=[self.horizontal.IsChecked(i) for i in range(n)]
            if self.override_dirs.GetValue() else None,
            remove_existing_tracks=self.remove_existing.GetValue(),
            refill_zones=self.refill.GetValue(),
            max_heap_mb=self.heap.GetValue(),
            extra_args=self.extra.GetValue().strip(),
        )

    def native_options(self, o: RouteOptions) -> NativeOptions:
        scope = (SCOPE_BOARD, SCOPE_SELECTION, SCOPE_SHEETS)[self.scope_choice.GetSelection()]
        return NativeOptions(
            pitch_mm=o.native_pitch_mm, via_cost=o.native_via_cost, allow_vias=o.native_allow_vias,
            max_expansions=o.native_max_expansions_k * 1000, retry_failed=o.native_retry,
            fanout_vias=o.native_fanout_vias, diff_pairs=o.native_diff_pairs,
            stitch_vias=o.native_stitch_vias, stitch_pitch_mm=o.native_stitch_pitch_mm,
            power_pours=o.native_power_pours,
            skip_nets=[s.strip() for s in o.skip_nets.split(",") if s.strip()],
            ignore_net_classes=list(o.ignore_net_classes),
            routable_layers=o.routable_layers, horizontal_layers=o.horizontal_layers,
            scope=scope, scope_refs=list(self.sel_refs), scope_nets=list(self.sel_nets),
            scope_sheets=[self.sheets[i][0] for i in self.sheet_list.GetCheckedItems()],
            track_mode=o.native_track_mode, track_width=o.native_track_width,
            via_mode=o.native_via_mode, via_diameter=o.native_via_diameter, via_drill=o.native_via_drill)

    def _save_settings(self):
        self.settings.java_path = self.java_path.GetValue().strip()
        self.settings.jar_path = self.jar_path.GetValue().strip()
        if hasattr(self, "ai_model"):
            self.settings.ai_model = self.ai_model.GetValue().strip()
        self.settings.options = self._collect_options()
        try:
            self.settings.save()
        except OSError as e:
            logger.warning("Could not save settings: %s", e)

    def _on_engine_choice(self, event):
        builtin = self.engine_choice.GetSelection() == 0
        for ctrl in (self.pitch, self.native_via_cost, self.native_budget, self.skip_nets,
                     self.native_vias, self.native_retry, self.native_fanout, self.native_pairs,
                     self.native_stitch, self.stitch_pitch, self.native_power, self.track_size, self.via_size,
                     self.scope_choice):
            ctrl.Enable(builtin)
        self.sheet_list.Enable(builtin and self.scope_choice.GetSelection() == 2)

    # ---- Freerouting availability -------------------------------------------

    def _refresh_engine_status(self):
        self._java = java.find_java(RELEASE.min_java, self.java_path.GetValue().strip() or None)
        self._jar = freerouting.find_jar(self.jar_path.GetValue().strip() or None)
        parts = [f"Java: {self._java}" if self._java else f"Java: none found (need {RELEASE.min_java}+)",
                 f"Engine: {self._jar}" if self._jar else f"Engine: {RELEASE.jar_name} not downloaded yet"]
        self.engine_status.SetLabel("\n".join(parts))
        self.install_btn.Enable(not (self._java and self._jar))
        self.Layout()

    def _ensure_engine(self, then):
        self._refresh_engine_status()
        if self._java and self._jar:
            if then:
                then()
            return
        missing = []
        if not self._jar:
            missing.append(f"Freerouting {RELEASE.version} (about 62 MB)")
        if not self._java:
            missing.append(f"a Java {RELEASE.min_java} runtime from adoptium.net (about 55 MB)")
        answer = wx.MessageBox(
            "The Freerouting engine needs to be downloaded first:\n  - " + "\n  - ".join(missing)
            + f"\n\ninto {data_dir()}\n\nDownload now?", "Autorouter", wx.YES_NO | wx.ICON_QUESTION, self)
        if answer != wx.YES:
            return
        self._cancel = False
        self._set_busy(True)
        self._append_log("[plugin] downloading engine components...")

        def progress(label, done, total):
            wx.CallAfter(self._show_progress, f"{label}  {_mb(done)} / {_mb(total)}",
                         (done / total) if total else None)

        def work():
            error = None
            try:
                if not self._jar:
                    freerouting.install_jar(progress, lambda: self._cancel)
                if not self._java:
                    java.install_jre(RELEASE.min_java, progress, lambda: self._cancel)
            except Exception as e:
                error = e
            wx.CallAfter(finish, error)

        def finish(error):
            self._set_busy(False)
            self._refresh_engine_status()
            if error is not None:
                logger.exception("Engine download failed", exc_info=error)
                self._show_progress(f"Download failed: {error}", 0.0)
                return
            self._show_progress("Engine ready.", 0.0)
            if then and self._java and self._jar:
                then()

        threading.Thread(target=work, name="autorouter-download", daemon=True).start()

    # ---- starting a run -----------------------------------------------------

    def _on_route(self, event):
        self._save_settings()
        options = self._collect_options()
        if options.routable_layers and not any(options.routable_layers):
            wx.MessageBox("Enable at least one routable layer.", "Autorouter", wx.ICON_WARNING, self)
            return
        if options.engine == ENGINE_FREEROUTING:
            self._ensure_engine(then=self._start_freerouting)
        else:
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
        if native.scope == SCOPE_SHEETS and not native.scope_sheets:
            wx.MessageBox("Tick at least one schematic sheet, or route the whole board.",
                          "Autorouter", wx.ICON_WARNING, self)
            return
        self._set_busy(True)
        self._show_progress("Reading the board...", None)
        wx.SafeYield(self, True)
        try:
            problem = native_extract.extract(self.board, native, status=self._status_now)
        except Exception as e:
            self._set_busy(False)
            logger.exception("Reading the board failed")
            self._show_progress(f"Could not read the board: {e}", 0.0)
            return
        self._native_layers = problem.layers
        stats = problem.stats
        self._append_log(f"[plugin] grid {problem.grid.w}x{problem.grid.h}x{problem.grid.layers} cells at "
                         f"{native.pitch_mm:g} mm, {stats['nets']} nets, {stats['connections']} of "
                         f"{stats['all_connections']} connections in scope, {stats.get('fanout_vias', 0)} fanout vias, "
                         f"read in {stats['extract_seconds']:.1f}s")
        if not problem.connections and not problem.fanout and not problem.stitch:
            self._set_busy(False)
            self._show_progress("Nothing to route in this scope: every connection is done or skipped.", 1.0)
            return
        self._show_progress(f"Routing {stats['connections']} connections...", 0.0)
        started = time.time()

        def progress(done, total, routed, failed, name):
            wx.CallAfter(self._show_progress,
                         f"Routing {done}/{total}: {routed} routed, {failed} failed  ({time.time() - started:.0f}s)  {name}",
                         done / total if total else None)

        def on_path(path):
            wx.CallAfter(self._add_native_path, path)

        def on_remove(pid):
            wx.CallAfter(self._remove_native_path, pid)

        self._path_items = {}

        def work():
            try:
                result = native_router.run(problem, progress=progress, on_path=on_path,
                                           cancelled=lambda: self._cancel, on_remove=on_remove)
            except Exception as e:
                logger.exception("Routing failed")
                wx.CallAfter(self._native_done, None, e)
                return
            wx.CallAfter(self._native_done, result, None)

        threading.Thread(target=work, name="autorouter-native", daemon=True).start()

    def _remove_native_path(self, pid):
        """Rip-up: take a path's tracks and vias off the board again."""
        for item in self._path_items.pop(pid, []):
            try:
                self.board.Remove(item)
            except Exception as e:
                logger.warning("Could not remove a ripped-up item: %s", e)

    def _add_native_path(self, path):
        try:
            self._path_items[path.pid] = native_apply.add_path(self.board, self._native_layers, path)
            self.applied = True
        except Exception as e:
            logger.exception("Adding a routed path failed")
            self._append_log(f"[plugin] could not add a path: {e!r}")
        now = time.time()
        if now - self._last_refresh > 0.5:
            self._last_refresh = now
            pcbnew.Refresh()

    def _native_done(self, result, error):
        self._set_busy(False)
        if error is not None:
            pcbnew.Refresh()
            self._show_progress(f"Routing failed: {error}", 0.0)
            return
        self._refill_if_wanted()
        pcbnew.Refresh()
        if result.cancelled:
            text = f"Cancelled after {result.routed} connections; they stay on the board (Ctrl+Z removes them)."
            self._show_progress(text, 0.0)
            return
        failed = sorted({self.board.FindNet(c.net).GetNetname() for c in result.failed})
        text = f"Done: {result.routed} connections routed in {result.seconds:.0f}s"
        if getattr(result, "fanout", 0):
            text += f", {result.fanout} fanout vias"
        if getattr(result, "pairs", 0):
            text += f", {result.pairs} coupled pairs"
        if getattr(result, "stitched", 0):
            text += f", {result.stitched} stitching vias"
        for name, why in getattr(result, "pair_log", []):
            self._append_log(f"[plugin] pair {name} routed separately: {why}")
        if getattr(result, "ripped", 0):
            text += f" ({result.ripped} rerouted)"
        if result.failed:
            text += f", {len(result.failed)} not routed ({', '.join(failed[:6])}{'...' if len(failed) > 6 else ''})"
            self._append_log("[plugin] unrouted: " + ", ".join(failed))
        text += ".  Undo (Ctrl+Z) reverts it."
        self._show_progress(text, 1.0)

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

    # ---- Freerouting --------------------------------------------------------

    def _start_freerouting(self):
        options = self._collect_options()
        if not self._prepare(options):
            return
        try:
            session.export_dsn(self.board, self.job.dsn)
            self._append_log(f"[plugin] exported {self.job.dsn}")
            self.engine_log = open(self.job.directory / "engine.log", "w", encoding="utf-8")
        except Exception as e:
            logger.exception("Preparing the run failed")
            wx.MessageBox(str(e), "Autorouter", wx.ICON_ERROR, self)
            return
        command = freerouting.build_command(self._java.path, self._jar, options, self.job.dsn,
                                            self.job.ses, host_version=__version__,
                                            user_data=data_dir() / "freerouting")
        self._append_log("[plugin] " + " ".join(command))
        self.parser = ProgressParser()
        self._set_busy(True)
        self._show_progress("Starting the engine...", None)
        self.run = EngineRun(command, on_line=lambda line: wx.CallAfter(self._on_engine_line, line),
                             on_done=lambda rc: wx.CallAfter(self._on_engine_done, rc),
                             cwd=str(self.job.directory))
        try:
            self.run.start()
        except OSError as e:
            self._set_busy(False)
            wx.MessageBox(f"Could not start the engine:\n{e}", "Autorouter", wx.ICON_ERROR, self)

    def _on_engine_line(self, line):
        self._append_log(line)
        if self.engine_log:
            try:
                self.engine_log.write(line + "\n")
            except OSError:
                pass
        if self.parser:
            status = self.parser.feed(line)
            self._show_progress(status.summary(), status.fraction(self.passes.GetValue()))

    def _on_engine_done(self, returncode):
        self._set_busy(False)
        if self.engine_log:
            self.engine_log.close()
            self.engine_log = None
        status = self.parser.status if self.parser else None
        if returncode is None:
            self._show_progress("Cancelled. The board was not changed.", 0.0)
            return
        if status and status.finished_ok and self.job and self.job.ses.is_file():
            try:
                session.import_ses(self.board, self.job.ses)
                self.applied = True
                self._refill_if_wanted()
                pcbnew.Refresh()
            except Exception as e:
                logger.exception("Importing the session failed")
                self._show_progress(f"Routing finished but the import failed: {e}", 0.0)
                return
            text = status.summary().replace("Finished", "Done, routing applied to the board", 1)
            self._show_progress(text + ".  Undo (Ctrl+Z) reverts it.", 1.0)
            return
        reason = status.errors[-1] if status and status.errors else f"exit code {returncode}"
        if status and status.job_state and status.job_state != "COMPLETED":
            reason = f"engine state {status.job_state}"
        self._show_progress(f"The engine did not produce a session ({reason}). See the log.", 0.0)

    # ---- common -------------------------------------------------------------

    def _on_cancel(self, event):
        self._cancel = True
        if self.run and self.run.running:
            self._append_log("[plugin] cancelling...")
            threading.Thread(target=self.run.cancel, daemon=True).start()
        else:
            self._show_progress("Cancelling after the current connection...", None)

    def _on_close(self, event):
        if self.busy:
            if wx.MessageBox("Routing is still running. Stop it and close?", "Autorouter",
                             wx.YES_NO | wx.ICON_QUESTION, self) != wx.YES:
                return
            self._cancel = True
            if self.run and self.run.running:
                self.run.cancel()
        self._save_settings()
        self.EndModal(wx.ID_OK if self.applied else wx.ID_CANCEL)

    def _set_busy(self, busy: bool):
        self.busy = busy
        self.route_btn.Enable(not busy)
        for b in (getattr(self, "ai_ask", None), getattr(self, "ai_review", None)):
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
        self.log.AppendText(line + "\n")

    def _browse(self, ctrl, what, wildcard="*"):
        with wx.FileDialog(self, f"Choose the {what}", wildcard=f"{what}|{wildcard}|All files|*",
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                ctrl.SetValue(dlg.GetPath())
                self._refresh_engine_status()

    def _open_folder(self, path: Path):
        path.mkdir(parents=True, exist_ok=True)
        wx.LaunchDefaultBrowser(path.as_uri())
