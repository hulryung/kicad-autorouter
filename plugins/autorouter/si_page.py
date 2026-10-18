"""The dialog's "Signal integrity" tab: the stackup, impedance classes,
length groups and the routing order, kept per board in
<board>.autorouter.json (engine/si.py)."""
import wx
import wx.grid

from .engine import fab, si, stackup as stk

# Common interfaces: (label, class) for the "Add preset" menu.
PRESETS = [
    ("USB 2.0 (90 ohm differential)", si.ImpedanceClass("USB", ["*USB_D?", "*USB_D?_*", "*D+", "*D-"], 0, 90, 0, 0.1)),
    ("Ethernet MDI (100 ohm differential)", si.ImpedanceClass("ETH_MDI", ["*MDI*", "*TX?_?", "*RX?_?", "*TD?", "*RD?"],
                                                               0, 100, 0, 0.1)),
    ("LVDS / MIPI / HDMI (100 ohm differential)", si.ImpedanceClass("DIFF100", ["*LVDS*", "*MIPI*", "*HDMI*"],
                                                                     0, 100, 0, 0.1)),
    ("CAN / RS-485 (120 ohm differential)", si.ImpedanceClass("DIFF120", ["*CANH", "*CANL", "*CAN_H", "*CAN_L"],
                                                               0, 120, 0, 0)),
    ("RF / antenna (50 ohm single-ended)", si.ImpedanceClass("RF50", ["*RF*", "*ANT*"], 50, 0, 0, 0)),
    ("DDR data/address (40 ohm single-ended)", si.ImpedanceClass("DDR_SE40", ["*DQ*", "*DDR_A*", "*DDR_BA*"],
                                                                  40, 0, 0, 0)),
    ("DDR clock/strobe (80 ohm differential)", si.ImpedanceClass("DDR_DIFF80", ["*CK_?", "*DQS*"], 0, 80, 0, 0.05)),
]
LENGTH_PRESETS = [
    ("Memory byte lane DQ0-7 + DQS0 (+-0.5 mm)", si.LengthGroup("Byte lane 0", ["*DQ[0-7]", "*DQS0*", "*DM0"], 0.5)),
    ("Memory byte lane DQ8-15 + DQS1 (+-0.5 mm)", si.LengthGroup("Byte lane 1", ["*DQ[89]", "*DQ1[0-5]", "*DQS1*",
                                                                                 "*DM1"], 0.5)),
    ("SDRAM/FMC address + control (+-2.5 mm)", si.LengthGroup("Address", ["*_A[0-9]*", "*_BA*", "*RAS*", "*CAS*",
                                                                          "*WE*", "*CKE*"], 2.5)),
    ("QSPI / OSPI (+-1 mm)", si.LengthGroup("QSPI", ["*QSPI*", "*OSPI*"], 1.0)),
    ("RGMII TX group (+-1 mm)", si.LengthGroup("RGMII TX", ["*TXD?", "*TX_CTL", "*TX_EN", "*TXC*"], 1.0)),
    ("RGMII RX group (+-1 mm)", si.LengthGroup("RGMII RX", ["*RXD?", "*RX_CTL", "*RX_DV", "*RXC*"], 1.0)),
    ("SD card / SDIO (+-1 mm)", si.LengthGroup("SDIO", ["*SD_D?", "*SD_CMD", "*SD_CLK", "*SDIO*"], 1.0)),
]

Z_COLS = ["Name", "Nets (patterns, comma separated)", "Single ohm", "Diff ohm", "Gap mm (0 = auto)", "Skew mm"]
L_COLS = ["Name", "Nets (patterns, comma separated)", "Tolerance +- mm", "Target mm (0 = longest)"]


def _patterns(text: str):
    return [p.strip() for p in text.split(",") if p.strip()]


def _num(text: str) -> float:
    try:
        return float(text.strip() or 0)
    except ValueError:
        return 0.0


