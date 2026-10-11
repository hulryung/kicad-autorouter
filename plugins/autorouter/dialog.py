"""The Autorouter dialog: routing options, engine setup, live progress."""
import os
import threading
from pathlib import Path
from typing import Optional

import wx

from . import __version__
from .engine import freerouting, java, session
from .engine.freerouting import RELEASE, RouteOptions, SELECTION_STRATEGIES, UPDATE_STRATEGIES
from .engine.paths import data_dir
from .engine.progress import ProgressParser, STAGE_DONE
from .engine.runner import EngineRun
from .log import get_logger
from .settings import Settings

logger = get_logger("dialog")


def _mb(n: Optional[int]) -> str:
    return f"{n / 1048576:.0f} MB" if n else "?"


class AutorouterDialog(wx.Dialog):
    def __init__(self, parent, board):
        super().__init__(parent, title=f"Autorouter  (Freerouting {RELEASE.version})",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.board = board
        self.settings = Settings.load()
        self.layers = session.copper_layers(board)
        self.net_classes = session.net_class_names(board)
        self.run: Optional[EngineRun] = None
        self.parser: Optional[ProgressParser] = None
        self.job: Optional[session.JobFiles] = None
        self.applied = False
        self._cancel_download = False
        self._java: Optional[java.JavaInfo] = None
        self._jar: Optional[Path] = None

        self._build()
        self._load_options(self.settings.options)
        self._refresh_engine_status()
        self.SetMinSize((620, 640))
        self.Fit()
        self.CentreOnParent()
        self.Bind(wx.EVT_CLOSE, self._on_close)

    # ---- layout -------------------------------------------------------------

    def _build(self):
        root = wx.BoxSizer(wx.VERTICAL)
        self.notebook = wx.Notebook(self)
        self.notebook.AddPage(self._routing_page(self.notebook), "Routing")
        self.notebook.AddPage(self._layers_page(self.notebook), "Layers && net classes")
        self.notebook.AddPage(self._engine_page(self.notebook), "Engine")
        root.Add(self.notebook, 0, wx.EXPAND | wx.ALL, 8)

        progress = wx.StaticBoxSizer(wx.VERTICAL, self, "Progress")
        self.status = wx.StaticText(self, label="Ready.")
        self.gauge = wx.Gauge(self, range=1000)
        self.log = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL)
        self.log.SetFont(wx.Font(wx.FontInfo(10).Family(wx.FONTFAMILY_TELETYPE)))
        self.log.SetMinSize((-1, 160))
        progress.Add(self.status, 0, wx.EXPAND | wx.ALL, 4)
        progress.Add(self.gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 4)
        progress.Add(self.log, 1, wx.EXPAND | wx.ALL, 4)
        root.Add(progress, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.open_jobs = wx.Button(self, label="Open job folder")
        self.open_jobs.Bind(wx.EVT_BUTTON, lambda e: self._open_folder(
            self.job.directory if self.job else data_dir() / "jobs"))
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

        self.passes = row("Max routing passes", wx.SpinCtrl(page, min=0, max=100000, initial=100),
                          "Upper limit for autorouter passes. 0 = no limit.")
        self.threads = row("Optimizer threads", wx.SpinCtrl(page, min=-1, max=256, initial=0),
                           "0 = engine default (CPU cores - 1), -1 = skip the optimizer stage.")
        self.threshold = row("Optimizer stop threshold (%)",
                             wx.SpinCtrlDouble(page, min=0.0, max=100.0, initial=2.5, inc=0.5),
                             "Stop optimizing when a pass improves the score by less than this. "
                             "0.5-1 thorough, 2.5 balanced, 5+ quick.")
        self.threshold.SetDigits(1)
        self.via_costs = row("Via cost", wx.SpinCtrl(page, min=0, max=100000, initial=50),
                             "Higher = fewer vias, longer traces.")
        self.plane_via_costs = row("Plane via cost", wx.SpinCtrl(page, min=0, max=100000, initial=5))
        self.ripup_costs = row("Start rip-up cost", wx.SpinCtrl(page, min=0, max=100000, initial=100))
        self.update_strategy = row("Optimizer update strategy",
                                   wx.Choice(page, choices=list(UPDATE_STRATEGIES)))
        self.hybrid_ratio = row("Hybrid ratio (global:prioritized)", wx.TextCtrl(page, value="1:1"))
        self.selection_strategy = row("Optimizer item selection",
                                      wx.Choice(page, choices=list(SELECTION_STRATEGIES)))
        self.heap = row("JVM max heap (MB, 0 = default)", wx.SpinCtrl(page, min=0, max=262144, initial=0),
                        "Raise for very large boards if the engine runs out of memory.")
        self.extra = row("Extra engine arguments", wx.TextCtrl(page),
                         "Passed to Freerouting verbatim, e.g. --router.job_timeout=00:30:00")

        checks = wx.BoxSizer(wx.VERTICAL)
        self.fanout = wx.CheckBox(page, label="Fan out SMD pads before routing")
        self.remove_existing = wx.CheckBox(page, label="Remove existing tracks and vias first")
        self.remove_existing.SetToolTip("Otherwise existing routing is kept and only the "
                                        "unrouted connections are routed.")
        checks.Add(self.fanout, 0, wx.BOTTOM, 4)
        checks.Add(self.remove_existing, 0)

        box = wx.BoxSizer(wx.VERTICAL)
        box.Add(grid, 0, wx.EXPAND | wx.ALL, 10)
        box.Add(checks, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
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

    def _engine_page(self, parent):
        page = wx.Panel(parent)
        grid = wx.FlexGridSizer(cols=3, vgap=6, hgap=8)
        grid.AddGrowableCol(1)

        grid.Add(wx.StaticText(page, label="Java runtime"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.java_path = wx.TextCtrl(page, value=self.settings.java_path)
        self.java_path.SetHint("auto-detect")
        grid.Add(self.java_path, 1, wx.EXPAND)
        b = wx.Button(page, label="Browse...")
        b.Bind(wx.EVT_BUTTON, lambda e: self._browse(self.java_path, "java executable"))
        grid.Add(b)

        grid.Add(wx.StaticText(page, label="Freerouting jar"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.jar_path = wx.TextCtrl(page, value=self.settings.jar_path)
        self.jar_path.SetHint(f"managed download of {RELEASE.jar_name}")
        grid.Add(self.jar_path, 1, wx.EXPAND)
        b = wx.Button(page, label="Browse...")
        b.Bind(wx.EVT_BUTTON, lambda e: self._browse(self.jar_path, "Freerouting jar", "*.jar"))
        grid.Add(b)

        self.engine_status = wx.StaticText(page, label="")
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.install_btn = wx.Button(page, label="Download missing components")
        self.install_btn.Bind(wx.EVT_BUTTON, lambda e: self._ensure_engine(then=None))
        recheck = wx.Button(page, label="Re-check")
        recheck.Bind(wx.EVT_BUTTON, lambda e: self._refresh_engine_status())
        data = wx.Button(page, label="Open data folder")
        data.Bind(wx.EVT_BUTTON, lambda e: self._open_folder(data_dir()))
        row.Add(self.install_btn, 0, wx.RIGHT, 6)
        row.Add(recheck, 0, wx.RIGHT, 6)
        row.Add(data, 0)

        note = wx.StaticText(page, label=(
            f"The engine is Freerouting {RELEASE.version} (GPL-3.0, freerouting.org), run headless "
            f"with Java {RELEASE.min_java} or newer. Missing pieces are downloaded into the plugin's "
            f"data folder; nothing is installed system-wide.\n{data_dir()}"))
        note.Wrap(560)

        box = wx.BoxSizer(wx.VERTICAL)
        box.Add(grid, 0, wx.EXPAND | wx.ALL, 10)
        box.Add(self.engine_status, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        box.Add(row, 0, wx.ALL, 10)
        box.Add(note, 0, wx.EXPAND | wx.ALL, 10)
        page.SetSizer(box)
        return page

    # ---- options <-> widgets ------------------------------------------------

    def _load_options(self, o: RouteOptions):
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
        for i, name in enumerate(self.net_classes):
            self.skip_classes.Check(i, name in o.ignore_net_classes)
        # Layer lists are per board; only reuse them when the layer count matches.
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
            max_heap_mb=self.heap.GetValue(),
            extra_args=self.extra.GetValue().strip(),
        )

    def _save_settings(self):
        self.settings.java_path = self.java_path.GetValue().strip()
        self.settings.jar_path = self.jar_path.GetValue().strip()
        self.settings.options = self._collect_options()
        try:
            self.settings.save()
        except OSError as e:
            logger.warning("Could not save settings: %s", e)

    # ---- engine availability ------------------------------------------------

    def _refresh_engine_status(self):
        self._java = java.find_java(RELEASE.min_java, self.java_path.GetValue().strip() or None)
        self._jar = freerouting.find_jar(self.jar_path.GetValue().strip() or None)
        parts = [f"Java: {self._java}" if self._java
                 else f"Java: none found (need {RELEASE.min_java}+)",
                 f"Engine: {self._jar}" if self._jar else f"Engine: {RELEASE.jar_name} not downloaded yet"]
        self.engine_status.SetLabel("\n".join(parts))
        self.install_btn.Enable(not (self._java and self._jar))
        self.Layout()

    def _ensure_engine(self, then):
        """Make sure a Java runtime and the jar exist, downloading in the
        background if needed; call then() on the main thread when ready."""
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
            "The routing engine needs to be downloaded first:\n  - " + "\n  - ".join(missing)
            + f"\n\ninto {data_dir()}\n\nDownload now?", "Autorouter", wx.YES_NO | wx.ICON_QUESTION, self)
        if answer != wx.YES:
            return
        self._cancel_download = False
        self._set_busy(True)
        self._append_log("[plugin] downloading engine components...")

        def progress(label, done, total):
            wx.CallAfter(self._show_progress, f"{label}  {_mb(done)} / {_mb(total)}",
                         (done / total) if total else None)

        def work():
            error = None
            try:
                if not self._jar:
                    freerouting.install_jar(progress, lambda: self._cancel_download)
                if not self._java:
                    java.install_jre(RELEASE.min_java, progress, lambda: self._cancel_download)
            except Exception as e:           # reported in the dialog
                error = e
            wx.CallAfter(finish, error)

        def finish(error):
            self._set_busy(False)
            self._refresh_engine_status()
            if error is not None:
                logger.exception("Engine download failed", exc_info=error)
                self._show_progress(f"Download failed: {error}", 0.0)
                self._append_log(f"[plugin] download failed: {error!r}")
                return
            self._show_progress("Engine ready.", 0.0)
            self._append_log("[plugin] engine ready")
            if then and self._java and self._jar:
                then()

        threading.Thread(target=work, name="autorouter-download", daemon=True).start()

    # ---- the run ------------------------------------------------------------

    def _on_route(self, event):
        self._save_settings()
        self._ensure_engine(then=self._start_run)

    def _start_run(self):
        options = self._collect_options()
        if options.routable_layers and not any(options.routable_layers):
            wx.MessageBox("Enable at least one routable layer.", "Autorouter", wx.ICON_WARNING, self)
            return
        self.log.Clear()
        self.applied = False
        try:
            name = Path(self.board.GetFileName()).stem if self.board.GetFileName() else "board"
            self.job = session.new_job(name)
            if options.remove_existing_tracks:
                removed = session.remove_tracks(self.board)
                self._append_log(f"[plugin] removed {removed} existing tracks and vias")
            session.export_dsn(self.board, self.job.dsn)
            self._append_log(f"[plugin] exported {self.job.dsn}")
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
        if self.parser:
            status = self.parser.feed(line)
            self._show_progress(status.summary(), status.fraction(self._collect_options().max_passes))

    def _on_engine_done(self, returncode):
        self._set_busy(False)
        status = self.parser.status if self.parser else None
        if returncode is None:
            self._show_progress("Cancelled. The board was not changed.", 0.0)
            return
        if status and status.finished_ok and self.job and self.job.ses.is_file():
            try:
                session.import_ses(self.board, self.job.ses)
                self.applied = True
            except Exception as e:
                logger.exception("Importing the session failed")
                self._show_progress(f"Routing finished but the import failed: {e}", 0.0)
                return
            text = status.summary().replace("Finished", "Done, routing applied to the board", 1)
            text += ".  Undo (Ctrl+Z) reverts it."
            self._show_progress(text, 1.0)
            self._append_log(f"[plugin] imported {self.job.ses}")
            return
        reason = status.errors[-1] if status and status.errors else f"exit code {returncode}"
        if status and status.job_state and status.job_state != "COMPLETED":
            reason = f"engine state {status.job_state}"
        self._show_progress(f"The engine did not produce a session ({reason}). See the log.", 0.0)

    def _on_cancel(self, event):
        self._cancel_download = True
        if self.run and self.run.running:
            self._append_log("[plugin] cancelling...")
            threading.Thread(target=self.run.cancel, daemon=True).start()

    def _on_close(self, event):
        if self.run and self.run.running:
            if wx.MessageBox("The engine is still running. Stop it and close?", "Autorouter",
                             wx.YES_NO | wx.ICON_QUESTION, self) != wx.YES:
                return
            self.run.cancel()
        self._cancel_download = True
        self._save_settings()
        self.EndModal(wx.ID_OK if self.applied else wx.ID_CANCEL)

    # ---- small helpers ------------------------------------------------------

    def _set_busy(self, busy: bool):
        self.route_btn.Enable(not busy)
        self.cancel_btn.Enable(busy)
        self.notebook.Enable(not busy)
        if not busy:
            self.gauge.SetValue(self.gauge.GetValue())      # stop pulsing

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
