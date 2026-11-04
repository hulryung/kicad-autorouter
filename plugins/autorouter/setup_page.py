"""The dialog's "Board setup" tab: the fabrication template, and a check of
Board Setup and the project's net classes with fixes to tick and apply
(engine/board_setup.py)."""
import wx

from .engine import board_setup, fab

MARKS = {board_setup.ERROR: "✖", board_setup.WARNING: "⚠", board_setup.INFO: "ℹ"}


class SetupPage(wx.Panel):
    def __init__(self, parent, board, log, si_page, on_change=None):
        super().__init__(parent)
        self.board = board
        self.log = log                                # fn(str): a line in the dialog's log
        self.si_page = si_page                        # holds the fab choice in the board's intent file
        self.on_change = on_change                    # fn(findings): after every check
        self.findings = []
        box = wx.BoxSizer(wx.VERTICAL)

        fbox = wx.StaticBoxSizer(wx.VERTICAL, self, "Fabrication rules")
        self.fabs = [fab.NONE] + list(fab.TEMPLATES)
        self.fab = wx.Choice(self, choices=["The board's own (Board Setup > Constraints)"] + [
            f"{n}: {fab.TEMPLATES[n].description}" for n in fab.TEMPLATES])
        self.fab.SetToolTip("The board house whose standard, no-extra-cost capabilities the board should meet. "
                            "The check below compares the board's constraints and net classes with it; the fix "
                            "raises them (a stricter value already set stays) and puts the rules the Constraints "
                            "page cannot express in the .kicad_dru.")
        self.fab.SetSelection(self.fabs.index(si_page.fab_name) if si_page.fab_name in self.fabs else 0)
        self.fab.Bind(wx.EVT_CHOICE, self._on_fab)
        fbox.Add(self.fab, 0, wx.EXPAND | wx.ALL, 4)
        box.Add(fbox, 0, wx.EXPAND | wx.ALL, 6)

        cbox = wx.StaticBoxSizer(wx.VERTICAL, self, "Board and project check")
        self.summary = wx.StaticText(self, label="")
        cbox.Add(self.summary, 0, wx.EXPAND | wx.ALL, 4)
        self.items = wx.CheckListBox(self)
        self.items.SetMinSize((-1, 130))
        self.items.SetToolTip("Tick the fixes to apply. Items without a fix only explain what to do in KiCad.")
        self.items.Bind(wx.EVT_LISTBOX, lambda e: self._show(self.items.GetSelection()))
        self.items.Bind(wx.EVT_CHECKLISTBOX, self._on_tick)
        cbox.Add(self.items, 1, wx.EXPAND | wx.ALL, 4)
        self.detail = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_WORDWRAP)
        self.detail.SetMinSize((-1, 110))
        cbox.Add(self.detail, 1, wx.EXPAND | wx.ALL, 4)
        row = wx.BoxSizer(wx.HORIZONTAL)
        again = wx.Button(self, label="Check again")
        again.Bind(wx.EVT_BUTTON, lambda e: self.refresh())
        self.apply_btn = wx.Button(self, label="Apply ticked fixes")
        self.apply_btn.Bind(wx.EVT_BUTTON, self._on_apply)
        row.Add(again, 0, wx.RIGHT, 6)
        row.Add(self.apply_btn, 0, wx.RIGHT, 12)
        note = wx.StaticText(self, label="Fixes change Board Setup (Ctrl+Z does not undo them); "
                                         "save the board to keep them in the project file.")
        row.Add(note, 0, wx.ALIGN_CENTER_VERTICAL)
        cbox.Add(row, 0, wx.EXPAND | wx.ALL, 4)
        box.Add(cbox, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        self.SetSizer(box)
        self.refresh()

    # ---- the check -----------------------------------------------------------

    def fab_name(self) -> str:
        return self.fabs[max(0, self.fab.GetSelection())]

    def refresh(self):
        """Run the check again; returns the findings."""
        try:
            self.findings = board_setup.check(self.board, self.fab_name())
        except Exception as e:
            self.findings = []
            self.summary.SetLabel(f"Could not check the board: {e!r}")
            self.items.Set([])
            self.detail.SetValue("")
            return self.findings
        self.items.Set([f"{MARKS.get(f.severity, '')}  {f.title}" + (f"   [fix: {f.fix_label}]" if f.fixable else "")
                        for f in self.findings])
        for i, f in enumerate(self.findings):
            self.items.Check(i, f.fixable and f.default_on)
        counts = {s: sum(1 for f in self.findings if f.severity == s) for s in MARKS}
        if not self.findings:
            self.summary.SetLabel("Nothing to fix: the board setup looks ready for routing.")
        else:
            self.summary.SetLabel(f"{counts[board_setup.ERROR]} error(s), {counts[board_setup.WARNING]} warning(s), "
                                  f"{counts[board_setup.INFO]} note(s). Select an item for the details.")
        if self.findings:
            self.items.SetSelection(0)
            self._show(0)
        else:
            self.detail.SetValue("")
        self.apply_btn.Enable(any(f.fixable for f in self.findings))
        if self.on_change is not None:
            self.on_change(self.findings)
        return self.findings

    def _show(self, i):
        if 0 <= i < len(self.findings):
            f = self.findings[i]
            text = f.detail + (f"\n\nFix: {f.fix_label}." if f.fixable else "\n\nNo automatic fix: change it in KiCad.")
            self.detail.SetValue(text)

    def _on_tick(self, event):
        i = event.GetInt()
        if 0 <= i < len(self.findings) and not self.findings[i].fixable:
            self.items.Check(i, False)                # nothing to apply for this one
        self._show(i)

    def _on_fab(self, event):
        self.si_page.fab_name = self.fab_name()
        self.si_page.compute()
        self.refresh()

    def apply(self, findings) -> list:
        """Apply fixes, log what changed and check again."""
        done = board_setup.apply(self.board, findings)
        for line in done:
            self.log("[setup] " + line)
        if done:
            self.log("[setup] save the board (Ctrl+S) to keep these Board Setup changes")
        self.si_page.compute()
        self.refresh()
        return done

    def _on_apply(self, event):
        ticked = [self.findings[i] for i in self.items.GetCheckedItems() if i < len(self.findings)]
        if not ticked:
            wx.MessageBox("Tick the fixes to apply first.", "Autorouter", wx.ICON_INFORMATION, self)
            return
        done = self.apply(ticked)
        wx.MessageBox(f"{len(done)} change(s) made (see the log). Save the board to keep them.",
                      "Autorouter", wx.ICON_INFORMATION, self)
