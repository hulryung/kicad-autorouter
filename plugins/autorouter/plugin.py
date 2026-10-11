"""The SWIG action plugin (KiCad 9 and 10): the toolbar button and its Run()."""
from pathlib import Path

import pcbnew
import wx

from . import __version__
from .log import get_logger

logger = get_logger()


def _pcb_frame():
    """The PCB editor window, to parent our dialog on (None if not found)."""
    for name in ("PcbFrame", "PcbNew"):
        frame = wx.FindWindowByName(name)
        if frame:
            return frame
    return None


class AutorouterPlugin(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "Autorouter"
        self.category = "Routing"
        self.description = "Autoroute the board with Freerouting, inside KiCad"
        self.show_toolbar_button = True
        icon = Path(__file__).parent / "plugin_resources" / "icon-24.png"
        self.icon_file_name = str(icon) if icon.exists() else ""
        self.dark_icon_file_name = self.icon_file_name

    def Run(self):
        try:
            board = pcbnew.GetBoard()
            if board is None:
                wx.MessageBox("Open a board first.", "Autorouter", wx.ICON_ERROR)
                return
            from .dialog import AutorouterDialog

            dialog = AutorouterDialog(_pcb_frame(), board)
            try:
                dialog.ShowModal()
            finally:
                dialog.Destroy()
            # KiCad records the board changes made during Run() as one undo
            # step and rebuilds connectivity once Run() returns.
            pcbnew.Refresh()
        except Exception as e:                     # never let KiCad see a traceback
            logger.exception("Autorouter failed")
            wx.MessageBox(f"Autorouter {__version__} failed:\n{e}", "Autorouter", wx.ICON_ERROR)
