"""The Routing guide tab: pick how the board is routed, with a picture of
each approach, its description, and its exact settings as JSON.

Built-in guides come with the plugin; "Learn from a board" and "Describe
with AI" make guides of the user's own, which can be edited here and are
kept between sessions. The guide picked here is applied to every run of
the router and given to the AI assistant.
"""
import json
from pathlib import Path
from typing import Callable, Optional

import wx

from .engine import guides
from .log import get_logger

logger = get_logger("guides")
PREVIEW = 300


class GuidePage(wx.Panel):
    def __init__(self, parent, board, log: Callable[[str], None], selected: str,
                 provider: Callable[[], object], workdir: Callable[[], Path]):
        super().__init__(parent)
        self.board, self.log, self.provider, self.workdir = board, log, provider, workdir
        self.items = []
        root = wx.BoxSizer(wx.HORIZONTAL)

        left = wx.BoxSizer(wx.VERTICAL)
        left.Add(wx.StaticText(self, label="How should the board be routed?"), 0, wx.BOTTOM, 4)
        self.list = wx.ListBox(self, style=wx.LB_SINGLE)
        self.list.SetMinSize((230, 260))
        self.list.Bind(wx.EVT_LISTBOX, lambda e: self._show())
        left.Add(self.list, 1, wx.EXPAND)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.learn = wx.Button(self, label="Learn from a board...")
        self.learn.Bind(wx.EVT_BUTTON, self._on_learn)
        self.describe = wx.Button(self, label="Describe with AI...")
        self.describe.Bind(wx.EVT_BUTTON, self._on_describe)
        row.Add(self.learn, 0, wx.RIGHT, 4)
        row.Add(self.describe, 0)
        left.Add(row, 0, wx.TOP, 6)
        root.Add(left, 0, wx.EXPAND | wx.ALL, 8)

        right = wx.BoxSizer(wx.VERTICAL)
        top = wx.BoxSizer(wx.HORIZONTAL)
        self.picture = wx.StaticBitmap(self, size=(PREVIEW, PREVIEW))
        top.Add(self.picture, 0, wx.RIGHT, 10)
        words = wx.BoxSizer(wx.VERTICAL)
        self.title = wx.StaticText(self, label="")
        self.title.SetFont(self.title.GetFont().Bold().Scaled(1.15))
        self.summary = wx.StaticText(self, label="")
        words.Add(self.title, 0, wx.BOTTOM, 6)
        words.Add(self.summary, 1, wx.EXPAND)
        top.Add(words, 1, wx.EXPAND)
        right.Add(top, 0, wx.EXPAND)
        right.Add(wx.StaticText(self, label="Settings the router applies (JSON; editable for your own guides):"),
                  0, wx.TOP, 8)
        self.json = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.HSCROLL)
        self.json.SetFont(wx.Font(wx.FontInfo(10).Family(wx.FONTFAMILY_TELETYPE)))
        self.json.SetMinSize((-1, 120))
        right.Add(self.json, 1, wx.EXPAND | wx.TOP, 2)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.save = wx.Button(self, label="Save as my guide")
        self.save.Bind(wx.EVT_BUTTON, self._on_save)
        self.delete = wx.Button(self, label="Delete")
        self.delete.Bind(wx.EVT_BUTTON, self._on_delete)
        buttons.Add(self.save, 0, wx.RIGHT, 6)
        buttons.Add(self.delete, 0)
        right.Add(buttons, 0, wx.TOP, 6)
        root.Add(right, 1, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(root)
        self._reload(selected)

    # ---- the list -----------------------------------------------------------

    def _reload(self, select: str = ""):
        self.items = guides.load_all()
        self.list.Set([("" if g["builtin"] else "* ") + g.get("name", g["id"]) for g in self.items])
        index = next((i for i, g in enumerate(self.items) if g["id"] == select), None)
        if index is None:
            index = next((i for i, g in enumerate(self.items) if g["id"] == "router-default"), 0)
        if self.items:
            self.list.SetSelection(index)
        self._show()

    def current(self) -> Optional[dict]:
        i = self.list.GetSelection()
        return self.items[i] if 0 <= i < len(self.items) else None

    def selected_id(self) -> str:
        g = self.current()
        return g["id"] if g and not g.get("action") else "router-default"

    def guide(self) -> Optional[dict]:
        """The chosen guide for a run, with the JSON as the user left it in
        the box (None for the cards that only start an action)."""
        g = self.current()
        if not g or g.get("action"):
            return None
        g = dict(g)
        try:
            edited = json.loads(self.json.GetValue() or "{}")
            g["style"] = edited.get("style", g.get("style", {}))
            g["options"] = edited.get("options", g.get("options", {}))
        except ValueError:
            self.log("[guide] the settings box is not valid JSON; the saved guide is used")
        return g

    def _show(self):
        g = self.current()
        if not g:
            return
        self.title.SetLabel(g.get("name", ""))
        self.summary.SetLabel(g.get("summary", ""))
        self.summary.Wrap(max(200, self.GetSize().width - PREVIEW - 300))
        self.json.SetValue(json.dumps({"style": g.get("style", {}), "options": g.get("options", {})}, indent=1)
                           if not g.get("action") else "")
        self.json.SetEditable(not g["builtin"])
        self.save.Enable(not g.get("action"))
        self.delete.Enable(not g["builtin"])
        self._picture(guides.preview_path(g))
        self.Layout()

    def _picture(self, path: Optional[Path]):
        bmp = None
        if path is not None:
            img = wx.Image(str(path))
            if img.IsOk():
                img = img.Scale(PREVIEW, PREVIEW, wx.IMAGE_QUALITY_HIGH)
                bmp = wx.Bitmap(img)
        if bmp is None:
            img = wx.Image(PREVIEW, PREVIEW)
            img.Clear(235)
            bmp = wx.Bitmap(img)
            dc = wx.MemoryDC(bmp)
            dc.SetTextForeground(wx.Colour(110, 110, 110))
            dc.DrawLabel("no picture: the result depends\non the board and the request",
                         wx.Rect(0, 0, PREVIEW, PREVIEW), wx.ALIGN_CENTER)
            dc.SelectObject(wx.NullBitmap)
        self.picture.SetBitmap(bmp)

    # ---- making guides ------------------------------------------------------

    def _on_learn(self, event):
        with wx.FileDialog(self, "A finished, hand-routed board", wildcard="KiCad boards (*.kicad_pcb)|*.kicad_pcb",
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            path = dlg.GetPath()
        try:
            with wx.BusyCursor():
                g = guides.learn(path)
            saved = guides.save(g)
        except Exception as e:
            logger.exception("Learning a guide")
            wx.MessageBox(f"Could not learn from that board: {e}", "Autorouter", wx.ICON_ERROR, self)
            return
        self.log(f"[guide] learnt from {Path(path).name}: {g['summary']} ({saved})")
        self._reload(g["id"] if (guides.user_dir() / f"{g['id']}.json").exists() else Path(saved).stem)

    def _on_describe(self, event):
        with wx.TextEntryDialog(self, "How should this board be routed? (any language)\n"
                                      "e.g. Keep the inner layers whole, power wide and short, CAN pairs on top only.",
                                "Describe a routing guide", style=wx.TE_MULTILINE | wx.OK | wx.CANCEL) as dlg:
            dlg.SetSize((560, 260))
            if dlg.ShowModal() != wx.ID_OK or not dlg.GetValue().strip():
                return
            text = dlg.GetValue().strip()
        backend = self.provider()
        try:
            with wx.BusyInfo("Asking the assistant to write the guide..."):
                g = guides.describe(backend, text, self.board, self.workdir(), poll=lambda: wx.SafeYield(self, True))
        except Exception as e:
            logger.exception("Describing a guide")
            wx.MessageBox(f"The assistant could not write a guide: {e}", "Autorouter", wx.ICON_ERROR, self)
            return
        saved = guides.save(g)
        self.log(f"[guide] written by the assistant: {g['name']} ({saved})")
        self._reload(Path(saved).stem)

    def _on_save(self, event):
        g = self.guide()
        if not g:
            return
        with wx.TextEntryDialog(self, "Name of your guide:", "Save guide",
                                value=g.get("name", "") + ("" if not g["builtin"] else " (mine)")) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            name = dlg.GetValue().strip() or g.get("name", "guide")
        mine = {k: v for k, v in g.items() if k not in ("builtin", "path", "order")}
        mine["name"] = name
        mine["id"] = "" if g["builtin"] else g["id"]
        saved = guides.save(mine)
        self._reload(Path(saved).stem)

    def _on_delete(self, event):
        g = self.current()
        if not g or g["builtin"]:
            return
        if wx.MessageBox(f"Delete the guide \"{g.get('name')}\"?", "Autorouter", wx.YES_NO | wx.ICON_QUESTION,
                         self) != wx.YES:
            return
        try:
            Path(g["path"]).unlink()
        except OSError as e:
            wx.MessageBox(f"Could not delete it: {e}", "Autorouter", wx.ICON_ERROR, self)
        self._reload()