class SiPage(wx.Panel):
    def __init__(self, parent, board, log):
        super().__init__(parent)
        self.board = board
        self.log = log                                # fn(str): a line in the dialog's log
        box = wx.BoxSizer(wx.VERTICAL)

        # -- stackup and the computed sizes
        top = wx.BoxSizer(wx.HORIZONTAL)
        sbox = wx.StaticBoxSizer(wx.VERTICAL, self, "Stackup")
        self.stackups = [stk.BOARD] + list(stk.TEMPLATES)
        labels = ["Board stackup (Board Setup > Physical Stackup)"] + [
            f"{n}: {stk.TEMPLATES[n].description}" for n in stk.TEMPLATES]
        self.stackup = wx.Choice(self, choices=labels)
        self.stackup.SetToolTip("The layer build the impedance formulas use. The JLCPCB templates are their "
                                "standard 1.6 mm stackups; order the board with impedance control for exact widths.")
        self.stackup.Bind(wx.EVT_CHOICE, lambda e: self.compute())
        sbox.Add(self.stackup, 0, wx.EXPAND | wx.ALL, 4)
        self.fabs = [fab.NONE] + list(fab.TEMPLATES)
        self.fab = wx.Choice(self, choices=["Fabrication rules: the board's own (Board Setup > Constraints)"] + [
            f"Fabrication rules: {n} ({fab.TEMPLATES[n].description})" for n in fab.TEMPLATES])
        self.fab.SetToolTip("Raise the board's constraints (clearance, track width, vias, hole and edge clearances) "
                            "and the net classes to the board house's standard, no-extra-cost capabilities "
                            "before routing (a stricter value already set stays); the rules the Constraints page "
                            "cannot express go to the .kicad_dru.")
        self.fab.SetSelection(0)
        sbox.Add(self.fab, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 4)
        self.sizes = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL)
        self.sizes.SetFont(wx.Font(wx.FontInfo(9).Family(wx.FONTFAMILY_TELETYPE)))
        self.sizes.SetMinSize((-1, 90))
        sbox.Add(self.sizes, 1, wx.EXPAND | wx.ALL, 4)
        top.Add(sbox, 3, wx.EXPAND | wx.RIGHT, 6)

        obox = wx.StaticBoxSizer(wx.VERTICAL, self, "Routing order (first routed on top)")
        self.order = wx.ListBox(self, style=wx.LB_SINGLE)
        self.order.SetToolTip("Groups routed earlier get the straightest paths; later groups route around them "
                              "and may not rip up pairs, memory or high-speed routes.")
        obox.Add(self.order, 1, wx.EXPAND | wx.ALL, 4)
        row = wx.BoxSizer(wx.HORIZONTAL)
        up, down = wx.Button(self, label="Up"), wx.Button(self, label="Down")
        up.Bind(wx.EVT_BUTTON, lambda e: self._move(-1))
        down.Bind(wx.EVT_BUTTON, lambda e: self._move(1))
        row.Add(up, 1, wx.RIGHT, 4)
        row.Add(down, 1)
        obox.Add(row, 0, wx.EXPAND | wx.ALL, 4)
        top.Add(obox, 2, wx.EXPAND)
        box.Add(top, 1, wx.EXPAND | wx.ALL, 6)

        # -- impedance classes
        zbox = wx.StaticBoxSizer(wx.VERTICAL, self, "Controlled impedance (track width per layer from the stackup)")
        self.zgrid = self._grid(Z_COLS, [110, 230, 70, 70, 100, 60])
        zbox.Add(self.zgrid, 1, wx.EXPAND | wx.ALL, 4)
        zbox.Add(self._buttons(self.zgrid, PRESETS, lambda c: [c.name, ", ".join(c.nets), f"{c.single_ohm:g}",
                                                                f"{c.diff_ohm:g}", f"{c.gap_mm:g}", f"{c.skew_mm:g}"]),
                 0, wx.EXPAND | wx.ALL, 2)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self, label="Pairs whose ends are closer than"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.pair_span = wx.SpinCtrlDouble(self, min=0.0, max=50.0, inc=0.5, initial=3.0)
        self.pair_span.SetDigits(1)
        row.Add(self.pair_span, 0, wx.LEFT | wx.RIGHT, 4)
        row.Add(wx.StaticText(self, label="mm are connected directly, without coupling; so are pairs that cannot "
                                          "couple (crossed pins, no room)."), 0, wx.ALIGN_CENTER_VERTICAL)
        zbox.Add(row, 0, wx.LEFT | wx.RIGHT | wx.TOP, 4)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self, label="Uncoupled length allowed per pair"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.pair_uncoupled = wx.SpinCtrlDouble(self, min=0.0, max=100.0, inc=0.5, initial=5.0)
        self.pair_uncoupled.SetDigits(1)
        row.Add(self.pair_uncoupled, 0, wx.LEFT | wx.RIGHT, 4)
        row.Add(wx.StaticText(self, label="mm (KiCad rule); a directly connected pair no longer than this "
                                          "needs no skew matching either."), 0, wx.ALIGN_CENTER_VERTICAL)
        zbox.Add(row, 0, wx.ALL, 4)
        box.Add(zbox, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)

        # -- length groups
        lbox = wx.StaticBoxSizer(wx.VERTICAL, self, "Length matching (meanders after routing)")
        self.lgrid = self._grid(L_COLS, [110, 300, 100, 140])
        lbox.Add(self.lgrid, 1, wx.EXPAND | wx.ALL, 4)
        lbox.Add(self._buttons(self.lgrid, LENGTH_PRESETS, lambda g: [g.name, ", ".join(g.nets),
                                                                      f"{g.tolerance_mm:g}", f"{g.target_mm:g}"]),
                 0, wx.EXPAND | wx.ALL, 2)
        self.tune = wx.CheckBox(self, label="Match lengths after routing (pairs to their skew, groups to their target)")
        self.tune.SetValue(True)
        lbox.Add(self.tune, 0, wx.ALL, 4)
        self.kicad_update = wx.CheckBox(self, label="Hand the meanders to KiCad's length tuner "
                                                    "(tuning patterns, rebuilt by Update All Tuning Patterns)")
        self.kicad_update.SetToolTip("Each tuned net's meander becomes a KiCad tuning pattern with the group's "
                                     "target window. When the dialog closes, KiCad's Edit > Update All Tuning "
                                     "Patterns rebuilds them with its own meander engine; drag a pattern's handles "
                                     "or change its target to retune it by hand.")
        self.kicad_update.SetValue(True)
        lbox.Add(self.kicad_update, 0, wx.LEFT | wx.BOTTOM, 4)
        self.rounded = wx.CheckBox(self, label="Round the meander corners (fillets)")
        self.rounded.SetToolTip("Meander corners are arcs, like KiCad's round tuning pattern style; "
                                "off: 45-degree chamfers.")
        self.rounded.SetValue(True)
        lbox.Add(self.rounded, 0, wx.LEFT | wx.BOTTOM, 4)
        box.Add(lbox, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)

        # -- KiCad
        kbox = wx.BoxSizer(wx.HORIZONTAL)
        compute = wx.Button(self, label="Compute sizes")
        compute.Bind(wx.EVT_BUTTON, lambda e: self.compute())
        write = wx.Button(self, label="Write to KiCad (net classes + DRC rules)")
        write.SetToolTip("A net class per impedance class (track width, diff pair width and gap: KiCad's "
                         "interactive and differential pair routers use them) and rules in the project's "
                         ".kicad_dru for the per-layer widths, pair gap, skew and lengths (DRC and the "
                         "length tuner check against them). Other rules in the file are kept.")
        write.Bind(wx.EVT_BUTTON, self._on_write)
        kbox.Add(compute, 0, wx.RIGHT, 6)
        kbox.Add(write, 0)
        kbox.AddStretchSpacer()
        self.where = wx.StaticText(self, label="")
        kbox.Add(self.where, 0, wx.ALIGN_CENTER_VERTICAL)
        box.Add(kbox, 0, wx.EXPAND | wx.ALL, 6)
        self.SetSizer(box)
        self.set_config(si.load(board))

    # ---- widgets ---------------------------------------------------------------

    def _grid(self, cols, widths):
        g = wx.grid.Grid(self)
        g.CreateGrid(0, len(cols))
        g.SetRowLabelSize(0)
        for i, (c, w) in enumerate(zip(cols, widths)):
            g.SetColLabelValue(i, c)
            g.SetColSize(i, w)
        g.SetMinSize((-1, 80))
        return g

    def _buttons(self, grid, presets, row_of):
        row = wx.BoxSizer(wx.HORIZONTAL)
        add = wx.Button(self, label="Add")
        add.Bind(wx.EVT_BUTTON, lambda e: self._add_row(grid, None))
        preset = wx.Button(self, label="Add preset...")

        def menu(e):
            m = wx.Menu()
            for label, item in presets:
                mi = m.Append(wx.ID_ANY, label)
                self.Bind(wx.EVT_MENU, lambda ev, it=item: self._add_row(grid, row_of(it)), mi)
            preset.PopupMenu(m)
            m.Destroy()
        preset.Bind(wx.EVT_BUTTON, menu)
        remove = wx.Button(self, label="Remove")
        remove.Bind(wx.EVT_BUTTON, lambda e: self._remove_rows(grid))
        for b in (add, preset, remove):
            row.Add(b, 0, wx.RIGHT, 4)
        return row

    def _add_row(self, grid, values):
        grid.AppendRows(1)
        r = grid.GetNumberRows() - 1
        for c, v in enumerate(values or []):
            grid.SetCellValue(r, c, v)
        self.compute()

    def _remove_rows(self, grid):
        rows = sorted(set(grid.GetSelectedRows()) | {grid.GetGridCursorRow()}, reverse=True)
        for r in rows:
            if 0 <= r < grid.GetNumberRows():
                grid.DeleteRows(r, 1)
        self.compute()

    def _move(self, step):
        i = self.order.GetSelection()
        j = i + step
        if i < 0 or not 0 <= j < self.order.GetCount():
            return
        keys = self._order_keys
        keys[i], keys[j] = keys[j], keys[i]
        self._fill_order(keys)
        self.order.SetSelection(j)

    def _fill_order(self, keys):
        self._order_keys = list(keys)
        self.order.Set([f"{n + 1}. {si.GROUP_LABELS[k]}" for n, k in enumerate(keys)])

    # ---- the config ------------------------------------------------------------

    def set_config(self, cfg: si.SiConfig):
        self.stackup.SetSelection(self.stackups.index(cfg.stackup) if cfg.stackup in self.stackups else 0)
        self.fab.SetSelection(self.fabs.index(cfg.fab) if cfg.fab in self.fabs else 0)
        for grid in (self.zgrid, self.lgrid):
            if grid.GetNumberRows():
                grid.DeleteRows(0, grid.GetNumberRows())
        for c in cfg.impedance:
            self._add_row(self.zgrid, [c.name, ", ".join(c.nets), f"{c.single_ohm:g}", f"{c.diff_ohm:g}",
                                       f"{c.gap_mm:g}", f"{c.skew_mm:g}"])
        for g in cfg.length_groups:
            self._add_row(self.lgrid, [g.name, ", ".join(g.nets), f"{g.tolerance_mm:g}", f"{g.target_mm:g}"])
        self._fill_order(cfg.route_order)
        self.tune.SetValue(cfg.tune_lengths)
        self.kicad_update.SetValue(cfg.kicad_patterns)
        self.rounded.SetValue(cfg.rounded)
        self.pair_span.SetValue(cfg.pair_min_span_mm)
        self.pair_uncoupled.SetValue(cfg.pair_uncoupled_max_mm)
        p = si.config_path(self.board)
        self.where.SetLabel(f"Saved with the board: {p.name}" if p else "")
        self.compute()

    def config(self) -> si.SiConfig:
        cfg = si.SiConfig()
        cfg.stackup = self.stackups[max(0, self.stackup.GetSelection())]
        cfg.fab = self.fabs[max(0, self.fab.GetSelection())]
        for grid in (self.zgrid, self.lgrid):
            # commit a cell being typed into; with no editor open there is nothing to commit
            # (committing then writes the editor's stale text into the cursor cell)
            if grid.IsCellEditControlShown():
                grid.SaveEditControlValue()
        for r in range(self.zgrid.GetNumberRows()):
            v = [self.zgrid.GetCellValue(r, c) for c in range(len(Z_COLS))]
            if not v[0].strip() or not _patterns(v[1]):
                continue
            cfg.impedance.append(si.ImpedanceClass(v[0].strip(), _patterns(v[1]), _num(v[2]), _num(v[3]),
                                                   _num(v[4]), _num(v[5])))
        for r in range(self.lgrid.GetNumberRows()):
            v = [self.lgrid.GetCellValue(r, c) for c in range(len(L_COLS))]
            if not v[0].strip() or not _patterns(v[1]):
                continue
            cfg.length_groups.append(si.LengthGroup(v[0].strip(), _patterns(v[1]), _num(v[2]) or 1.0, _num(v[3])))
        cfg.route_order = list(self._order_keys)
        cfg.tune_lengths = self.tune.GetValue()
        cfg.kicad_patterns = self.kicad_update.GetValue()
        cfg.rounded = self.rounded.GetValue()
        cfg.pair_min_span_mm = self.pair_span.GetValue()
        cfg.pair_uncoupled_max_mm = self.pair_uncoupled.GetValue()
        return cfg

    def save(self):
        """Into <board>.autorouter.json, once the board has any intent (or the file exists)."""
        cfg = self.config()
        p = si.config_path(self.board)
        if p is None:
            return
        if p.exists() or cfg.impedance or cfg.length_groups or cfg.route_order != si.DEFAULT_ORDER \
                or cfg.stackup != stk.BOARD or cfg.fab or not cfg.kicad_patterns or not cfg.rounded or cfg.pair_min_span_mm != 3.0 \
                or cfg.pair_uncoupled_max_mm != 5.0:
            try:
                si.save(self.board, cfg)
            except OSError as e:
                self.log(f"[plugin] could not save {p.name}: {e}")

    def compute(self):
        """Show the stackup and the sizes each impedance class gets."""
        if not hasattr(self, "_order_keys"):
            return None
        try:
            plan = si.plan(self.board, self.config())
        except Exception as e:
            self.sizes.SetValue(f"Could not compute: {e!r}")
            return None
        text = plan.lines + [f"! {w}" for w in plan.warnings]
        self.sizes.SetValue("\n".join(text) if text else "No stackup.")
        return plan

    def _on_write(self, event):
        plan = self.compute()
        if plan is None:
            return
        cfg = self.config()
        try:
            if cfg.fab:
                for line in fab.apply(self.board, cfg.fab):
                    self.log(f"[plugin] {cfg.fab}: {line}")
                fab.write_drc_rules(self.board, cfg.fab)
                plan = self.compute() or plan
            for line in si.apply_net_classes(self.board, cfg, plan):
                self.log("[plugin] " + line)
            from .engine.native import tuning
            names = {c: n.GetNetname() for c, n in self.board.GetNetsByNetcode().items()}
            lengths = {names[c]: v for c, v in tuning.track_lengths(self.board).items()}
            p = si.write_drc_rules(self.board, si.drc_rules(self.board, cfg, plan, lengths,
                                                            getattr(self, "pair_exceptions", None)))
            self.save()
            self.log(f"[plugin] DRC rules written to {p.name if p else '(board not saved yet)'}; "
                     "save the board to keep the net classes")
        except Exception as e:
            self.log(f"[plugin] writing to KiCad failed: {e!r}")
